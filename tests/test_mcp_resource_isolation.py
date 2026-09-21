"""旧的独立入口资源不能在唯一工作台会话中复活。"""
import unittest

from codex_workbench.catalog import UI_URIS
from codex_workbench.mcp import McpSession
from test_mcp import FakeClient


class ResourceIsolationTest(unittest.TestCase):
    def test_old_resource_is_rejected_even_after_current_resource_was_read(self):
        session = McpSession(FakeClient())
        session.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        current = UI_URIS["open_workbench"]
        self.assertIn("result", session.handle({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": current}}))
        self.assertEqual(-32602, session.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/subscribe", "params": {"uri": "ui://codex-workbench/v4/task-board.html"}})["error"]["code"])

    def test_cached_workbench_revision_reads_current_page(self):
        session = McpSession(FakeClient())
        session.handle({"jsonrpc":"2.0","id":1,"method":"initialize"})
        session.handle({"jsonrpc":"2.0","method":"notifications/initialized"})
        base=UI_URIS["open_workbench"].removesuffix(".html")
        old=base+"/"+"a"*64+".html"
        response=session.handle({"jsonrpc":"2.0","id":2,"method":"resources/read","params":{"uri":old}})
        self.assertIn("result",response)
        self.assertIn("const INITIAL_PAGE=",response["result"]["contents"][0]["text"])
        for invalid in (base+"/../accounts.html",base+"/bad.html",old+"?x=1",old.replace("workbench/", "accounts/")):
            response=session.handle({"jsonrpc":"2.0","id":3,"method":"resources/read","params":{"uri":invalid}})
            self.assertIn("error",response)


if __name__ == "__main__":
    unittest.main()
