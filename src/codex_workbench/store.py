"""Codex 工作台的 SQLite 业务持久层。"""

from __future__ import annotations

import os
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .account_runtime import current_account_home, validate_account_home
from .appearance import AppearanceError, icon_value, validate_color, validate_icon
from .sessions import SessionRegistry


_MODEL_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,119}")
_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_FINISH_STATES = frozenset({"review", "failed", "cancelled"})
_MAX_EVENTS_PER_RUN = 200


class StoreError(ValueError):
    """工作台持久层的可处理错误，``code`` 用于调用方稳定地分支处理。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Store:
    """以单个 SQLite 文件保存项目、代理、任务、运行和有限事件。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """打开或初始化 ``path`` 指向的数据库文件。"""
        self.path = os.fspath(path)
        parent = Path(self.path).expanduser().resolve().parent
        if not parent.exists() or not parent.is_dir():
            raise StoreError("validation", "数据库父目录不存在")
        self.sessions = SessionRegistry(self.path)
        self._initialize()

    def create_section(self, name: str, color: str | None = None, icon: dict[str, str] | None = None) -> dict[str, Any]:
        """创建工作台自有分区；分区的外观只接受受限 emoji 或 SVG。"""
        self._text(name, "分区名称", 80)
        color, icon = self._appearance(color, icon)
        section_id, now = self._id(), self._now()
        with self._connection() as conn:
            try:
                conn.execute("INSERT INTO sections(id, name, color, icon, created_at) VALUES (?, ?, ?, ?, ?)",
                             (section_id, name, color, icon, now))
            except sqlite3.IntegrityError as exc:
                raise StoreError("conflict", "分区名称已存在") from exc
        return self._section_dict(section_id)

    def list_sections(self) -> list[dict[str, Any]]:
        """按创建时间列出工作台自有分区。"""
        with self._connection() as conn:
            return [self._appearance_dict(dict(row)) for row in conn.execute("SELECT * FROM sections ORDER BY created_at, id")]

    def create_project(
        self, name: str, cwd: str, section_name: str | None = None, *, section_id: str | None = None,
        color: str | None = None, icon: dict[str, str] | None = None, native_id: str | None = None,
        is_workspace: bool = False,
    ) -> dict[str, Any]:
        """创建项目；``cwd`` 必须是当前存在的绝对目录，创建后不可在此接口修改。"""
        self._text(name, "项目名称", 160)
        normalized_cwd = self._cwd(cwd)
        section_name = self._section_name(section_name)
        color, icon = self._appearance(color, icon)
        self._native_id(native_id)
        project_id = self._id()
        now = self._now()
        with self._connection() as conn:
            try:
                self._validate_section_id(conn, section_id)
                conn.execute(
                    """INSERT INTO projects(id, name, cwd, section_name, section_id, color, icon, native_id, is_workspace, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (project_id, name, normalized_cwd, section_name, section_id, color, icon, native_id, int(is_workspace), now),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("conflict", "项目名称或工作目录已存在") from exc
        return self._project_dict(project_id)

    def list_projects(self, include_workspace: bool = False) -> list[dict[str, Any]]:
        """按创建时间返回全部项目。"""
        with self._connection() as conn:
            return [self._appearance_dict(dict(row)) for row in conn.execute("SELECT * FROM projects WHERE is_workspace=0 OR ? ORDER BY created_at, id", (int(include_workspace),))]

    def get_project(self, project_id: str) -> dict[str, Any]:
        """读取项目或默认工作目录的内部记录。"""
        return self._project_dict(project_id)

    def refresh_native_project(self, project_id: str, name: str, cwd: str, section_id: str | None) -> dict[str, Any]:
        """更新已绑定的原生项目执行引用，不修改原生项目或用户工作目录。"""
        self._text(name, "项目名称", 160)
        normalized = self._cwd(cwd)
        with self._connection() as conn:
            project = self._require(conn, "projects", project_id, "项目")
            if not project["native_id"]:
                raise StoreError("forbidden", "只允许刷新原生项目引用")
            conn.execute("UPDATE projects SET name=?,cwd=?,section_id=? WHERE id=?", (name, normalized, section_id, project_id))
        return self._project_dict(project_id)

    def create_agent(self, name: str, instructions: str = "", description: str | None = None, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        """创建专业助手；模型、推理强度和沙箱由每项任务决定。"""
        self._text(name, "代理名称", 160)
        self._text(instructions, "代理指令", 30_000, allow_empty=True)
        if description is not None:
            self._text(description, "代理描述", 2_000, allow_empty=True)
        agent_id = self._id()
        now = self._now()
        with nullcontext(connection) if connection is not None else self._connection() as conn:
            try:
                conn.execute(
                    """INSERT INTO agents
                    (id, name, instructions, description, version, created_at)
                    VALUES (?, ?, ?, ?, 1, ?)""",
                    (agent_id, name, instructions, description, now),
                )
            except sqlite3.IntegrityError as exc:
                raise self._agent_integrity_error(exc) from exc
            return self._agent_projection(dict(self._require(conn, "agents", agent_id, "代理")))

    def list_agents(self) -> list[dict[str, Any]]:
        """按创建时间返回全部代理配置。"""
        with self._connection() as conn:
            return [self._agent_projection(dict(row)) for row in conn.execute("SELECT * FROM agents ORDER BY created_at, id")]

    def create_execution_account(self, name: str, codex_home: str) -> dict[str, Any]:
        """登记新任务使用的独立 Codex 配置目录引用；不会登录、复制或读取凭据。"""
        self._text(name, "执行账户名称", 160)
        normalized_home = self._execution_home(codex_home)
        account_id = self._id()
        with self._connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO execution_accounts(id, name, codex_home, kind, version, created_at) VALUES (?, ?, ?, 'isolated', 1, ?)",
                    (account_id, name, normalized_home, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("conflict", "执行账户名称或配置目录已存在") from exc
        return self._execution_account_dict(account_id)

    def register_current_account(self, name: str, codex_home: str, subject_id: str) -> dict[str, Any]:
        """登记已由上层官方入口核验的当前 Codex 身份，不读取认证材料。"""
        self._text(name, "执行账户名称", 160)
        self._text(subject_id, "当前账户标识", 255)
        try:
            expected_home = os.path.realpath(current_account_home())
        except ValueError as exc:
            raise StoreError("validation", str(exc)) from exc
        if not isinstance(codex_home, str) or not os.path.isabs(codex_home) or os.path.realpath(codex_home) != expected_home:
            raise StoreError("validation", "当前账户目录必须是 Codex 当前账户目录")
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO execution_accounts(id, name, codex_home, kind, subject_id, version, created_at)
                VALUES ('current', ?, ?, 'current', ?, 1, ?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, codex_home=excluded.codex_home,
                    kind='current', subject_id=excluded.subject_id, version=execution_accounts.version+1""",
                (name, expected_home, subject_id, self._now()),
            )
        return self._execution_account_dict("current")

    def list_execution_accounts(self) -> list[dict[str, Any]]:
        """返回执行账户配置引用及当前默认标识，不访问任何账户认证材料。"""
        with self._connection() as conn:
            default_id = self._default_execution_account_id(conn)
            return [
                {**dict(row), "name": row["display_name"] or row["name"], "isDefault": row["id"] == default_id}
                for row in conn.execute("SELECT * FROM execution_accounts ORDER BY created_at, id")
            ]

    def rename_execution_account(self, account_id: str, name: str, expected_email: str | None = None) -> dict[str, Any]:
        """修改显示别名，官方账户刷新不会覆盖别名或改变登录主体。"""
        self._text(name, "账户名称", 160)
        name = name.strip()
        if expected_email is not None:
            if account_id == "current" or not isinstance(expected_email, str) or len(expected_email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", expected_email):
                raise StoreError("validation", "请填写此独立执行账户的目标邮箱")
            expected_email = expected_email.strip().lower()
        with self._connection(immediate=True) as conn:
            self._require(conn, "execution_accounts", account_id, "账户")
            if conn.execute("SELECT 1 FROM execution_accounts WHERE id<>? AND COALESCE(display_name,name)=?", (account_id, name)).fetchone():
                raise StoreError("conflict", "账户名称已存在")
            conn.execute("UPDATE execution_accounts SET display_name=?,version=version+1 WHERE id=?", (name, account_id))
            if expected_email is not None:
                conn.execute("UPDATE execution_accounts SET expected_email=? WHERE id=?", (expected_email, account_id))
        return self._execution_account_dict(account_id)

    def record_account_subject(self, account_id: str, subject: str) -> dict[str, Any]:
        """保存官方已确认的账户主体，用于会话与后续任务的身份一致性检查。"""
        self._text(subject, "账户主体", 255)
        with self._connection(immediate=True) as conn:
            self._require(conn, "execution_accounts", account_id, "账户")
            conn.execute("UPDATE execution_accounts SET subject_id=?,version=version+1 WHERE id=? AND (subject_id IS NULL OR subject_id<>?)", (subject, account_id, subject))
        return self._execution_account_dict(account_id)

    def set_default_execution_account(self, account_id: str) -> dict[str, Any]:
        """持久化新任务的默认执行账户；既有任务和运行快照不会随之改变。"""
        with self._connection(immediate=True) as conn:
            account = self._require(conn, "execution_accounts", account_id, "执行账户")
            conn.execute(
                """INSERT INTO preferences(singleton, default_execution_account_id) VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET default_execution_account_id = excluded.default_execution_account_id""",
                (account_id,),
            )
            return {**dict(account), "isDefault": True}

    def update_agent(self, agent_id: str, version: int, *, connection: sqlite3.Connection | None = None, **fields: Any) -> dict[str, Any]:
        """乐观更新代理配置；变更仅供后续领取生成运行快照，已运行任务不受影响。"""
        allowed = {"name", "instructions", "description"}
        if not fields or set(fields) - allowed:
            raise StoreError("validation", "只能更新 name、instructions 或 description")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise StoreError("validation", "版本号无效")
        if "name" in fields:
            self._text(fields["name"], "代理名称", 160)
        if "instructions" in fields:
            self._text(fields["instructions"], "代理指令", 30_000, allow_empty=True)
        if "description" in fields and fields["description"] is not None:
            self._text(fields["description"], "代理描述", 2_000, allow_empty=True)
        with nullcontext(connection) if connection is not None else self._connection() as conn:
            agent = self._require(conn, "agents", agent_id, "代理")
            if agent["version"] != version:
                raise StoreError("version_conflict", "代理已被其他操作更新")
            assignments = [f"{column} = ?" for column in fields]
            values = [fields[column] for column in fields]
            values.extend([agent_id, version])
            try:
                cursor = conn.execute(
                    f"UPDATE agents SET {', '.join(assignments)}, version = version + 1 WHERE id = ? AND version = ?", values
                )
            except sqlite3.IntegrityError as exc:
                raise self._agent_integrity_error(exc) from exc
            if cursor.rowcount != 1:
                raise StoreError("version_conflict", "代理已被其他操作更新")
            return self._agent_projection(dict(self._require(conn, "agents", agent_id, "代理")))

    @contextmanager
    def agent_transaction(self) -> Iterator[sqlite3.Connection]:
        """助手与模型绑定共用提交边界，失败时整体回滚。"""
        with self._connection(immediate=True) as conn:
            yield conn

    def create_task(
        self,
        project_id: str,
        title: str,
        prompt: str = "",
        agent_id: str | None = None,
        execution_account_id: str | None = None,
        resource_paths: list[str] | None = None,
        section_name: str | None = None,
        section_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        concurrency: int = 1,
        sandbox: str = "read-only",
        session_id: str | None = None,
        sync_session_defaults: bool = False,
    ) -> dict[str, Any]:
        """创建任务并固定当时默认执行账户；不指定代理的人工任务不能被领取执行。"""
        self._text(title, "任务标题", 300)
        self._text(prompt, "任务提示", 100_000, allow_empty=True)
        encoded_resources = self._resource_paths(resource_paths or [])
        section_name = self._section_name(section_name)
        self._execution_settings(model, effort, concurrency, sandbox)
        task_id = self._id()
        now = self._now()
        with self._connection() as conn:
            self._require(conn, "projects", project_id, "项目")
            if session_id is not None:
                session = self._require(conn, "conversation_sessions", session_id, "会话")
                if session["project_id"] != project_id:
                    raise StoreError("validation", "会话不属于所选项目")
            self._validate_section_id(conn, section_id)
            if agent_id is not None:
                self._require(conn, "agents", agent_id, "代理")
            selected_account_id = execution_account_id
            if selected_account_id is None:
                selected_account_id = self._default_execution_account_id(conn)
            else:
                self._require(conn, "execution_accounts", selected_account_id, "执行账户")
            if sync_session_defaults and session_id:
                self.sessions.configure_defaults(session_id,model=model,effort=effort,account_id=selected_account_id,
                                                account_subject=self._account_subject(conn,selected_account_id),connection=conn)
            conn.execute(
                """INSERT INTO tasks(
                    id, project_id, title, prompt, agent_id, execution_account_id, execution_account_subject, resource_paths, section_name, section_id,
                    model, effort, concurrency, sandbox, session_id, state, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'backlog', 1, ?, ?)""",
                (task_id, project_id, title, prompt, agent_id, selected_account_id,
                 self._account_subject(conn, selected_account_id), encoded_resources, section_name, section_id,
                 model, effort, concurrency, sandbox, session_id, now, now),
            )
        return self.get_task(task_id)

    def list_tasks(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """返回任务；指定 ``project_id`` 时仅返回该项目任务。"""
        with self._connection() as conn:
            if project_id is not None:
                self._require(conn, "projects", project_id, "项目")
                rows = conn.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at, id", (project_id,))
            else:
                rows = conn.execute("SELECT * FROM tasks ORDER BY created_at, id")
            return [self._task_dict(row) for row in rows]

    def task_run_summaries(self, task_id: str | None = None) -> dict[str, dict[str, Any]]:
        """批量读取每项任务的最后一次运行摘要，列表不加载长正文和资源快照。"""
        with self._connection() as conn:
            where = "WHERE task_id=?" if task_id else ""
            rows=conn.execute("SELECT id,task_id,state,duration_ms,input_tokens,output_tokens,cached_input_tokens,finished_at,thread_id FROM (SELECT id,task_id,state,duration_ms,input_tokens,output_tokens,cached_input_tokens,finished_at,thread_id,ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY created_at DESC,id DESC) AS position FROM runs "+where+") WHERE position=1",(task_id,) if task_id else ()).fetchall()
            return {r['task_id']:dict(r) for r in rows}

    def get_task(self, task_id: str) -> dict[str, Any]:
        """读取一个任务，不存在时抛出 ``StoreError(code='not_found')``。"""
        with self._connection() as conn:
            row = self._require(conn, "tasks", task_id, "任务")
            return self._task_dict(row)

    def update_task(self, task_id: str, version: int, **fields: Any) -> dict[str, Any]:
        """乐观更新任务；状态转移区分人工任务和必须经运行完成的 AI 任务。"""
        sync_session_defaults = fields.pop("_sync_session_defaults", False)
        allowed = {"title", "prompt", "agent_id", "execution_account_id", "resource_paths", "section_name", "section_id", "model", "effort", "concurrency", "sandbox", "state", "session_id"}
        if not fields or set(fields) - allowed:
            raise StoreError("validation", "任务更新字段无效")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise StoreError("validation", "版本号无效")
        if "title" in fields:
            self._text(fields["title"], "任务标题", 300)
        if "prompt" in fields:
            self._text(fields["prompt"], "任务提示", 100_000, allow_empty=True)
        if "resource_paths" in fields:
            fields["resource_paths"] = self._resource_paths(fields["resource_paths"])
        if "section_name" in fields:
            fields["section_name"] = self._section_name(fields["section_name"])
        if any(key in fields for key in ("model", "effort", "concurrency", "sandbox")):
            self._execution_settings(fields.get("model"), fields.get("effort"), fields.get("concurrency", 1), fields.get("sandbox", "read-only"))
        with self._connection() as conn:
            task = self._require(conn, "tasks", task_id, "任务")
            if task["state"] == "running" and set(fields) != {"title"}:
                raise StoreError("conflict", "运行中的任务不能编辑")
            if task["version"] != version:
                raise StoreError("version_conflict", "任务已被其他操作更新")
            if "state" in fields:
                self._validate_task_transition(task["state"], fields["state"], fields.get("agent_id", task["agent_id"]) is None)
            effective_state = fields.get("state", task["state"])
            if "execution_account_id" in fields and effective_state not in {"backlog", "ready"}:
                raise StoreError("conflict", "修改已执行任务的执行账户时，必须退回待规划或待执行")
            content_changed = any(
                key in fields and fields[key] != task[key]
                for key in ("prompt", "agent_id", "resource_paths", "model", "effort", "concurrency", "sandbox", "session_id")
            )
            if content_changed and task["state"] in {"done", "archived"} and fields.get("state", task["state"]) not in {"backlog", "ready"}:
                raise StoreError("conflict", "修改已执行任务的内容或负责人时，必须退回待规划或待执行")
            if "agent_id" in fields and fields["agent_id"] is not None:
                self._require(conn, "agents", fields["agent_id"], "代理")
            if "section_id" in fields:
                self._validate_section_id(conn, fields["section_id"])
            if fields.get("session_id") is not None:
                session = self._require(conn, "conversation_sessions", fields["session_id"], "会话")
                if session["project_id"] != task["project_id"]:
                    raise StoreError("validation", "会话不属于任务项目")
            if "execution_account_id" in fields and fields["execution_account_id"] is not None:
                self._require(conn, "execution_accounts", fields["execution_account_id"], "执行账户")
                fields["execution_account_subject"] = self._account_subject(conn, fields["execution_account_id"])
            elif "execution_account_id" in fields:
                fields["execution_account_subject"] = None
            if sync_session_defaults:
                session_identity=fields.get("session_id",task["session_id"])
                account_identity=fields.get("execution_account_id",task["execution_account_id"])
                if session_identity:
                    self.sessions.configure_defaults(session_identity,model=fields.get("model",task["model"]),effort=fields.get("effort",task["effort"]),
                                                    account_id=account_identity,account_subject=self._account_subject(conn,account_identity),connection=conn)
            assignments = [f"{column} = ?" for column in fields]
            values = [fields[column] for column in fields]
            assignments.extend(["version = version + 1", "updated_at = ?"])
            values.extend([self._now(), task_id, version])
            cursor = conn.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ? AND version = ?", values
            )
            if cursor.rowcount != 1:
                raise StoreError("version_conflict", "任务已被其他操作更新")
        return self.get_task(task_id)

    def claim(self, task_id: str) -> dict[str, Any]:
        """原子领取 ready 任务，并固定任务执行设置与专业助手规则。"""
        with self._connection(immediate=True) as conn:
            task = self._require(conn, "tasks", task_id, "任务")
            if task["state"] != "ready":
                raise StoreError("conflict", "只有 ready 任务可以领取")
            if task["execution_account_id"] is None:
                raise StoreError("account_required", "请先为任务配置独立执行账户，不会使用桌面账户代替")
            session = None
            if task["session_id"]:
                session = self._require(conn, "conversation_sessions", task["session_id"], "会话")
                if session["project_id"] != task["project_id"] or (session["native_thread_id"] and (session["thread_account_id"] != task["execution_account_id"] or session["thread_account_subject"] != task["execution_account_subject"])):
                    raise StoreError("session_boundary", "任务与会话的项目或账户绑定不一致")
                if conn.execute("SELECT 1 FROM tasks WHERE session_id=? AND state='running'", (session["id"],)).fetchone():
                    raise StoreError("session_busy", "该会话正在执行另一项任务")
            agent = None if task["agent_id"] is None else conn.execute("SELECT * FROM agents WHERE id = ?", (task["agent_id"],)).fetchone()
            agent_id = agent["id"] if agent is not None else None
            agent_name = agent["name"] if agent is not None else "默认助手"
            agent_instructions = agent["instructions"] if agent is not None else ""
            run_id = self._id()
            now = self._now()
            cursor = conn.execute(
                "UPDATE tasks SET state = 'running', version = version + 1, updated_at = ? WHERE id = ? AND state = 'ready'",
                (now, task_id),
            )
            if cursor.rowcount != 1:
                raise StoreError("conflict", "任务已被其他执行者领取")
            project = self._require(conn, "projects", task["project_id"], "项目")
            execution_account = None
            if task["execution_account_id"] is not None:
                execution_account = self._require(conn, "execution_accounts", task["execution_account_id"], "执行账户")
            conn.execute(
                """INSERT INTO runs(
                    id, task_id, project_id, agent_id, state, task_title, task_prompt, project_name, project_cwd,
                    agent_name, agent_instructions, execution_model, execution_effort, execution_concurrency, execution_sandbox,
                    execution_account_id, execution_account_name, execution_account_home, execution_account_kind, execution_account_subject, session_id, created_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, task_id, project["id"], agent_id, task["title"], task["prompt"], project["name"],
                 session["cwd"] if session else project["cwd"], agent_name, agent_instructions, task["model"], task["effort"], task["concurrency"], task["sandbox"],
                 execution_account["id"] if execution_account else None,
                 (execution_account["display_name"] or execution_account["name"]) if execution_account else None,
                 execution_account["codex_home"] if execution_account else None,
                 execution_account["kind"] if execution_account else None, task["execution_account_subject"], task["session_id"], now),
            )
            conn.execute("UPDATE runs SET location_marker=? WHERE id=?",("[工作台执行 "+run_id+"]",run_id))
            run = self._run_dict_in(conn, run_id)
            return {
                "id": run["id"],
                "state": run["state"],
                "session": dict(session) if session is not None else {},
                "task": {
                    "id": run["task_id"], "project_id": run["project_id"], "agent_id": run["agent_id"],
                    "title": run["task_title"], "prompt": run["task_prompt"], "state": "running",
                    "resource_paths": json.loads(task["resource_paths"]),
                },
                "project": {"id": run["project_id"], "name": run["project_name"], "cwd": run["project_cwd"]},
                "agent": {
                    "id": run["agent_id"], "name": run["agent_name"], "instructions": run["agent_instructions"],
                },
                "execution": {"model": run["execution_model"], "effort": run["execution_effort"],
                              "concurrency": run["execution_concurrency"], "sandbox": run["execution_sandbox"]},
                "execution_account": None if run["execution_account_id"] is None else {
                    "id": run["execution_account_id"],
                    "name": run["execution_account_name"],
                    "codex_home": run["execution_account_home"],
                    "kind": run["execution_account_kind"],
                    "subject_id": run["execution_account_subject"],
                },
            }

    def attach_thread(self, run_id: str, thread_id: str, turn_id: str | None = None) -> dict[str, Any]:
        """为运行保存 Codex thread/turn 引用；不保存完整会话，且已绑定引用不可被覆盖。"""
        self._text(thread_id, "线程标识", 255)
        if turn_id is not None:
            self._text(turn_id, "轮次标识", 255)
        with self._connection() as conn:
            run = self._require(conn, "runs", run_id, "运行")
            if run["state"] != "running":
                raise StoreError("conflict", "只有运行中的任务可以绑定线程")
            if run["thread_id"] is not None and run["thread_id"] != thread_id:
                raise StoreError("conflict", "运行已绑定其他线程")
            if run["session_id"] is not None:
                self.sessions.attach_thread(run["session_id"], thread_id, connection=conn, account_id=run["execution_account_id"], account_subject=run["execution_account_subject"])
            conn.execute("UPDATE runs SET thread_id = ?, turn_id = ? WHERE id = ?", (thread_id, turn_id, run_id))
            return self._run_dict_in(conn, run_id)

    def snapshot_resources(self, run_id: str, instructions: str, manifest: list[dict]) -> None:
        """固定本次运行实际使用的规则与素材摘要，后续文件编辑不改变这份记录。"""
        self._text(instructions, "资源指令", 100_000, allow_empty=True)
        with self._connection(immediate=True) as conn:
            run = self._require(conn, "runs", run_id, "运行")
            if run["state"] != "running":
                raise StoreError("conflict", "运行已结束")
            conn.execute("UPDATE runs SET instructions_snapshot=?, resource_manifest=? WHERE id=?",
                         (instructions, json.dumps(manifest, ensure_ascii=False), run_id))

    def finish(self, run_id: str, state: str, result: str = "", error: str = "") -> dict[str, Any]:
        """结束运行到 review、failed 或 cancelled，保留运行结果类型，任务统一进入已完成。"""
        if state not in _FINISH_STATES:
            raise StoreError("validation", "运行结束状态只能为 review、failed 或 cancelled")
        self._text(result, "运行结果", 100_000, allow_empty=True)
        self._text(error, "运行错误", 30_000, allow_empty=True)
        with self._connection(immediate=True) as conn:
            run = self._require(conn, "runs", run_id, "运行")
            if run["state"] != "running":
                raise StoreError("conflict", "运行已经结束")
            now = self._now()
            conn.execute(
                "UPDATE runs SET state = ?, result = ?, error = ?, finished_at = ? WHERE id = ?",
                (state, result, error, now, run_id),
            )
            conn.execute(
                "UPDATE tasks SET state = ?, version = version + 1, updated_at = ? WHERE id = ?",
                ("done", now, run["task_id"]),
            )
            return self._run_dict_in(conn, run_id)

    def record_run_metrics(self, run_id: str, *, duration_ms: int, input_tokens: int | None = None,
                           output_tokens: int | None = None, cached_input_tokens: int | None = None,
                           turn_id: str | None = None, item_id: str | None = None) -> dict[str, Any]:
        """记录本次真实执行的有界指标；缺失 token 保持空而不是伪造零值。"""
        self._metric_value(duration_ms, "执行耗时", required=True)
        for value, label in ((input_tokens, "输入 token"), (output_tokens, "输出 token"), (cached_input_tokens, "缓存 token")):
            self._metric_value(value, label)
        if turn_id is not None:
            self._text(turn_id, "轮次标识", 255)
        if item_id is not None:
            self._text(item_id, "条目标识", 255)
        with self._connection(immediate=True) as conn:
            run = self._require(conn, "runs", run_id, "运行")
            if run["state"] != "running":
                raise StoreError("conflict", "只有运行中的任务可以记录指标")
            conn.execute("""UPDATE runs SET duration_ms=?,input_tokens=?,output_tokens=?,cached_input_tokens=?,
                turn_id=COALESCE(?,turn_id),item_id=COALESCE(?,item_id) WHERE id=?""",
                         (duration_ms, input_tokens, output_tokens, cached_input_tokens, turn_id, item_id, run_id))
            return self._run_dict_in(conn, run_id)

    def append_event(self, run_id: str, kind: str, message: str) -> dict[str, Any]:
        """记录有限的运行事件；每个运行最多保存 200 条，避免将完整会话写入数据库。"""
        self._text(kind, "事件类型", 80)
        self._text(message, "事件内容", 4_000)
        with self._connection(immediate=True) as conn:
            self._require(conn, "runs", run_id, "运行")
            count = conn.execute("SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)).fetchone()[0]
            if count >= _MAX_EVENTS_PER_RUN:
                raise StoreError("limit", "单次运行的事件数量已达上限")
            event_id = self._id()
            conn.execute(
                "INSERT INTO events(id, run_id, kind, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (event_id, run_id, kind, message, self._now()),
            )
            return dict(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())

    def list_runs(self, task_id: str) -> list[dict[str, Any]]:
        """按创建时间返回任务的运行记录及其不可变配置快照。"""
        with self._connection() as conn:
            self._require(conn, "tasks", task_id, "任务")
            return [dict(row) for row in conn.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY created_at, id", (task_id,))]

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        """按创建时间返回运行事件。"""
        with self._connection() as conn:
            self._require(conn, "runs", run_id, "运行")
            return [dict(row) for row in conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY created_at, id", (run_id,))]

    def recover_interrupted(self) -> list[dict[str, Any]]:
        """把遗留 running 运行标记为 interrupted，并将对应任务标记 done；不会自动重跑。"""
        with self._connection(immediate=True) as conn:
            runs = list(conn.execute("SELECT * FROM runs WHERE state = 'running' ORDER BY created_at, id"))
            if not runs:
                return []
            now = self._now()
            run_ids = [run["id"] for run in runs]
            conn.execute("UPDATE runs SET state = 'interrupted', error = 'interrupted', finished_at = ? WHERE state = 'running'", (now,))
            conn.execute(
                "UPDATE tasks SET state = 'done', version = version + 1, updated_at = ? WHERE state = 'running'",
                (now,),
            )
            return [self._run_dict_in(conn, run_id) for run_id in run_ids]

    def settings_hash(self) -> str | None:
        """读取此数据库最后成功发布的设置摘要，不把云端现有文件当作新库基线。"""
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM local_metadata WHERE key='settings_sha256'").fetchone()
            return row["value"] if row else None

    def record_settings_hash(self, digest: str) -> None:
        """仅在文件发布成功后记录摘要；崩溃期间的不一致下次按冲突处理。"""
        with self._connection() as conn:
            conn.execute("INSERT INTO local_metadata(key,value) VALUES ('settings_sha256',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (digest,))

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 160),
                    cwd TEXT NOT NULL UNIQUE, section_name TEXT, section_id TEXT, color TEXT, icon TEXT,
                    native_id TEXT UNIQUE, is_workspace INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sections (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 80),
                    color TEXT, icon TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 160),
                    model TEXT, effort TEXT,
                    instructions TEXT NOT NULL CHECK(length(instructions) <= 30000),
                    description TEXT,
                    sandbox TEXT, concurrency INTEGER,
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_accounts (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 160),
                    codex_home TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'isolated', subject_id TEXT, display_name TEXT,
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    default_execution_account_id TEXT REFERENCES execution_accounts(id)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 300), prompt TEXT NOT NULL CHECK(length(prompt) <= 100000),
                    agent_id TEXT REFERENCES agents(id), execution_account_id TEXT REFERENCES execution_accounts(id),
                    execution_account_subject TEXT,
                    resource_paths TEXT NOT NULL DEFAULT '[]', section_name TEXT, section_id TEXT,
                    model TEXT CHECK(model IS NULL OR length(model) BETWEEN 1 AND 120),
                    effort TEXT CHECK(effort IS NULL OR effort IN ('none','minimal','low','medium','high','xhigh','max','ultra')),
                    concurrency INTEGER NOT NULL DEFAULT 1 CHECK(concurrency BETWEEN 1 AND 32),
                    sandbox TEXT NOT NULL DEFAULT 'read-only' CHECK(sandbox IN ('read-only','workspace-write','danger-full-access')),
                    state TEXT NOT NULL CHECK(state IN ('backlog','ready','running','done','archived')),
                    version INTEGER NOT NULL CHECK(version >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), project_id TEXT NOT NULL REFERENCES projects(id),
                    agent_id TEXT REFERENCES agents(id), state TEXT NOT NULL CHECK(state IN ('running','review','failed','cancelled','interrupted')),
                    task_title TEXT NOT NULL, task_prompt TEXT NOT NULL, project_name TEXT NOT NULL, project_cwd TEXT NOT NULL,
                    agent_name TEXT NOT NULL, agent_instructions TEXT NOT NULL,
                    execution_model TEXT, execution_effort TEXT,
                    execution_concurrency INTEGER NOT NULL DEFAULT 1 CHECK(execution_concurrency BETWEEN 1 AND 32),
                    execution_sandbox TEXT NOT NULL DEFAULT 'read-only' CHECK(execution_sandbox IN ('read-only','workspace-write','danger-full-access')),
                    execution_account_id TEXT REFERENCES execution_accounts(id), execution_account_name TEXT,
                    execution_account_home TEXT, execution_account_kind TEXT, execution_account_subject TEXT,
                    thread_id TEXT, turn_id TEXT, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL CHECK(length(kind) BETWEEN 1 AND 80),
                    message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 4000), created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_task_created ON runs(task_id, created_at);
                CREATE INDEX IF NOT EXISTS events_run_created ON events(run_id, created_at);
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_column(conn, "projects", "section_name", "TEXT")
                self._ensure_column(conn, "projects", "section_id", "TEXT")
                self._ensure_column(conn, "projects", "color", "TEXT")
                self._ensure_column(conn, "projects", "icon", "TEXT")
                self._ensure_column(conn, "projects", "native_id", "TEXT")
                self._ensure_column(conn, "projects", "is_workspace", "INTEGER NOT NULL DEFAULT 0")
                self._ensure_column(conn, "agents", "description", "TEXT")
                self._ensure_column(conn, "tasks", "execution_account_id", "TEXT REFERENCES execution_accounts(id)")
                self._ensure_column(conn, "tasks", "execution_account_subject", "TEXT")
                self._ensure_column(conn, "tasks", "section_name", "TEXT")
                self._ensure_column(conn, "tasks", "resource_paths", "TEXT NOT NULL DEFAULT '[]'")
                self._ensure_column(conn, "tasks", "section_id", "TEXT")
                self._ensure_column(conn, "tasks", "model", "TEXT")
                self._ensure_column(conn, "tasks", "effort", "TEXT")
                self._ensure_column(conn, "tasks", "concurrency", "INTEGER NOT NULL DEFAULT 1")
                self._ensure_column(conn, "tasks", "sandbox", "TEXT NOT NULL DEFAULT 'read-only'")
                self._ensure_column(conn, "runs", "execution_account_id", "TEXT REFERENCES execution_accounts(id)")
                self._ensure_column(conn, "runs", "execution_account_name", "TEXT")
                self._ensure_column(conn, "runs", "execution_account_home", "TEXT")
                self._ensure_column(conn, "runs", "execution_account_kind", "TEXT")
                self._ensure_column(conn, "runs", "execution_account_subject", "TEXT")
                self._ensure_column(conn, "execution_accounts", "kind", "TEXT NOT NULL DEFAULT 'isolated'")
                self._ensure_column(conn, "execution_accounts", "subject_id", "TEXT")
                self._ensure_column(conn, "execution_accounts", "display_name", "TEXT")
                self._ensure_column(conn, "execution_accounts", "expected_email", "TEXT")
                self._ensure_column(conn, "tasks", "session_id", "TEXT REFERENCES conversation_sessions(id)")
                self._ensure_column(conn, "runs", "session_id", "TEXT REFERENCES conversation_sessions(id)")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_running_task_per_session ON tasks(session_id) WHERE session_id IS NOT NULL AND state='running'")
                self._ensure_column(conn, "runs", "instructions_snapshot", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, "runs", "resource_manifest", "TEXT NOT NULL DEFAULT '[]'")
                self._ensure_column(conn, "runs", "execution_model", "TEXT")
                self._ensure_column(conn, "runs", "execution_effort", "TEXT")
                self._ensure_column(conn, "runs", "execution_concurrency", "INTEGER NOT NULL DEFAULT 1")
                self._ensure_column(conn, "runs", "execution_sandbox", "TEXT NOT NULL DEFAULT 'read-only'")
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        self._migrate_agents_execution_fields()
        self._migrate_runs_agent_nullable()
        self._migrate_system_sandbox()
        self._migrate_run_metrics()
        self._migrate_task_states()

    def _migrate_task_states(self) -> None:
        """五种任务状态迁移；运行结果保留原表，旧终态统一为已完成。"""
        raw = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("BEGIN IMMEDIATE")
            definition = raw.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
            if not definition or "'archived'" in definition[0]:
                raw.execute("COMMIT")
                return
            sql = definition[0]
            definitions = [r[0] for r in raw.execute("SELECT sql FROM sqlite_master WHERE tbl_name='tasks' AND type IN ('index','trigger') AND sql IS NOT NULL")]
            revised, count = re.subn(r"CHECK\s*\(\s*state\s+IN\s*\([^)]*\)\s*\)", "CHECK(state IN ('backlog','ready','running','done','archived'))", sql, count=1, flags=re.I)
            if count != 1:
                if count == 0 and sql.rstrip().endswith(')') and not re.search(r"CHECK\s*\([^)]*\bstate\b",sql,re.I):
                    revised=sql.rstrip()[:-1]+", CHECK(state IN ('backlog','ready','running','done','archived')))"
                else:
                    raise StoreError("migration_invalid", "无法识别任务状态约束，未修改数据")
            revised = re.sub(r'(?i)^CREATE TABLE\s+(?:IF NOT EXISTS\s+)?["`\[]?tasks["`\]]?', "CREATE TABLE tasks_state_migration", revised, count=1)
            raw.execute(revised)
            columns = [r[1] for r in raw.execute("PRAGMA table_info(tasks)")]
            names = ','.join('"'+c.replace('"','""')+'"' for c in columns)
            projection = ','.join("CASE WHEN state IN ('review','failed','cancelled') THEN 'done' ELSE state END" if c=='state' else "version + CASE WHEN state IN ('review','failed','cancelled') THEN 1 ELSE 0 END" if c=='version' else '"'+c.replace('"','""')+'"' for c in columns)
            raw.execute(f"INSERT INTO tasks_state_migration ({names}) SELECT {projection} FROM tasks")
            raw.execute("DROP TABLE tasks")
            raw.execute("ALTER TABLE tasks_state_migration RENAME TO tasks")
            for statement in definitions:
                raw.execute(statement)
            if raw.execute("PRAGMA foreign_key_check").fetchone():
                raise StoreError("migration_invalid", "任务状态迁移的外键校验失败")
            raw.execute("COMMIT")
        except BaseException:
            if raw.in_transaction:
                raw.execute("ROLLBACK")
            raise
        finally:
            raw.close()

    def _migrate_agents_execution_fields(self) -> None:
        """将旧助手的任务执行字段改为可空历史字段，保留所有被引用的助手记录。"""
        raw = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            columns = {row[1]: row for row in raw.execute("PRAGMA table_info(agents)")}
            if not columns or (columns["sandbox"][3] == 0 and columns["concurrency"][3] == 0):
                return
            definitions = [row[0] for row in raw.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name='agents' AND type IN ('index','trigger') AND sql IS NOT NULL"
            )]
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("BEGIN IMMEDIATE")
            raw.execute("""CREATE TABLE agents_rebuilt (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 160),
                model TEXT, effort TEXT,
                instructions TEXT NOT NULL CHECK(length(instructions) <= 30000), description TEXT,
                sandbox TEXT, concurrency INTEGER,
                version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1), created_at TEXT NOT NULL
            )""")
            raw.execute("""INSERT INTO agents_rebuilt(
                id,name,model,effort,instructions,description,sandbox,concurrency,version,created_at
            ) SELECT id,name,model,effort,instructions,description,sandbox,concurrency,version,created_at FROM agents""")
            raw.execute("DROP TABLE agents")
            raw.execute("ALTER TABLE agents_rebuilt RENAME TO agents")
            for statement in definitions:
                raw.execute(statement)
            if list(raw.execute("PRAGMA foreign_key_check")):
                raise StoreError("migration_invalid", "专业助手迁移的外键校验未通过")
            raw.execute("COMMIT")
        except BaseException:
            if raw.in_transaction:
                raw.execute("ROLLBACK")
            raise
        finally:
            raw.close()

    def _migrate_runs_agent_nullable(self) -> None:
        """重建旧 runs 表，使默认助手运行可保存空 agent_id 而不丢失旧运行。"""
        raw = sqlite3.connect(self.path, isolation_level=None)
        try:
            columns = {row[1]: row for row in raw.execute("PRAGMA table_info(runs)")}
            if not columns or columns["agent_id"][3] == 0:
                return
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("BEGIN IMMEDIATE")
            raw.execute("DROP INDEX IF EXISTS runs_agent_running")
            raw.execute("""CREATE TABLE runs_rebuilt (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), project_id TEXT NOT NULL REFERENCES projects(id),
                agent_id TEXT REFERENCES agents(id), state TEXT NOT NULL CHECK(state IN ('running','review','failed','cancelled','interrupted')),
                task_title TEXT NOT NULL, task_prompt TEXT NOT NULL, project_name TEXT NOT NULL, project_cwd TEXT NOT NULL,
                agent_name TEXT NOT NULL, agent_model TEXT, agent_effort TEXT, agent_instructions TEXT NOT NULL,
                agent_sandbox TEXT CHECK(agent_sandbox IS NULL OR agent_sandbox IN ('read-only','workspace-write','danger-full-access')),
                agent_concurrency INTEGER, execution_model TEXT, execution_effort TEXT,
                execution_concurrency INTEGER NOT NULL DEFAULT 1 CHECK(execution_concurrency BETWEEN 1 AND 32),
                execution_sandbox TEXT NOT NULL DEFAULT 'read-only' CHECK(execution_sandbox IN ('read-only','workspace-write','danger-full-access')),
                execution_account_id TEXT REFERENCES execution_accounts(id), execution_account_name TEXT, execution_account_home TEXT,
                execution_account_kind TEXT, execution_account_subject TEXT, session_id TEXT REFERENCES conversation_sessions(id),
                thread_id TEXT, turn_id TEXT, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                instructions_snapshot TEXT NOT NULL DEFAULT '', resource_manifest TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, finished_at TEXT
            )""")
            raw.execute("""INSERT INTO runs_rebuilt(
                id, task_id, project_id, agent_id, state, task_title, task_prompt, project_name, project_cwd,
                agent_name, agent_model, agent_effort, agent_instructions, agent_sandbox, agent_concurrency,
                execution_model, execution_effort, execution_concurrency, execution_sandbox,
                execution_account_id, execution_account_name, execution_account_home, execution_account_kind, execution_account_subject, session_id,
                thread_id, turn_id, result, error,
                instructions_snapshot, resource_manifest, created_at, finished_at
            ) SELECT id, task_id, project_id, agent_id, state, task_title, task_prompt, project_name, project_cwd,
                agent_name, agent_model, agent_effort, agent_instructions, agent_sandbox, agent_concurrency,
                COALESCE(execution_model, agent_model), COALESCE(execution_effort, agent_effort),
                COALESCE(execution_concurrency, agent_concurrency, 1), COALESCE(execution_sandbox, agent_sandbox, 'read-only'),
                execution_account_id, execution_account_name, execution_account_home, execution_account_kind, execution_account_subject, session_id,
                thread_id, turn_id, result, error,
                instructions_snapshot, resource_manifest, created_at, finished_at FROM runs""")
            raw.execute("DROP TABLE runs")
            raw.execute("ALTER TABLE runs_rebuilt RENAME TO runs")
            raw.execute("CREATE INDEX IF NOT EXISTS runs_task_created ON runs(task_id, created_at)")
            raw.execute("COMMIT")
        except BaseException:
            raw.execute("ROLLBACK")
            raise
        finally:
            raw.close()

    def _migrate_system_sandbox(self) -> None:
        """扩展旧表的沙箱枚举，完整保留任务、运行、索引和外键引用。"""
        raw = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            definitions = []
            for table in ("tasks", "runs"):
                row = raw.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if row and "'danger-full-access'" not in row[0]:
                    definitions.append((table, row[0]))
            if not definitions:
                return
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("BEGIN IMMEDIATE")
            for table, sql in definitions:
                indexes = [row[0] for row in raw.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,))]
                revised = sql.replace("('read-only','workspace-write')", "('read-only','workspace-write','danger-full-access')")
                temporary = table + "_sandbox_migration"
                revised = re.sub(r"(?i)^CREATE TABLE\s+(?:IF NOT EXISTS\s+)?[\"`\[]?" + table + r"[\"`\]]?", "CREATE TABLE " + temporary, revised, count=1)
                raw.execute(revised)
                raw.execute(f"INSERT INTO {temporary} SELECT * FROM {table}")
                raw.execute(f"DROP TABLE {table}")
                raw.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
                for statement in indexes:
                    raw.execute(statement)
            if raw.execute("PRAGMA foreign_key_check").fetchone():
                raise StoreError("migration_invalid", "系统执行策略迁移校验失败")
            raw.execute("COMMIT")
        except BaseException:
            if raw.in_transaction:
                raw.execute("ROLLBACK")
            raise
        finally:
            raw.close()

    def _migrate_run_metrics(self) -> None:
        """为既有运行表补充可空真实执行指标，不回填历史缺失数据。"""
        raw = sqlite3.connect(self.path, isolation_level=None)
        try:
            columns = {row[1] for row in raw.execute("PRAGMA table_info(runs)")}
            if not columns:
                return
            additions = {
                "duration_ms": "INTEGER",
                "input_tokens": "INTEGER",
                "output_tokens": "INTEGER",
                "cached_input_tokens": "INTEGER",
                "item_id": "TEXT",
                "location_marker": "TEXT",
            }
            missing = [(name, definition) for name, definition in additions.items() if name not in columns]
            if not missing:
                return
            raw.execute("BEGIN IMMEDIATE")
            for name, definition in missing:
                raw.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            raw.execute("COMMIT")
        except BaseException:
            if raw.in_transaction:
                raw.execute("ROLLBACK")
            raise
        finally:
            raw.close()

    @contextmanager
    def _connection(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """建立一个带外键、事务和确定关闭行为的短生命周期连接。"""
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def _resource_paths(value: Any) -> str:
        if not isinstance(value, list) or len(value) > 20 or any(not isinstance(p, str) or not p or len(p) > 500 for p in value):
            raise StoreError("validation", "资源选择必须是最多 20 个相对路径")
        return json.dumps(list(dict.fromkeys(value)), ensure_ascii=False)

    @staticmethod
    def _section_name(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 80:
            raise StoreError("validation", "分区名称不能超过 80 个字符")
        return value.strip() or None

    @staticmethod
    def _appearance(color: Any, icon: Any) -> tuple[str | None, str | None]:
        try:
            return validate_color(color), validate_icon(icon)
        except AppearanceError as exc:
            raise StoreError("validation", str(exc)) from exc

    @staticmethod
    def _native_id(value: Any) -> None:
        if value is not None and (not isinstance(value, str) or not value or len(value) > 255):
            raise StoreError("validation", "原生项目标识无效")

    @staticmethod
    def _execution_settings(model: Any, effort: Any, concurrency: Any, sandbox: Any) -> None:
        Store._model(model)
        if effort is not None and effort not in _EFFORTS:
            raise StoreError("validation", "不支持的推理强度")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 32:
            raise StoreError("validation", "并发上限必须在 1 到 32 之间")
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise StoreError("validation", "沙箱只能为 read-only 或 workspace-write")

    @staticmethod
    def _metric_value(value: Any, label: str, required: bool = False) -> None:
        if value is None and not required:
            return
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**12:
            raise StoreError("validation", f"{label}无效")

    def _validate_section_id(self, conn: sqlite3.Connection, section_id: Any) -> None:
        if section_id is None:
            return
        if not isinstance(section_id, str) or not section_id:
            raise StoreError("validation", "分区标识无效")
        if section_id.startswith("native:section:"):
            if len(section_id) > 255 or not section_id.removeprefix("native:section:"):
                raise StoreError("validation", "原生分区标识无效")
            return
        self._require(conn, "sections", section_id, "分区")

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["resource_paths"] = json.loads(value.get("resource_paths", "[]"))
        return value

    @staticmethod
    def _appearance_dict(value: dict[str, Any]) -> dict[str, Any]:
        if "icon" in value:
            value["icon"] = icon_value(value["icon"])
        return value

    @staticmethod
    def _agent_projection(value: dict[str, Any]) -> dict[str, Any]:
        return {key: value.get(key) for key in ("id", "name", "description", "instructions", "version", "created_at")}

    @staticmethod
    def _agent_integrity_error(exc: sqlite3.IntegrityError) -> StoreError:
        """只把助手名称唯一约束转换为重名，其他数据库约束保留真实错误语义。"""
        if "UNIQUE constraint failed: agents.name" in str(exc):
            return StoreError("conflict", "代理名称已存在")
        return StoreError("constraint", "专业助手数据约束异常，请检查工作台升级状态")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="microseconds")

    @staticmethod
    def _text(value: Any, label: str, maximum: int, allow_empty: bool = False) -> None:
        if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
            raise StoreError("validation", f"{label}无效")

    @staticmethod
    def _model(model: Any) -> None:
        if model is not None and (not isinstance(model, str) or _MODEL_PATTERN.fullmatch(model) is None):
            raise StoreError("validation", "模型标识无效")

    @staticmethod
    def _validate_task_transition(current: str, target: Any, is_manual: bool) -> None:
        transitions = {
            "backlog": {"ready", "archived"},
            "ready": {"backlog", "archived"},
            "done": {"backlog", "ready", "archived"},
            "archived": {"backlog", "ready", "done"},
        }
        if is_manual:
            transitions["backlog"].add("done")
            transitions["ready"].add("done")
        if target not in transitions.get(current, set()):
            raise StoreError("conflict", f"不允许从 {current} 转为 {target}")

    @staticmethod
    def _cwd(cwd: Any) -> str:
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise StoreError("validation", "工作目录必须是当前存在的绝对目录")
        return os.path.realpath(cwd)

    @staticmethod
    def _execution_home(codex_home: Any) -> str:
        """委托运行时统一校验独立配置目录，并转换为工作台稳定错误码。"""
        try:
            return validate_account_home(codex_home)
        except ValueError as exc:
            raise StoreError("validation", str(exc)) from exc

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _require(conn: sqlite3.Connection, table: str, row_id: str, label: str) -> sqlite3.Row:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise StoreError("not_found", f"{label}不存在")
        return row

    def _project_dict(self, project_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            return self._appearance_dict(dict(self._require(conn, "projects", project_id, "项目")))

    def _agent_dict(self, agent_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            return self._agent_projection(dict(self._require(conn, "agents", agent_id, "代理")))

    def _section_dict(self, section_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            return self._appearance_dict(dict(self._require(conn, "sections", section_id, "分区")))

    def _execution_account_dict(self, account_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            account = self._require(conn, "execution_accounts", account_id, "执行账户")
            return {**dict(account), "name": account["display_name"] or account["name"], "isDefault": account_id == self._default_execution_account_id(conn)}

    @staticmethod
    def _default_execution_account_id(conn: sqlite3.Connection) -> str | None:
        row = conn.execute("SELECT default_execution_account_id FROM preferences WHERE singleton = 1").fetchone()
        if row is not None:
            return row["default_execution_account_id"]
        current = conn.execute("SELECT id FROM execution_accounts WHERE id = 'current'").fetchone()
        return current["id"] if current is not None else None

    @staticmethod
    def _account_subject(conn: sqlite3.Connection, account_id: str | None) -> str | None:
        if account_id is None:
            return None
        row = Store._require(conn, "execution_accounts", account_id, "执行账户")
        return row["subject_id"]

    @staticmethod
    def _run_dict_in(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        return dict(Store._require(conn, "runs", run_id, "运行"))
