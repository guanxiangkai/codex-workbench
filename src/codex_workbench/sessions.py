"""业务会话与 Codex 原生线程引用的独立持久化边界。"""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


_SANDBOXES = frozenset({"read-only", "workspace-write", "danger-full-access"})
_SOURCES = frozenset({"workbench", "codex"})
_NATIVE_TITLE_SYNC = frozenset({"unbound", "unknown", "synced", "conflict"})


class SessionError(ValueError):
    """会话登记的稳定可处理错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SessionRegistry:
    """保存业务会话的固定项目、账户和执行策略，原生线程只是受控引用。"""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(db_path)
        if not Path(self.path).expanduser().resolve().parent.is_dir():
            raise SessionError("validation", "数据库父目录不存在")
        self._migrate_optional_account()
        self._initialize()

    def create(self, project_id: str, section_id: str | None, cwd: str, execution_account_id: str,
               account_subject: str, sandbox: str, concurrency: int, approval_policy: str,
               title: str = "", description: str = "") -> dict[str, Any]:
        """创建工作台业务会话，并固定其项目、账户主体及执行策略快照。"""
        data = self._workbench_fields(project_id, section_id, cwd, execution_account_id, account_subject,
                                      sandbox, concurrency, approval_policy, title, description)
        session_id, now = str(uuid.uuid4()), self._now()
        with self._connection(immediate=True) as conn:
            conn.execute("""INSERT INTO conversation_sessions(
                id,title,description,project_id,section_id,cwd,execution_account_id,account_subject,native_thread_id,
                source,sandbox,concurrency,approval_policy,native_title_sync,version,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,NULL,'workbench',?,?,?,'unbound',1,?,?)""",
                         (session_id, data["title"], data["description"], data["project_id"], data["section_id"], data["cwd"],
                          data["execution_account_id"], data["account_subject"], data["sandbox"], data["concurrency"],
                          data["approval_policy"], now, now))
        return self.get(session_id)

    def create_draft(self, project_id: str, section_id: str | None, cwd: str, title: str, description: str = "") -> dict:
        """新建会话只保存归属和描述；执行偏好由第一次任务配置回写。"""
        self._text(title, "会话标题", 300)
        self._text(description, "会话说明", 2000, allow_empty=True)
        if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise SessionError("validation", "会话目录不可用")
        identity, now = str(uuid.uuid4()), self._now()
        with self._connection(immediate=True) as conn:
            conn.execute("""INSERT INTO conversation_sessions(id,title,description,project_id,section_id,cwd,source,version,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,'workbench',1,?,?)""", (identity,title.strip(),description.strip(),project_id,section_id,os.path.realpath(cwd),now,now))
        return self.get(identity)

    def configure_defaults(self, session_id: str, *, model: str | None, effort: str | None, account_id: str,
                           account_subject: str, connection: sqlite3.Connection | None = None) -> None:
        """在任务保存事务内回写三项默认值；不改变已有执行线程的账户归属。"""
        if model is not None and (not isinstance(model,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}",model)):
            raise SessionError("validation", "模型标识无效")
        if effort not in {None,"none","minimal","low","medium","high","xhigh","max","ultra"}:
            raise SessionError("validation", "推理等级无效")
        self._text(account_id,"账户标识",128);self._text(account_subject,"账户主体",255)
        def apply(conn):
            row=self._require(conn,session_id)
            if row["native_thread_id"] and (row["thread_account_id"] != account_id or row["thread_account_subject"] != account_subject):
                raise SessionError("session_account_history", "此会话已有其他账户的执行历史，请为新账户新建会话")
            conn.execute("UPDATE conversation_sessions SET model=?,effort=?,execution_account_id=?,account_subject=?,version=version+1,updated_at=? WHERE id=?",
                         (model,effort,account_id,account_subject,self._now(),session_id))
        if connection is not None:apply(connection)
        else:
            with self._connection(immediate=True) as conn:apply(conn)

    def ensure_native(self, native_thread_id: str, project_id: str, section_id: str | None, cwd: str,
                      execution_account_id: str, account_subject: str, title: str = "", description: str = "") -> dict[str, Any]:
        """幂等登记原生 Codex 线程，不臆测其历史 sandbox、并发或审批策略。"""
        thread_id = self._uuid(native_thread_id, "原生会话标识")
        data = self._identity_fields(project_id, section_id, cwd, execution_account_id, account_subject, title, description)
        session_id, now = "native:" + thread_id, self._now()
        with self._connection(immediate=True) as conn:
            row = conn.execute("SELECT * FROM conversation_sessions WHERE native_thread_id=?", (thread_id,)).fetchone()
            if row is None:
                conn.execute("""INSERT INTO conversation_sessions(
                    id,title,description,project_id,section_id,cwd,execution_account_id,account_subject,native_thread_id,
                    source,sandbox,concurrency,approval_policy,native_title_sync,version,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'codex',NULL,NULL,NULL,'unknown',1,?,?)""",
                             (session_id, data["title"], data["description"], data["project_id"], data["section_id"], data["cwd"],
                              data["execution_account_id"], data["account_subject"], thread_id, now, now))
            elif (row["project_id"] != data["project_id"] or row["cwd"] != data["cwd"]
                  or row["execution_account_id"] != data["execution_account_id"] or row["account_subject"] != data["account_subject"]):
                raise SessionError("conflict", "原生会话已绑定其他项目或账户主体")
            else:
                session_id = row["id"]
                if row["section_id"] != data["section_id"]:
                    conn.execute("UPDATE conversation_sessions SET section_id=?,version=version+1,updated_at=? WHERE id=?",
                                 (data["section_id"], now, session_id))
            conn.execute("UPDATE conversation_sessions SET thread_account_id=execution_account_id,thread_account_subject=account_subject WHERE id=? AND thread_account_id IS NULL",(session_id,))
        return self.get(session_id)

    def get(self, session_id: str) -> dict[str, Any]:
        """读取业务会话及可选任务聚合，不读取 Codex 原生会话正文。"""
        self._session_id(session_id)
        with self._connection() as conn:
            return self._with_counts(conn, self._require(conn, session_id))

    def list(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """按最近修改时间列出会话；指定项目时只返回该项目会话。"""
        if project_id is not None:
            self._text(project_id, "项目标识", 128)
        with self._connection() as conn:
            query = "SELECT * FROM conversation_sessions" + (" WHERE project_id=?" if project_id is not None else "") + " ORDER BY updated_at DESC,id"
            rows = conn.execute(query, (() if project_id is None else (project_id,))).fetchall()
            return [self._with_counts(conn, row) for row in rows]

    def update_title(self, session_id: str, version: int, *, title: str | None = None,
                     description: str | None = None) -> dict[str, Any]:
        """乐观更新用户可编辑的标题或说明，不改变固定执行边界。"""
        self._session_id(session_id)
        self._version(version)
        if title is None and description is None:
            raise SessionError("validation", "至少更新标题或说明")
        if title is not None:
            self._text(title, "会话标题", 300, allow_empty=True)
        if description is not None:
            self._text(description, "会话说明", 2_000, allow_empty=True)
        with self._connection(immediate=True) as conn:
            current = self._require(conn, session_id)
            if current["version"] != version:
                raise SessionError("version_conflict", "会话已被其他操作更新")
            changes = {"title": title, "description": description}
            assignments = [f"{key}=?" for key, value in changes.items() if value is not None]
            values = [value for value in changes.values() if value is not None]
            values.extend([self._now(), session_id, version])
            cursor = conn.execute(f"UPDATE conversation_sessions SET {','.join(assignments)},version=version+1,updated_at=? WHERE id=? AND version=?", values)
            if cursor.rowcount != 1:
                raise SessionError("version_conflict", "会话已被其他操作更新")
        return self.get(session_id)

    def sync_native_title(self, session_id: str, version: int, requested_title: str,
                          observed_title: str | None) -> dict[str, Any]:
        """按官方读回值同步已绑定线程标题；不相等时保留原生值并明确标为冲突。"""
        self._session_id(session_id)
        self._version(version)
        self._text(requested_title, "会话标题", 300, allow_empty=True)
        if observed_title is not None:
            self._text(observed_title, "原生会话标题", 300, allow_empty=True)
        native_title = observed_title or ""
        with self._connection(immediate=True) as conn:
            current = self._require(conn, session_id)
            if current["native_thread_id"] is None:
                raise SessionError("validation", "会话尚未绑定原生 Codex 会话")
            if current["version"] != version:
                raise SessionError("version_conflict", "会话已被其他操作更新")
            sync = "synced" if native_title == requested_title else "conflict"
            cursor = conn.execute("UPDATE conversation_sessions SET title=?,native_title_sync=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                                  (native_title, sync, self._now(), session_id, version))
            if cursor.rowcount != 1:
                raise SessionError("version_conflict", "会话已被其他操作更新")
        return self.get(session_id)

    def observe_native_title(self, session_id: str, observed_title: str | None) -> dict[str, Any]:
        """按后续官方读取刷新原生权威标题，并标记本地待处理改名是否已收敛。"""
        self._session_id(session_id)
        if observed_title is not None:
            self._text(observed_title, "原生会话标题", 300, allow_empty=True)
        native_title = observed_title or ""
        with self._connection(immediate=True) as conn:
            current = self._require(conn, session_id)
            if current["native_thread_id"] is None:
                raise SessionError("validation", "会话尚未绑定原生 Codex 会话")
            sync = "synced" if current["title"] == native_title else "conflict"
            conn.execute("UPDATE conversation_sessions SET title=?,native_title_sync=?,version=version+1,updated_at=? WHERE id=?",
                         (native_title, sync, self._now(), session_id))
        return self.get(session_id)

    def attach_thread(self, session_id: str, thread_id: str, connection: sqlite3.Connection | None = None, account_id: str | None = None, account_subject: str | None = None) -> dict[str, Any]:
        """首次绑定或重验同一 Codex UUID；会话不得被切换到另一个原生线程。"""
        self._session_id(session_id)
        thread_id = self._uuid(thread_id, "Codex 会话标识")
        if connection is not None:
            if not isinstance(connection, sqlite3.Connection):
                raise SessionError("validation", "事务连接无效")
            self._attach(connection, session_id, thread_id, account_id, account_subject)
            return self._with_counts(connection, self._require(connection, session_id))
        with self._connection(immediate=True) as conn:
            self._attach(conn, session_id, thread_id, account_id, account_subject)
        return self.get(session_id)

    def _attach(self, conn: sqlite3.Connection, session_id: str, thread_id: str, account_id: str | None = None, account_subject: str | None = None) -> None:
        current = self._require(conn, session_id)
        if current["native_thread_id"] is not None and current["native_thread_id"] != thread_id:
            raise SessionError("conflict", "会话已绑定其他 Codex 会话")
        if current["native_thread_id"] is None:
            owner = conn.execute("SELECT id FROM conversation_sessions WHERE native_thread_id=? AND id<>?", (thread_id, session_id)).fetchone()
            if owner is not None:
                raise SessionError("conflict", "Codex 会话已绑定其他业务会话")
            try:
                conn.execute("UPDATE conversation_sessions SET native_thread_id=?,thread_account_id=?,thread_account_subject=?,native_title_sync='unknown',version=version+1,updated_at=? WHERE id=?", (thread_id, account_id or current["execution_account_id"], account_subject or current["account_subject"], self._now(), session_id))
            except sqlite3.IntegrityError:
                raise SessionError("conflict", "Codex 会话已绑定其他业务会话") from None

    def _migrate_optional_account(self) -> None:
        """保留全部会话和外键引用，仅允许新会话尚未选择执行账户。"""
        conn=sqlite3.connect(self.path,timeout=10)
        try:
            row=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='conversation_sessions'").fetchone()
            if not row or not any(r[1]=="execution_account_id" and r[3] for r in conn.execute("PRAGMA table_info(conversation_sessions)")):return
            sql=row[0].replace("execution_account_id TEXT NOT NULL","execution_account_id TEXT").replace("account_subject TEXT NOT NULL","account_subject TEXT")
            indexes=[r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name='conversation_sessions' AND type IN ('index','trigger') AND sql IS NOT NULL")]
            conn.execute("PRAGMA foreign_keys=OFF");conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql.replace("conversation_sessions","conversation_sessions_nullable",1))
            conn.execute("INSERT INTO conversation_sessions_nullable SELECT * FROM conversation_sessions")
            conn.execute("DROP TABLE conversation_sessions");conn.execute("ALTER TABLE conversation_sessions_nullable RENAME TO conversation_sessions")
            for statement in indexes:conn.execute(statement)
            if conn.execute("PRAGMA foreign_key_check").fetchall():raise SessionError("migration_failed","会话迁移外键校验未通过")
            conn.commit()
        except BaseException:conn.rollback();raise
        finally:conn.close()

    def _initialize(self) -> None:
        with self._connection(immediate=True) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS conversation_sessions(
                id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, project_id TEXT NOT NULL,
                section_id TEXT, cwd TEXT NOT NULL, execution_account_id TEXT, account_subject TEXT,
                native_thread_id TEXT, source TEXT NOT NULL CHECK(source IN ('workbench','codex')),
                sandbox TEXT CHECK(sandbox IN ('read-only','workspace-write','danger-full-access')),
                concurrency INTEGER CHECK(concurrency BETWEEN 1 AND 32), approval_policy TEXT,
                native_title_sync TEXT NOT NULL DEFAULT 'unbound' CHECK(native_title_sync IN ('unbound','unknown','synced','conflict')),
                version INTEGER NOT NULL CHECK(version>=1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            columns={row[1] for row in conn.execute("PRAGMA table_info(conversation_sessions)")}
            for name in ("model","effort","thread_account_id","thread_account_subject"):
                if name not in columns:conn.execute(f"ALTER TABLE conversation_sessions ADD COLUMN {name} TEXT")
            if "native_title_sync" not in columns:
                conn.execute("ALTER TABLE conversation_sessions ADD COLUMN native_title_sync TEXT NOT NULL DEFAULT 'unbound'")
            conn.execute("UPDATE conversation_sessions SET thread_account_id=execution_account_id,thread_account_subject=account_subject WHERE native_thread_id IS NOT NULL AND thread_account_id IS NULL")
            conn.execute("UPDATE conversation_sessions SET native_title_sync='unknown' WHERE native_thread_id IS NOT NULL AND native_title_sync='unbound'")
            conn.execute("CREATE INDEX IF NOT EXISTS conversation_sessions_project_updated ON conversation_sessions(project_id,updated_at DESC)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS conversation_sessions_native_thread_unique ON conversation_sessions(native_thread_id) WHERE native_thread_id IS NOT NULL")

    def _with_counts(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        columns = {item[1] for item in conn.execute("PRAGMA table_info(tasks)")} if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone() else set()
        if "session_id" not in columns:
            return {**value, "task_count": 0, "running_count": 0}
        counts = conn.execute("SELECT COUNT(*) AS task_count,COALESCE(SUM(CASE WHEN state='running' THEN 1 ELSE 0 END),0) AS running_count FROM tasks WHERE session_id=?", (value["id"],)).fetchone()
        return {**value, "task_count": counts[0], "running_count": counts[1]}

    @contextmanager
    def _connection(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _workbench_fields(self, project_id: Any, section_id: Any, cwd: Any, account_id: Any, subject: Any,
                          sandbox: Any, concurrency: Any, approval: Any, title: Any, description: Any) -> dict[str, Any]:
        data = self._identity_fields(project_id, section_id, cwd, account_id, subject, title, description)
        if sandbox not in _SANDBOXES:
            raise SessionError("validation", "沙箱策略无效")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 32:
            raise SessionError("validation", "会话并发无效")
        self._text(approval, "审批策略", 80)
        return {**data, "sandbox": sandbox, "concurrency": concurrency, "approval_policy": approval}

    def _identity_fields(self, project_id: Any, section_id: Any, cwd: Any, account_id: Any, subject: Any,
                         title: Any, description: Any) -> dict[str, Any]:
        self._text(project_id, "项目标识", 128)
        if section_id is not None:
            self._text(section_id, "分区标识", 128)
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise SessionError("validation", "会话工作目录必须是当前存在的绝对目录")
        self._text(account_id, "执行账户标识", 128)
        self._text(subject, "账户主体", 255)
        self._text(title, "会话标题", 300, allow_empty=True)
        self._text(description, "会话说明", 2_000, allow_empty=True)
        return {"project_id": project_id, "section_id": section_id, "cwd": os.path.realpath(cwd),
                "execution_account_id": account_id, "account_subject": subject, "title": title.strip(), "description": description.strip()}

    @staticmethod
    def _text(value: Any, label: str, maximum: int, allow_empty: bool = False) -> None:
        if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
            raise SessionError("validation", f"{label}无效")

    @staticmethod
    def _uuid(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise SessionError("validation", f"{label}无效")
        try:
            return str(uuid.UUID(value))
        except ValueError:
            raise SessionError("validation", f"{label}无效") from None

    @staticmethod
    def _session_id(value: Any) -> None:
        if not isinstance(value, str):
            raise SessionError("validation", "会话标识无效")
        if value.startswith("native:"):
            SessionRegistry._uuid(value.removeprefix("native:"), "会话标识")
        else:
            SessionRegistry._uuid(value, "会话标识")

    @staticmethod
    def _version(value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SessionError("validation", "版本号无效")

    @staticmethod
    def _require(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM conversation_sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise SessionError("not_found", "会话不存在")
        return row

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="microseconds")
