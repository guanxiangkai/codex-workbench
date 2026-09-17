"""本机单实例运行核心；多个 MCP 会话通过受目录权限保护的 Unix socket 共用状态。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from .service import Workbench


MAX_MESSAGE = 2 * 1024 * 1024
PREVIEW_DESCRIPTOR = "preview.json"


def default_data_dir() -> Path:
    """返回本机运行目录，避免把活动数据库放入 iCloud。"""
    return Path.home() / ("Library/Application Support/CodexWorkbench" if sys.platform == "darwin" else ".local/share/codex-workbench")


def default_resources_dir() -> Path:
    """返回公开资源目录；可由环境变量覆盖，默认与本机数据目录相邻。"""
    configured = os.environ.get("WORKBENCH_RESOURCES_DIR")
    if configured:
        return Path(configured).expanduser()
    return default_data_dir() / "resources"


def secure_directory(path: Path) -> Path:
    """创建或核验专用私有目录，拒绝符号链接和过宽权限。"""
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError("运行目录不能是符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = path.resolve()
    if "Mobile Documents" in resolved.parts or "CloudStorage" in resolved.parts:
        raise ValueError("运行目录必须位于本机")
    info = resolved.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("运行目录必须属于当前用户且权限为 0700")
    return resolved


def safe_error(error: Exception) -> dict:
    """仅公开业务错误；内部异常不包含请求或凭据内容。"""
    if isinstance(error, ValueError):
        return {"code": getattr(error, "code", "invalid_request"), "message": str(error)[:500]}
    return {"code": "internal_error", "message": "工作台操作失败，请检查运行状态后重试"}


def preview_port(data_dir: Path, requested_port: int | None = None) -> int | None:
    """返回此运行核心应管理的回环预览端口；临时运行目录默认不监听 HTTP。"""
    if requested_port is not None:
        if not 0 <= requested_port <= 65535:
            raise ValueError("预览端口必须在 0 到 65535 之间")
        return requested_port
    if data_dir == default_data_dir().expanduser().resolve():
        return 18741
    return None


class RuntimeServer(socketserver.ThreadingUnixStreamServer):
    """Unix socket 由私有目录和 0600 权限保护，最大并发 16。"""

    daemon_threads = True

    def __init__(self, path: Path, service: Workbench):
        self.service = service
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(str(path), RuntimeHandler)
        path.chmod(0o600)

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

    def handle_error(self, request, client_address):
        """避免默认 traceback 将请求上下文写入运行日志。"""


class RuntimeHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(90)
        line = self.rfile.readline(MAX_MESSAGE + 1)
        if not line or len(line) > MAX_MESSAGE:
            return
        try:
            data = json.loads(line)
            if not isinstance(data, dict) or set(data) != {"name", "arguments"}:
                raise ValueError("运行请求格式错误")
            if data["name"] == "_runtime_manifest":
                arguments=data["arguments"]
                if not isinstance(arguments,dict) or set(arguments)-{"page","known_revision"}:raise ValueError("入口同步参数无效")
                value=self.server.service.manifest(arguments.get("page"),arguments.get("known_revision"))
            elif data["name"] == "_runtime_page":
                if data["arguments"] != {}:raise ValueError("页面请求参数无效")
                value=self.server.service.page(native=True)
            else:
                value = self.server.service.call(data["name"], data["arguments"])
            result = {"result": value}
        except Exception as error:
            result = {"error": safe_error(error)}
        payload = (json.dumps(result, ensure_ascii=False) + "\n").encode()
        if len(payload) > MAX_MESSAGE:
            payload = b'{"error":{"code":"too_large","message":"Result too large"}}\n'
        self.wfile.write(payload)


class RuntimeClient:
    """按需启动自己的运行核心；不会启动多个调度器或夺取正在运行的实例。"""

    def __init__(self, data_dir: Path, resources_dir: Path, codex: str):
        self.data_dir = secure_directory(data_dir)
        self.resources_dir = resources_dir
        self.codex = codex
        self.socket_path = self.data_dir / "runtime.sock"

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(90)
        try:
            sock.connect(str(self.socket_path))
            return sock
        except OSError:
            sock.close()
            raise

    def ensure(self) -> None:
        try:
            with self._connect():
                return
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        startup = self.data_dir / "startup.lock"
        if startup.is_symlink():
            raise ValueError("启动锁不能是符号链接")
        with startup.open("a") as lock:
            os.chmod(startup, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with self._connect():
                    return
            except (FileNotFoundError, ConnectionRefusedError):
                pass
            launch = Path(__file__).resolve().parents[2] / "launch.py"
            subprocess.Popen([sys.executable, str(launch), "runtime", "--data-dir", str(self.data_dir),
                              "--resources-dir", str(self.resources_dir), "--codex", self.codex],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with self._connect():
                        return
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(0.1)
            raise ValueError("本地运行核心未能启动；请检查目录权限或运行诊断")

    def manifest_watch_key(self):
        """空闲入口只监视发布文件与核心进程标记，避免高频业务查询。"""
        release=Path(__file__).resolve().parents[2]/"ui/release.json"
        result=[]
        for path in (release,self.data_dir/"runtime.pid"):
            try:stat=path.stat();result.append((stat.st_mtime_ns,stat.st_size))
            except OSError:result.append(None)
        return tuple(result)

    def manifest(self, page: str, known_revision: str | None = None) -> dict:
        """读取运行核心的当前工具与 UI 一致快照。"""
        return self.call("_runtime_manifest",{"page":page,"known_revision":known_revision})

    def preview_url(self) -> str:
        """读取当前运行核心已实际绑定的本机预览地址，不猜测或接管其他进程的端口。"""
        self.ensure()
        descriptor = self.data_dir / PREVIEW_DESCRIPTOR
        try:
            value = json.loads(descriptor.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("本机预览不可用；端口可能正被其他进程占用") from error
        url = value.get("url") if isinstance(value, dict) else None
        if not isinstance(url, str) or not url.startswith("http://127.0.0.1:"):
            raise ValueError("本机预览地址无效")
        return url

    def call(self, name: str, arguments: dict):
        """发送前可恢复一次重启断连；请求发出后绝不自动重放业务操作。"""
        self.ensure()
        payload = (json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False) + "\n").encode()
        if len(payload) > MAX_MESSAGE:
            raise ValueError("请求过大")
        try:
            connection = self._connect()
        except (FileNotFoundError, ConnectionRefusedError):
            # 探测与连接之间核心可能刚退出，此时请求尚未发送，重连不会重复写入。
            self.ensure()
            connection = self._connect()
        with connection as sock, sock.makefile("rwb") as stream:
            stream.write(payload)
            stream.flush()
            line = stream.readline(MAX_MESSAGE + 1)
        if not line or len(line) > MAX_MESSAGE:
            raise ValueError("本地运行核心响应不完整")
        response = json.loads(line)
        if "error" in response:
            error = ValueError(response["error"]["message"])
            error.code = response["error"]["code"]
            raise error
        return response["result"]


def serve(data_dir: Path, resources_dir: Path, codex: str, preview_port_override: int | None = None) -> None:
    """持有唯一运行锁并监听本机 socket；业务服务只读，不恢复或派发执行。"""
    os.umask(0o077)
    directory = secure_directory(data_dir)
    managed_preview_port = preview_port(directory, preview_port_override)
    lock_path = directory / "runtime.lock"
    if lock_path.is_symlink():
        raise ValueError("运行锁不能是符号链接")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        for name in ("workbench.sqlite3", "workbench.sqlite3-wal", "workbench.sqlite3-shm", "runtime.pid", PREVIEW_DESCRIPTOR):
            if (directory / name).is_symlink():
                raise ValueError("数据文件不能是符号链接")
        service = Workbench(directory, resources_dir, codex, lease_fd=lock.fileno())
        endpoint = directory / "runtime.sock"
        server = None
        preview = None
        preview_thread = None
        preview_descriptor = directory / PREVIEW_DESCRIPTOR
        try:
            preview_descriptor.unlink(missing_ok=True)
            if endpoint.exists():
                if not stat.S_ISSOCK(endpoint.lstat().st_mode):
                    raise ValueError("运行 socket 路径被其他文件占用")
                endpoint.unlink()
            server = RuntimeServer(endpoint, service)
            (directory / "runtime.pid").write_text(str(os.getpid()), encoding="ascii")
            if managed_preview_port is not None:
                # 延迟导入避免 API 与 runtime 的错误映射形成循环依赖。
                from .api import PreviewServer
                try:
                    preview = PreviewServer(("127.0.0.1", managed_preview_port), service)
                except OSError:
                    # 未知进程已占用端口时绝不接管或终止它；socket 核心仍可继续服务 MCP。
                    preview = None
                else:
                    preview_descriptor.write_text(json.dumps({"url": f"http://127.0.0.1:{preview.server_port}"}), encoding="utf-8")
                    os.chmod(preview_descriptor, 0o600)
                    preview_thread = threading.Thread(target=preview.serve_forever, name="codex-workbench-preview", daemon=True)
                    preview_thread.start()
            def stop(signum, frame):
                threading.Thread(target=server.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            server.serve_forever(poll_interval=0.2)
        finally:
            try:
                try:
                    if server is not None:
                        server.server_close()
                finally:
                    try:
                        if preview is not None:
                            if preview_thread is not None:
                                preview.shutdown()
                                preview_thread.join(2)
                            preview.server_close()
                    finally:
                        service.close()
            finally:
                # 关闭失败仍清理本实例入口，子进程守卫继续持有租约直到退出。
                if server is not None:
                    endpoint.unlink(missing_ok=True)
                    (directory / "runtime.pid").unlink(missing_ok=True)
                    preview_descriptor.unlink(missing_ok=True)
