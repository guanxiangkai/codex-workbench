"""执行账户的本机环境边界；只传递目录引用，不读取或复制凭据。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping


def validate_account_home(value: str) -> str:
    """仅凭路径和元数据核验私有账户目录；认证、配置必须是当前用户的独立普通文件。"""
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("执行账户目录必须是绝对路径")
    path = Path(value)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("执行账户目录必须存在且不能是符号链接")
    resolved = path.resolve()
    if "Mobile Documents" in resolved.parts or "CloudStorage" in resolved.parts:
        raise ValueError("执行账户登录目录必须保存在本机")
    defaults = [Path.home() / ".codex"]
    if os.environ.get("CODEX_HOME"):
        defaults.append(Path(os.environ["CODEX_HOME"]))
    if any(resolved == root.expanduser().resolve() or resolved.is_relative_to(root.expanduser().resolve()) for root in defaults):
        raise ValueError("不得复用或嵌套在桌面默认 Codex 登录目录")
    info = resolved.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("账户目录必须为当前用户所有且权限为 0700")
    for name in ("auth.json", "config.toml"):
        try:
            file_info = (resolved / name).lstat()
        except FileNotFoundError:
            # 未登录的新目录可以没有这些文件；不读取任何认证内容。
            continue
        if not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1 or file_info.st_uid != os.getuid():
            raise ValueError("账户认证和配置文件必须为当前用户所有的独立普通文件，不能使用符号链接或硬链接")
    return str(resolved)


def current_account_home() -> str:
    """引用当前 Codex 的官方本机状态目录，不读取或复制认证材料。"""
    path = (Path.home() / ".codex").resolve()
    if not path.is_dir() or "Mobile Documents" in path.parts or "CloudStorage" in path.parts:
        raise ValueError("当前账户的官方状态目录必须位于本机")
    return str(path)


def account_environment(account_home: str, inherited: Mapping[str, str] | None = None, *, use_current: bool = False) -> dict[str, str]:
    """仅修改子进程环境；清除可覆盖账户身份的环境凭据，避免借用宿主账户。"""
    if use_current:
        directory = current_account_home()
        if str(Path(account_home).resolve()) != directory:
            raise ValueError("当前账户只能使用已配置的官方本机目录")
    else:
        directory = validate_account_home(account_home)
    result = dict(os.environ if inherited is None else inherited)
    for name in ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_FEDERATION_RULE_ID",
                 "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"):
        result.pop(name, None)
    result["CODEX_HOME"] = directory
    return result


def account_options() -> list[str]:
    """官方 CLI 管理独立目录中的认证文件；只允许 ChatGPT 登录模式。"""
    return ["-c", 'cli_auth_credentials_store="file"', "-c", 'forced_login_method="chatgpt"',
            "-c", 'model_provider="openai"']


def login_invocation(command: tuple[str, ...], account_home: str) -> tuple[list[str], dict[str, str]]:
    """生成官方浏览器登录调用；调用方必须在用户选定目标账户后才能启动。"""
    if not command:
        raise ValueError("Codex CLI 命令不能为空")
    return [*command, "login", *account_options()], account_environment(account_home)
