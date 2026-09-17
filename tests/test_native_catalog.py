"""原生 Codex 只读目录的合成数据测试。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.native_catalog import NativeCatalog
from codex_workbench.native_sessions import name_token_matches


class NativeCatalogTests(unittest.TestCase):
    """验证白名单、归属优先级、截断和格式错误可见性。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self._write_state()
        self.database = self.home / "state_1.sqlite"
        self._database()

    def test_revision_ignores_window_noise_but_tracks_business_changes(self):
        source=NativeCatalog(self.home)
        before=source.revision()
        self._change_state(lambda value:value.update({'window-size':{'width':1000}}))
        self.assertEqual(before,source.revision())
        with sqlite3.connect(self.database) as db:db.execute("UPDATE threads SET first_user_message='unrelated body', updated_at=updated_at+100")
        self.assertEqual(before,source.revision())
        self._change_state(lambda value:value['project-appearances'].update({'project-a':{'color':'pink'}}))
        self.assertNotEqual(before,source.revision());before=source.revision()
        with sqlite3.connect(self.database) as db:db.execute("UPDATE threads SET title='changed' WHERE id='thread-assigned'")
        self.assertNotEqual(before,source.revision())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _change_state(self, change) -> None:
        path = self.home / ".codex-global-state.json"
        payload = json.loads(path.read_text())
        change(payload)
        path.write_text(json.dumps(payload))

    def _session(self, thread_id: str = "thread-assigned", **kwargs) -> dict:
        snapshot = NativeCatalog(self.home).snapshot(**kwargs)
        self.assertEqual("ok", snapshot["status"]["state"], snapshot["status"])
        return next(item for item in snapshot["sessions"] if item["native_id"] == thread_id)

    def _write_state(self, *, multiple_accounts: bool = False) -> None:
        custom = {"account-current": {"sectionOrder": ["focus"], "sections": [
            {"id": "focus", "name": "聚焦", "itemKeys": ["codex:project:project-a"],
             "appearance": {"color": "blue", "marker": {"kind": "symbol", "icon": "target"}}}
        ]}}
        if multiple_accounts:
            custom["account-other"] = {"sectionOrder": [], "sections": []}
        payload = {
            "local-projects": {
                "project-a": {"id": "project-a", "name": "项目 A", "rootPaths": ["/work/a"], "createdAt": 1, "updatedAt": 2},
                "project-b": {"id": "project-b", "name": "项目 B", "rootPaths": ["/work/a/nested"], "createdAt": 1, "updatedAt": 2},
            },
            "project-order": ["project-b", "project-a"],
            "thread-project-assignments": {"thread-assigned": {"projectId": "project-a", "projectKind": "local"}},
            "project-appearances": {"project-a": {"color": "red", "marker": {"kind": "icon", "icon": "graduation-cap"}}, "project-b": {}},
            "electron-persisted-atom-state": {"mcp-extension-sidebar-catalog": {"accountId": "account-current"},
                                                 "unified-sidebar-pinned-order-v1": ["codex:project:project-b"],
                                                 "sidebar-custom-sections-v3": custom},
        }
        (self.home / ".codex-global-state.json").write_text(json.dumps(payload), encoding="utf-8")

    def _database(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL, cwd TEXT NOT NULL, archived INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, project_id TEXT, thread_section_id TEXT, is_pinned INTEGER NOT NULL DEFAULT 0, first_user_message TEXT, thread_source TEXT, name TEXT)")
        connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            ("thread-assigned", "显式归属", "/work/a/nested/worktree", 0, 1, 5, None, "focus", 0, "never selected", None, None),
            ("thread-path", "路径归属", "/work/a/nested/worktree", 0, 2, 4, None, None, 1, "never selected", None, None),
            ("thread-inherits", "继承项目分区", "/work/a/worktree", 0, 3, 3, None, None, 0, "never selected", None, None),
            ("thread-pinned-project", "继承置顶项目", "/work/a/nested/worktree", 0, 4, 2, None, None, 0, "never selected", None, None),
            ("thread-subagent", "内部子线程", "/work/a", 0, 5, 6, None, None, 0, "never selected", "subagent", None),
            ("thread-archived", "已归档", "/work/a", 1, 3, 6, None, None, 0, "never selected", None, None),
        ])
        connection.commit()
        connection.close()

    def test_snapshot_projects_sections_and_sessions_use_only_native_metadata(self) -> None:
        snapshot = NativeCatalog(self.home).snapshot()
        self.assertEqual("ok", snapshot["status"]["state"])
        self.assertEqual(["native:project:project-b", "native:project:project-a"], [item["id"] for item in snapshot["projects"]])
        self.assertEqual({"native:section:focus"}, {item["id"] for item in snapshot["sections"]})
        project_a = next(item for item in snapshot["projects"] if item["id"] == "native:project:project-a")
        project_b = next(item for item in snapshot["projects"] if item["id"] == "native:project:project-b")
        self.assertEqual("native:section:focus", project_a["section_id"])
        self.assertIsNone(project_b["section_id"])
        self.assertEqual({"kind": "symbol", "value": "graduation-cap"}, project_a["icon"])
        assigned = next(item for item in snapshot["sessions"] if item["native_id"] == "thread-assigned")
        by_path = next(item for item in snapshot["sessions"] if item["native_id"] == "thread-path")
        inherits = next(item for item in snapshot["sessions"] if item["native_id"] == "thread-inherits")
        inherits_pinned = next(item for item in snapshot["sessions"] if item["native_id"] == "thread-pinned-project")
        self.assertEqual("native:project:project-a", assigned["project_id"])
        self.assertEqual("native:project:project-b", by_path["project_id"])
        self.assertEqual("native:section:focus", assigned["section_id"])
        self.assertIsNone(by_path["section_id"])
        self.assertEqual("native:section:focus", inherits["section_id"])
        self.assertIsNone(inherits_pinned["section_id"])
        self.assertNotIn("state", assigned)
        self.assertEqual("unarchived", assigned["native_status"])
        self.assertNotIn("can_run", assigned)
        self.assertNotIn("tasks", snapshot)
        self.assertTrue(project_b["is_pinned"])
        self.assertTrue(by_path["is_pinned"])
        self.assertFalse(inherits_pinned["is_pinned"])
        self.assertNotIn("first_user_message", assigned)
        self.assertNotIn("thread-subagent", {item["native_id"] for item in snapshot["sessions"]})

    def test_native_emoji_marker_uses_emoji_field(self) -> None:
        self.assertEqual({"kind": "emoji", "value": "🧪"}, NativeCatalog._icon({"kind": "emoji", "emoji": "🧪"}))

    def test_native_titles_are_display_safe_without_returning_original_text(self) -> None:
        raw_title = "请修复任务列表，token=synthetic-secret-value https://example.test/a?key=synthetic"
        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE threads SET title = ? WHERE id = ?", (raw_title, "thread-assigned"))
        connection.commit()
        connection.close()
        task = next(item for item in NativeCatalog(self.home).snapshot()["sessions"] if item["native_id"] == "thread-assigned")
        self.assertEqual("修复任务列表", task["title"])
        self.assertNotIn("synthetic-secret-value", task["title"])
        self.assertNotEqual(raw_title, task["title"])
        self.assertNotIn("source_title", task)
        self.assertNotIn("original_title", task)

    def test_native_custom_name_precedes_original_prompt_title(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE threads SET title=?,name=? WHERE id=?", ("原始首条提示词", "用户改名后的会话", "thread-assigned"))
        connection.commit()
        connection.close()
        task = next(item for item in NativeCatalog(self.home).snapshot()["sessions"] if item["native_id"] == "thread-assigned")
        self.assertEqual("用户改名后的会话", task["title"])

    def test_native_title_falls_back_when_custom_name_is_missing_or_empty(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE threads SET title=?,name=? WHERE id=?", ("无名称回退", None, "thread-assigned"))
        connection.execute("UPDATE threads SET title=?,name=? WHERE id=?", ("空名称回退", "", "thread-path"))
        connection.commit()
        connection.close()
        tasks = {item["native_id"]: item for item in NativeCatalog(self.home).snapshot()["sessions"]}
        self.assertEqual("无名称回退", tasks["thread-assigned"]["title"])
        self.assertEqual("空名称回退", tasks["thread-path"]["title"])

    def test_ambiguous_account_sections_are_an_error_not_an_empty_catalog(self) -> None:
        self._write_state(multiple_accounts=True)
        payload = json.loads((self.home / ".codex-global-state.json").read_text())
        payload["electron-persisted-atom-state"]["mcp-extension-sidebar-catalog"] = {}
        (self.home / ".codex-global-state.json").write_text(json.dumps(payload))
        snapshot = NativeCatalog(self.home).snapshot()
        self.assertEqual("error", snapshot["status"]["state"])
        self.assertEqual(["ambiguous_sidebar_account"], snapshot["status"]["errors"])

    def test_explicit_current_account_overrides_stale_sidebar_cache(self) -> None:
        self._write_state(multiple_accounts=True)
        payload = json.loads((self.home / ".codex-global-state.json").read_text())
        payload["electron-persisted-atom-state"]["sidebar-custom-sections-v3"]["account-other"] = {
            "sectionOrder": ["other"], "sections": [{"id": "other", "name": "其他", "itemKeys": ["codex:project:project-b"], "appearance": {}}]
        }
        (self.home / ".codex-global-state.json").write_text(json.dumps(payload))
        snapshot = NativeCatalog(self.home).snapshot(current_account_id="account-other")
        project_b = next(item for item in snapshot["projects"] if item["id"] == "native:project:project-b")
        self.assertEqual("native:section:other", project_b["section_id"])
        self.assertIn("native:section:other", {item["id"] for item in snapshot["sections"]})

    def test_stale_mcp_and_inline_accounts_cannot_choose_between_account_sections(self) -> None:
        self._write_state(multiple_accounts=True)
        self._change_state(lambda state: state["electron-persisted-atom-state"].update({
            "inline-sidebar-customization-state-by-account-id-v1": {"orderByAccountId": {"account-current": []}}
        }))
        snapshot = NativeCatalog(self.home).snapshot()
        self.assertEqual(["ambiguous_sidebar_account"], snapshot["status"]["errors"])
        self.assertEqual({"method": "unresolved", "verified": False}, snapshot["status"]["account_selection"])

    def test_explicit_account_without_custom_sections_is_valid_and_not_replaced(self) -> None:
        self._write_state(multiple_accounts=True)
        snapshot = NativeCatalog(self.home).snapshot(current_account_id="account-new")
        self.assertEqual("ok", snapshot["status"]["state"])
        self.assertEqual([], snapshot["sections"])
        self.assertTrue(all(item["section_id"] is None for item in snapshot["projects"]))
        self.assertTrue(all(item["section_id"] is None for item in snapshot["sessions"]))
        self.assertEqual({"method": "explicit", "verified": True}, snapshot["status"]["account_selection"])
        self.assertNotIn("account-new", json.dumps(snapshot))

    def test_provenance_does_not_claim_shared_threads_belong_to_sidebar_account(self) -> None:
        snapshot = NativeCatalog(self.home).snapshot()
        self.assertEqual({"method": "single_sidebar_account", "verified": False}, snapshot["status"]["account_selection"])
        self.assertEqual(self.home.as_posix(), snapshot["status"]["provenance"]["directory"])
        self.assertEqual("state_1.sqlite", snapshot["status"]["provenance"]["database"])
        self.assertEqual("unverified", snapshot["status"]["provenance"]["window_binding"])
        self.assertEqual("unknown", snapshot["status"]["provenance"]["thread_account_ownership"])
        for session in snapshot["sessions"]:
            self.assertEqual("unknown", session["provenance"]["account_ownership"])
            self.assertNotIn("execution_account_id", session)
        self.assertNotIn("account-current", json.dumps(snapshot))

    def test_explicit_unassignment_or_unresolved_project_never_falls_back_to_cwd(self) -> None:
        for assignment, source in ((None, "explicit_unassigned"), ({"projectId": None}, "explicit_unassigned"),
                                   ({"projectId": "deleted"}, "unresolved_assignment"),
                                   ({"projectId": "project-a", "projectKind": "remote"}, "unresolved_assignment")):
            with self.subTest(assignment=assignment):
                self._change_state(lambda state: state["thread-project-assignments"].update({"thread-assigned": assignment}))
                session = self._session()
                self.assertIsNone(session["project_id"])
                self.assertEqual(source, session["provenance"]["project"])

    def test_shared_deepest_root_is_unassigned_instead_of_lexical_project_choice(self) -> None:
        self._change_state(lambda state: state["local-projects"]["project-a"].update({"rootPaths": ["/work/a/nested"]}))
        session = self._session("thread-path")
        self.assertIsNone(session["project_id"])
        self.assertEqual("ambiguous_cwd", session["provenance"]["project"])
        self.assertEqual("native:project:project-a", self._session()["project_id"])

    def test_missing_explicit_thread_project_does_not_get_reassigned_by_cwd(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE threads SET project_id='deleted' WHERE id='thread-path'")
        session = self._session("thread-path")
        self.assertIsNone(session["project_id"])
        self.assertEqual("unresolved_thread_project", session["provenance"]["project"])

    def test_pinned_project_and_thread_keep_their_custom_section(self) -> None:
        def change(state):
            electron = state["electron-persisted-atom-state"]
            electron["unified-sidebar-pinned-order-v1"].append("codex:thread:thread-path")
            electron["sidebar-custom-sections-v3"]["account-current"]["sections"][0]["itemKeys"].append("codex:project:project-b")
        self._change_state(change)
        snapshot = NativeCatalog(self.home).snapshot()
        project = next(p for p in snapshot["projects"] if p["id"] == "native:project:project-b")
        self.assertEqual("native:section:focus", project["section_id"])
        self.assertTrue(project["is_pinned"])
        self.assertEqual("native:section:focus", self._session("thread-path")["section_id"])
        self.assertTrue(self._session("thread-path")["is_pinned"])
        self.assertFalse(self._session("thread-pinned-project")["is_pinned"])

    def test_thread_section_and_host_alias_override_project_section(self) -> None:
        def change(state):
            state["electron-persisted-atom-state"]["sidebar-custom-sections-v3"]["account-current"]["sections"].append({
                "id": "thread-only", "name": "会话分区", "hostSectionIds": {"local": "server-section"},
                "itemKeys": ["codex:thread:thread-assigned"]})
        self._change_state(change)
        self.assertEqual("native:section:thread-only", self._session()["section_id"])
        self.assertEqual("sidebar_thread_assignment", self._session()["provenance"]["section"])
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE threads SET thread_section_id='server-section' WHERE id='thread-inherits'")
        self.assertEqual("native:section:thread-only", self._session("thread-inherits")["section_id"])

    def test_conflicting_section_membership_is_explicit_error(self) -> None:
        self._change_state(lambda state: state["electron-persisted-atom-state"]["sidebar-custom-sections-v3"]["account-current"]["sections"].append({
            "id": "conflict", "name": "重复归属", "itemKeys": ["codex:project:project-a"]}))
        snapshot = NativeCatalog(self.home).snapshot()
        self.assertEqual(["ambiguous_sidebar_section_assignment"], snapshot["status"]["errors"])

    def test_source_json_subagents_are_filtered_before_session_limit(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("ALTER TABLE threads ADD COLUMN source TEXT")
            connection.execute("UPDATE threads SET thread_source=NULL, source=? WHERE id='thread-subagent'", (json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "synthetic"}}}),))
            connection.execute("UPDATE threads SET source='vscode' WHERE id='thread-assigned'")
        with patch.object(NativeCatalog, "_MAX_SESSIONS", 4):
            snapshot = NativeCatalog(self.home).snapshot()
        self.assertEqual(4, len(snapshot["sessions"]))
        self.assertFalse(snapshot["status"]["truncated"])
        self.assertNotIn("thread-subagent", {item["native_id"] for item in snapshot["sessions"]})
        self.assertTrue(all("source" not in item["provenance"] for item in snapshot["sessions"]))

    def test_official_name_and_execution_preferences_are_preserved(self) -> None:
        official_name = "一份由用户明确命名且不应该被默认任务标题长度机械裁剪的原生会话名称，用于完整核对原生会话与工作台会话显示的一致性"
        with sqlite3.connect(self.database) as connection:
            connection.execute("ALTER TABLE threads ADD COLUMN model TEXT")
            connection.execute("ALTER TABLE threads ADD COLUMN reasoning_effort TEXT")
            connection.execute("UPDATE threads SET name=?,model='synthetic-model',reasoning_effort='high' WHERE id='thread-assigned'", (official_name,))
        session = self._session()
        self.assertEqual(official_name, session["title"])
        self.assertEqual("synthetic-model", session["model"])
        self.assertEqual("high", session["effort"])

    def test_database_errors_are_safe_fixed_codes(self) -> None:
        with patch("codex_workbench.native_catalog.sqlite3.connect", side_effect=sqlite3.OperationalError("synthetic private account and database failure")):
            snapshot = NativeCatalog(self.home).snapshot()
        self.assertEqual(["unreadable_native_state_database"], snapshot["status"]["errors"])
        self.assertNotIn("synthetic private account", json.dumps(snapshot))

    def test_bound_sessions_distinguish_archived_missing_and_unarchived(self) -> None:
        """绑定读取依据精确 ID，归档及内部线程不会被列表过滤器误判为缺失。"""
        identities = ["thread-assigned", "thread-archived", "thread-subagent", "thread-missing"]
        bound = NativeCatalog(self.home).read_bound_sessions(identities)
        self.assertEqual(set(identities), set(bound))
        self.assertEqual("unarchived", bound["thread-assigned"]["native_status"])
        self.assertEqual("archived", bound["thread-archived"]["native_status"])
        self.assertEqual("unarchived", bound["thread-subagent"]["native_status"])
        self.assertEqual({"native_id": "thread-missing", "native_status": "missing"}, bound["thread-missing"])
        self.assertEqual("native:project:project-a", bound["thread-assigned"]["project_id"])

    def test_bound_sessions_report_unavailable_when_database_cannot_be_read(self) -> None:
        """读取失败时，已知及未知 ID 都不能被推断为真实缺失。"""
        identities = ["thread-assigned", "thread-archived", "thread-missing"]
        with patch("codex_workbench.native_catalog.sqlite3.connect", side_effect=sqlite3.OperationalError("synthetic private database error")):
            bound = NativeCatalog(self.home).read_bound_sessions(identities)
        self.assertEqual({identity: {"native_id": identity, "native_status": "unavailable"}
                          for identity in identities}, bound)
        self.assertNotIn("synthetic private", json.dumps(bound))

    def test_bound_sessions_survive_unreadable_global_sidebar(self) -> None:
        """侧栏格式损坏只影响归属投影，不妨碍数据库确认存在与归档状态。"""
        (self.home / ".codex-global-state.json").write_text("{synthetic invalid sidebar", encoding="utf-8")
        catalog = NativeCatalog(self.home)
        self.assertEqual("error", catalog.snapshot()["status"]["state"])
        bound = catalog.read_bound_sessions(["thread-assigned", "thread-archived", "thread-missing"])
        self.assertEqual("unarchived", bound["thread-assigned"]["native_status"])
        self.assertEqual("archived", bound["thread-archived"]["native_status"])
        self.assertEqual("missing", bound["thread-missing"]["native_status"])
        self.assertNotIn("project_id", bound["thread-assigned"])
        self.assertNotIn("synthetic invalid sidebar", json.dumps(bound))

    def test_bound_session_outside_snapshot_limit_remains_unarchived(self) -> None:
        """最近会话截断不能改变已绑定旧会话的真实归档状态。"""
        with patch.object(NativeCatalog, "_MAX_SESSIONS", 1):
            catalog = NativeCatalog(self.home)
            snapshot = catalog.snapshot()
            bound = catalog.read_bound_sessions(["thread-pinned-project"])
        self.assertTrue(snapshot["status"]["truncated"])
        self.assertNotIn("thread-pinned-project", {item["native_id"] for item in snapshot["sessions"]})
        self.assertEqual("unarchived", bound["thread-pinned-project"]["native_status"])

    def test_bound_reads_parameterize_only_requested_ids_and_metadata_columns(self) -> None:
        """绑定查询不得加载无关归档行、消息列或 rollout 文件。"""
        rollout = self.home / "rollout-synthetic.jsonl"
        rollout.write_text("synthetic rollout body must never be read", encoding="utf-8")
        with sqlite3.connect(self.database) as connection:
            connection.execute("ALTER TABLE threads ADD COLUMN rollout_path TEXT")
            connection.execute("UPDATE threads SET rollout_path=?", (str(rollout),))
            # 无关归档行的非法元数据使意外扩大读取范围直接暴露为测试失败。
            connection.execute("UPDATE threads SET title=? WHERE id='thread-archived'", (sqlite3.Binary(b"unrelated archived row"),))
        selected = []
        connect = sqlite3.connect
        path_open = Path.open

        class TracedConnection(sqlite3.Connection):
            """保留真实临时数据库行为，并记录参数化语句。"""

            def execute(self, sql, parameters=()):
                if sql.startswith("SELECT "):
                    selected.append((sql, parameters))
                return super().execute(sql, parameters)

        def traced_connect(*args, **kwargs):
            return connect(*args, factory=TracedConnection, **kwargs)

        def metadata_open(path, *args, **kwargs):
            self.assertEqual(self.home / ".codex-global-state.json", path)
            return path_open(path, *args, **kwargs)

        identities = ["thread-assigned", "thread-missing') OR 1=1 --"]
        with patch("codex_workbench.native_catalog.sqlite3.connect", side_effect=traced_connect), \
                patch.object(Path, "open", metadata_open):
            bound = NativeCatalog(self.home).read_bound_sessions(identities)
        self.assertEqual(set(identities), set(bound))
        self.assertEqual("unarchived", bound[identities[0]]["native_status"])
        self.assertEqual("missing", bound[identities[1]]["native_status"])
        self.assertEqual(1, len(selected))
        query, parameters = selected[0]
        fields, where = query.removeprefix("SELECT ").split(" FROM threads ", 1)
        allowed = {"id", "title", "cwd", "archived", "created_at", "updated_at", "project_id",
                   "thread_section_id", "is_pinned", "model", "reasoning_effort", "name"}
        self.assertTrue(set(fields.split(", ")).issubset(allowed), fields)
        self.assertEqual("WHERE id IN (?,?)", where)
        self.assertEqual(tuple(identities), parameters)
        serialized = json.dumps(bound)
        for forbidden in ("first_user_message", "rollout_path", "synthetic rollout body", "never selected", "unrelated archived row"):
            self.assertNotIn(forbidden, serialized)

    def test_bound_and_list_names_share_raw_name_tokens_without_leaking_names(self) -> None:
        """同一原生名称在列表和绑定读取中使用同一令牌，净化不能掩盖并发变化。"""
        identity = "thread-assigned"
        names = ("请修复任务列表，token=synthetic-first-value", "请修复任务列表，token=synthetic-second-value")
        observations = []
        for raw_name in names:
            with sqlite3.connect(self.database) as connection:
                connection.execute("UPDATE threads SET name=? WHERE id=?", (raw_name, identity))
            listed = self._session(identity)
            bound = NativeCatalog(self.home).read_bound_sessions([identity])[identity]
            self.assertEqual(listed["name_token"], bound["name_token"])
            self.assertTrue(name_token_matches(bound["name_token"], identity, raw_name))
            self.assertNotIn(raw_name, json.dumps([listed, bound], ensure_ascii=False))
            self.assertNotIn(raw_name.split("token=")[1], json.dumps([listed, bound]))
            self.assertNotIn("name", bound)
            observations.append(bound)
        self.assertEqual(observations[0]["title"], observations[1]["title"])
        self.assertNotEqual(observations[0]["name_token"], observations[1]["name_token"])
        self.assertFalse(name_token_matches(observations[0]["name_token"], identity, names[1]))

    def test_bound_name_token_does_not_use_fallback_title_for_null_name(self) -> None:
        """没有原生命名时，展示标题变化不能伪装成原始 name 变化。"""
        identity = "thread-assigned"
        catalog = NativeCatalog(self.home)
        before = catalog.read_bound_sessions([identity])[identity]
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE threads SET title='合成的新展示标题' WHERE id=?", (identity,))
        after = catalog.read_bound_sessions([identity])[identity]
        self.assertNotEqual(before["title"], after["title"])
        self.assertEqual(before["name_token"], after["name_token"])
        self.assertTrue(name_token_matches(after["name_token"], identity, None))

    def test_title_body_and_source_columns_are_never_selected(self) -> None:
        selected = []
        connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(selected.append)
            return connection
        with patch("codex_workbench.native_catalog.sqlite3.connect", side_effect=traced_connect):
            self._session()
        query = next(sql for sql in selected if sql.startswith("SELECT "))
        self.assertNotIn("first_user_message", query)
        self.assertNotIn("SELECT *", query)

    def test_missing_required_thread_column_is_an_explicit_error(self) -> None:
        self.database.unlink()
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE threads (id TEXT, cwd TEXT, archived INTEGER, created_at INTEGER, updated_at INTEGER)")
        connection.commit()
        connection.close()
        snapshot = NativeCatalog(self.home).snapshot()
        self.assertEqual("error", snapshot["status"]["state"])
        self.assertEqual(["unsupported_native_thread_schema"], snapshot["status"]["errors"])

    def test_session_limit_sets_truncated_without_silently_dropping_the_signal(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (f"thread-{index}", f"任务 {index}", "/work/a", 0, index + 10, index + 10, None, None, 0, "never selected", None, None)
            for index in range(2001)
        ])
        connection.commit()
        connection.close()
        snapshot = NativeCatalog(self.home).snapshot()
        self.assertEqual(2000, len(snapshot["sessions"]))
        self.assertTrue(snapshot["status"]["truncated"])

    def test_highest_numbered_state_database_is_used_without_reading_older_schema(self) -> None:
        old = self.home / "state_1.sqlite"
        self.database.rename(old)
        connection = sqlite3.connect(old)
        connection.execute("DROP TABLE threads")
        connection.execute("CREATE TABLE threads (id TEXT)")
        connection.commit()
        connection.close()
        self.database = self.home / "state_2.sqlite"
        self._database()
        snapshot = NativeCatalog(self.home).snapshot()
        self.assertEqual("ok", snapshot["status"]["state"])
        self.assertEqual(4, len(snapshot["sessions"]))


if __name__ == "__main__":
    unittest.main()
