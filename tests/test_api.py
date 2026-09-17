"""本机预览 HTTP 的来源与工具转发契约。"""
import http.client
import json
import threading
import unittest
from codex_workbench.api import PreviewServer
from codex_workbench.catalog import DEFAULT_VIEW, UI_VIEWS


class FakeClient:
    def call(self, name, arguments):
        if name != "workbench_state":
            raise ValueError("工具不存在")
        return {"agents": [], "provider_models": [], "accounts": []}


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.server = PreviewServer(("127.0.0.1", 0), FakeClient())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path="/rpc", method="POST", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, json.dumps(body or {"name": "workbench_state", "arguments": {}}) if method=="POST" else None,
                               {"Content-Type": "application/json", "X-Workbench-Request": "1", **(headers or {})})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_real_ui_resource(self):
        status, body, headers = self.request("/", "GET")
        self.assertEqual(status, 200)
        self.assertIn(b"ui/initialize", body)
        self.assertNotIn(b"__WORKBENCH_INITIAL_PAGE__", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_tool_result_shape(self):
        status, body, _ = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["structuredContent"]["agents"], [])

    def test_all_internal_views_share_one_html_route_contract(self):
        for path, page in [("/", DEFAULT_VIEW), *[(f"/{view}", view) for view in sorted(UI_VIEWS)]]:
            status, body, _ = self.request(path, "GET")
            self.assertEqual(200, status)
            self.assertIn("const INITIAL_PAGE='" + page + "';", body.decode())
        self.assertEqual(404, self.request("/unknown-page", "GET")[0])
        self.assertEqual(404, self.request("/board", "GET")[0])

    def test_unknown_tool_is_a_tool_error(self):
        _, body, _ = self.request(body={"name": "arbitrary_shell", "arguments": {}})
        self.assertTrue(json.loads(body)["isError"])

    def test_cross_origin_and_rebinding_are_denied(self):
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"X-Workbench-Request": ""}):
            self.assertEqual(self.request(headers=headers)[0], 403)

    def test_no_file_traversal(self):
        self.assertEqual(self.request("/../../auth.json", "GET")[0], 404)

    def test_no_external_listener(self):
        with self.assertRaises(ValueError):
            PreviewServer(("0.0.0.0", 0), FakeClient())


if __name__ == "__main__":
    unittest.main()
