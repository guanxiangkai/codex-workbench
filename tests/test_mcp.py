"""单一工作台 MCP 入口的协议与资源契约。"""
from __future__ import annotations

import unittest

from codex_workbench.catalog import DEFAULT_VIEW, PAGES, UI_URIS, VERSION, tools_for_page
from codex_workbench.mcp import McpSession
from codex_workbench.ui_resources import page_icons, ui_html


class FakeClient:
    def __init__(self):
        self.calls = []
        self.revision = "r1"

    def call(self, name, arguments):
        if name=="_runtime_page":return {"html":ui_html(),"csp":{"connectDomains":["http://127.0.0.1:18741"],"resourceDomains":[]}}
        if name != "_runtime_manifest":
            self.calls.append((name, arguments))
            return {"name": name}
        if arguments.get("known_revision") == self.revision:
            return {"revision": self.revision, "unchanged": True}
        page = arguments["page"]
        entry = PAGES[page][1]
        return {"revision": self.revision, "version": VERSION, "tools": tools_for_page(page),
                "page": page, "entry": entry, "title": PAGES[page][2],
                "resource_uri": UI_URIS[entry], "html": ui_html() + "__WORKBENCH_RESOURCE_URI__",
                "icons": page_icons(page)}


class McpSessionTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.session = McpSession(self.client)

    def initialize(self):
        response = self.session.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual("codex-workbench", response["result"]["serverInfo"]["name"])
        self.assertIsNone(self.session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_only_workbench_is_a_native_global_entry_and_all_business_tools_share_it(self):
        self.initialize()
        tools = self.session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        entries = [tool for tool in tools if tool.get("_meta", {}).get("openai/ui", {}).get("entrypoints")]
        self.assertEqual(["open_workbench"], [tool["name"] for tool in entries])
        self.assertEqual(set(tool["name"] for tool in tools_for_page("workbench")), {tool["name"] for tool in tools})
        self.assertIn("workbench_state", {tool["name"] for tool in tools})

    def test_only_current_workbench_resource_is_readable(self):
        self.initialize()
        uri = UI_URIS["open_workbench"]
        content = self.session.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": uri}})["result"]["contents"][0]
        self.assertEqual(uri, content["uri"])
        self.assertIn(f"const INITIAL_PAGE='{DEFAULT_VIEW}';", content["text"])
        for obsolete in ("ui://codex-workbench/v4/task-board.html", "ui://codex-workbench/v4/agents.html", "ui://codex-workbench/v4/accounts.html"):
            rejected = self.session.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": obsolete}})
            self.assertEqual(-32602, rejected["error"]["code"])


if __name__ == "__main__":
    unittest.main()
