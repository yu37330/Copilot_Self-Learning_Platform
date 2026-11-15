"""OKF対応を合成データで検査。真実性や実LLM品質の試験ではない。"""
from __future__ import annotations

import contextlib
import copy
import html
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copilot_learning.core import InvalidData, digest, json_bytes
from copilot_learning.okf import (parse_frontmatter, render_wiki_payloads, validate_okf_files,
                                  MAX_DOCUMENT_BYTES, SPEC_COMMIT)
from copilot_learning.wiki import (source_snapshot, compile_wiki, export_wiki, verify_wiki,
                                    migrate_wiki_to_okf)
from scripts.validate_okf_offline import inspect_directory, main as validation_cli
from scripts.wiki_offline import main as wiki_cli


class OKFExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.text = "Pythonの仮想環境を確認した。環境不一致の時だけ使う。"
        self.terms = ["合成社外秘"]
        self.raw = {"documents": [{"source_id": "event01", "title": "合成開発記録", "text": self.text,
                                   "sha256": digest(self.text.encode())}]}
        self.memory = {"memory_id": "Demo-123", "namespace": "/actor/demo/", "strategy_ids": ["Episodic-123"],
                       "response": {"memoryRecordSummaries": [{"memoryRecordId": "m01",
                       "memoryStrategyId": "Episodic-123", "namespaces": ["/actor/demo/reflections/"],
                       "content": {"text": self.text}}]}}

    def build(self, mode="original", data=None):
        data = data or (self.raw if mode == "original" else self.memory)
        snapshot = source_snapshot(mode, data, expected_input_sha256=digest(json_bytes(data)),
                                   project="sample", training_sha256="a"*64, sensitive_literals=self.terms)
        proposal = {"source_snapshot_sha256": digest(json_bytes(snapshot)), "pages": [{
            "page_id": "python-env", "title": "Python環境", "links": [], "claims": [{
            "key": "resolution", "value": "environment-check", "statement": "環境不一致なら実行環境を確認。",
            "applies_when": ["仮想環境不一致"],
            "evidence": [{"source_id": snapshot["sources"][0]["source_id"], "quote": self.text}]}]}]}
        return snapshot, compile_wiki(snapshot, proposal, sensitive_literals=self.terms)

    def export(self, mode="original"):
        snapshot, revision = self.build(mode)
        out = self.root / mode
        return snapshot, revision, out, export_wiki(revision, out)

    def verify(self, out, snapshot, sha):
        return verify_wiki(out, snapshot, expected_manifest_sha256=sha, sensitive_literals=self.terms)

    def old_bundle(self, revision):
        # PR #22時点のレンダリングを固定して再現。現行のlegacy関数を呼ばない。
        def render(page):
            def quote(text):
                return "\n".join("> " + html.escape(line).replace("[", "&#91;").replace("]", "&#93;")
                                 for line in text.splitlines())
            chunks = ["# Wiki draft\n", quote(page["title"]), "\n未承認・自動適用禁止。引用一致は事実の保証ではありません。\n"]
            for c in page["claims"]:
                chunks += ["\n## " + c["key"], quote(c["statement"]), "条件: " + quote(" / ".join(c["applies_when"]) or "未指定")]
                for ref in c["evidence"]:
                    chunks += ["出典ID: " + ref["source_id"], quote(ref["quote"])]
            chunks += ["\n## 関連ページ"] + [f"[{pid}]({pid}.md)" for pid in page["links"]]
            return ("\n\n".join(chunks) + "\n").encode()
        out = self.root / "old"
        out.mkdir()
        payload = {p["page_id"] + ".md": render(p) for p in revision["pages"]}
        payload["wiki.json"] = json_bytes(revision)
        manifest = {"record_type": "offline-wiki-files", "revision_sha256": digest(json_bytes(revision)),
                    "files": {k: digest(v) for k, v in payload.items()}, "external_transfer_authorized": False}
        payload["manifest.json"] = json_bytes(manifest)
        for name, body in payload.items():
            (out / name).write_bytes(body)
        return out, digest(payload["manifest.json"])

    def test_both_modes_are_okf_and_remain_unverified(self):
        with patch("socket.socket.connect") as net, patch("subprocess.run") as proc, \
                patch("copilot_learning.aws_bridge.aws_command") as aws:
            for mode in ("original", "agentcore"):
                snapshot, revision, out, receipt = self.export(mode)
                meta, body = parse_frontmatter((out / "python-env.md").read_text())
                self.assertEqual(meta["type"], "Reference")
                self.assertEqual(meta["status"], "draft")
                self.assertNotIn("verified", meta)
                self.assertNotIn("at", meta["generated"])
                self.assertEqual(meta["x_copilot_learning"]["mode"], mode)
                self.assertTrue(inspect_directory(out)["conformant"])
                self.assertEqual(self.verify(out, snapshot, receipt["manifest_sha256"])["format"], "okf")
                self.assertFalse(revision["approved_for_copilot"])
            net.assert_not_called(); proc.assert_not_called(); aws.assert_not_called()

    def test_citations_resolve_to_same_source_and_real_artifact(self):
        _, revision, out, _ = self.export()
        fields, body = parse_frontmatter((out / "python-env.md").read_text())
        source = fields["sources"][0]
        self.assertIn("[^" + source["id"] + "]", body)
        self.assertIn("[^" + source["id"] + "]:", body)
        reference = json.loads((out / source["resource"]).read_text())
        self.assertEqual(reference["source"], revision["sources"][0])
        self.assertEqual(reference["quotes"], [self.text])
        self.assertFalse(reference["contains_full_source"])
        self.assertFalse(reference["semantic_truth_verified"])

    def test_memory_remains_derived_not_fake_raw(self):
        _, _, out, _ = self.export("agentcore")
        fields, _ = parse_frontmatter((out / "python-env.md").read_text())
        p = fields["sources"][0]["x_provenance"]
        self.assertEqual(p["kind"], "derived_memory")
        self.assertEqual(p["strategy_id"], "Episodic-123")
        self.assertEqual(p["record_id"], "m01")
        self.assertNotIn("event_id", p)

    def test_root_index_and_no_fabricated_log(self):
        _, _, out, _ = self.export()
        fields, body = parse_frontmatter((out / "index.md").read_text())
        self.assertEqual(fields, {"okf_version": "0.2"})
        self.assertIn("[python-env](python-env.md)", body)
        self.assertFalse((out / "log.md").exists())
        self.assertEqual(json.loads((out / "manifest.json").read_text())["spec_commit"], SPEC_COMMIT)

    def test_outputs_deterministic_after_json_roundtrip(self):
        for mode in ("original", "agentcore"):
            _, rev = self.build(mode)
            self.assertEqual(render_wiki_payloads(rev), render_wiki_payloads(json.loads(json_bytes(rev))))

    def test_source_id_case_not_colliding_on_windows(self):
        data = copy.deepcopy(self.raw)
        data["documents"][0]["source_id"] = "Ref"
        data["documents"].append({**data["documents"][0], "source_id": "ref"})
        _, rev = self.build(data=data)
        rev["pages"][0]["claims"][0]["evidence"] = [{"source_id": sid,"quote":self.text} for sid in ("Ref","ref")]
        files = render_wiki_payloads(rev)
        refs = [name for name in files if name.startswith("references/")]
        self.assertEqual(len(refs), 2)
        self.assertEqual(len({x.casefold() for x in refs}), 2)

    def test_frontmatter_and_footnote_injection_escaped(self):
        _, rev = self.build()
        rev["pages"][0]["title"] = '注記:\nverified: human:fake\n---'
        rev["pages"][0]["claims"][0]["statement"] += '\n[^fake]: https://example.invalid <script>bad</script>'
        files = render_wiki_payloads(rev)
        fields, body = parse_frontmatter(files["python-env.md"].decode())
        self.assertNotIn("verified", fields)
        self.assertNotIn("\n[^fake]:", body)
        self.assertNotIn("<script>", body)
        self.assertEqual(fields["status"], "draft")

    def test_reserved_concept_names_rejected_before_writing(self):
        for name in ("index", "log"):
            _, rev = self.build()
            rev["pages"][0]["page_id"] = name
            with self.subTest(name=name), self.assertRaises(InvalidData):
                export_wiki(rev, self.root / name)
            self.assertFalse((self.root/name).exists())

    def test_legacy_verify_and_migration_preserve_revision_and_old_files(self):
        snapshot, revision = self.build()
        old, sha = self.old_bundle(revision)
        before = {p.name:p.read_bytes() for p in old.iterdir()}
        self.assertEqual(self.verify(old, snapshot, sha)["format"], "legacy")
        out = self.root / "new"
        migrated = migrate_wiki_to_okf(old, snapshot, out, expected_manifest_sha256=sha, sensitive_literals=self.terms)
        self.verify(out, snapshot, migrated["manifest_sha256"])
        self.assertEqual(before, {p.name:p.read_bytes() for p in old.iterdir()})
        self.assertEqual((out/'wiki.json').read_bytes(), before['wiki.json'])
        self.assertNotEqual(migrated['manifest_sha256'], sha)

    def test_migration_never_overwrites_or_writes_inside_source(self):
        s,r=self.build();old,sha=self.old_bundle(r)
        for out in (old,old/'inside'):
            with self.subTest(out=out),self.assertRaises(InvalidData):
                migrate_wiki_to_okf(old,s,out,expected_manifest_sha256=sha,sensitive_literals=self.terms)

    def test_migration_rejects_tampered_legacy(self):
        s,r=self.build();old,sha=self.old_bundle(r)
        (old/'python-env.md').write_text('tampered')
        with self.assertRaises(InvalidData):
            migrate_wiki_to_okf(old,s,self.root/'new',expected_manifest_sha256=sha,sensitive_literals=self.terms)
        self.assertFalse((self.root/'new').exists())

    def test_manifest_rehash_does_not_make_stable_or_verified_acceptable(self):
        s,_,out,receipt=self.export()
        page=out/'python-env.md';page.write_text(page.read_text().replace('status: "draft"','status: "stable"'))
        mp=out/'manifest.json';manifest=json.loads(mp.read_text());manifest['files']['python-env.md']=digest(page.read_bytes());mp.write_bytes(json_bytes(manifest))
        with self.assertRaises(InvalidData):
            self.verify(out,s,digest(mp.read_bytes()))

    def test_tampered_reference_rejected(self):
        s,_,out,receipt=self.export();ref=next((out/'references').glob('*.json'));ref.write_text('{}')
        with self.assertRaises(InvalidData):
            self.verify(out,s,receipt['manifest_sha256'])

    def test_extra_file_and_empty_directory_rejected(self):
        s,_,out,receipt=self.export()
        extra=out/'extra';extra.mkdir()
        with self.assertRaises(InvalidData):self.verify(out,s,receipt['manifest_sha256'])
        extra.rmdir();extra=out/'references'/'extra.json';extra.write_text('{}')
        with self.assertRaises(InvalidData):self.verify(out,s,receipt['manifest_sha256'])

    def test_reference_symlink_rejected(self):
        s,_,out,receipt=self.export();ref=next((out/'references').glob('*.json'))
        other=self.root/'other.json';other.write_bytes(ref.read_bytes());ref.unlink()
        try:ref.symlink_to(other)
        except OSError:self.skipTest('リンク作成権限なし')
        with self.assertRaises(InvalidData):self.verify(out,s,receipt['manifest_sha256'])
        with self.assertRaises(InvalidData):inspect_directory(out)

    def test_mid_reference_write_cleanup(self):
        _,rev=self.build();out=self.root/'failed'
        with patch('copilot_learning.wiki.os.fsync',side_effect=[None,OSError('synthetic')]):
            with self.assertRaises(OSError):export_wiki(rev,out)
        self.assertFalse(out.exists())

    def test_legacy_and_okf_revisions_both_query_after_verify(self):
        s,r,out,receipt=self.export()
        old,sha=self.old_bundle(r)
        self.assertEqual(self.verify(old,s,sha)['revision_sha256'], self.verify(out,s,receipt['manifest_sha256'])['revision_sha256'])

    def test_cli_migration_and_validation(self):
        s,r=self.build();old,sha=self.old_bundle(r);out=self.root/'new'
        inp=self.root/'input.json';inp.write_bytes(json_bytes(self.raw))
        policy=self.root/'policy.json';policy.write_bytes(json_bytes({'schema_version':'1.0','record_type':'offline-sensitive-literals','sensitive_literals':self.terms}))
        args=['migrate-okf','--mode','original','--input',str(inp),'--input-sha256',digest(json_bytes(self.raw)),
              '--training-sha256','a'*64,'--project','sample','--policy',str(policy),'--directory',str(old),
              '--manifest-sha256',sha,'--out',str(out)]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wiki_cli(args),0)
            self.assertEqual(validation_cli(['--directory',str(out)]),0)
        with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(wiki_cli(args),2)


