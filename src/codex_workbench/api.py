"""与 MCP 共用业务核心的本机预览，只提供单页和同源 RPC。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from .catalog import VERSION, validate, UI_VIEWS, DEFAULT_VIEW
from .mcp import ui_html
from .runtime import safe_error


class PreviewServer(ThreadingHTTPServer):
    """最多 16 个并发连接，只监听 127.0.0.1。"""
    daemon_threads = True

    def __init__(self, address, client):
        if address[0] != "127.0.0.1":
            raise ValueError("只允许本机回环监听")
        self.client = client
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    """严格同源请求，不提供任意文件读取。"""
    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def log_message(self, *args):
        """不记录账户、凭证、URL。"""

    def _allowed(self):
        authority = f"127.0.0.1:{self.server.server_port}"
        return self.headers.get("Host") == authority and self.headers.get("Origin", f"http://{authority}") == f"http://{authority}"

    def _respond(self, code, value, html=False):
        body = value.encode("utf-8") if html else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self._allowed():
            return self._respond(403, {"error": "origin_denied"})
        path = urlsplit(self.path).path
        if path == "/" or path[1:] in UI_VIEWS:
            page=path[1:] if path != "/" else DEFAULT_VIEW
            if hasattr(self.server.client,"page"):
                try:html=self.server.client.page(page)["html"]
                except Exception:return self._respond(503,{"error":"initial_data_unavailable"})
            elif hasattr(self.server.client,"manifest"):
                manifest=self.server.client.manifest("workbench")
                html=manifest["html"].replace("__WORKBENCH_RESOURCE_URI__",manifest["resource_uri"])
                html=html.replace(f"const INITIAL_PAGE='{DEFAULT_VIEW}';", f"const INITIAL_PAGE='{page}';")
            else:
                html=ui_html(page)
            return self._respond(200,html,html=True)
        if path == "/health":
            return self._respond(200, {"status": "ready", "version": VERSION})
        return self._respond(404, {"error": "not_found"})

    def do_OPTIONS(self):
        """原生视图通过宿主通道读取，预览不开放跨域访问。"""
        self._respond(403, {"error": "cross_origin_denied"})

    def do_POST(self):
        if not self._allowed() or self.headers.get("X-Workbench-Request") != "1":
            return self._respond(403, {"error": "origin_denied"})
        if self.path != "/rpc":
            return self._respond(404, {"error": "not_found"})
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            return self._respond(415, {"error": "json_required"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise ValueError("请求体大小无效")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict) or set(data) != {"name", "arguments"}:
                raise ValueError("RPC 请求格式错误")
            validate(data["name"], data["arguments"])
            result = self.server.client.call(data["name"], data["arguments"])
            self._respond(200, {"structuredContent": result if isinstance(result, dict) else {"items": result}, "content": [{"type": "text", "text": "操作已完成"}]})
        except Exception as error:
            safe = safe_error(error)
            self._respond(200, {"isError": True, "structuredContent": {"error": safe}, "content": [{"type": "text", "text": safe["message"]}]})
