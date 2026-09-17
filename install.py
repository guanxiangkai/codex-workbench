#!/usr/bin/env python3
"""通过官方 CLI 注册本地 MCP，并只更新本工具的配置项。"""

from __future__ import annotations

import hashlib
import stat
from contextlib import contextmanager
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import shutil
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt


def _default_lock_directory() -> Path:
    """返回当前平台的本机安装锁目录。"""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "CodexWorkbench" / "install-locks"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/CodexWorkbench/install-locks"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "codex-workbench" / "install-locks"


LOCK_DIRECTORY = _default_lock_directory()

NAME = "codex-workbench"
# 入口进程会长期驻留；连接参数随协议代码变化，触发宿主重建而不是复用旧 Python 模块。
_ENTRY_ROOT=Path(__file__).resolve().parent
ENTRY_REVISION=hashlib.sha256(b''.join((_ENTRY_ROOT/'src/codex_workbench'/name).read_bytes() for name in ('mcp.py','__main__.py'))).hexdigest()[:16]
SERVERS = {
    NAME: ("mcp", "--entry-revision", ENTRY_REVISION),
}
REMOVED_SERVERS = ("codex-workbench-assistants", "codex-workbench-accounts")
PUBLIC_PROFILE = {"enabled": "true", "required": "false", "startup_timeout_sec": "30", "tool_timeout_sec": "90"}
PRIVATE_PROFILE = {"enabled": "false", "required": "false"}


