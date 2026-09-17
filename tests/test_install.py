"""安装器仅修改同源工作台配置的回归。"""
import importlib.util
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("workbench_installer", Path(__file__).resolve().parents[1] / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(installer, "LOCK_DIRECTORY", Path(self.temp.name) / "locks")
        patch.start()
        self.addCleanup(patch.stop)

    def test_update_and_remove_preserve_unrelated_profile_fields(self):
        path = Path(self.temp.name) / "config.toml"
        path.write_text('model="keep"\n[mcp_servers.codex-workbench-assistants]\nenabled=true\n[mcp_servers.other]\nenabled=false\n')
        installer.update_section(path, {"enabled": "true", "required": "false"})
        with installer._install_lock():
            installer._remove_section_unlocked(path, "codex-workbench-assistants")
        parsed = tomllib.loads(path.read_text())
        self.assertEqual("keep", parsed["model"])
        self.assertTrue(parsed["mcp_servers"]["codex-workbench"]["enabled"])
        self.assertNotIn("codex-workbench-assistants", parsed["mcp_servers"])
        self.assertFalse(parsed["mcp_servers"]["other"]["enabled"])

    def test_register_replaces_only_same_source_and_removes_legacy_servers(self):
        root = Path(self.temp.name)
        (root / "config.toml").write_text('model="keep"\n')
        (root / "private.config.toml").write_text('[mcp_servers.codex-workbench-assistants]\nenabled=true\n')
        entry = str(Path(installer.__file__).resolve().parent / "mcp.command")
        existing = {
            "codex-workbench": [entry, "mcp", "--page", "board"],
            "codex-workbench-assistants": [entry, "mcp", "--page", "agents"],
            "codex-workbench-accounts": [entry, "mcp", "--page", "accounts"],
        }
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if command[2] == "get":
                name = command[3]
                if name not in existing:
                    return mock.Mock(returncode=1, stdout="")
                return mock.Mock(returncode=0, stdout=json.dumps({"enabled": True, "transport": {"command": "/bin/sh", "args": existing[name]}}))
            if command[2] == "remove":
                existing.pop(command[3], None)
                return mock.Mock(returncode=0, stdout="")
            self.assertEqual("add", command[2])
            existing[command[3]] = command[6:]
            return mock.Mock(returncode=0, stdout="")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(root)}, clear=False), mock.patch.object(installer.subprocess, "run", side_effect=run):
            installer.main()
        self.assertEqual(["codex-workbench-assistants", "codex-workbench-accounts"], [call[3] for call in calls if call[2] == "remove"])
        self.assertFalse(any(call[2]=="add" for call in calls))
        public=tomllib.loads((root/"config.toml").read_text())
        self.assertEqual([entry,*installer.SERVERS["codex-workbench"]],public["mcp_servers"]["codex-workbench"]["args"])
        private = tomllib.loads((root / "private.config.toml").read_text())
        self.assertFalse(private["mcp_servers"]["codex-workbench"]["enabled"])
        self.assertNotIn("codex-workbench-assistants", private["mcp_servers"])


if __name__ == "__main__":
    unittest.main()
