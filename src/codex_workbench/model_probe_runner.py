"""在受控子进程验证模型；保险库直接把凭据交给消费者，不经过工作台日志。"""
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


class ModelProbeRunner:
    """为一次探测提供 40 秒硬时限和父进程退出清理，不借用 Codex 认证。"""

    def __init__(self, data_dir: Path, lease_fd: int | None = None, vault_command: Path | None = None, timeout: float = 40):
        self.directory = Path(data_dir) / "model-probes"
        self.lease_fd = lease_fd
        self.vault_command = vault_command or Path.home() / ".codex/scripts/key-vault/key-vault.sh"
        self.timeout = timeout

    def __call__(self, model: dict, cancel_event: threading.Event) -> dict:
        """执行明确配置目标的微型能力检查，只返回状态和固定错误说明。"""
        if cancel_event.is_set():
            return self._error("cancelled", "模型验证已取消")
        if self.directory.is_symlink():
            return self._error("local_directory", "模型验证目录不可用")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            return self._error("local_directory", "模型验证目录权限无效")
        keys = ("name", "model_type", "base_url", "model", "protocol", "credential_ref", "voice")
        config = {key: model.get(key) for key in keys}
        reference = config.get("credential_ref") or ""
        if reference and not re.fullmatch(r"vault:[A-Za-z0-9][A-Za-z0-9_.:-]{0,180}", reference):
            return self._error("credential_reference", "请填写有效的保险库引用")
        environment = dict(os.environ)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL", "OPENAI_API_BASE", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "CODEX_HOME"):
            environment.pop(key, None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        worker = Path(__file__).with_name("model_probe_worker.py")
        control_read, control_write = os.pipe()
        process = None
        selector = selectors.DefaultSelector()
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="probe-", suffix=".json", dir=self.directory) as file:
                json.dump(config, file, ensure_ascii=False); file.flush()
                command = [sys.executable, "-I", str(worker), file.name]
                if reference:
                    if not self.vault_command.is_file():
                        return self._error("credential_unavailable", "保险库入口不可用，等待配置后重试")
                    command = [str(self.vault_command), "exec-stdin", reference[6:], *command]
                guard = [sys.executable, "-I", str(Path(__file__).with_name("guard.py")), str(control_read), *command]
                process = subprocess.Popen(guard, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           env=environment, start_new_session=True,
                                           pass_fds=(control_read,) + (() if self.lease_fd is None else (self.lease_fd,)))
                os.close(control_read); control_read = -1
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                output = bytearray(); deadline = time.monotonic() + self.timeout
                while selector.get_map():
                    if cancel_event.is_set():
                        return self._error("cancelled", "模型验证已取消")
                    if time.monotonic() >= deadline:
                        return self._error("timeout", "模型验证超时，请手动重新验证")
                    for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                        block = os.read(key.fd, 4096)
                        if not block:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(block)
                            if len(output) > 8192:
                                return self._error("invalid_result", "模型验证返回格式无效")
                process.wait(timeout=2)
                try:
                    result = json.loads(output)
                    if not isinstance(result, dict) or type(result.get("success")) is not bool:
                        raise ValueError()
                    code = result.get("code", "verified" if result["success"] else "probe_failed")
                    message = result.get("message", "验证通过" if result["success"] else "模型验证失败")
                    if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code) or not isinstance(message, str):
                        raise ValueError()
                    return {"success": result["success"], "code": code, "message": message[:200]}
                except (ValueError, UnicodeDecodeError):
                    return self._error("credential_unavailable" if reference else "probe_failed", "无法完成模型验证；请检查配置和保险库引用")
        except (OSError, subprocess.SubprocessError):
            return self._error("probe_failed", "无法启动或完成模型验证")
        finally:
            selector.close()
            if control_read >= 0:
                os.close(control_read)
            # 先关闭控制端，守卫才会立即终止仍在读取或联网的子进程。
            if control_write >= 0:
                os.close(control_write)
                control_write = -1
            if process is not None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                if process.stdout:
                    process.stdout.close()

    @staticmethod
    def _error(code: str, message: str) -> dict:
        return {"success": False, "code": code, "message": message}