@contextmanager
def _install_lock():
    """本工具所有配置写入共用本机旁路锁；不承诺约束外部编辑器。"""
    LOCK_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = LOCK_DIRECTORY.lstat()
    if not stat.S_ISDIR(info.st_mode) or (os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022)):
        raise RuntimeError("安装锁目录不安全，未修改配置")
    descriptor = os.open(LOCK_DIRECTORY / "configuration.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_nlink != 1)):
            raise RuntimeError("安装锁文件不安全，未修改配置")
        if fcntl:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:
            if info.st_size == 0:
                os.write(descriptor, b"\\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        yield
    finally:
        os.close(descriptor)
    # 不删除旁路锁：删除会让等待者和新调用锁住不同 inode。


def update_section(path: Path, values: dict[str, str], name: str = NAME) -> None:
    """序列化本工具写入；保留链接并拒绝最终读回时已观察到的外部冲突。"""
    with _install_lock():
        _update_section_unlocked(path, values, name)


def _update_section_unlocked(path: Path, values: dict[str, str], name: str = NAME) -> None:
    """保留原文件及其链接，只原子更新本 MCP 段落。"""
    actual = path.resolve()
    previous = actual.read_bytes() if actual.exists() else b""
    source = previous.decode("utf-8")
    quoted_root = r'''(?:mcp_servers|"mcp_servers"|'mcp_servers')'''
    quoted_name = rf'''(?:{re.escape(name)}|"{re.escape(name)}"|'{re.escape(name)}')'''
    match = re.search(r'(?m)^[ \t]*\[[ \t]*' + quoted_root + r'[ \t]*\.[ \t]*' + quoted_name + r'[ \t]*\][ \t]*(?:#[^\n]*)?$', source)
    if match:
        tail = re.search(r'(?m)^[ \t]*\[', source[match.end():])
        end = match.end() + tail.start() if tail else len(source)
        block = source[match.end():end]
        for key, value in values.items():
            quoted_key = f'''(?:{re.escape(key)}|"{re.escape(key)}"|'{re.escape(key)}')'''
            pattern = r'(?m)^([ \t]*)' + quoted_key + r'[ \t]*=.*$'
            if re.search(pattern, block):
                block = re.sub(pattern, lambda found: found[1] + key + " = " + value, block)
            else:
                block = "\n" + key + " = " + value + block
        replacement = source[:match.end()] + block + source[end:]
    else:
        replacement = source.rstrip() + f'\n\n[mcp_servers."{name}"]\n' + '\n'.join(k + " = " + v for k, v in values.items()) + '\n'
    before, after = tomllib.loads(source), tomllib.loads(replacement)
    for parsed in (before, after):
        own = parsed.get("mcp_servers", {}).get(name, {})
        for key in values:
            own.pop(key, None)
        if not own:
            parsed.get("mcp_servers", {}).pop(name, None)
        if not parsed.get("mcp_servers"):
            parsed.pop("mcp_servers", None)
    if before != after:
        raise RuntimeError("配置存在不支持的写法，未修改无关字段")
    _replace_checked(actual, previous, replacement)


def _replace_checked(actual: Path, previous: bytes, replacement: str) -> None:
    """原子替换已校验内容；在写入前拒绝可观察到的并发修改。"""
    actual.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".workbench-config-", dir=actual.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(replacement.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        current = actual.read_bytes() if actual.exists() else b""
        if hashlib.sha256(current).digest() != hashlib.sha256(previous).digest():
            raise RuntimeError("配置已被其他进程修改，未覆盖；可重新运行注册")
        os.chmod(temp, 0o600)
        os.replace(temp, actual)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _remove_section_unlocked(path: Path, name: str) -> None:
    """只删除指定 MCP 段落，保留同一配置文件的其他字段。"""
    actual = path.resolve()
    if not actual.exists():
        return
    previous = actual.read_bytes()
    source = previous.decode("utf-8")
    quoted_root = r'''(?:mcp_servers|"mcp_servers"|'mcp_servers')'''
    quoted_name = rf'''(?:{re.escape(name)}|"{re.escape(name)}"|'{re.escape(name)}')'''
    match = re.search(r'(?m)^[ \t]*\[[ \t]*' + quoted_root + r'[ \t]*\.[ \t]*' + quoted_name + r'[ \t]*\][ \t]*(?:#[^\n]*)?$', source)
    if not match:
        return
    tail = re.search(r'(?m)^[ \t]*\[', source[match.end():])
    end = match.end() + tail.start() if tail else len(source)
    replacement = source[:match.start()] + source[end:]
    before, after = tomllib.loads(source), tomllib.loads(replacement)
    expected = before.get("mcp_servers", {}).pop(name, None)
    if not before.get("mcp_servers"):
        before.pop("mcp_servers", None)
    if expected is None or before != after:
        raise RuntimeError("配置存在不支持的写法，未修改无关字段")
    _replace_checked(actual, previous, replacement)




def _register() -> None:
    """核验来源后注册唯一工作台入口，并移除同源旧辅助入口。"""
    root = Path(__file__).resolve().parent
    codex = _codex_executable()
    entry = str(root / "mcp.command")
    registrations: dict[str, bool] = {}
    matches: dict[str, bool] = {}
    for name, suffix in {**SERVERS, **{legacy: () for legacy in REMOVED_SERVERS}}.items():
        check = subprocess.run([codex, "mcp", "get", name, "--json"], capture_output=True, text=True)
        if check.returncode == 0:
            existing = json.loads(check.stdout)
            transport = existing.get("transport", {})
            args = transport.get("args")
            is_own_source = transport.get("command") == "/bin/sh" and isinstance(args, list) and args[:1] == [entry]
            if not is_own_source:
                raise RuntimeError(f"已有不同来源的同名 MCP：{name}，未覆盖")
            registrations[name] = True
            matches[name] = args == [entry, *suffix]
        else:
            registrations[name] = False
            matches[name] = False

    config_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    private = config_root / "private.config.toml"

    for name in REMOVED_SERVERS:
        if registrations[name]:
            subprocess.run([codex, "mcp", "remove", name], check=True, capture_output=True, text=True)
    for name, suffix in SERVERS.items():
        if matches[name]:
            continue
        if registrations[name]:
            _update_section_unlocked(config_root / "config.toml", {"args":json.dumps([entry,*suffix],ensure_ascii=False)}, name)
        else:
            subprocess.run([codex, "mcp", "add", name, "--", "/bin/sh", entry, *suffix], check=True, capture_output=True, text=True)
    for name in SERVERS:
        _update_section_unlocked(config_root / "config.toml", PUBLIC_PROFILE, name)
    if private.exists():
        for name in SERVERS:
            _update_section_unlocked(private, PRIVATE_PROFILE, name)
        for name in REMOVED_SERVERS:
            _remove_section_unlocked(private, name)
    results = {}
    for name in SERVERS:
        verified = subprocess.run([codex, "mcp", "get", name, "--json"], check=True, capture_output=True, text=True)
        results[name] = json.loads(verified.stdout).get("enabled")
    print(json.dumps({"names": list(SERVERS), "enabled": results, "registered": True, "source": str(root),
                      "privateProfileDisabled": private.exists()}, ensure_ascii=False))


def _codex_executable() -> str:
    """按显式配置、官方 macOS 应用路径和 PATH 查找 Codex CLI。"""
    configured = os.environ.get("WORKBENCH_CODEX_PATH")
    if configured:
        return configured
    for candidate in (
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which("codex")
    if found:
        return found
    raise RuntimeError("找不到 Codex CLI；请设置 WORKBENCH_CODEX_PATH 或将 codex 加入 PATH")


def main() -> None:
    """从注册检查到公共/私有配置写入保持同一锁，避免本工具多实例丢更新。"""
    with _install_lock():
        _register()


if __name__ == "__main__":
    main()
