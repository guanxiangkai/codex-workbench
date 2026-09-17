"""目录选择与取消不隐式创建项目。"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codex_workbench.workspace_picker import choose_workspace


class WorkspacePickerTests(unittest.TestCase):
    def test_selected_existing_directory(self):
        with tempfile.TemporaryDirectory() as temporary, patch("codex_workbench.workspace_picker.sys.platform", "darwin"), patch("codex_workbench.workspace_picker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, temporary + "\n", "")):
            self.assertEqual({"path": str(Path(temporary).resolve()), "cancelled": False}, choose_workspace())

    def test_cancel_is_not_a_path(self):
        with patch("codex_workbench.workspace_picker.sys.platform", "darwin"), patch("codex_workbench.workspace_picker.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "User canceled (-128)")):
            self.assertEqual({"path": None, "cancelled": True}, choose_workspace())
