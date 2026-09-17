"""只读投影指定 Codex 数据目录的项目、侧栏分区与未归档会话元数据。"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .titles import safe_display_title
from .native_sessions import name_token


class NativeCatalogError(RuntimeError):
    """原生状态格式无法按白名单安全读取时的明确错误。"""


class NativeCatalog:
    """读取 Codex 原生目录；不读取认证、会话正文或 rollout 文件。"""

    _MAX_SESSIONS = 2000
    _MAX_GLOBAL_STATE_BYTES = 16 * 1024 * 1024
    _GLOBAL_STATE = ".codex-global-state.json"
    _REQUIRED_THREAD_COLUMNS = frozenset({"id", "title", "cwd", "archived", "created_at", "updated_at"})

    def __init__(self, codex_home: Path, *, include_archived: bool = False):
        """绑定只读数据目录；此目录不证明当前窗口或任何会话的账户归属。"""
        self.codex_home = Path(codex_home)
        self.include_archived = include_archived

    def snapshot(self, current_account_id: str | None = None) -> dict[str, Any]:
        """返回原生分区、项目和未归档会话的只读投影。

        ``current_account_id`` 只选择该账户的侧栏分区，不用于推断线程所有权。
        无显式账户时只允许唯一账户分区的未验证推断；不会信任 MCP 目录缓存。
        只读取白名单元数据。格式不兼容时返回固定错误码，不暴露原始错误或路径。
        """
        return self._snapshot(current_account_id)

    def revision(self) -> str:
        """仅对展示相关元数据计算修订，排除窗口状态与无关 SQLite 写入。"""
        state=self._global_state()
        electron=state.get('electron-persisted-atom-state') or {}
        if not isinstance(electron,dict):raise NativeCatalogError('unsupported_sidebar_schema')
        relevant={key:state.get(key) for key in ('local-projects','project-order','project-appearances','thread-project-assignments')}
        relevant['sidebar']={key:electron.get(key) for key in ('sidebar-custom-sections-v3','unified-sidebar-pinned-order-v1')}
        sessions,relevant['truncated']=self._read_database(self._state_database())
        # 官方读取本身也会更新活动时间，不能据此反复判定目录失效；执行状态另由短期惰性核验覆盖。
        relevant['sessions']=[{k:v for k,v in row.items() if k!='updated_at'} for row in sessions]
        return hashlib.sha256(json.dumps(relevant,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

    def read_bound_sessions(self, thread_ids: list[str], current_account_id: str | None = None) -> dict[str, dict[str, Any]]:
        """仅按已有绑定 ID 读取白名单元数据，含归档记录；缺失仅由成功的精确查询确认。

        侧栏损坏不妨碍单独确认线程存在性。数据库不可读时返回 unavailable，
        不扫描归档目录、不读取 rollout、不恢复或移动任何原生会话。
        """
        identities = tuple(dict.fromkeys(thread_ids))
        if not identities:
            return {}
        if any(not isinstance(item, str) or not item or len(item) > 255 for item in identities):
            raise NativeCatalogError("invalid_bound_thread_id")
        result = self._snapshot(current_account_id, identities)
        if result["status"]["state"] == "ok":
            found = {item["native_id"]: item for item in result["sessions"]}
        else:
            try:
                rows, _ = self._read_database(self._state_database(), identities)
                found = {row["id"]: self._thread_metadata(row) for row in rows}
            except (NativeCatalogError, OSError):
                return {identity: {"native_id": identity, "native_status": "unavailable"} for identity in identities}
        return {identity: found.get(identity, {"native_id": identity, "native_status": "missing"}) for identity in identities}

    def _snapshot(self, current_account_id: str | None, thread_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
        provenance = {"directory": str(self.codex_home), "global_state": self._GLOBAL_STATE,
                      "database": None, "window_binding": "unverified", "thread_account_ownership": "unknown"}
        account_selection = {"method": "explicit" if current_account_id is not None else "unresolved",
                             "verified": isinstance(current_account_id, str) and bool(current_account_id.strip())}
        try:
            state = self._global_state()
            projects, project_roots = self._projects(state)
            sections, section_aliases, project_sections, thread_sections, account_selection = self._sections(state, current_account_id)
            pinned_projects, pinned_threads = self._pinned_items(state)
            for project in projects:
                project["section_id"] = self._native("section", project_sections[project["native_id"]]) if project["native_id"] in project_sections else None
                project["is_pinned"] = project["native_id"] in pinned_projects
                project.pop("native_id")
            database = self._state_database()
            provenance["database"] = database.name
            sessions, truncated = self._sessions(state, project_roots, section_aliases, project_sections, thread_sections, pinned_threads, database, thread_ids)
        except NativeCatalogError as error:
            return self._error_snapshot(str(error), provenance, account_selection)
        except OSError:
            return self._error_snapshot("unreadable_native_catalog", provenance, account_selection)
        return {
            "sections": sections,
            "projects": projects,
            "sessions": sessions,
            "status": {
                "state": "ok",
                "source": "codex",
                "provenance": provenance,
                "account_selection": account_selection,
                "truncated": truncated,
                "session_limit": self._MAX_SESSIONS,
                "errors": [],
            },
        }

    def _global_state(self) -> dict[str, Any]:
        path = self.codex_home / self._GLOBAL_STATE
        try:
            if path.stat().st_size > self._MAX_GLOBAL_STATE_BYTES:
                raise NativeCatalogError("global_state_too_large")
            raw = json.loads(path.read_bytes().decode("utf-8"))
        except FileNotFoundError as error:
            raise NativeCatalogError("missing_global_state") from error
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NativeCatalogError("invalid_global_state") from error
        if not isinstance(raw, dict):
            raise NativeCatalogError("invalid_global_state_schema")
        required = ("local-projects", "project-order", "thread-project-assignments", "project-appearances")
        if any(key not in raw for key in required):
            raise NativeCatalogError("unsupported_global_state_schema")
        return raw

    def _projects(self, state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[tuple[str, str]]]]:
        raw_projects = state["local-projects"]
        appearances = state["project-appearances"]
        order = state["project-order"]
        if not isinstance(raw_projects, dict) or not isinstance(appearances, dict) or not isinstance(order, list):
            raise NativeCatalogError("unsupported_project_schema")
        ordered_ids = list(dict.fromkeys(item for item in order if isinstance(item, str) and item in raw_projects))
        ordered_ids.extend(project_id for project_id in raw_projects if project_id not in ordered_ids)
        projects: list[dict[str, Any]] = []
        roots: dict[str, list[tuple[str, str]]] = {}
        for project_id in ordered_ids:
            value = raw_projects[project_id]
            if not isinstance(value, dict) or value.get("id") != project_id or not isinstance(value.get("name"), str):
                raise NativeCatalogError("unsupported_local_project_schema")
            root_paths = value.get("rootPaths")
            if not isinstance(root_paths, list) or not all(isinstance(path, str) for path in root_paths):
                raise NativeCatalogError("unsupported_local_project_schema")
            appearance = appearances.get(project_id, {})
            if not isinstance(appearance, dict):
                raise NativeCatalogError("unsupported_project_appearance_schema")
            roots[project_id] = [(self._path_key(path), path) for path in root_paths if path]
            projects.append({
                "id": self._native("project", project_id),
                "native_id": project_id,
                "name": value["name"],
                "cwd": root_paths[0] if root_paths else None,
                "section_id": None,
                "updated_at": value.get("updatedAt"), "created_at": value.get("createdAt"),
                "color": appearance.get("color") if isinstance(appearance.get("color"), str) else None,
                "icon": self._icon(appearance.get("marker")),
                "source": "codex",
            })
        return projects, roots

    def _sections(self, state: dict[str, Any], current_account_id: str | None) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str], dict[str, str], dict[str, Any]]:
        electron = state.get("electron-persisted-atom-state", {})
        if electron is None:
            electron = {}
        if not isinstance(electron, dict):
            raise NativeCatalogError("unsupported_sidebar_schema")
        sections = []
        project_sections: dict[str, str] = {}
        thread_sections: dict[str, str] = {}
        section_aliases: dict[str, str] = {}
        raw_by_account = electron.get("sidebar-custom-sections-v3", {})
        if raw_by_account is None:
            raw_by_account = {}
        if not isinstance(raw_by_account, dict):
            raise NativeCatalogError("unsupported_sidebar_schema")
        account_id, selection = self._current_account_id(current_account_id, raw_by_account)
        if account_id is None or account_id not in raw_by_account:
            return sections, section_aliases, project_sections, thread_sections, selection
        account_state = raw_by_account[account_id]
        if not isinstance(account_state, dict) or not isinstance(account_state.get("sections"), list):
            raise NativeCatalogError("unsupported_sidebar_section_schema")
        values = account_state["sections"]
        by_id: dict[str, dict[str, Any]] = {}
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not isinstance(value.get("name"), str):
                raise NativeCatalogError("unsupported_sidebar_section_schema")
            if not value["id"] or value["id"] in by_id:
                raise NativeCatalogError("unsupported_sidebar_section_schema")
            by_id[value["id"]] = value
        order = account_state.get("sectionOrder", list(by_id))
        if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
            raise NativeCatalogError("unsupported_sidebar_section_schema")
        ids = list(dict.fromkeys(item for item in order if item in by_id))
        ids.extend(item for item in by_id if item not in ids)
        for section_id in ids:
            value = by_id[section_id]
            appearance = value.get("appearance", {})
            if appearance is None:
                appearance = {}
            if not isinstance(appearance, dict):
                raise NativeCatalogError("unsupported_sidebar_section_schema")
            sections.append({"id": self._native("section", section_id), "name": value["name"],
                             "color": appearance.get("color") if isinstance(appearance.get("color"), str) else None,
                             "icon": self._icon(appearance.get("marker") or appearance.get("icon")), "source": "codex"})
            host_sections = value.get("hostSectionIds", {})
            if not isinstance(host_sections, dict):
                raise NativeCatalogError("unsupported_sidebar_section_schema")
            aliases = [section_id, *(item for item in host_sections.values() if isinstance(item, str))]
            for alias in aliases:
                if alias in section_aliases and section_aliases[alias] != section_id:
                    raise NativeCatalogError("ambiguous_sidebar_section_assignment")
                section_aliases[alias] = section_id
            item_keys = value.get("itemKeys", [])
            if not isinstance(item_keys, list) or not all(isinstance(item, str) for item in item_keys):
                raise NativeCatalogError("unsupported_sidebar_section_schema")
            for item in item_keys:
                project_id = self._project_id_from_item(item)
                if project_id is not None:
                    self._assign_section(project_sections, project_id, section_id)
                elif item.startswith("codex:thread:") and item.removeprefix("codex:thread:"):
                    self._assign_section(thread_sections, item.removeprefix("codex:thread:"), section_id)
        return sections, section_aliases, project_sections, thread_sections, selection

    @staticmethod
    def _current_account_id(current_account_id: str | None, custom: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        if current_account_id is not None:
            if not isinstance(current_account_id, str) or not current_account_id.strip():
                raise NativeCatalogError("invalid_current_sidebar_account")
            return current_account_id, {"method": "explicit", "verified": True}
        if len(custom) > 1:
            raise NativeCatalogError("ambiguous_sidebar_account")
        if custom:
            return next(iter(custom)), {"method": "single_sidebar_account", "verified": False}
        return None, {"method": "no_sidebar_accounts", "verified": False}

    @staticmethod
    def _assign_section(assignments: dict[str, str], item_id: str, section_id: str) -> None:
        if item_id in assignments and assignments[item_id] != section_id:
            raise NativeCatalogError("ambiguous_sidebar_section_assignment")
        assignments[item_id] = section_id

    def _sessions(self, state: dict[str, Any], project_roots: dict[str, list[tuple[str, str]]], section_aliases: dict[str, str], project_sections: dict[str, str], thread_sections: dict[str, str], pinned_threads: set[str], database: Path, thread_ids: tuple[str, ...] | None = None) -> tuple[list[dict[str, Any]], bool]:
        assignments = state["thread-project-assignments"]
        if not isinstance(assignments, dict):
            raise NativeCatalogError("unsupported_thread_assignment_schema")
        rows, truncated = self._read_database(database, thread_ids)
        sessions = []
        for row in rows:
            project_id, project_source = self._project_for(row, assignments, project_roots)
            if row["id"] in thread_sections:
                section_id, section_source = thread_sections[row["id"]], "sidebar_thread_assignment"
            elif row["thread_section_id"]:
                section_id = section_aliases.get(row["thread_section_id"])
                section_source = "thread_metadata" if section_id else "unresolved_thread_section"
            else:
                section_id = project_sections.get(project_id)
                section_source = "project" if section_id else "none"
            sessions.append({**self._thread_metadata(row), "id": self._native("session", row["id"]),
                          "cwd": row["cwd"], "model": row.get("model"), "effort": row.get("reasoning_effort"),
                          "project_id": self._native("project", project_id) if project_id else None,
                          "section_id": self._native("section", section_id) if section_id else None,
                          "is_pinned": bool(row["is_pinned"]) or row["id"] in pinned_threads,
                          "source": "codex",
                          "provenance": {"project": project_source, "section": section_source, "account_ownership": "unknown"},
                          "updated_at": row["updated_at"], "created_at": row["created_at"]})
        return sessions, truncated

    @staticmethod
    def _thread_metadata(row: dict[str, Any]) -> dict[str, Any]:
        """原始 name 仅参与 HMAC；缺少 name 列时不能把 title 假定为原生名称。"""
        source_title = row.get("name") if isinstance(row.get("name"), str) and row["name"].strip() else row["title"]
        return {"native_id": row["id"], "title": safe_display_title(source_title, 300 if row.get("name") else 50),
                "cwd": row["cwd"], "native_status": "archived" if row["archived"] else "unarchived",
                "name_token": name_token(row["id"], row["name"]) if row["_name_available"] else None}

    def _read_database(self, path: Path, thread_ids: tuple[str, ...] | None = None) -> tuple[list[dict[str, Any]], bool]:
        try:
            connection = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=1000")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if not self._REQUIRED_THREAD_COLUMNS.issubset(columns):
                raise NativeCatalogError("unsupported_native_thread_schema")
            optional = {name for name in ("project_id", "thread_section_id", "is_pinned", "model", "reasoning_effort", "name") if name in columns}
            fields = ["id", "title", "cwd", "archived", "created_at", "updated_at"]
            fields.extend(sorted(optional))
            select = ", ".join(fields)
            conditions = ["1=1"] if self.include_archived else ["archived = 0"]
            if "thread_source" in columns:
                conditions.append("(thread_source IS NULL OR thread_source <> 'subagent')")
            if "source" in columns:
                # source 是原生来源枚举；在 SQL 内过滤，不将其内部代理元数据读入投影。
                conditions.append("""(source IS NULL OR (source NOT IN ('subagent', '"subagent"') AND
                    CASE WHEN json_valid(source) THEN
                        json_type(source, '$.subagent') IS NULL AND json_type(source, '$.subAgent') IS NULL
                    ELSE 1 END))""")
            if thread_ids is None:
                result = connection.execute(
                    f"SELECT {select} FROM threads WHERE {' AND '.join(conditions)}"
                    + " ORDER BY updated_at DESC, id DESC LIMIT ?", (self._MAX_SESSIONS + 1,)
                ).fetchall()
            else:
                # 每批参数有界；无列表上限、归档或子代理过滤，缺失判断只依赖绑定 ID。
                result = []
                for start in range(0, len(thread_ids), 500):
                    batch = thread_ids[start:start + 500]
                    result.extend(connection.execute(
                        f"SELECT {select} FROM threads WHERE id IN ({','.join('?' for _ in batch)})", batch
                    ).fetchall())
        except sqlite3.Error as error:
            raise NativeCatalogError("unreadable_native_state_database") from error
        finally:
            if 'connection' in locals():
                connection.close()
        normalized = []
        for row in result:
            values = dict(row)
            if not isinstance(values["id"], str) or not isinstance(values["title"], str) or not isinstance(values["cwd"], str):
                raise NativeCatalogError("unsupported_native_thread_schema")
            values.setdefault("project_id", None)
            values.setdefault("thread_section_id", None)
            values.setdefault("is_pinned", 0)
            values.setdefault("name", None)
            values["_name_available"] = "name" in columns
            if any(values.get(key) is not None and not isinstance(values[key], str)
                   for key in ("name", "project_id", "thread_section_id", "model", "reasoning_effort")):
                raise NativeCatalogError("unsupported_native_thread_schema")
            if values["archived"] not in (0, 1) or values["is_pinned"] not in (0, 1) or any(not isinstance(values[key], int) for key in ("created_at", "updated_at")):
                raise NativeCatalogError("unsupported_native_thread_schema")
            normalized.append(values)
        return (normalized, False) if thread_ids is not None else (normalized[:self._MAX_SESSIONS], len(result) > self._MAX_SESSIONS)

    def _state_database(self) -> Path:
        """选择当前 state_<n>.sqlite，避免历史库改变当前目录的读取结果。"""
        candidates = sorted(self.codex_home.glob("state*.sqlite"))
        numbered = [(int(match.group(1)), path) for path in candidates if (match := re.fullmatch(r"state_(\d+)\.sqlite", path.name))]
        if numbered:
            highest = max(number for number, _ in numbered)
            selected = [path for number, path in numbered if number == highest]
            if len(selected) != 1:
                raise NativeCatalogError("ambiguous_native_state_database")
            return selected[0]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise NativeCatalogError("missing_native_state_database")
        raise NativeCatalogError("ambiguous_native_state_database")

    @staticmethod
    def _project_id_from_item(item: str) -> str | None:
        """解析原生侧栏公开的项目 item key，不将未知 item 猜为项目。"""
        prefix = "codex:project:"
        return item[len(prefix):] if item.startswith(prefix) and item[len(prefix):] else None

    def _pinned_items(self, state: dict[str, Any]) -> tuple[set[str], set[str]]:
        electron = state.get("electron-persisted-atom-state") or {}
        values = electron.get("unified-sidebar-pinned-order-v1", [])
        if values is None:
            return set(), set()
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise NativeCatalogError("unsupported_pinned_sidebar_schema")
        return ({project_id for item in values if (project_id := self._project_id_from_item(item)) is not None},
                {item.removeprefix("codex:thread:") for item in values if item.startswith("codex:thread:")})

    def _project_for(self, row: dict[str, Any], assignments: dict[str, Any], roots: dict[str, list[tuple[str, str]]]) -> tuple[str | None, str]:
        # 显式解绑或失效的归属仍是权威事实；不能用 cwd 把它悄悄分回另一个项目。
        if row["id"] in assignments:
            assignment = assignments[row["id"]]
            if assignment is None:
                return None, "explicit_unassigned"
            if not isinstance(assignment, dict) or "projectId" not in assignment:
                raise NativeCatalogError("unsupported_thread_assignment_schema")
            project_id = assignment["projectId"]
            if project_id is None:
                return None, "explicit_unassigned"
            if not isinstance(project_id, str):
                raise NativeCatalogError("unsupported_thread_assignment_schema")
            if assignment.get("projectKind", "local") != "local" or project_id not in roots:
                return None, "unresolved_assignment"
            return project_id, "assignment"
        if row["project_id"] is not None:
            return ((row["project_id"], "thread_metadata") if row["project_id"] in roots
                    else (None, "unresolved_thread_project"))
        cwd = self._path_key(row["cwd"])
        candidates = [(len(root), project_id) for project_id, values in roots.items() for root, _ in values
                      if cwd == root or cwd.startswith(root.rstrip(os.sep) + os.sep)]
        if not candidates:
            return None, "none"
        longest = max(length for length, _ in candidates)
        matches = {project_id for length, project_id in candidates if length == longest}
        if len(matches) != 1:
            return None, "ambiguous_cwd"
        return next(iter(matches)), "cwd_unique_root"

    @staticmethod
    def _path_key(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    @staticmethod
    def _native(kind: str, native_id: str) -> str:
        return f"native:{kind}:{native_id}"

    @staticmethod
    def _icon(value: object) -> dict[str, str] | None:
        if not isinstance(value, dict):
            return None
        kind = value.get("kind")
        if kind == "icon" and isinstance(value.get("icon"), str):
            return {"kind": "symbol", "value": value["icon"]}
        if kind == "emoji" and isinstance(value.get("emoji"), str):
            return {"kind": "emoji", "value": value["emoji"]}
        icon = value.get("value", value.get("icon"))
        if kind not in {"svg", "symbol"} or not isinstance(icon, str):
            return None
        return {"kind": kind, "value": icon}

    @staticmethod
    def _error_snapshot(error: str, provenance: dict[str, Any], account_selection: dict[str, Any]) -> dict[str, Any]:
        return {"sections": [], "projects": [], "sessions": [],
                "status": {"state": "error", "source": "codex", "truncated": False,
                           "session_limit": NativeCatalog._MAX_SESSIONS, "errors": [error],
                           "provenance": provenance, "account_selection": account_selection}}
