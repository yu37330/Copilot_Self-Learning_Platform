"""Windows の npm CLI 解決を、外部実行なしの合成配置で検証する。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.cli_process import resolve_cli_command
from copilot_learning.core import InvalidData


class CliProcessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.npm = self.root / "npm with spaces & (literal)"
        self.npm.mkdir()
        self.env = {"Path": str(self.npm)}

    def _package(self, command="codex", *, bin_value=None, suffix=".cmd"):
        package_name = "@openai/codex" if command == "codex" else "@github/copilot"
        package = self.npm / "node_modules" / package_name
        package.mkdir(parents=True)
        entry = package / "bin" / "entry.js"
        entry.parent.mkdir()
        entry.write_text("// 合成テスト用。実行しない。\n", encoding="utf-8")
        (package / "package.json").write_text(json.dumps({
            "name": package_name,
            "bin": {command: "bin/entry.js"} if bin_value is None else bin_value,
        }), encoding="utf-8")
        # .cmd 本文の命令を解析・実行しないことも、この内容で確認する。
        (self.npm / (command + suffix)).write_text(
            "@echo PRIVATE_SHIM_MUST_NOT_RUN & exit 93\n", encoding="utf-8")
        return package, entry

    def _node(self, directory=None):
        path = (directory or self.npm) / "node.exe"
        path.write_bytes(b"synthetic node, never executed")
        return path

    def test_non_windows_preserves_command_without_lookup(self):
        with patch("pathlib.Path.is_file", side_effect=AssertionError("lookup")):
            self.assertEqual(resolve_cli_command("codex", {}, windows=False), ["codex"])
            self.assertEqual(resolve_cli_command("copilot", {}, windows=False), ["copilot"])

    def test_native_executable_is_used_without_node(self):
        exe = self.npm / "codex.exe"
        exe.write_bytes(b"synthetic executable")
        self.assertEqual(resolve_cli_command("codex", self.env, windows=True),
                         [str(exe.resolve())])

    def test_npm_cmd_uses_node_and_argv_preserves_metacharacters(self):
        _, entry = self._package()
        node = self._node()
        with patch("subprocess.run", side_effect=AssertionError("must not execute")):
            result = resolve_cli_command("codex", self.env, windows=True)
        self.assertEqual(result, [str(node.resolve()), str(entry.resolve())])
        self.assertIn("spaces & (literal)", result[0])
        self.assertNotIn("cmd", [Path(part).name.casefold() for part in result])

    def test_copilot_ps1_and_string_bin_are_supported(self):
        _, entry = self._package("copilot", bin_value="./bin/entry.js", suffix=".ps1")
        node = self._node()
        self.assertEqual(resolve_cli_command("copilot", self.env, windows=True),
                         [str(node.resolve()), str(entry.resolve())])

    def test_node_can_be_found_on_a_later_path_and_path_name_is_case_insensitive(self):
        _, entry = self._package(suffix=".CMD")
        node_dir = self.root / "Node runtime"
        node_dir.mkdir()
        node = self._node(node_dir)
        env = {"pAtH": f'"{self.npm}";{node_dir}'}
        self.assertEqual(resolve_cli_command("codex", env, windows=True),
                         [str(node.resolve()), str(entry.resolve())])

    def test_local_node_is_preferred_over_path_node(self):
        _, entry = self._package()
        local_node = self._node()
        earlier = self.root / "earlier"
        earlier.mkdir()
        self._node(earlier)
        env = {"PATH": f"{earlier};{self.npm}"}
        self.assertEqual(resolve_cli_command("codex", env, windows=True),
                         [str(local_node.resolve()), str(entry.resolve())])

    def test_unknown_shim_is_rejected_without_reading_its_commands(self):
        (self.npm / "codex.cmd").write_text("echo PRIVATE_SHIM_CONTENT", encoding="utf-8")
        self._node()
        with self.assertRaises(InvalidData) as error:
            resolve_cli_command("codex", self.env, windows=True)
        self.assertNotIn(str(self.npm), str(error.exception))
        self.assertNotIn("PRIVATE_SHIM_CONTENT", str(error.exception))

    def test_missing_node_is_rejected(self):
        self._package()
        with self.assertRaisesRegex(InvalidData, "node.exe"):
            resolve_cli_command("codex", self.env, windows=True)

    def test_package_name_must_match_command(self):
        package, _ = self._package()
        self._node()
        (package / "package.json").write_text(json.dumps({
            "name": "unrelated-package", "bin": {"codex": "bin/entry.js"},
        }), encoding="utf-8")
        with self.assertRaises(InvalidData):
            resolve_cli_command("codex", self.env, windows=True)

    def test_bin_must_be_contained_relative_javascript_file(self):
        package, _ = self._package()
        self._node()
        for entry in ("../outside.js", "bin/../../outside.js", "C:\\private\\run.js",
                      "C:relative.js", "\\\\host\\share\\run.js", "/tmp/outside.js",
                      "bin/entry.cmd", "bin/missing.js", "bin", "", {"other": "bin/entry.js"}):
            with self.subTest(entry=entry):
                (package / "package.json").write_text(json.dumps({
                    "name": "@openai/codex", "bin": entry,
                }), encoding="utf-8")
                with self.assertRaises(InvalidData):
                    resolve_cli_command("codex", self.env, windows=True)

    def test_symlink_entrypoint_outside_package_is_rejected(self):
        package, entry = self._package()
        self._node()
        external = self.root / "outside.js"
        external.write_text("// 実行しない。", encoding="utf-8")
        entry.unlink()
        try:
            entry.symlink_to(external)
        except OSError:
            self.skipTest("この環境ではsymlinkを作成できない")
        with self.assertRaises(InvalidData):
            resolve_cli_command("codex", self.env, windows=True)

    def test_invalid_or_oversized_metadata_is_redacted(self):
        package, _ = self._package()
        self._node()
        for raw in (b'{"secret": PRIVATE_VALUE}', b" " * (128 * 1024 + 1),
                    b'{"name":"@openai/codex","name":"other","bin":"bin/entry.js"}'):
            with self.subTest(size=len(raw)):
                (package / "package.json").write_bytes(raw)
                with self.assertRaises(InvalidData) as error:
                    resolve_cli_command("codex", self.env, windows=True)
                self.assertNotIn("PRIVATE_VALUE", str(error.exception))
                self.assertNotIn(str(package), str(error.exception))

    def test_no_cwd_search_and_unrecognized_commands_are_rejected(self):
        for env in ({}, {"PATH": ";."}, {"PATH": "relative-directory"}):
            with self.subTest(env=env), self.assertRaises(InvalidData):
                resolve_cli_command("codex", env, windows=True)
        for command in ("cmd", "codex.exe", "../codex", "codex & echo injected"):
            with self.subTest(command=command), self.assertRaises(InvalidData):
                resolve_cli_command(command, self.env, windows=True)


if __name__ == "__main__":
    unittest.main()
