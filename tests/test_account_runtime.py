"""执行账户环境边界测试；仅使用临时目录和虚构环境值。"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_workbench.account_runtime import account_environment, login_invocation, validate_account_home
from codex_workbench.executor import CodexExecutor, Request


class AccountRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.account = Path(self.directory.name).resolve() / "account"
        self.account.mkdir(mode=0o700)

    def test_only_child_environment_selects_account(self):
        source = {"CODEX_HOME": "/original", "CODEX_API_KEY": "fixture-only",
                  "OPENAI_API_KEY": "fixture-only", "CODEX_ACCESS_TOKEN": "fixture-only", "PATH": "/bin"}
        child = account_environment(str(self.account), source)
        self.assertEqual(child["CODEX_HOME"], str(self.account))
        self.assertEqual(source["CODEX_HOME"], "/original")
        self.assertNotIn("CODEX_API_KEY", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn("CODEX_ACCESS_TOKEN", child)
        self.assertEqual(child["PATH"], "/bin")

    def test_shared_cloud_and_symlink_directories_are_rejected(self):
        self.account.chmod(0o755)
        with self.assertRaises(ValueError):
            validate_account_home(str(self.account))
        self.account.chmod(0o700)
        link = Path(self.directory.name) / "linked"
        link.symlink_to(self.account, target_is_directory=True)
        with self.assertRaises(ValueError):
            validate_account_home(str(link))
        cloud = Path(self.directory.name) / "Mobile Documents" / "account"
        cloud.mkdir(parents=True, mode=0o700)
        with self.assertRaises(ValueError):
            validate_account_home(str(cloud))

    def test_auth_file_cannot_alias_another_account(self):
        (self.account / "auth.json").symlink_to(Path(self.directory.name) / "another-auth-file")
        with self.assertRaises(ValueError):
            validate_account_home(str(self.account))

    def test_hardlinked_auth_and_config_are_rejected_without_reading(self):
        for name in ("auth.json", "config.toml"):
            with self.subTest(name=name):
                source = Path(self.directory.name) / ("another-" + name)
                source.touch(mode=0o600)
                target = self.account / name
                os.link(source, target)
                try:
                    self.assertEqual(source.stat().st_ino, target.stat().st_ino)
                    self.assertEqual(2, target.stat().st_nlink)
                    with patch.object(Path, "open", side_effect=AssertionError("校验不得读取文件内容")):
                        with self.assertRaises(ValueError):
                            validate_account_home(str(self.account))
                finally:
                    target.unlink()

    def test_private_regular_files_and_missing_files_are_valid_without_reading(self):
        self.assertEqual(str(self.account), validate_account_home(str(self.account)))
        for name in ("auth.json", "config.toml"):
            (self.account / name).touch(mode=0o600)
        with patch.object(Path, "open", side_effect=AssertionError("校验不得读取文件内容")):
            self.assertEqual(str(self.account), validate_account_home(str(self.account)))

    def test_auth_and_config_must_be_regular_files(self):
        for name in ("auth.json", "config.toml"):
            target = self.account / name
            with self.subTest(name=name, kind="directory"):
                target.mkdir()
                try:
                    with self.assertRaises(ValueError):
                        validate_account_home(str(self.account))
                finally:
                    target.rmdir()
            with self.subTest(name=name, kind="fifo"):
                os.mkfifo(target, mode=0o600)
                try:
                    with self.assertRaises(ValueError):
                        validate_account_home(str(self.account))
                finally:
                    target.unlink()

    def test_auth_and_config_must_belong_to_current_user(self):
        original_lstat = Path.lstat
        for name in ("auth.json", "config.toml"):
            with self.subTest(name=name):
                target = self.account / name
                target.touch(mode=0o600)

                def foreign_owner(path, *args, **kwargs):
                    info = original_lstat(path, *args, **kwargs)
                    if path == target:
                        return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_uid=os.getuid() + 1)
                    return info

                try:
                    with patch.object(Path, "lstat", foreign_owner), self.assertRaises(ValueError):
                        validate_account_home(str(self.account))
                finally:
                    target.unlink()

    def test_login_and_execution_share_isolated_store(self):
        args, environment = login_invocation(("codex",), str(self.account))
        self.assertIn("login", args)
        self.assertIn('cli_auth_credentials_store="file"', args)
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertEqual(environment["CODEX_HOME"], str(self.account))
        exec_args = CodexExecutor().argv(Request(str(self.account), "测试", "", account_home=str(self.account)))
        self.assertIn('cli_auth_credentials_store="file"', exec_args)
        self.assertIn('forced_login_method="chatgpt"', exec_args)


if __name__ == "__main__":
    unittest.main()
