"""Publication policy tests. Every payload is manufactured locally in a temporary repo."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib


ROOT = Path(__file__).resolve().parents[1]
MARKER = "TG-SYNTHETIC-DO-NOT-ECHO"
FIXTURE = "tests/fixtures/quotes.csv"
GENERATOR = "tests/make-quotes.py"
MANIFEST = "policy/synthetic-fixtures.json"
RULES = "policy/data-guard.json"
LICENSES = "policy/market-data-licenses.json"
APPROVALS = "policy/market-data-approvals.json"


def sample_rows():
    # Synthetic instrument, calendar date and decimal strings. Generator seed is 41.
    return ("symbol,price,size\n"
            "TG-DEMO,41.1250,2\n").encode()


class PublicationPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.repo = self.parent / "repo"
        self.repo.mkdir()
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_AUTHOR_NAME": "TG fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                    "GIT_COMMITTER_NAME": "TG fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
        self.git("init", "-q")
        for name in ("policy", ".githooks"):
            shutil.copytree(ROOT / name, self.repo / name)
        (self.repo / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/data-guard.py", self.repo / "scripts/data-guard.py")
        self.git("config", "core.hooksPath", ".githooks")
        self.stage("README.md", "# Temporary textual fixture\n")
        self.git("add", ".githooks", "policy", "scripts")
        self.base = self.commit("Fixture repository")

    def git(self, *args, check=True):
        return self.run_cmd(["git", *args], check=check)

    def run_cmd(self, command, input=None, check=False):
        return subprocess.run(command, cwd=self.repo, env=self.env, input=input, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

    def guard(self, *args):
        return self.run_cmd([sys.executable, "-B", "scripts/data-guard.py", *args])

    def commit(self, message, unchecked=False):
        opts = ("-c", "core.hooksPath=/dev/null") if unchecked else ()
        self.git(*opts, "commit", "-qm", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def write(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def stage(self, name, data):
        path = self.write(name, data)
        self.git("add", "-f", "--", name)
        return path

    def stage_json(self, name, value):
        return self.stage(name, json.dumps(value))

    def assert_rejected(self, result, reason):
        self.assertEqual(result.returncode, 1, result)
        self.assertIn(f"data-guard: {reason}", result.stderr)
        self.assertNotIn(MARKER, result.stdout + result.stderr)

    def assert_passed(self, result):
        self.assertEqual(result.returncode, 0, result)

    def push_hook(self, head, old):
        return self.run_cmd([".githooks/pre-push", "test", "/tmp/offline-remote"],
                            input=f"refs/heads/main {head} refs/heads/main {old}\n")

    def review_sources(self, manifest):
        # Model the separate review step by advancing the local integration ref.
        self.stage_json(MANIFEST, manifest)
        reviewed = self.commit("Review synthetic source digests")
        self.git("update-ref", "refs/remotes/test/main", reviewed)
        return reviewed

    def test_pre_commit_uses_index_even_with_a_different_working_copy(self):
        self.stage("README.md", b"PG" + b"DMP" + MARKER.encode())
        self.write("README.md", "A safe working copy")
        result = self.git("commit", "-qm", "This must fail", check=False)
        self.assert_rejected(result, "file-signature")
        self.stage("README.md", "# An ordinary report\nOnly the aggregate result is recorded.\n")
        self.write("README.md", b"PG" + b"DMP" + MARKER.encode())
        self.commit("Reviewable staged documentation")

    def test_pre_commit_uses_staged_rule_version_and_values(self):
        original = (self.repo / RULES).read_bytes()
        rules = json.loads(original)
        self.stage_json(RULES, dict(rules, schema_version=2))
        self.write(RULES, original)
        result = self.git("commit", "-qm", "Unsupported staged rules", check=False)
        self.assert_rejected(result, "unsupported-rule-version")

        self.stage_json(RULES, dict(rules, blocked_extensions=rules["blocked_extensions"] + [".txt"]))
        self.write(RULES, original)
        self.stage("note.txt", "Ordinary text\n")
        self.assert_rejected(self.guard("index"), "blocked-extension")
        self.git("rm", "--cached", "note.txt")
        self.write(RULES, '{"schema_version":2}')
        self.commit("Use valid staged rules despite uncommitted changes")

    def test_published_history_validates_candidate_rules(self):
        original = (self.repo / RULES).read_bytes()
        self.stage_json(RULES, dict(json.loads(original), schema_version=2))
        bad = self.commit("Introduce unsupported rules", unchecked=True)
        self.stage(RULES, original)
        head = self.commit("Restore supported rules")
        for candidate in (bad, head):
            with self.subTest(candidate=candidate):
                self.assert_rejected(self.guard("range", self.base, candidate), "unsupported-rule-version")
                self.assert_rejected(self.push_hook(candidate, self.base), "unsupported-rule-version")

    def test_rule_schema_rejects_wrong_fields_and_types(self):
        rules = json.loads((self.repo / RULES).read_bytes())
        self.stage("reports/session.log", MARKER)
        self.assert_rejected(self.guard("index"), "blocked-extension")
        changes = (
            {"blocked_extensions": ""}, {"blocked_extensions": [".log", None]},
            {"blocked_extensions": []}, {"blocked_filenames": "env.sh"},
            {"fixture_extensions": {".csv": True}}, {"generator_extensions": ".py"},
            {"blocked_directory_pattern": []}, {"blocked_directory_pattern": "["},
            {"fixture_root": "tests/fixtures"}, {"fixture_manifest": "../manifest.json"},
            {"max_blob_bytes": True}, {"max_pr_bytes": "65536"}, {"max_pr_bytes": 0},
            {"unexpected": MARKER},
        )
        for change in changes:
            with self.subTest(field=next(iter(change))):
                self.stage_json(RULES, rules | change)
                self.assert_rejected(self.guard("index"), "invalid-rule-file")
        missing = dict(rules)
        del missing["blocked_filenames"]
        self.stage_json(RULES, missing)
        self.assert_rejected(self.guard("index"), "invalid-rule-file")
        self.stage_json(RULES, rules | {"schema_version": "1"})
        self.assert_rejected(self.guard("index"), "unsupported-rule-version")

        self.stage_json(RULES, rules | {"blocked_extensions": ""})
        bad = self.commit("Malformed v1 rules", unchecked=True)
        self.assert_rejected(self.guard("range", self.base, bad), "invalid-rule-file")
        self.assert_rejected(self.push_hook(bad, self.base), "invalid-rule-file")

    def test_directory_pattern_compile_errors_have_one_safe_diagnostic(self):
        rules = json.loads((self.repo / RULES).read_bytes())
        for pattern in ("[", MARKER + "a{" + "9" * 20 + "}", "(" * 2000 + MARKER + ")" * 2000):
            self.stage_json(RULES, rules | {"blocked_directory_pattern": pattern})
            result = self.guard("index")
            self.assert_rejected(result, "invalid-rule-file")
            self.assertEqual(result.stderr, "data-guard: invalid-rule-file\n")
        bad = self.commit("Malformed directory pattern", unchecked=True)
        for result in (self.guard("range", self.base, bad), self.push_hook(bad, self.base)):
            self.assert_rejected(result, "invalid-rule-file")
            self.assertEqual(result.stderr, "data-guard: invalid-rule-file\n")

    def test_paths_and_masquerading_file_signatures(self):
        cases = [
            ("notes/Data-Paper/part.txt", MARKER, "private-directory"),
            ("snapshots/results.txt", MARKER, "private-directory"),
            ("notes/runtime.env", MARKER, "private-filename"),
            ("assets/preview.PnG", MARKER, "blocked-extension"),
            ("reports/session.log", MARKER, "blocked-extension"),
            ("reports/saved.sqlite3", MARKER, "blocked-extension"),
            ("reports/payload.txt", b"\x89PNG\r\n\x1a\n", "file-signature"),
            ("reports/payload.txt", b"PAR" + b"1", "file-signature"),
            ("reports/payload.txt", b"SQLite " + b"format 3\0", "file-signature"),
            ("reports/payload.txt", b"PK\x03\x04", "file-signature"),
            ("reports/payload.txt", b" " * 257 + b"ustar", "file-signature"),
            ("reports/payload.txt", b"\0\1\2", "opaque-binary"),
            ("reports/payload.txt", b"a" * 1048577, "blob-too-large"),
        ]
        for name, content, reason in cases:
            with self.subTest(name=name, reason=reason):
                path = self.stage(name, content)
                self.assert_rejected(self.guard("index"), reason)
                self.git("reset", "-q", "HEAD", "--", name)
                path.unlink()

    def test_public_text_blocks_embedded_data_images_exports_and_credentials(self):
        cases = [
            (b"\n".join([b"## Data", sample_rows()]), "market-data-shape"),
            (json.dumps(dict(symbol="TG-DEMO", price="41.1250", size="2")), "market-data-shape"),
            ("| 2099-01-01 | 41.1250 | 2 |", "market-data-shape"),
            ("!" + "[image](preview.png)", "attachment"),
            ("<" + 'img src="preview.png">', "attachment"),
            (base64.b64encode(b"\x89PNG\r\n\x1a\n" + MARKER.encode() * 3), "attachment"),
            ("-- PostgreSQL " + "database dump\n" + MARKER, "database-or-log-export"),
            ("2099-01-01T12:00:00Z " + "INFO " + MARKER, "database-or-log-export"),
            ("WEB_ADMIN_TOKEN=" + MARKER, "credential"),
        ]
        for content, reason in cases:
            with self.subTest(reason=reason):
                self.stage("README.md", content)
                self.assert_rejected(self.guard("index"), reason)

    def test_markdown_market_tables_and_quoted_csv_in_docs_and_prs(self):
        cells = sample_rows().decode().splitlines()[1].split(",")
        header = ("Symbol", "Price", "Size")
        columns = " | ".join(header)
        row = " | ".join(cells)
        table = f"{columns}\n:--- | ---: | ---:\n{row}\n"
        pipe_table = "\n".join(
            f"| {line} |" for line in table.splitlines()
        )
        quoted_csv = "\n".join("> > " + line for line in sample_rows().decode().splitlines())
        head = self.commit_clean_change()
        event = self.parent / "event.json"
        ordinary = "| Topic | Notes |\n| --- | --- |\n| Format | Record capability only |\n"
        for body, reason in ((table, "market-data-shape"), (pipe_table, "market-data-shape"),
                             (quoted_csv, "market-data-shape"), (ordinary, None)):
            with self.subTest(reason=reason, body_type=len(body)):
                self.stage("README.md", f"## Summary\n\n{body}\n")
                event.write_text(json.dumps({"pull_request": {"title": "Review change", "body": body}}))
                results = (self.guard("index"), self.guard("range", self.base, head, "--pr-event", event))
                for result in results:
                    if reason:
                        self.assert_rejected(result, reason)
                    else:
                        self.assert_passed(result)

    def test_order_book_fields_and_image_forms_require_registration(self):
        head = self.commit_clean_change()
        event = self.parent / "event.json"
        tables = (
            ("Symbol", "Bid Price", "Bid Size", "Ask Price", "Ask Size"),
            ("Level", "BID", "ASK", "Size"),
            ("档位", "买一价", "买盘数量", "卖一价", "卖盘数量"),
            ("时间戳", "成交价格", "成交量"),
            ("ticker", "`Best-Bid_Px [USD]`", "**AskQty**"),
            ("价格", "数量"),
            ("Bid / Ask", "Quote Size"),
            ("成交金额", "交易数量"),
        )
        for columns in tables:
            header = " | ".join(columns)
            divider = " | ".join("---" for _ in columns)
            row = " | ".join("41" for _ in columns)
            self.assert_doc_and_pr(head, event, f"{header}\n{divider}\n{row}\n", "market-data-shape")
        html_table = "<table><tr>" + "".join(f"<th>{cell}</th>" for cell in tables[0]) + "</tr></table>"
        self.assert_doc_and_pr(head, event, html_table, "market-data-shape")

        definition = "[preview]: <relative/synthetic.PNG?rev=1>"
        image = "!" + "[preview]"
        data_uri = "data" + ":;base64," + base64.b64encode(b"TG fixture").decode()
        for body in (image + "\n\nAn ordinary paragraph.\n\n" + definition,
                     definition + "\n\nAn ordinary paragraph.\n\n" + image,
                     definition, image + "[]\n\n" + definition,
                     "<" + 'IMG src="relative/preview">', f"[sample]({data_uri})"):
            self.assert_doc_and_pr(head, event, body, "attachment")
        self.assert_doc_and_pr(
            head, event, "[guide]\n\n[guide]: docs/guide.md\n\n| Category | Notes |\n| --- | --- |\n", None,
        )

    def assert_doc_and_pr(self, head, event, body, reason):
        self.stage("README.md", body)
        event.write_text(json.dumps({"pull_request": {"title": "Review change", "body": body}}))
        for result in (self.guard("index"), self.guard("range", self.base, head, "--pr-event", event)):
            if reason:
                self.assert_rejected(result, reason)
            else:
                self.assert_passed(result)

    def test_document_allowlist_pins_path_digest_and_synthetic_provenance(self):
        doc = "docs/market-example.md"
        # Seed 41 selects the RGB values for a generated 1x1, metadata-free PNG.
        generator = (
            "import base64, struct, sys, zlib\n"
            "seed = int(sys.argv[1])\n"
            "def chunk(tag, data):\n"
            "    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))\n"
            "header = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)\n"
            "png = b'\\x89PNG\\r\\n\\x1a\\n' + chunk(b'IHDR', header)\n"
            "png += chunk(b'IDAT', zlib.compress(bytes((0, seed, seed + 1, seed + 2)))) + chunk(b'IEND', b'')\n"
            "print('!' + '[Synthetic pixel](data' + ':image/png;base64,' + base64.b64encode(png).decode() + ')')\n"
            "print('\\nLevel | Bid | Ask\\n--- | --- | ---\\n1 | 41 | 42')\n"
        )
        self.stage(GENERATOR, generator)
        sample = self.run_cmd([sys.executable, GENERATOR, "41"], check=True).stdout
        self.stage(doc, sample)
        self.assert_rejected(self.guard("index"), "attachment")
        entry = dict(source="synthetic", generator=GENERATOR, seed="41",
                     sha256=hashlib.sha256(sample.encode()).hexdigest())
        manifest = {"schema_version": 1, "fixtures": {}, "documents": {doc: entry}}
        self.stage_json(MANIFEST, manifest)
        self.assert_rejected(self.guard("index"), "attachment")
        self.git("rm", "--cached", doc)
        reviewed = self.review_sources(manifest)
        self.stage(doc, sample)
        head = self.commit("Register an offline synthetic document")
        self.assert_passed(self.push_hook(head, self.base))
        self.assert_passed(self.guard("range", reviewed, head))

        event = self.parent / "event.json"
        event.write_text(json.dumps({"pull_request": {"title": "Review change", "body": sample}}))
        self.assert_rejected(self.guard("range", reviewed, head, "--pr-event", event), "attachment")
        self.stage("docs/copied-example.md", sample)
        self.assert_rejected(self.guard("index"), "attachment")
        self.git("rm", "--cached", "docs/copied-example.md")
        modified = sample + "\n!" + "[External example](https://visual.example.invalid/example.png)"
        self.stage(doc, modified)
        self.assert_rejected(self.guard("index"), "synthetic-digest-mismatch")
        entry["sha256"] = hashlib.sha256(modified.encode()).hexdigest()
        self.stage_json(MANIFEST, manifest)
        self.assert_rejected(self.guard("index"), "attachment")

        private = sample + "\nWEB_ADMIN_TOKEN=" + MARKER
        self.git("rm", "--cached", doc)
        entry["sha256"] = hashlib.sha256(private.encode()).hexdigest()
        self.review_sources(manifest)
        self.stage(doc, private)
        self.assert_rejected(self.guard("index"), "credential")
        entry["source"] = "external"
        self.stage_json(MANIFEST, manifest)
        self.assert_rejected(self.guard("index"), "invalid-fixture-manifest")

    def test_registered_png_must_have_complete_chunks_and_valid_scanlines(self):
        # In-memory vectors derived from a generated 1x1 RGB image, seed 41.
        magic = b"\x89PNG\r\n\x1a\n"

        def chunk(kind, data=b""):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        header = magic + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        scanline = zlib.compress(bytes((0, 41, 42, 43)))
        pixels = chunk(b"IDAT", scanline)
        end = chunk(b"IEND")
        complete = header + pixels + end
        invalid = {
            "signature": magic,
            "fake-payload": magic + MARKER.encode(),
            "trailing-bytes": complete + MARKER.encode(),
            "truncated": complete[:-2],
            "bad-crc": complete[:-1] + bytes([complete[-1] ^ 1]),
            "missing-image-data": header + end,
            "bad-order": magic + pixels + header[len(magic):] + end,
            "extra-chunk": header + pixels + chunk(b"tEXt", MARKER.encode()) + end,
            "extra-stream": header + chunk(b"IDAT", scanline + scanline) + end,
            "invalid-zlib": header + chunk(b"IDAT", MARKER.encode()) + end,
            "wrong-row-size": header + chunk(b"IDAT", zlib.compress(bytes((0, 41)))) + end,
            "bad-filter": header + chunk(b"IDAT", zlib.compress(bytes((5, 41, 42, 43)))) + end,
        }
        samples = {
            f"docs/png-{case}.md": "!" + "[Synthetic PNG](data" + ":image/png;base64,"
            + base64.b64encode(data).decode() + ")\n"
            for case, data in ({"valid": complete} | invalid).items()
        }
        self.stage(GENERATOR, "# Mutation vectors come from tests/data_policy_test.py, seed 41.\n")
        manifest = {"schema_version": 1, "fixtures": {}, "documents": {
            path: dict(source="synthetic", generator=GENERATOR, seed="41",
                       sha256=hashlib.sha256(sample.encode()).hexdigest())
            for path, sample in samples.items()
        }}
        self.review_sources(manifest)
        for path, sample in samples.items():
            with self.subTest(path=path):
                self.stage(path, sample)
                result = self.guard("index")
                if path.endswith("/png-valid.md"):
                    self.assert_passed(result)
                else:
                    self.assert_rejected(result, "attachment")
                self.git("rm", "--cached", path)

    def test_candidate_approvals_never_replace_the_integration_base(self):
        doc = "docs/candidate.md"
        sample = "| Level | Bid | Ask |\n| --- | --- | --- |\n| 1 | 41 | 42 |\n"
        self.stage(GENERATOR, "# Synthetic market fixture, seed 41.\n")
        entry = dict(source="synthetic", generator=GENERATOR, seed="41",
                     sha256=hashlib.sha256(sample.encode()).hexdigest())
        self.stage_json(MANIFEST, {"schema_version": 1, "fixtures": {}, "documents": {doc: entry}})
        self.stage(doc, sample)
        self.assert_rejected(self.guard("index"), "market-data-shape")
        unreviewed = self.commit("Simulate a branch with an unreviewed exception", unchecked=True)
        self.git("update-ref", "refs/remotes/test/main", self.base)
        self.assert_rejected(self.push_hook(unreviewed, "0" * len(unreviewed)), "market-data-shape")

        tip = self.commit_clean_change()
        self.assert_rejected(self.push_hook(tip, unreviewed), "market-data-shape")
        self.assert_rejected(self.guard("range", self.base, tip), "market-data-shape")

    def test_path_credentials_are_rejected_and_diagnostics_hide_filenames(self):
        credential = "gh" + "p_" + "TgDemo" * 6
        for suffix, reason in ((".md", "credential"), (".log", "blocked-extension")):
            with self.subTest(suffix=suffix):
                name = f"notes/export_{credential}{suffix}"
                self.stage(name, "Harmless text\n")
                result = self.guard("index")
                self.assert_rejected(result, reason)
                oid = self.git("rev-parse", f":{name}").stdout.strip()
                self.assertIn(oid, result.stderr)
                self.assertNotIn(credential, result.stdout + result.stderr)
                self.assertNotIn("notes/export_", result.stdout + result.stderr)
                self.git("rm", "--cached", name)
        self.stage(f"notes/{MARKER}.log", "Harmless text\n")
        self.assert_rejected(self.guard("index"), "blocked-extension")

    def test_lfs_is_blocked_even_if_the_payload_is_not_in_git(self):
        pointer = "version https://" + "git-lfs.github.com/spec/v1\n"
        self.stage("note.txt", pointer + f"oid sha256:{'1' * 64}\nsize 7\n")
        self.assert_rejected(self.guard("index"), "lfs-pointer")
        self.git("rm", "--cached", "-f", "note.txt")
        self.stage(".gitattributes", "*.txt filter=" + "lfs diff=lfs merge=lfs -text\n")
        self.assert_rejected(self.guard("index"), "lfs-attributes")
        self.stage(".gitattributes", "* text=auto\n")
        self.stage(".lfsconfig", "[lfs]\n")
        self.assert_rejected(self.guard("index"), "private-filename")

    def test_fixture_requires_a_staged_generator_seed_and_exact_blob(self):
        self.stage(FIXTURE, sample_rows())
        self.assert_rejected(self.guard("index"), "missing-synthetic-source")
        manifest = {"schema_version": 1, "fixtures": {FIXTURE: {
            "source": "synthetic", "generator": GENERATOR, "seed": "41",
            "sha256": hashlib.sha256(sample_rows()).hexdigest(),
        }}}
        self.stage_json(MANIFEST, manifest)
        self.assert_rejected(self.guard("index"), "invalid-fixture-manifest")
        self.stage(GENERATOR, "import sys\nseed = int(sys.argv[1])\n"
                   "print('symbol,' + 'price,size')\n"
                   "print(f'TG-DEMO,{seed}.1250,2')\n")
        generated = self.run_cmd([sys.executable, GENERATOR, "41"], check=True).stdout.encode()
        self.assertEqual(generated, sample_rows())
        self.assert_rejected(self.guard("index"), "market-data-shape")
        self.git("rm", "--cached", FIXTURE)
        self.review_sources(manifest)
        self.stage(FIXTURE, generated)
        head = self.commit("Add a reproducible synthetic sample")
        self.assert_passed(self.push_hook(head, "0" * len(head)))
        self.stage(FIXTURE, b"modified\n")
        self.assert_rejected(self.guard("index"), "synthetic-digest-mismatch")
        png = b"\x89PNG\r\n\x1a\n"
        self.stage(FIXTURE, png)
        manifest["fixtures"][FIXTURE]["sha256"] = hashlib.sha256(png).hexdigest()
        self.stage_json(MANIFEST, manifest)
        self.assert_rejected(self.guard("index"), "file-signature")
        manifest["fixtures"][FIXTURE]["source"] = "redacted-real"
        self.stage_json(MANIFEST, manifest)
        self.assert_rejected(self.guard("index"), "invalid-fixture-manifest")

    def test_pre_push_checks_all_new_commits_with_or_without_remote_base(self):
        self.stage("note.txt", b"PG" + b"DMP" + MARKER.encode())
        self.commit("Transient bad data", unchecked=True)
        self.git("rm", "note.txt")
        head = self.commit("Remove bad data from the tip")
        for old in (self.base, "0" * len(head), "1" * len(head)):
            with self.subTest(old=old):
                self.assert_rejected(self.push_hook(head, old), "file-signature")
        self.assert_passed(self.push_hook("0" * len(head), self.base))

    def test_ci_range_checks_pr_metadata_and_intermediate_commits(self):
        event = self.parent / "event.json"
        head = self.commit_clean_change()

        def check_pr(body, title="Review change"):
            event.write_text(json.dumps({"pull_request": {"title": title, "body": body}}))
            return self.guard("range", self.base, head, "--pr-event", event)

        self.assert_passed(check_pr("Closes #41\n测试结果\n合成验证通过"))
        link = "https://github.com/" + "user-attachments/assets/" + MARKER
        self.assert_rejected(check_pr("[attachment](" + link + ")"), "attachment")
        self.assert_rejected(check_pr(None, title=link), "attachment")
        self.assert_rejected(check_pr(MARKER + "\n" + sample_rows().decode()), "market-data-shape")
        event.write_text('{"pull_request": {}}')
        self.assert_rejected(self.guard("range", self.base, head, "--pr-event", event), "invalid-pr-event")

        self.stage("notes.txt", b"PAR" + b"1" + MARKER.encode())
        self.commit("Accidentally include bytes", unchecked=True)
        self.git("rm", "notes.txt")
        head = self.commit("Remove the bytes in a later commit")
        self.assert_rejected(check_pr("Clean pull request text"), "file-signature")

    def test_pre_push_checks_commit_messages_and_rejects_shallow_history(self):
        self.stage("README.md", "# Clean contents\n")
        head = self.commit("Document a test\n\n" + sample_rows().decode())
        self.assert_rejected(self.push_hook(head, self.base), "market-data-shape")
        self.git("clone", "--depth", "1", "--", self.repo.as_uri(), str(self.parent / "shallow"))
        result = subprocess.run(
            [sys.executable, "-B", "scripts/data-guard.py", "range", "HEAD", "HEAD"],
            cwd=self.parent / "shallow", env=self.env, text=True, capture_output=True,
        )
        self.assert_rejected(result, "shallow-history")

    def commit_clean_change(self):
        self.stage("README.md", "# Changed textual fixture\n")
        return self.commit("Update documentation")

    def test_license_and_approval_records_validate_uses_and_exact_prices(self):
        licenses = json.loads((self.repo / LICENSES).read_bytes())
        approvals = json.loads((self.repo / APPROVALS).read_bytes())
        product = licenses["products"]["ibkr-opra-l1"]
        approval = approvals["approvals"]["ibkr-opra-l1"]
        approval.update(capability="approved", price={"amount": "0.1250", "currency": "USD", "period": "month"})
        product.update(display_live=True, basis="https://legal.example.invalid/terms")
        self.stage_json(LICENSES, licenses)
        self.stage_json(APPROVALS, approvals)
        self.assert_passed(self.guard("index"))
        approval["account"] = MARKER
        self.stage_json(APPROVALS, approvals)
        self.assert_rejected(self.guard("index"), "invalid-policy-record")
        del approval["account"]
        approval["price"]["amount"] = 0.125
        self.stage_json(APPROVALS, approvals)
        self.assert_rejected(self.guard("index"), "invalid-policy-record")
        approval["price"]["amount"] = "0.1250"
        self.stage_json(APPROVALS, approvals)
        product["redistribute"] = True
        self.stage_json(LICENSES, licenses)
        self.assert_rejected(self.guard("index"), "invalid-policy-record")
        product.update(redistribute=False, basis="pending")
        self.stage_json(LICENSES, licenses)
        self.assert_rejected(self.guard("index"), "invalid-policy-record")

    def test_private_paths_exclude_all_worktrees_and_symlink_aliases(self):
        other = self.parent / "second-checkout"
        private = self.parent / "repo-private"
        alias = self.parent / "elsewhere"
        self.git("worktree", "add", "-q", "--detach", str(other))
        alias.symlink_to(other, target_is_directory=True)
        self.assert_passed(self.guard("private-root", private, self.parent / "test-output"))
        for path in (self.repo / "data", alias / "exports", other / "images", self.parent):
            with self.subTest(path=path):
                self.assert_rejected(self.guard("private-root", path), "private-root-overlaps-worktree")
        self.assert_rejected(self.guard("private-root", "relative"), "private-root-must-be-absolute")

    def test_ci_artifacts_serialize_only_approved_summary_fields(self):
        output = self.parent / "bootstrap-timing.json"
        timing = self.parent / "timing.env"
        timing.write_text("cold_seconds=41\nwarm_seconds=2\n")
        self.assert_passed(self.guard("artifact", "bootstrap", output, "--timing", timing))
        self.assertEqual(json.loads(output.read_bytes()), {"schema_version": 1, "check": "bootstrap",
                         "result": "passed", "cold_seconds": 41, "warm_seconds": 2})
        output.unlink()
        timing.write_text("cold_seconds=41\nwarm_seconds=2\n" + MARKER)
        self.assert_rejected(self.guard("artifact", "bootstrap", output, "--timing", timing),
                             "invalid-artifact-timings")
        self.assertFalse(output.exists())
        alias = self.parent / "alias.env"
        alias.symlink_to(timing)
        self.assert_rejected(self.guard("artifact", "bootstrap", output, "--timing", alias),
                             "invalid-artifact-timings")
        self.assert_rejected(self.guard("artifact", "supervisor", output, "--timing", timing),
                             "unexpected-artifact-input")
        self.assert_rejected(self.guard("artifact", "supervisor", self.repo / "report.json"),
                             "private-root-overlaps-worktree")
        self.assert_passed(self.guard("artifact", "supervisor", output))
        self.assertEqual(json.loads(output.read_bytes()), {"schema_version": 1, "check": "supervisor",
                         "result": "passed"})
        before = output.read_bytes()
        self.assert_rejected(self.guard("artifact", "supervisor", output), "invalid or unavailable input")
        self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
