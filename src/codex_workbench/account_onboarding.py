"""工作台新增 Codex 账户的最小登记存储。

该模块只维护 ``execution_accounts`` 和默认账户偏好需要的表，不调用
``Store`` 的全库初始化或迁移。认证材料始终由官方 Codex 登录流程管理。
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from .account_runtime import validate_account_home
from .store import StoreError


class AccountRegistry:
    """仅实现 :class:`AccountService` 所需的账户登记接口。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.pending: dict[str, dict] = {}
        self._ensure_schema()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect("file:" + quote(str(self.path.resolve()), safe="/") + "?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute(
                """CREATE TABLE IF NOT EXISTS execution_accounts (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 160),
                    codex_home TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'isolated', subject_id TEXT,
                    display_name TEXT, expected_email TEXT,
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), created_at TEXT NOT NULL
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(execution_accounts)")}
            for name, declaration in (("kind", "TEXT NOT NULL DEFAULT 'isolated'"), ("subject_id", "TEXT"),
                                      ("display_name", "TEXT"), ("expected_email", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE execution_accounts ADD COLUMN {name} {declaration}")
            db.execute(
                """CREATE TABLE IF NOT EXISTS preferences (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    default_execution_account_id TEXT REFERENCES execution_accounts(id)
                )"""
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _text(value: object, label: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise StoreError("validation", f"{label}无效")
        return value.strip()

    def _account(self, db: sqlite3.Connection, account_id: str) -> dict:
        row = db.execute("SELECT * FROM execution_accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise StoreError("not_found", "执行账户不存在")
        return dict(row)

    def _default_id(self, db: sqlite3.Connection) -> str | None:
        row = db.execute("SELECT default_execution_account_id FROM preferences WHERE singleton=1").fetchone()
        return row[0] if row else None

    def _view(self, db: sqlite3.Connection, account_id: str) -> dict:
        account = self._account(db, account_id)
        account["name"] = account.get("display_name") or account["name"]
        account["isDefault"] = account_id == self._default_id(db)
        return account

    def list_execution_accounts(self) -> list[dict]:
        with self._connection() as db:
            default = self._default_id(db)
            return [{**dict(row), "name": row["display_name"] or row["name"], "isDefault": row["id"] == default}
                    for row in db.execute("SELECT * FROM execution_accounts ORDER BY created_at,id")] + list(self.pending.values())

    def create_execution_account(self, name: str, codex_home: str) -> dict:
        name = self._text(name, "执行账户名称", 160)
        try:
            home = validate_account_home(codex_home)
        except ValueError as error:
            raise StoreError("validation", str(error)) from error
        if any(a["name"] == name or a["codex_home"] == home for a in self.list_execution_accounts()):
            raise StoreError("conflict", "执行账户名称或配置目录已存在")
        account_id = str(uuid.uuid4())
        account = {"id": account_id, "name": name, "codex_home": home, "kind": "isolated",
                   "subject_id": None, "display_name": None, "expected_email": None,
                   "version": 1, "created_at": self._now(), "isDefault": False}
        self.pending[account_id] = account
        return dict(account)

    def record_account_subject(self, account_id: str, subject: str) -> dict:
        subject = self._text(subject, "账户主体", 255)
        with self._connection() as db:
            pending = self.pending.get(account_id)
            if pending is not None:
                db.execute("INSERT INTO execution_accounts(id,name,codex_home,kind,version,created_at,subject_id) VALUES(?,?,?,'isolated',1,?,?)",
                           (account_id, pending["name"], pending["codex_home"], pending["created_at"], subject))
            self._account(db, account_id)
            db.execute("UPDATE execution_accounts SET subject_id=?,version=version+1 WHERE id=? AND (subject_id IS NULL OR subject_id<>?)",
                       (subject, account_id, subject))
            result = self._view(db, account_id)
        self.pending.pop(account_id, None)
        return result

    def list_tasks(self) -> list[dict]:
        """仅用于避免运行中账户被重新登录；旧库没有 tasks 时视为空。"""
        with self._connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
            if not exists:
                return []
            return [dict(row) for row in db.execute("SELECT state,execution_account_id FROM tasks WHERE state='running'")]
