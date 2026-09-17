"""受控探测与登记仅使用临时合成保险库替身，不读取真实秘密。"""

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys

from codex_workbench.credential_probe import CredentialProbeError, apply_probe, probe_entry
from codex_workbench.credentials import CredentialCatalog, CredentialCatalogError


class CredentialProbeTest(unittest.TestCase):
    """覆盖固定消费者、双阶段写入、错误不回显和竞争拒绝。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.entries = self.root / "entries.json"
        self.entries.write_text('[{"entryId":"synthetic-entry","revision":1}]')
        self.mode = self.root / "mode"
        self.mode.write_text("success")
        self.command = self.root / "fake-vault.py"
        self.command.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, subprocess, sys, time\n"
            "root=pathlib.Path(__file__).parent\n"
            "operation=sys.argv[1]\n"
            "if operation=='status': print('{\"ready\":true}'); raise SystemExit(0)\n"
            "if operation=='list': print((root/'entries.json').read_text()); raise SystemExit(0)\n"
            "assert operation=='exec-stdin' and sys.argv[2]=='synthetic-entry'\n"
            f"assert sys.argv[3]=={sys.executable!r}\n"
            "assert sys.argv[4:]==['-B','-m','codex_workbench.credential_probe']\n"
            "mode=(root/'mode').read_text()\n"
            "secret='synthetic-secret-shaped-value'\n"
            "if mode=='stderr': print(secret, file=sys.stderr); raise SystemExit(0)\n"
            "if mode=='failure': print(secret); print(secret,file=sys.stderr); raise SystemExit(3)\n"
            "if mode=='malformed': print(secret); raise SystemExit(0)\n"
            "if mode=='oversized': print(secret*2000); raise SystemExit(0)\n"
            "if mode=='timeout': print(secret,flush=True); time.sleep(5); raise SystemExit(0)\n"
            "if mode=='race': (root/'entries.json').write_text('[{\"entryId\":\"synthetic-entry\",\"revision\":2}]')\n"
            "if mode=='forged': print(json.dumps({'secret-key-name':secret})); raise SystemExit(0)\n"
            "payload=json.dumps({'account':secret,'password':secret,'secret-key-name':secret}).encode()\n"
            "raise SystemExit(subprocess.run(sys.argv[3:],input=payload,check=False).returncode)\n",
            encoding="utf-8",
        )
        self.command.chmod(0o700)
        self.catalog = CredentialCatalog(self.root / "catalog.sqlite3", self.command, poll_seconds=0)

    def tearDown(self):
        self.temp.cleanup()

    def test_success_returns_safe_ticket_then_explicit_apply_only_writes_structure(self):
        folder = self.catalog.folder_create("自定义目录")
        self.catalog.entry_update("synthetic-entry", label="自定义名称", folder_id=folder["id"], tags=["自定义标签"])
        ticket = probe_entry(self.catalog, "synthetic-entry")
        self.assertNotIn("synthetic-secret-shaped-value", json.dumps(ticket))
        self.assertNotIn("secret-key-name", json.dumps(ticket))
        self.assertEqual(1, ticket["revision"])
        self.assertEqual(0, ticket["expected_generation"])
        self.assertEqual(["account", "password"], ticket["structure"]["has_fields"])
        with sqlite3.connect(self.catalog.db_path) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM credential_structure").fetchone()[0])
        indexed = apply_probe(self.catalog, ticket)
        self.assertEqual("password", indexed["kind"])
        self.assertEqual("probe", indexed["structure_source"])
        self.assertEqual("自定义名称", indexed["label"])
        self.assertEqual(folder["id"], indexed["folder_id"])
        self.assertEqual(["自定义标签"], indexed["tags"])
        self.assertNotIn(b"synthetic-secret-shaped-value", self.catalog.db_path.read_bytes())

    def test_bad_subprocess_outputs_and_timeout_never_echo_secrets(self):
        for mode, expected in (("stderr", "probe_failed"), ("failure", "probe_failed"),
                               ("malformed", "probe_reply_invalid"), ("forged", "probe_reply_invalid"),
                               ("oversized", "probe_reply_invalid"), ("timeout", "probe_timeout")):
            with self.subTest(mode=mode):
                self.mode.write_text(mode)
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(CredentialProbeError) as caught:
                        probe_entry(self.catalog, "synthetic-entry", timeout=0.2 if mode == "timeout" else 5)
                self.assertEqual(expected, caught.exception.code)
                self.assertEqual("", stdout.getvalue() + stderr.getvalue())
                self.assertNotIn("synthetic-secret-shaped-value", str(caught.exception))
                self.assertNotIn("secret-key-name", str(caught.exception))
                self.assertNotIn("stdout", vars(caught.exception))
                self.assertNotIn("stderr", vars(caught.exception))
        with sqlite3.connect(self.catalog.db_path) as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM credential_structure").fetchone()[0])

    def test_revision_race_and_generation_race_both_reject_apply(self):
        self.mode.write_text("race")
        ticket = probe_entry(self.catalog, "synthetic-entry")
        self.assertEqual(1, ticket["revision"])
        with self.assertRaises(CredentialCatalogError) as caught:
            apply_probe(self.catalog, ticket)
        self.assertEqual("structure_stale", caught.exception.code)
        self.mode.write_text("success")
        ticket = probe_entry(self.catalog, "synthetic-entry")
        apply_probe(self.catalog, ticket)
        with self.assertRaises(CredentialCatalogError) as caught:
            apply_probe(self.catalog, ticket)
        self.assertEqual("structure_conflict", caught.exception.code)

    def test_invalid_parameters_and_ticket_are_fixed_errors(self):
        for timeout in (True, 0, -1, 61, float("nan"), float("inf")):
            with self.assertRaises(CredentialProbeError) as caught:
                probe_entry(self.catalog, "synthetic-entry", timeout=timeout)
            self.assertEqual("probe_invalid", caught.exception.code)
        with self.assertRaises(CredentialProbeError):
            probe_entry(self.catalog, "bad\nentry")
        with self.assertRaises(CredentialProbeError):
            apply_probe(self.catalog, {"raw": "synthetic-secret-shaped-value"})
