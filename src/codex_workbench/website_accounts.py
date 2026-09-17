"""网站身份的本机登记簿；记录可见核对结果，不保存或接管网页登录态。"""
from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class WebsiteAccounts:
    """GitHub 身份与 Codex 执行账户分开保存，避免误用为任务执行身份。"""

    def __init__(self, path: Path):
        self.path = path
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS website_accounts (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL CHECK(provider='github'),
                username TEXT NOT NULL COLLATE NOCASE UNIQUE, display_name TEXT NOT NULL,
                checked_at TEXT, observed_in TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _username(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", value) or "--" in value:
            raise ValueError("请填写有效的 GitHub 用户名")
        normalized = value.lower()
        if normalized in {"login", "logout", "settings", "sessions", "account", "accounts", "join", "new", "organizations", "orgs", "enterprises", "apps", "notifications", "issues", "pulls", "search", "marketplace", "explore", "copilot", "site"}:
            raise ValueError("该名称是 GitHub 的系统页面，请填写个人账户用户名")
        return normalized

    @staticmethod
    def _name(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 160:
            raise ValueError("账户名称须为 1 至 160 个字符")
        return value.strip()

    @staticmethod
    def _view(row) -> dict:
        value = dict(row)
        return {**value, "kind": "website", "service": "GitHub",
                "profile_url": "https://github.com/" + value["username"],
                "verification_status": "browser_confirmed" if value["checked_at"] else "unverified"}

    def list(self) -> list[dict]:
        """返回本机身份与最近核对时间，不声称当前浏览器持续保持登录。"""
        with self._connect() as db:
            return [self._view(row) for row in db.execute("SELECT * FROM website_accounts ORDER BY created_at,id")]

    def register(self, username: str, display_name: str | None = None) -> dict:
        """登记可编辑的账户名；仅手工填写用户名不会被标成已登录。"""
        username = self._username(username)
        name = self._name(display_name) if display_name else username
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            previous = db.execute("SELECT * FROM website_accounts WHERE username=?", (username,)).fetchone()
            if previous:
                return self._view(previous)
            identity = str(uuid.uuid4())
            db.execute("INSERT INTO website_accounts VALUES (?,?,?,?,NULL,NULL,?,?)",
                       (identity, "github", username, name, now, now))
            return self._view(db.execute("SELECT * FROM website_accounts WHERE id=?", (identity,)).fetchone())

    def record_observation(self, username: str, observed_profile_url: str, observed_in: str,
                           display_name: str | None = None) -> dict:
        """代理实际查看官方用户菜单后登记观察；只记录来源和时间，不接收 Cookie。"""
        username = self._username(username)
        if not isinstance(observed_profile_url, str) or observed_profile_url.rstrip("/").lower() != "https://github.com/" + username or observed_in not in {"chrome", "codex-browser"}:
            raise ValueError("核对来源必须匹配 GitHub 官方账户主页和可见浏览器")
        account = self.register(username, display_name)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("UPDATE website_accounts SET checked_at=?, observed_in=?, updated_at=? WHERE id=?",
                       (now, observed_in, now, account["id"]))
            return self._view(db.execute("SELECT * FROM website_accounts WHERE id=?", (account["id"],)).fetchone())

    def rename(self, identity: str, display_name: str) -> dict:
        """只修改本机显示名，用户名及官方登录状态保持其各自权威。"""
        name = self._name(display_name)
        with self._connect() as db:
            result = db.execute("UPDATE website_accounts SET display_name=?,updated_at=? WHERE id=?",
                                (name, datetime.now(timezone.utc).isoformat(), identity))
            if result.rowcount != 1:
                raise ValueError("网站账户不存在")
            return self._view(db.execute("SELECT * FROM website_accounts WHERE id=?", (identity,)).fetchone())
