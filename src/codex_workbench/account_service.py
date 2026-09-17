"""独立执行账户的建立、官方登录与额度概览；登录材料由 Codex 管理。"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .accounts import normalize_account
from .codex_rpc import AccountRpc, RpcError
from .account_runtime import current_account_home
from .store import Store, StoreError


class AccountService:
    """只管理工作台自己的执行账户，不改变桌面默认账户。"""

    def __init__(self, store: Store, root: Path, codex: str, shared_config: Path, lease_fd: int | None = None):
        self.store, self.root, self.codex = store, root, codex
        self.shared_config, self.lease_fd = shared_config, lease_fd
        self.logins: dict[str, dict] = {}
        self.cache: dict[str, dict] = {}
        self.current_cache: dict | None = None
        self.model_cache: dict | None = None

    @staticmethod
    def _config_defaults(config: dict, models: list[dict] | None = None) -> dict:
        """配置优先于目录默认模型；缺省推理等级只取已选模型，未知时保留空值。"""
        models = models or []
        model = config.get("model")
        if model:
            selected = next((item for item in models if item.get("model") == model), None)
            selected = selected or next((item for item in models if item.get("id") == model), {})
        else:
            selected = next((item for item in models if item.get("isDefault") is True and item.get("model")), {})
            model = selected.get("model")
        agents = config.get("agents") or {}
        concurrent = agents.get("max_concurrent_threads_per_session", agents.get("max_threads"))
        sandbox = config.get("sandbox_mode")
        return {"model": model,
                "effort": config.get("model_reasoning_effort") or selected.get("defaultReasoningEffort"),
                "concurrency": concurrent if type(concurrent) is int and 1 <= concurrent <= 32 else 1,
                "sandbox": sandbox if sandbox in {"read-only", "workspace-write", "danger-full-access"} else "read-only",
                "approval_policy": config.get("approval_policy") or "never",
                "auth_store": config.get("cli_auth_credentials_store")}

    @staticmethod
    def _read_models(rpc: AccountRpc) -> list[dict]:
        """只保留官方可见模型的选择与推理元数据，不猜测模型名称。"""
        response = rpc.request("model/list", {"includeHidden": False}) or {}
        return [{key: item.get(key) for key in ("id", "model", "displayName", "supportedReasoningEfforts", "defaultReasoningEffort", "isDefault")}
                for item in response.get("data", []) if isinstance(item, dict)]

    def _read_defaults(self, rpc: AccountRpc, models: list[dict] | None = None) -> dict:
        """每次读取新鲜配置；只有缺省模型或推理等级时才补读模型目录。"""
        config = (rpc.request("config/read", {"includeLayers": False}) or {}).get("config", {})
        if models is None and (not config.get("model") or not config.get("model_reasoning_effort")):
            models = self._read_models(rpc)
        return self._config_defaults(config, models)

    def system_defaults(self) -> dict:
        """独立读取新会话默认值，不查询登录或额度；失败不复用旧账户投影。"""
        # 此入口没有验证账户主体，不能沿用此前登录状态或主体相关模型缓存。
        self.current_cache = None
        self.model_cache = None
        with AccountRpc(self.codex, current_account_home(), self.lease_fd, current=True) as rpc:
            return self._read_defaults(rpc)

    @staticmethod
    def _username(identity: dict) -> str | None:
        """仅展示官方实际返回的用户名，不由邮箱拼接出不存在的身份字段。"""
        for key in ("username", "displayName", "name"):
            value = identity.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:160]
        return None

    def current(self, refresh: bool = False) -> dict:
        """只读显示当前登录；显式刷新重读配置和模型，身份变化或读取失败使旧缓存失效。"""
        if not refresh and self.current_cache and time.monotonic() - self.current_cache["time"] < 45:
            return self.current_cache["value"]
        # 新鲜读取失败后不允许下一次普通查询复用旧的已登录投影。
        self.current_cache = None
        if refresh:
            self.model_cache = None
        placeholder = {"account": {"id": "current", "name": "当前 Codex 账户", "kind": "current", "isCurrent": True},
                       "usage": None, "login": {"status": "unavailable"}, "models": [],
                       "taskDefaults": {"model": None, "effort": None, "concurrency": 1, "sandbox": "read-only"}}
        try:
            directory = current_account_home()
            with AccountRpc(self.codex, directory, self.lease_fd, current=True) as rpc:
                identity = (rpc.request("account/read", {"refreshToken": False}) or {}).get("account")
                if not isinstance(identity, dict) or identity.get("type") != "chatgpt":
                    self.model_cache = None
                    placeholder["login"] = {"status": "not_logged_in"}
                    return placeholder
                raw = rpc.request("account/rateLimits/read") or {}
                after = (rpc.request("account/read", {"refreshToken": False}) or {}).get("account")
                subject = raw.get("accountId")
                if identity != after or not isinstance(subject, str) or not subject:
                    raise RpcError("无法确认当前账户身份，请稍后刷新")
                cached_models = self.model_cache
                if (not cached_models or time.monotonic() - cached_models["time"] > 300
                        or cached_models.get("subject") != subject or cached_models.get("identity") != identity
                        or cached_models.get("directory") != directory):
                    self.model_cache = None
                    models = self._read_models(rpc)
                    cached_models = {"time": time.monotonic(), "models": models, "subject": subject,
                                     "identity": dict(identity), "directory": directory}
                models = cached_models["models"]
                defaults = self._read_defaults(rpc, models)
                if identity != (rpc.request("account/read", {"refreshToken": False}) or {}).get("account"):
                    raise RpcError("读取配置时账户身份发生变化，请重新刷新")
            account = self.store.register_current_account("当前 Codex 账户", directory, subject)
            now = datetime.now(UTC)
            usage = normalize_account(raw, observed_at=now, current_account_id=subject, now=now)
            usage["plan"] = identity.get("planType") or usage.get("plan")
            value = {"account": {**account, "isCurrent": True, "email": identity.get("email"), "username": self._username(identity), "username_source": "official" if self._username(identity) else None},
                     "usage": usage, "login": {"status": "ready"}, "identity_id": subject,
                     "models": models, "taskDefaults": defaults}
            self.model_cache = cached_models
            self.current_cache = {"time": time.monotonic(), "value": value}
            return value
        except (RpcError, OSError, ValueError):
            self.model_cache = None
            placeholder["login"]["message"] = "当前账户暂时无法读取，请刷新后重试"
            return placeholder

    def create(self, name: str) -> dict:
        """创建私有目录和非秘密配置引用；不会自动登录或复制认证文件。"""
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise StoreError("validation", "执行账户名称无效")
        name = name.strip()
        if any(a["name"] == name for a in self.store.list_execution_accounts()):
            raise StoreError("conflict", "执行账户名称已存在")
        if self.root.is_symlink():
            raise StoreError("validation", "账户根目录不能是符号链接")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = self.root / str(uuid.uuid4())
        directory.mkdir(mode=0o700)
        (directory / "config.toml").write_text('cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\nmodel_provider = "openai"\n', encoding="utf-8")
        (directory / "config.toml").chmod(0o600)
        for name_in_config in ("AGENTS.md", "instructions", "skills", "agents"):
            source = self.shared_config / name_in_config
            if source.exists():
                (directory / name_in_config).symlink_to(source.resolve(), target_is_directory=source.is_dir())
        return self.store.create_execution_account(name.strip(), str(directory))

    def get(self, account_id: str) -> dict:
        """只按精确 ID 查找工作台登记的账户。"""
        for account in self.store.list_execution_accounts():
            if account["id"] == account_id:
                return account
        raise StoreError("not_found", "执行账户不存在")

    def _has_running(self, account_id: str) -> bool:
        return any(t["state"] == "running" and t["execution_account_id"] == account_id for t in self.store.list_tasks())

    def login(self, account_id: str) -> dict:
        """发起目标账户的官方登录；运行中的同账户不允许重新登录。"""
        if account_id == "current":
            raise StoreError("forbidden", "当前账户由 Codex 管理，请在 Codex 中完成登录")
        account = self.get(account_id)
        if self._has_running(account_id):
            raise StoreError("account_busy", "此账户有任务运行，请等待完成后再登录")
        state = self._login_state(account_id)
        if state.get("status") == "pending":
            return state
        # 登录请求即使失败也可能已改变官方状态，不能继续展示旧的已登录投影。
        self.cache.pop(account_id, None)
        rpc = AccountRpc(self.codex, account["codex_home"], self.lease_fd)
        try:
            response = rpc.request("account/login/start", {"type": "chatgpt"})
            url = response.get("authUrl") if isinstance(response, dict) else None
            parsed = urlsplit(url or "")
            if parsed.scheme != "https" or parsed.username or parsed.password or parsed.hostname not in {"auth.openai.com", "auth0.openai.com", "chatgpt.com"}:
                raise RpcError("官方登录未返回可识别的 HTTPS 页面")
            self.logins[account_id] = {"rpc": rpc, "status": "pending", "url": url,
                                       "login_id": response.get("loginId"), "started": time.monotonic()}
            return {"status": "pending", "url": url}
        except Exception:
            rpc.close()
            raise

    def _login_state(self, account_id: str) -> dict:
        active = self.logins.get(account_id)
        if not active:
            return {"status": "idle"}
        if active["status"] == "pending":
            while True:
                try:
                    event = active["rpc"].events.get_nowait()
                except queue.Empty:
                    break
                if event["method"] == "account/login/completed":
                    active["status"] = "completed" if event.get("params", {}).get("success") else "failed"
            if time.monotonic() - active["started"] > 600:
                active["status"] = "expired"
            if active["status"] != "pending":
                active["rpc"].close()
                active.pop("url", None)
        return {"status": active["status"], **({"url": active["url"]} if active.get("url") else {})}

    def status(self, account_id: str, refresh: bool = True) -> dict:
        """读取同一独立登录目录下的身份和额度；不保存原始响应或令牌。"""
        if account_id == "current":
            return self.current(refresh=refresh)
        account = self.get(account_id)
        login = self._login_state(account_id)
        cached = self.cache.get(account_id)
        if login["status"] == "pending":
            return {"account": account, "usage": None, "login": login}
        if not refresh and cached and time.monotonic() - cached["time"] < 90:
            return cached["value"]
        # 独立账户与当前账户一致：刷新失败不能复活旧的已登录状态。
        self.cache.pop(account_id, None)
        with AccountRpc(self.codex, account["codex_home"], self.lease_fd) as rpc:
            identity = rpc.request("account/read", {"refreshToken": False}) or {}
            identity = identity.get("account")
            if not isinstance(identity, dict) or identity.get("type") != "chatgpt":
                value = {"account": account, "usage": None, "login": {"status": "not_logged_in"}}
            elif account.get("expected_email") and str(identity.get("email") or "").lower() != account["expected_email"].lower():
                value = {"account": account, "usage": None, "login": {"status": "identity_mismatch"}}
            else:
                raw = rpc.request("account/rateLimits/read") or {}
                after = (rpc.request("account/read", {"refreshToken": False}) or {}).get("account")
                if identity != after:
                    raise RpcError("读取时账户身份发生变化，请重新刷新")
                subject = raw.get("accountId")
                if not isinstance(subject, str) or not subject:
                    raise RpcError("无法确认执行账户主体")
                account = self.store.record_account_subject(account_id, subject)
                raw = {**raw, "accountId": account_id}
                now = datetime.now(UTC)
                usage = normalize_account(raw, observed_at=now, current_account_id=account_id, now=now)
                usage["plan"] = identity.get("planType") or usage["plan"]
                value = {"account": {**account, "email": identity.get("email"), "username": self._username(identity), "username_source": "official" if self._username(identity) else None}, "usage": usage,
                         "login": {"status": "ready"}, "identity_id": subject}
        self.cache[account_id] = {"time": time.monotonic(), "value": value}
        return value

    def require_ready(self, account_id: str, expected_subject: str | None = None) -> None:
        """派发前确认账户已登录；不在失败时回退到桌面账户。"""
        value = self.status(account_id, refresh=True)
        if value["login"]["status"] != "ready":
            raise StoreError("login_required", "请先登录任务绑定的执行账户")
        if expected_subject is not None and value.get("identity_id") != expected_subject:
            raise StoreError("account_changed", "当前登录已改变；请为此任务重新选择账户后再执行")

    def close(self) -> None:
        """停止本工作台拥有的登录会话。"""
        for active in self.logins.values():
            active["rpc"].close()