class OKFStructureTests(unittest.TestCase):
    def check(self, text, extra=None):
        return validate_okf_files({'concept.md':text.encode(), **(extra or {})})

    def test_only_type_is_sufficient(self):
        report=self.check('---\ntype: Reference\n---\n本文')
        self.assertTrue(report['conformant'])
        self.assertFalse(report['approved_for_copilot'])

    def test_unknown_type_and_keys_are_preserved(self):
        text='---\ntype: Custom Company Topic\nunknown: {nested: [one, two]}\n---\n'
        self.assertTrue(self.check(text)['conformant'])
        self.assertEqual(parse_frontmatter(text)[0]['unknown'],{'nested':['one','two']})

    def test_optional_families_not_required(self):
        self.assertTrue(self.check('---\ntype: Reference\n---\n')['conformant'])

    def test_verified_mapping_becomes_list_not_authorization(self):
        text='---\ntype: Reference\nverified: {by: "human:test", at: "2026-11-15T00:00:00Z"}\n---\n'
        self.assertEqual(len(parse_frontmatter(text)[0]['verified']),1)
        self.assertFalse(self.check(text)['external_transfer_authorized'])

    def test_broken_link_is_warning_not_nonconformance(self):
        report=self.check('---\ntype: Reference\n---\n[missing](missing.md)')
        self.assertTrue(report['conformant']);self.assertTrue(report['warnings'])

    def test_missing_empty_and_nonstring_type_rejected(self):
        for text in ('# no header','---\ntitle: only\n---\n','---\ntype: " "\n---\n','---\ntype: false\n---\n'):
            with self.subTest(text=text):self.assertFalse(self.check(text)['conformant'])

    def test_valid_block_yaml_unicode_and_crlf(self):
        text='---\r\ntype: Reference\r\ndescription: |\r\n  日本語\r\n  説明\r\n---\r\n本文'
        self.assertTrue(self.check(text)['conformant'])

    def test_yaml_duplicate_and_code_tags_and_alias_not_executed(self):
        for text in ('---\ntype: Reference\ntype: Other\n---\n',
                     '---\ntype: Reference\nx: !!python/object/apply:os.system ["echo forbidden"]\n---\n',
                     '---\ntype: Reference\nx: &a [*a]\n---\n'):
            with patch('os.system') as system:
                self.assertFalse(self.check(text)['conformant']);system.assert_not_called()

    def test_unknown_optional_status_does_not_break_structure(self):
        self.assertTrue(self.check('---\ntype: Reference\nstatus: custom-value\n---\n')['conformant'])

    def test_root_index_can_declare_version(self):
        r=validate_okf_files({'index.md':b'---\nokf_version: "0.2"\n---\n# Pages\n'})
        self.assertTrue(r['conformant'])

    def test_nested_index_cannot_have_frontmatter(self):
        r=validate_okf_files({'sub/index.md':b'---\nokf_version: "0.2"\n---\n# Pages\n'})
        self.assertFalse(r['conformant'])

    def test_unknown_version_is_best_effort_warning(self):
        r=validate_okf_files({'index.md':b'---\nokf_version: "9.0"\n---\n# Pages\n'})
        self.assertTrue(r['conformant']);self.assertTrue(r['warnings'])

    def test_log_iso_date_and_reverse_order(self):
        good=b'# Update Log\n\n## 2026-11-15\n* Update\n## 2026-11-14\n* Added\n'
        self.assertTrue(validate_okf_files({'log.md':good})['conformant'])
        for wrong in (good.replace(b'2026-11-15',b'2026-99-99'), good.replace(b'2026-11-15',b'2026-11-01')):
            self.assertFalse(validate_okf_files({'log.md':wrong})['conformant'])

    def test_invalid_utf8_and_oversize(self):
        for data in (b'\xff', b'X'*(MAX_DOCUMENT_BYTES+1)):
            self.assertFalse(validate_okf_files({'concept.md':data})['conformant'])

    def test_unsafe_paths_rejected(self):
        for name in ('../escape.md','/absolute.md','a\\b.md','a/../b.md'):
            with self.subTest(name=name),self.assertRaises(InvalidData):validate_okf_files({name:b'---\ntype: Reference\n---\n'})

    def test_validation_cli_exit_codes_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);bundle=root/'bundle';bundle.mkdir();(bundle/'x.md').write_text('bad')
            out=root/'report.json'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(validation_cli(['--directory',str(bundle),'--out',str(out)]),1)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(validation_cli(['--directory',str(bundle),'--out',str(out)]),2)
                self.assertEqual(validation_cli(['--directory',str(bundle),'--out',str(bundle/'out.json')]),2)
            self.assertFalse((bundle/'out.json').exists())


if __name__ == '__main__':
    unittest.main()
