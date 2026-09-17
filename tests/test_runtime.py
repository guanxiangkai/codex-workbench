"""运行核心退出失败时的入口与资源清理。"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from codex_workbench.runtime import PREVIEW_DESCRIPTOR, RuntimeClient, preview_port, serve


class RuntimeCleanupTests(unittest.TestCase):
    def test_only_default_directory_automatically_manages_the_standard_preview_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("codex_workbench.runtime.default_data_dir", return_value=root / "primary"):
                self.assertEqual(18741, preview_port(root / "primary"))
                self.assertIsNone(preview_port(root / "temporary"))
            self.assertEqual(0, preview_port(root / "temporary", 0))

    def test_runtime_starts_preview_after_socket_and_closes_it_with_core(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = root / "runtime"
            service = MagicMock()
            observed = {}

            class FakePreview:
                server_port = 42680

                def __init__(self, address, client):
                    observed["socket_ready"] = (data / "runtime.sock").exists()
                    observed["address"] = address
                    observed["client"] = client
                    self.stopped = threading.Event()

                def serve_forever(self):
                    self.stopped.wait(2)

                def shutdown(self):
                    observed["shutdown"] = True
                    self.stopped.set()

                def server_close(self):
                    observed["closed"] = True

            old_mask = os.umask(0o077)
            try:
                with patch("codex_workbench.runtime.Workbench", return_value=service), patch("codex_workbench.runtime.RuntimeServer.serve_forever"), patch("codex_workbench.runtime.signal.signal"), patch("codex_workbench.api.PreviewServer", FakePreview):
                    serve(data, root / "resources", "synthetic-codex", 0)
                self.assertTrue(observed["socket_ready"])
                self.assertEqual(("127.0.0.1", 0), observed["address"])
                self.assertIs(service, observed["client"])
                self.assertTrue(observed["shutdown"])
                self.assertTrue(observed["closed"])
                service.store.recover_interrupted.assert_not_called()
                self.assertFalse((data / PREVIEW_DESCRIPTOR).exists())
            finally:
                os.umask(old_mask)

    def client_with_stream(self):
        client=RuntimeClient.__new__(RuntimeClient)
        client.ensure=MagicMock()
        sock=MagicMock();sock.__enter__.return_value=sock
        stream=sock.makefile.return_value.__enter__.return_value
        stream.readline.return_value=b'{"result":{"ok":true}}\n'
        return client,sock,stream

    def test_restarts_connection_before_sending_a_mutation_only_once(self):
        client,sock,stream=self.client_with_stream()
        client._connect=MagicMock(side_effect=[ConnectionRefusedError(),sock])
        self.assertEqual({'ok':True},client.call('agent_create',{'name':'验收'}))
        self.assertEqual(2,client.ensure.call_count)
        stream.write.assert_called_once()

    def test_response_disconnect_never_replays_sent_mutation(self):
        client,sock,stream=self.client_with_stream()
        client._connect=MagicMock(return_value=sock)
        stream.readline.side_effect=ConnectionResetError()
        with self.assertRaises(ConnectionResetError):
            client.call('agent_create',{'name':'验收'})
        client._connect.assert_called_once()
        stream.write.assert_called_once()

    def test_close_failure_still_removes_owned_socket_and_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "runtime"
            service = MagicMock()
            service.close.side_effect = RuntimeError("合成关闭失败")
            old_mask = os.umask(0o077)
            try:
                with patch("codex_workbench.runtime.Workbench", return_value=service), patch("codex_workbench.runtime.RuntimeServer.serve_forever"), patch("codex_workbench.runtime.signal.signal"):
                    with self.assertRaisesRegex(RuntimeError, "合成关闭失败"):
                        serve(data, root / "resources", "synthetic-codex")
                service.close.assert_called_once()
                self.assertFalse((data / "runtime.sock").exists())
                self.assertFalse((data / "runtime.pid").exists())
            finally:
                os.umask(old_mask)

    def test_startup_failure_closes_worker_and_preserves_foreign_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "runtime"
            data.mkdir(mode=0o700)
            endpoint = data / "runtime.sock"
            endpoint.write_text("保留用户文件")
            service = MagicMock()
            old_mask = os.umask(0o077)
            try:
                with patch("codex_workbench.runtime.Workbench", return_value=service):
                    with self.assertRaisesRegex(ValueError, "被其他文件占用"):
                        serve(data, root / "resources", "synthetic-codex")
                service.close.assert_called_once()
                self.assertEqual("保留用户文件", endpoint.read_text())
            finally:
                os.umask(old_mask)


if __name__ == "__main__":
    unittest.main()
