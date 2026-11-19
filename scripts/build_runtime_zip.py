#!/usr/bin/env python3
"""依存同梱CodeZipをローカル生成する。通信・AWSデプロイは行わない。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from copilot_learning.core import InvalidData, assert_external_output

# Runtimeから到達する既存モジュールのみ。デモ、台帳、鍵、辞書、業務データは同梱しない。
MODULES = ("__init__", "core", "learning", "model_review_contract", "review_safety", "applicability", "wiki", "okf",
           "retrieval", "privacy_scan", "knowledge_store", "frozen_bundle", "preflight")
PACKAGE_ROOTS = {"boto3", "botocore", "s3transfer", "jmespath", "dateutil", "six.py", "urllib3", "yaml", "_yaml"}
NATIVE_SUFFIXES = (".so", ".pyd", ".dll", ".dylib")


def expected_versions() -> dict:
    rows = (ROOT / "requirements-runtime.txt").read_text("utf-8").splitlines()
    return dict(line.split("==", 1) for line in rows if line and not line.startswith("#"))


def payloads_from_installed() -> tuple[dict[str, bytes], dict]:
    files, omitted = {}, []
    for name, version in expected_versions().items():
        dist = metadata.distribution(name)
        if dist.version != version:
            raise InvalidData("依存バージョンがrequirements-runtime.txtと一致しません")
        for entry in dist.files or []:
            key = str(entry).replace("\\", "/")
            parts = key.split("/")
            if ".." in parts or "__pycache__" in parts or key.endswith((".pyc", ".pyo")):
                continue
            if parts[0] not in PACKAGE_ROOTS and not parts[0].endswith(".dist-info"):
                continue
            if key.endswith(NATIVE_SUFFIXES):
                # PyYAMLには純Python実装がある。x86拡張をARM64用に誤配布しない。
                if name != "PyYAML":
                    raise InvalidData("未対応のネイティブ依存です。Linux ARM64の専用ビルドが必要です")
                omitted.append(key)
                continue
            if parts[-1] == "RECORD":
                continue  # 拡張を省いたので、元wheelのファイル一覧を完全なものとして残さない。
            path = Path(dist.locate_file(entry))
            if path.is_symlink() or not path.is_file():
                raise InvalidData("依存ファイルがリンクまたは通常ファイルでありません")
            data = path.read_bytes()
            if data[:4] == b"\x7fELF" or data[:2] == b"MZ":
                raise InvalidData("実行バイナリを純Python ZIPへ混入できません")
            files[key] = data
    manifest = {"dependency_versions": expected_versions(), "pure_python_only": True,
                "omitted_optional_yaml_extensions": omitted,
                "target": "AgentCore CodeZip PYTHON_3_13 / Lambda python3.13 arm64",
                "arm64_service_boot_verified": False}
    return files, manifest


def build(destination: Path) -> dict:
    out = destination.expanduser().resolve()
    assert_external_output(out, ROOT)
    if out.exists():
        raise InvalidData("既存ZIPを上書きしません")
    files, manifest = payloads_from_installed()
    # MITライセンスの表示は、コードを再配布するCodeZipにも必ず含める。
    sources = [ROOT / "main.py", ROOT / "lambda_function.py", ROOT / "LICENSE"]
    sources += [ROOT / "copilot_learning" / (name + ".py") for name in MODULES]
    sources += sorted((ROOT / "runtime_service").glob("*.py"))
    for source in sources:
        if source.is_symlink() or not source.is_file():
            raise InvalidData("ソースが不足またはリンクです")
        files[source.relative_to(ROOT).as_posix()] = source.read_bytes()
    manifest["files"] = {k: hashlib.sha256(v).hexdigest() for k, v in sorted(files.items())}
    files["BUILD-MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    expanded = sum(map(len, files.values()))
    if expanded > 750_000_000:
        raise InvalidData("CodeZipの展開サイズ制限を超えています")
    # Lambdaにも同じZIPを使うため、その展開上限を厳しい側のガードにする。
    if expanded > 250_000_000:
        raise InvalidData("Lambdaの共通ZIP展開サイズ上限を超えています")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        directories = sorted({str(Path(k).parent).replace("\\", "/") for k in files if "/" in k})
        all_dirs = set()
        for directory in directories:
            path = Path(directory)
            all_dirs.update(str(p).replace("\\", "/") + "/" for p in [path, *path.parents] if str(p) != ".")
        for directory in sorted(all_dirs):
            info = zipfile.ZipInfo(directory, (2026, 1, 1, 0, 0, 0))
            info.create_system, info.external_attr = 3, (stat.S_IFDIR | 0o755) << 16
            archive.writestr(info, b"")
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.create_system, info.external_attr = 3, (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    if out.stat().st_size > 250_000_000:
        out.unlink()
        raise InvalidData("CodeZipの圧縮サイズ上限を超えています")
    return {"sha256": hashlib.sha256(out.read_bytes()).hexdigest(), "zip_bytes": out.stat().st_size,
            "expanded_bytes": expanded, "file_count": len(files), "network_calls": 0,
            "artifact_kind": "dependency_bundled_codezip", "aws_boot_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args.out), ensure_ascii=False, indent=2))
        return 0
    except (InvalidData, OSError, metadata.PackageNotFoundError):
        print("ERROR: 固定依存・ソース・新規出力先を確認してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
