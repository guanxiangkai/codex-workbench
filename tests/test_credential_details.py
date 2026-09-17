"""受控详情只使用临时合成保险库，验证父进程永不取得明文。"""

import base64
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codex_workbench.credential_details import CredentialDetailsError, _aad, _format_payload, read_details
from codex_workbench.credentials import CredentialCatalog


class CredentialDetailsTest(unittest.TestCase):
    """成功、绑定、防泄漏、修订竞态及输入输出边界均使用合成值。"""

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
            "import pathlib, subprocess, sys\n"
            "root=pathlib.Path(__file__).parent; operation=sys.argv[1]\n"
            "if operation=='status': print('{\\\"ready\\\":true}'); raise SystemExit(0)\n"
            "if operation=='list': print((root/'entries.json').read_text()); raise SystemExit(0)\n"
            "assert operation=='exec-stdin' and sys.argv[2]=='synthetic-entry'\n"
            f"assert sys.argv[3]=={sys.executable!r}\n"
            "assert sys.argv[4:7]==['-B','-m','codex_workbench.credential_details']\n"
            "mode=(root/'mode').read_text(); secret='synthetic-detail-private-value'\n"
            "if mode=='stderr': print(secret,file=sys.stderr); raise SystemExit(0)\n"
            "if mode=='failure': print(secret); raise SystemExit(3)\n"
            "if mode=='large': secret='x'*(1024*1024+1)\n"
            "if mode=='race': (root/'entries.json').write_text('[{\\\"entryId\\\":\\\"synthetic-entry\\\",\\\"revision\\\":2}]')\n"
            "payload=('{\\\"credential\\\":{\\\"account\\\":\\\"synthetic-user\\\",\\\"password\\\":\\\"'+secret+'\\\"},\\\"target\\\":{\\\"name\\\":\\\"synthetic\\\"}}').encode()\n"
            "raise SystemExit(subprocess.run(sys.argv[3:],input=payload,check=False).returncode)\n",
            encoding="utf-8",
        )
        self.command.chmod(0o700)
        self.catalog = CredentialCatalog(self.root / "catalog.sqlite3", self.command, poll_seconds=0)
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public = base64.b64encode(self.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode("ascii")
        self.request_id = "request_0123456789abcdef"

    def tearDown(self):
        self.temp.cleanup()

    def decrypt(self, reply, *, request_id=None, entry_id=None, revision=None):
        aes = self.private.decrypt(base64.b64decode(reply["wrapped_key"]), padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        return AESGCM(aes).decrypt(base64.b64decode(reply["iv"]), base64.b64decode(reply["ciphertext"]), _aad(
            entry_id or reply["entry_id"], revision or reply["revision"], request_id or reply["request_id"], reply["public_key_sha256"]))

    def test_browser_key_can_decrypt_original_wrapper_and_parent_result_has_no_secret(self):
        reply = read_details(self.catalog, "synthetic-entry", self.public, self.request_id)
        encoded = json.dumps(reply)
        self.assertNotIn("synthetic-detail-private-value", encoded)
        self.assertEqual({"entry_id", "revision", "generation", "request_id", "public_key_sha256", "wrapped_key", "iv", "ciphertext"}, set(reply))
        payload = json.loads(self.decrypt(reply))
        self.assertEqual("workbench_credential", payload["format"])
        self.assertEqual("synthetic-detail-private-value", payload["fields"]["credential"]["password"])
        self.assertEqual("synthetic-user", payload["fields"]["credential"]["account"])
        self.assertFalse(self.catalog.db_path.read_bytes().find(b"synthetic-detail-private-value") >= 0)

    def test_aad_rejects_request_or_revision_tampering(self):
        reply = read_details(self.catalog, "synthetic-entry", self.public, self.request_id)
        with self.assertRaises(InvalidTag):
            self.decrypt(reply, request_id="request_abcdef0123456789")
        with self.assertRaises(InvalidTag):
            self.decrypt(reply, revision=2)

    def test_unknown_json_and_armored_gpg_text_keep_original_value_without_name_mapping(self):
        raw_json = b'{"unmapped-secret-name":{"password":"synthetic-detail-private-value"}}'
        self.assertEqual({"format": "json", "fields": {"unmapped-secret-name": {"password": "synthetic-detail-private-value"}}}, _format_payload(raw_json))
        armored = b"-----BEGIN PGP PRIVATE KEY BLOCK-----\nsynthetic-detail-private-value\n-----END PGP PRIVATE KEY BLOCK-----"
        self.assertEqual({"format": "text", "text": armored.decode("utf-8")}, _format_payload(armored))

    def test_bad_output_revision_drift_and_boundaries_have_fixed_errors_without_stdout(self):
        cases = (("stderr", "details_failed"), ("failure", "details_failed"), ("large", "details_failed"), ("race", "details_stale"))
        for mode, expected in cases:
            with self.subTest(mode=mode):
                self.entries.write_text('[{"entryId":"synthetic-entry","revision":1}]')
                self.mode.write_text(mode)
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(CredentialDetailsError) as caught:
                        read_details(self.catalog, "synthetic-entry", self.public, self.request_id)
                self.assertEqual(expected, caught.exception.code)
                self.assertEqual("", stdout.getvalue() + stderr.getvalue())
                self.assertNotIn("synthetic-detail-private-value", str(caught.exception))
        for invalid in ("not-base64", base64.b64encode(b"not a key").decode(), "", self.public[:-2] + "!!"):
            with self.assertRaises(CredentialDetailsError) as caught:
                read_details(self.catalog, "synthetic-entry", invalid, self.request_id)
            self.assertEqual("details_invalid", caught.exception.code)
        with self.assertRaises(CredentialDetailsError):
            read_details(self.catalog, "synthetic-entry", self.public, "short")
