"""Windows の npm CLI をシェルを介さず起動するための解決処理。"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from .core import InvalidData

_PACKAGES = {"codex": "@openai/codex", "copilot": "@github/copilot"}
_MAX_PACKAGE_JSON_BYTES = 128 * 1024


def _env_value(env: dict[str, str], name: str) -> str:
    """Windows の環境変数名は大文字・小文字を区別しない。"""
    for key, value in env.items():
        if key.casefold() == name.casefold():
            return value
    return ""


def _path_directories(env: dict[str, str]) -> list[Path]:
    # 空項目や相対 PATH を cwd として扱わず、明示された場所だけを探す。
    directories: list[Path] = []
    for raw in _env_value(env, "PATH").split(";"):
        value = raw.strip().strip('"')
        if value:
            path = Path(value)
            if path.is_absolute():
                directories.append(path)
    return directories


def _find_file(directory: Path, name: str) -> Path | None:
    candidate = directory / name
    if candidate.is_file():
        return candidate
    # Linux 上でも Windows の探索を合成テストできるようにする。
    if os.name != "nt" and directory.is_dir():
        matches = [path for path in directory.iterdir()
                   if path.name.casefold() == name.casefold() and path.is_file()]
        if len(matches) == 1:
            return matches[0]
    return None


def _windows_cli(command: str, directories: list[Path]) -> Path:
    for directory in directories:
        # npm の .cmd/.ps1 は実行せず、後で package.json から実体を求める。
        for suffix in (".exe", ".cmd", ".ps1"):
            candidate = _find_file(directory, command + suffix)
            if candidate is not None:
                return candidate
    raise InvalidData("Windows用モデルCLIがPATHにありません")


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidData("モデルCLIのpackage.jsonに重複項目があります")
        result[key] = value
    return result


def _native_path(path: Path) -> str:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.suffix.casefold() != ".exe":
        raise InvalidData("Windows用CLIの実行ファイル形式が不正です")
    return str(resolved)


def _npm_entrypoint(command: str, shim: Path) -> Path:
    """対応する公式 npm パッケージ内の JavaScript だけを候補にする。"""
    directory = shim.parent.resolve(strict=True)
    package = directory.joinpath("node_modules", *_PACKAGES[command].split("/"))
    package = package.resolve(strict=True)
    if not package.is_relative_to(directory):
        raise InvalidData("モデルCLIのnpmパッケージが想定範囲外です")
    metadata = (package / "package.json").resolve(strict=True)
    if not metadata.is_relative_to(package) or not metadata.is_file():
        raise InvalidData("モデルCLIのnpm定義を確認できません")
    with metadata.open("rb") as stream:
        raw = stream.read(_MAX_PACKAGE_JSON_BYTES + 1)
    if not 0 < len(raw) <= _MAX_PACKAGE_JSON_BYTES:
        raise InvalidData("モデルCLIのnpm定義が容量上限を超えています")
    spec = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
    if not isinstance(spec, dict) or spec.get("name") != _PACKAGES[command]:
        raise InvalidData("モデルCLIのnpmパッケージ名が一致しません")
    entry = spec.get("bin")
    if isinstance(entry, dict):
        entry = entry.get(command)
    if not isinstance(entry, str) or not entry or "\x00" in entry:
        raise InvalidData("モデルCLIのnpm起動定義が不正です")
    # 両 OS の絶対パス・ドライブ・親参照を検査し、区切り文字を統一する。
    win_path, posix_path = PureWindowsPath(entry), PurePosixPath(entry)
    if (win_path.drive or win_path.root or posix_path.is_absolute()
            or ".." in win_path.parts or ".." in posix_path.parts):
        raise InvalidData("モデルCLIのnpm起動ファイルが範囲外です")
    target = package.joinpath(*entry.replace("\\", "/").split("/"))
    target = target.resolve(strict=True)
    if (not target.is_relative_to(package) or not target.is_file()
            or target.suffix.casefold() not in {".js", ".mjs", ".cjs"}):
        raise InvalidData("モデルCLIのnpm起動ファイルを確認できません")
    return target


def resolve_cli_command(command: str, env: dict[str, str], *,
                        windows: bool | None = None) -> list[str]:
    """shell=False に渡す argv の先頭を返す。認証やモデル呼び出しはしない。

    Windows は .exe または既知の npm パッケージを node.exe で直接起動する。
    .cmd/.ps1 本文は評価しない。非 Windows は通常の PATH 探索に委ねる。
    """
    if command not in _PACKAGES:
        raise InvalidData("対応していないモデルCLIです")
    if not (os.name == "nt" if windows is None else windows):
        return [command]
    try:
        directories = _path_directories(env)
        executable = _windows_cli(command, directories)
        if executable.suffix.casefold() == ".exe":
            return [_native_path(executable)]
        entrypoint = _npm_entrypoint(command, executable)
        # npm と同じ場所の Node を優先し、なければ明示された PATH を探す。
        for directory in [executable.parent, *directories]:
            node = _find_file(directory, "node.exe")
            if node is not None:
                return [_native_path(node), str(entrypoint)]
        raise InvalidData("モデルCLIの起動に必要なnode.exeがありません")
    except InvalidData:
        raise
    except (OSError, ValueError, UnicodeError, RuntimeError, TypeError, AttributeError):
        # ローカルパスや package.json の本文を例外に含めない。
        raise InvalidData("Windows用モデルCLIの起動構成を確認できません") from None
