"""账户运行时仍隔离账户目录和受控环境变量。"""
import os
import tempfile
import unittest
from pathlib import Path

from codex_workbench.account_runtime import validate_account_home


class AccountRuntimeTests(unittest.TestCase):
    def test_account_home_requires_an_isolated_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'codex-a'
            root.mkdir()
            os.chmod(root, 0o700)
            self.assertEqual(str(root.resolve()), validate_account_home(str(root)))
            with self.assertRaises(ValueError):
                validate_account_home('../escape')
