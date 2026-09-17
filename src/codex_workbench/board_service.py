"""任务看板的按需操作层，与助手和凭证的配置读取隔离。"""
from __future__ import annotations
import hashlib,json,os,uuid
from pathlib import Path
from .catalog import validate,VERSION
from .store import StoreError
from .executor import CodexExecutor
from .runner import Runner
from .titles import suggest_title,safe_display_title
from .native_catalog import NativeCatalog
from .native_sessions import NativeSessionClient,name_token,name_token_matches
from .title_generator import ModelTitleGenerator
from .workspace_picker import choose_workspace
from .capability_manifest import create_capability_manifest,read_capability_events,revoke_capability_manifest

BOARD_TOOLS = frozenset(['board_state', 'project_create', 'run_cancel', 'section_create', 'session_create', 'session_detail', 'session_update', 'task_create', 'task_detail', 'task_start', 'task_update', 'workspace_choose'])

class BoardOperations:
    """只在进入看板或明确调用看板工具时初始化会话目录和执行器。"""
    def _ensure_board(self):
        with self.lock:
            if self._closing:
                raise StoreError("unavailable","工作台正在关闭")
            if self._board_initialized:
                return
            options=self._board_options
            self.sessions=self.store.sessions
            self._view_defaults={}
            self.title_generator=options['title_generator'] or ModelTitleGenerator(self.data_dir,options['codex'],options['lease_fd'])
            self.native=options['native_catalog'] if options['native_catalog'] is not None else (None if options['injected_account'] else NativeCatalog(options['shared_config']))
            self._native={"sections":[],"projects":[],"sessions":[],"status":{"state":"disabled"}}
            self.native_session_factory=options['native_session_factory'] or (None if options['injected_account'] else lambda account:NativeSessionClient(options['codex'],account['codex_home'],options['lease_fd'],use_current_account=account['id']=='current'))
            self.runner=Runner(self.store,options['executor'] or CodexExecutor((options['codex'],),lease_fd=options['lease_fd']),prepare=self._prepare,native_attached=self._native_thread_attached)
            self.store.recover_interrupted()
            self._board_initialized=True

    def board_state(self) -> dict:
        """读取工作台事实，不通过后台登录切换获取未授权账户信息。"""
        self._ensure_board()
        current = self.accounts.current() if hasattr(self.accounts, "current") else None
        self._refresh_native(current)
        accounts = self._account_views()
        self._view_defaults = (current or {}).get("taskDefaults", {})
        tasks=self.store.list_tasks()
        self._run_summaries=self.store.task_run_summaries()
        own_tasks = [self._task_view(t, summary=True) for t in tasks]
        return {"sections": self._sections(), "projects": self._projects(),
                "agents": [self._agent_view(a) for a in self.store.list_agents()],
                "tasks": own_tasks, "sessions": [session for session in self._session_views() if session.get("native_status") != "missing"], "accounts": accounts,
                "ready_accounts": [a for a in accounts if a.get("login", {}).get("status") == "ready"],
                "current_account_id": "current" if current else None,
                "current_account_source": "official_cli_default_home" if current else None,
                "task_scope": "registered_business_tasks",
                "taskDefaults": current["taskDefaults"] if current else {"model": None, "effort": None, "concurrency": 1, "sandbox": "read-only"},
                "models": current["models"] if current else [], "nativeStatus": self._native["status"],
                "provider_models": [self._model_view(m) for m in self.models.list_models()],
                "model_validation_mode": "manual_retry",
                "resourceRoot": str(self.resources_dir),
                "runtime": {"version": VERSION, "ui_revision": self.ui_release.revision, "syncStatus": self.sync_status}}

    def _ready_account(self, account_id: str, expected_subject: str | None = None) -> dict:
        if expected_subject is None:
            self.accounts.require_ready(account_id)
        else:
            self.accounts.require_ready(account_id, expected_subject=expected_subject)
        value = self.accounts.status(account_id, refresh=False)
        if value.get("account", {}).get("id", account_id) != account_id:
            raise StoreError("account_identity", "无法确认所选账户身份")
        if value.get("login", {}).get("status") != "ready":
            raise StoreError("login_required", "所选账户尚未确认登录")
        saved = next((a for a in self.store.list_execution_accounts() if a["id"] == account_id), None)
        if saved is None:
            raise StoreError("account_required", "所选账户不可用")
        subject = value.get("identity_id") or value.get("account", {}).get("subject_id") or saved.get("subject_id")
        if not subject:
            raise StoreError("account_identity", "无法确认所选账户身份")
        if expected_subject is not None and subject != expected_subject:
            raise StoreError("account_changed", "会话绑定的账户身份已改变")
        if saved.get("subject_id") != subject:
            saved = self.store.record_account_subject(account_id, subject)
        return {**saved, "subject_id": subject}


    def _select_account(self, account_id=None, session=None, allow_none=False) -> dict | None:
        if session:
            bound=session.get("thread_account_id") or (session.get("execution_account_id") if session.get("native_thread_id") else None)
            subject=session.get("thread_account_subject") or session.get("account_subject")
            if bound and account_id and bound!=account_id:
                raise StoreError("session_account_history", "此会话已有其他账户的执行历史，请为新账户新建会话")
            selected=account_id or session.get("execution_account_id")
            if selected:return self._ready_account(selected,subject if selected==bound else None)
        if account_id:return self._ready_account(account_id)
        available=[a for a in self._account_views() if a.get("login",{}).get("status")=="ready"]
        selected=next((a for a in available if a.get("isDefault")),None) or next((a for a in available if a["id"]=="current"),None) or (available[0] if available else None)
        if selected:return self._ready_account(selected["id"])
        if allow_none:return None
        raise StoreError("login_required", "请先登录可用于执行的账户")


    def _system_defaults(self) -> dict:
        if hasattr(self.accounts,"system_defaults"):
            result=self.accounts.system_defaults()
        else:
            current=self.accounts.current() if hasattr(self.accounts,"current") else {}
            result={"sandbox":"read-only","concurrency":1,"approval_policy":"never",**(current or {}).get("taskDefaults",{})}
        self._view_defaults=result
        return result


    def _preference_view(self, session: dict) -> dict:
        return {"model":session.get("model") or self._view_defaults.get("model"),
                "effort":session.get("effort") or self._view_defaults.get("effort"),
                "execution_account_id":session.get("execution_account_id")}


    def _resolve_task_preferences(self, session: dict, args: dict) -> dict:
        defaults=self._system_defaults()
        return {"model":args.get("model") or session.get("model") or defaults.get("model"),
                "effort":args.get("effort") or session.get("effort") or defaults.get("effort")}


    def _execution_context_changed(self, session: dict) -> bool:
        """原生目录变化必须先核对，不能先调用标题模型或派发业务执行。"""
        native = self._native_metadata(session.get("native_thread_id"))
        return bool(native and native.get("cwd") and os.path.realpath(native["cwd"]) != os.path.realpath(session["cwd"]))


    def _native_metadata(self, native_id: str | None) -> dict | None:
        """绑定 ID 精确查询优先于可能截断的列表；未观察到不推断为归档或缺失。"""
        return self._native.get("bound_sessions", {}).get(native_id) or next(
            (item for item in self._native["sessions"] if item["native_id"] == native_id), None)


    def _native_status(self, session: dict) -> str:
        """区分归档、精确查询缺失、读取失败与未观察状态，草稿无需原生记录。"""
        if not session.get("native_thread_id"):
            return "unbound"
        native = self._native_metadata(session["native_thread_id"])
        if native:
            return native.get("native_status", "unarchived")
        return "unavailable" if self._native["status"]["state"] == "error" else "not_observed"


    def _require_native_available(self, session: dict) -> None:
        """新增绑定、任务和执行须先确认未归档；已有历史和本机说明仍可读取编辑。"""
        status = self._native_status(session)
        messages = {"archived": "原生会话已归档，请在 Codex 中处理后刷新工作台",
                    "missing": "原生会话在绑定目录中已缺失，请核对后再继续",
                    "unavailable": "原生会话元数据读取失败，请稍后刷新重试",
                    "not_observed": "尚未确认原生会话状态，请刷新后再继续"}
        if status in messages:
            raise StoreError("native_" + status, messages[status])


    def _session_view(self, session: dict) -> dict:
        """导航随原生会话移动；已保存的账户、工作目录和执行偏好保持原快照。"""
        project = self.store.get_project(session["project_id"])
        public_project = None if project["is_workspace"] else project.get("native_id") or project["id"]
        native = self._native_metadata(session.get("native_thread_id"))
        # 工作台自建项目尚未登记为原生项目时，原生空归属不是解除本地关联。
        # 原生已有明确归属或原项目本身来自 Codex 时，仍以原生导航为准。
        native_navigation = bool(native and "project_id" in native and (native.get("project_id") or native.get("section_id") or project.get("native_id") or project["is_workspace"]))
        native_project_navigation = native_navigation and bool(native.get("project_id") or project.get("native_id") or project["is_workspace"])
        section_id = native.get("section_id") if native_navigation else session.get("section_id")
        section = next((s for s in self._sections() if s["id"] == section_id), None)
        native_project = next((p for p in self._native["projects"] if native and p["id"] == native.get("project_id")), None)
        project_name = (native_project or {}).get("name") if native_project_navigation else (None if project["is_workspace"] else project["name"])
        status = self._native_status(session)
        presence = "observed" if status == "unarchived" else status
        return {**session, **self._preference_view(session), "project_id": native.get("project_id") if native_project_navigation else public_project,
                "project_name": project_name, "section_id": section_id,
                "section_name": section["name"] if section else None, "native_id": session.get("native_thread_id"),
                "title": native["title"] if native and "title" in native else safe_display_title(session["title"], 300),
                "name_token": (native or {}).get("name_token") if session.get("native_thread_id") else name_token(session["id"], session["title"]),
                "native_status": status, "can_create_task": status in {"unbound", "unarchived"} and not self._execution_context_changed(session),
                "native_name_concurrency": "observed_check_only" if session.get("native_thread_id") else "local_version",
                "navigation_source": "codex" if native_navigation else "saved_binding", "native_presence": presence,
                "native_runtime_status": "unknown", "count_scope": "workbench_tasks",
                "execution_context_changed": self._execution_context_changed(session),
                "native_url": "codex://threads/" + session["native_thread_id"] if session.get("native_thread_id") else None}


    def _native_session(self, native: dict) -> dict:
        project = next((p for p in self._native["projects"] if p["id"] == native.get("project_id")), None)
        section_id = native.get("section_id")
        section = next((s for s in self._sections() if s["id"] == section_id), None)
        return {**self._preference_view(native), "id": "native:" + native["native_id"], "title": native["title"], "description": "", "source": "codex", "version": 1,
                "native_id": native["native_id"], "native_thread_id": native["native_id"], "native_url": "codex://threads/" + native["native_id"],
                "project_id": native.get("project_id"), "project_name": project["name"] if project else None,
                "section_id": section_id, "section_name": section["name"] if section else None, "cwd": native.get("cwd") or (project or {}).get("cwd"),
                "execution_account_id": "current", "thread_account_id": None, "account_subject": None,
                "thread_account_ownership": "unknown", "execution_account_source": "catalog_default",
                "navigation_source": "codex", "native_presence": "observed" if native.get("native_status", "unarchived") == "unarchived" else native["native_status"],
                "native_status": native.get("native_status", "unarchived"), "name_token": native.get("name_token"),
                "can_create_task": native.get("native_status", "unarchived") == "unarchived", "native_name_concurrency": "observed_check_only",
                "native_runtime_status": "unknown", "count_scope": "workbench_tasks",
                "created_at": native.get("created_at"), "updated_at": native.get("updated_at"), "task_count": 0, "running_count": 0}


    def _session_name_view(self, session: dict) -> dict:
        """详情缺少原始名称令牌时只读该绑定 ID；不把本机显示标题当作已观察原名。

        不请求轮次历史，不保存读回原名。RPC 摘要可能含 preview，由客户端丢弃。
        名称读取失败保留详情与业务历史，页面应提示刷新而非猜测写入前提。
        """
        if not session.get("native_thread_id") or session.get("name_token"):
            return session
        if self.native_session_factory:
            try:
                account = self._ready_account(
                    session.get("thread_account_id") or session.get("execution_account_id") or "current",
                    session.get("thread_account_subject") or session.get("account_subject"),
                )
                client = self.native_session_factory(account)
                try:
                    observed = client.read(session["native_thread_id"])
                finally:
                    client.close()
                return {**session, "title": safe_display_title(observed["name"], 300) if observed["name"] and observed["name"].strip() else session["title"],
                        "name_token": name_token(session["native_thread_id"], observed["name"])}
            except (ValueError, OSError):
                pass
        return {**session, "name_token_error": "native_name_unavailable"}


    def _session_views(self) -> list[dict]:
        result = {"native:" + s["native_id"]: self._native_session(s) for s in self._native["sessions"]}
        for session in self.sessions.list():
            if session.get("native_thread_id"):
                result.pop("native:" + session["native_thread_id"], None)
            result[session["id"]] = self._session_view(session)
        return list(result.values())


    def _find_session(self, identity: str) -> dict:
        try:
            return self.sessions.get(identity)
        except ValueError:
            native = next((s for s in self._native["sessions"] if "native:" + s["native_id"] == identity), None)
            if native is None:
                raise StoreError("not_found", "会话不存在或当前不可读取") from None
            return self._native_session(native)


    def _bind_session(self, identity: str, account: dict) -> dict:
        candidate = self._find_session(identity)
        self._require_native_available(candidate)
        try:
            saved = self.sessions.get(identity)
        except ValueError:
            saved = None
        if saved and self._execution_context_changed(saved):
            raise StoreError("native_context_changed", "原生会话工作目录已变化，请核对执行目录后再继续")
        if candidate["source"] == "codex":
            if account["id"] != "current":
                raise StoreError("session_boundary", "原生会话需使用当前 Codex 登录账户")
            raw = next((s for s in self._native["sessions"] if "native:" + s["native_id"] == identity), None)
            view = self._native_session(raw) if raw else self._session_view(candidate)
            if saved:
                candidate = saved
            else:
                project = self._project_for_create(view.get("project_id"))
                candidate = self.sessions.ensure_native(view["native_id"], project["id"], view.get("section_id"), view["cwd"], account["id"], account["subject_id"], view["title"])
        if candidate.get("native_thread_id") and ((candidate.get("thread_account_id") or candidate.get("execution_account_id")) != account["id"] or (candidate.get("thread_account_subject") or candidate.get("account_subject")) != account["subject_id"]):
            raise StoreError("session_boundary", "会话与执行账户不一致")
        return candidate


    def _create_session(self, args: dict, account: dict | None = None) -> dict:
        project = self._project_for_create(args.get("project_id"))
        section_id = args.get("section_id", project.get("section_id"))
        if not project["is_workspace"] and section_id != project.get("section_id"):
            raise StoreError("validation", "项目不属于所选分区")
        self._section(section_id)
        return self.sessions.create_draft(project["id"], section_id, project["cwd"], args.get("title") or "新会话", args.get("description") or "")


    def _refresh_native(self, current=None) -> None:
        """按已保存的线程目录读取绑定元数据，不用默认目录缺席推定独立账户线程缺失。"""
        if self.native:
            account_id = current.get("identity_id") if current else None
            snapshot = self.native.snapshot(current_account_id=account_id)
            reader = getattr(self.native, "read_bound_sessions", None)
            if reader:
                bound = [session for session in self.sessions.list() if session.get("native_thread_id")]
                if isinstance(self.native, NativeCatalog):
                    accounts = {account["id"]: account for account in self.store.list_execution_accounts()}
                    groups = {}
                    observations = {}
                    for session in bound:
                        binding = session.get("thread_account_id") or session.get("execution_account_id")
                        home = self.native.codex_home if binding == "current" else (accounts.get(binding) or {}).get("codex_home")
                        if not home:
                            observations[session["native_thread_id"]] = {"native_id": session["native_thread_id"], "native_status": "unavailable"}
                            continue
                        groups.setdefault(Path(home), []).append(session["native_thread_id"])
                    for home, ids in groups.items():
                        catalog = self.native if home == self.native.codex_home else NativeCatalog(home)
                        observations.update(catalog.read_bound_sessions(ids, current_account_id=account_id if catalog is self.native else None))
                    snapshot["bound_sessions"] = observations
                else:
                    snapshot["bound_sessions"] = reader([session["native_thread_id"] for session in bound], current_account_id=account_id)
            self._native = snapshot


    def _sections(self) -> list[dict]:
        return self._native["sections"] + [{**section, "source": "workbench"} for section in self.store.list_sections()]


    def _projects(self) -> list[dict]:
        return self._native["projects"] + [{**project, "source": "workbench"} for project in self.store.list_projects() if not project.get("native_id")]


    def _section(self, section_id: str | None) -> dict | None:
        if section_id is None:
            return None
        found = next((s for s in self._sections() if s["id"] == section_id), None)
        if found is None:
            raise StoreError("not_found", "所选分区不存在或暂时无法读取")
        return found


    def _project_for_create(self, project_id: str | None) -> dict:
        """原生项目只保存任务执行需要的引用；原生目录仍是权威来源。"""
        if not project_id:
            return self._workspace_project()
        if not project_id.startswith("native:"):
            return self.store.get_project(project_id)
        project = next((p for p in self._native["projects"] if p["id"] == project_id), None)
        if not project or not project.get("cwd"):
            raise StoreError("not_found", "所选原生项目或工作目录暂时不可用")
        existing = next((p for p in self.store.list_projects(include_workspace=True) if p.get("native_id") == project_id), None)
        if existing:
            return self.store.refresh_native_project(existing["id"], project["name"], project["cwd"], project["section_id"])
        return self.store.create_project(project["name"], project["cwd"], section_id=project["section_id"], native_id=project_id)


    def _workspace_project(self) -> dict:
        """给无项目任务提供明确的本机工作目录；内部记录不作为项目标签显示。"""
        for project in self.store.list_projects(include_workspace=True):
            if project["is_workspace"]:
                return project
        workspace = self.data_dir / "workspace"
        if workspace.is_symlink():
            raise StoreError("validation", "工作台默认目录不能是符号链接")
        workspace.mkdir(mode=0o700, exist_ok=True)
        return self.store.create_project("工作台默认目录", str(workspace), is_workspace=True)


    def _task_view(self, task: dict, summary: bool = False) -> dict:
        """业务任务保持真实状态，导航归属与其会话一致，不重写执行快照。"""
        project = self.store.get_project(task["project_id"])
        public_id = project.get("native_id") or project["id"]
        native_project = next((p for p in self._native["projects"] if p["id"] == public_id), None)
        section_id = task.get("section_id")
        section = next((s for s in self._sections() if s["id"] == section_id), None)
        session = self.sessions.get(task["session_id"]) if task.get("session_id") else None
        session_view = self._session_view(session) if session else None
        result = {**task, "project_id": None if project["is_workspace"] else public_id,
                "project_name": None if project["is_workspace"] else (native_project or project)["name"],
                "session_title": session_view["title"] if session_view else None,
                "section_name": section["name"] if section else task.get("section_name"), "source": "workbench", "can_run": True}
        if session_view and session_view["navigation_source"] == "codex":
            for field in ("project_id", "project_name", "section_id", "section_name"):
                result[field] = session_view[field]
            result["can_run"] = not session_view["execution_context_changed"]
        if session_view:
            result["can_run"] = result["can_run"] and session_view["can_create_task"] and not session_view["execution_context_changed"]
        result["navigation_source"] = session_view["navigation_source"] if session_view else "saved_binding"
        latest=(getattr(self,"_run_summaries",{}) if summary else self.store.task_run_summaries(task['id'])).get(task['id'])
        result['last_run']=latest
        result['session_native_status']=session_view.get('native_status') if session_view else None
        if summary:
            # 看板只需要索引信息；大段需求与资源清单按任务详情读取。
            for key in ("prompt", "resource_paths", "execution_account_subject"):
                result.pop(key, None)
        return result


    def _validate_resources(self, agent_id: str | None, paths: list[str]) -> None:
        if paths and not agent_id:
            raise StoreError("validation", "选择模板前必须指定智能体")
        if agent_id:
            self._agent(agent_id)
            self.library.resolve_selection(agent_id, paths)


    def _rename_session(self, args: dict) -> dict:
        """原始名称用令牌核验，显示名只供展示；公开 RPC 不保证读写间的原子 CAS。"""
        session = self._find_session(args["id"])
        if session["version"] != args["version"]:
            raise StoreError("version_conflict", "会话已更新，请重新打开后修改")
        title = args.get("title")
        if title is not None and not title.strip():
            raise StoreError("validation", "会话名称不能为空")
        if "description" in args and args["description"] != session.get("description", ""):
            try:
                self.sessions.get(session["id"])
            except ValueError:
                raise StoreError("session_unbound", "此原生会话尚未登记到工作台，暂不能保存本机说明") from None
        native_id = session.get("native_thread_id")
        if not native_id or title is None:
            if title is not None and "expected_name_token" in args and not name_token_matches(args["expected_name_token"], session["id"], session["title"]):
                raise StoreError("version_conflict", "会话名称已变化，请重新打开后修改")
            changes = {key:args[key] for key in ("title", "description") if key in args}
            return self._session_view(self.sessions.update_title(session["id"], args["version"], **changes))
        # expected_title 只是旧页面的显示快照，不能作为原始名称的并发前提。
        if not args.get("expected_name_token"):
            raise StoreError("name_token_required", "请刷新会话取得名称校验令牌后再改名")
        if not self.native_session_factory:
            raise StoreError("native_unavailable", "原生会话接口暂不可用")
        # 先核对线程保存的主体，避免同一账户目录换号后向错误主体发起改名。
        account = self._ready_account(
            session.get("thread_account_id") or session.get("execution_account_id") or "current",
            session.get("thread_account_subject") or session.get("account_subject"),
        )
        client = self.native_session_factory(account)
        try:
            current = client.read(native_id)
            if not name_token_matches(args["expected_name_token"], native_id, current["name"]):
                raise StoreError("version_conflict", "原生会话已改名，请重新打开后修改")
            try:
                before_write = self.sessions.get(session["id"])
            except ValueError:
                before_write = None
            if before_write and before_write["version"] != args["version"]:
                raise StoreError("version_conflict", "会话已更新，请重新打开后修改")
            observed = client.set_name(native_id, title.strip())
        finally:
            client.close()
        observed_name = observed.get("name")
        display = safe_display_title(observed_name or "", 300)
        sync = "synced" if observed_name == title.strip() else "conflict"
        observation = {"title": display, "name_token": name_token(native_id, observed_name),
                       "native_title_sync": sync, "native_name_concurrency": "observed_check_only"}
        try:
            saved = self.sessions.get(session["id"])
        except ValueError:
            self._refresh_native(self.accounts.current() if hasattr(self.accounts,"current") else None)
            return {**session, **observation}
        # 本机只保存净化名称；空请求值强制保留“原始名称不同”的冲突语义，
        # 避免两个原始名称净化后相同而被登记簿误记为 synced。
        saved = self.sessions.sync_native_title(saved["id"], args["version"], display if sync == "synced" else "", display)
        if "description" in args:
            saved = self.sessions.update_title(saved["id"], saved["version"], description=args["description"])
        self._refresh_native(self.accounts.current() if hasattr(self.accounts,"current") else None)
        return {**self._session_view(saved), **observation}


    def _native_thread_attached(self, run: dict, native_id: str) -> None:
        """新执行线程使用会话名；恢复执行只读取原生名，避免覆盖用户后来改名。"""
        if not self.native_session_factory:
            return
        account = run.get("execution_account")
        if not account:
            return
        client = self.native_session_factory(account)
        try:
            original = run.get("session", {})
            requested = original.get("title") or run["task"]["title"]
            observed = client.read(native_id) if original.get("native_thread_id") else client.set_name(native_id, requested)
            if not original.get("native_thread_id"):
                display = safe_display_title(observed.get("name") or "", 300)
                saved = self.sessions.get(run["session"]["id"])
                # 首次命名也按原始读回值判断成功，不能因显示净化而误报冲突。
                self.sessions.sync_native_title(saved["id"], saved["version"], display if observed.get("name") == requested else "", display)
            elif observed.get("name") is not None:
                self.sessions.observe_native_title(run["session"]["id"], safe_display_title(observed["name"], 300))
        finally:
            client.close()


    def _prepare(self, run: dict) -> None:
        agent_id = run["agent"]["id"]
        models = []
        selection = self.library.resolve_selection(agent_id, run["task"]["resource_paths"]) if agent_id else []
        if sum(item["size"] for item in selection) > 256 * 1024 * 1024:
            raise StoreError("too_large", "本次选定资源总量不能超过 256 MiB")
        instructions = self.library.build_instructions(agent_id, []) if agent_id else ""
        if run.get("session", {}).get("description"):
            instructions += "\n\n会话目标：\n" + run["session"]["description"]
        if agent_id:
            models = self.models.selected_models(agent_id, require_verified=True)
            if models:
                snapshot = [{key: model.get(key) for key in ("id", "name", "model_type", "base_url", "model", "version", "last_verified_at")} for model in models]
                instructions += "\n\n专业能力模型的已验证配置快照（仅在任务要求时调用，不含凭据明文）：\n" + json.dumps(snapshot, ensure_ascii=False)
        if selection:
            snapshot_dir = self.data_dir / "runs" / run["id"] / "resources"
            snapshot_dir.mkdir(parents=True, mode=0o700)
            for index, item in enumerate(selection):
                source = Path(item["absolutePath"])
                destination = snapshot_dir / (str(index + 1) + "-" + source.name)
                parent_fd = self.library._open_parent(source)
                try:
                    source_fd = os.open(source.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                    digest = hashlib.sha256()
                    total = 0
                    with os.fdopen(source_fd, "rb") as reader, destination.open("xb") as writer:
                        while chunk := reader.read(1024 * 1024):
                            total += len(chunk)
                            if total > 100 * 1024 * 1024:
                                raise StoreError("too_large", "资源读取时超过大小限制")
                            digest.update(chunk)
                            writer.write(chunk)
                    if digest.hexdigest() != item["sha256"]:
                        raise StoreError("version_conflict", "模板或素材在准备时发生变化，请重新选择")
                    destination.chmod(0o400)
                    item["snapshotPath"] = str(destination)
                    if item["path"].startswith("prompts/"):
                        if total > 256 * 1024:
                            raise StoreError("too_large", "单个提示词文件过长")
                        instructions += "\n\n提示词 " + item["path"] + "：\n" + destination.read_text(encoding="utf-8")
                finally:
                    os.close(parent_fd)
        if selection:
            instructions += "\n\n本次资源快照路径（请使用此版本，不修改原模板）：\n" + "\n".join(x["path"] + " → " + x["snapshotPath"] for x in selection)
        if len(instructions.encode()) > 100000:
            raise StoreError("too_large", "规则和提示词过长，请减少本次选择")
        if models:
            binding = create_capability_manifest(self.data_dir, run["id"], run["project"]["cwd"], run["execution"]["sandbox"], models, selection)
            run["capability_manifest"] = binding.manifest_path
            instructions += "\n\n专业模型通过 workbench-run-capabilities MCP 的已列出工具调用。只按用户任务需要选择上述模型 ID；不要自行查找 Key 或改用其他接口。输出文件保留工具返回的产物路径。"
        self.store.snapshot_resources(run["id"], instructions, selection)
        run["agent"]["instructions"] = instructions


    def board_call(self, name: str, arguments: dict) -> dict | list:
        """按白名单执行一个工具，保持登录与派发互斥并核验请求字段。"""
        self._ensure_board()
        args = validate(name, arguments)
        if name == "task_create" and not args.get("session_id"):
            raise StoreError("session_required", "请选择会话后创建任务")
        if name == "task_create" and not args["prompt"].strip():
            raise StoreError("validation", "请填写任务描述")
        if name == "workspace_choose":
            return choose_workspace()
        if name == "task_create" and not (args.get("title") or "").strip():
            # 标题推理在主业务锁外执行，避免读取看板被模型延迟阻塞。
            with self.lock:
                self._refresh_native(self.accounts.current() if hasattr(self.accounts, "current") else None)
                session = self._find_session(args["session_id"]) if args.get("session_id") else None
                if session:
                    self._require_native_available(session)
                if session and self._execution_context_changed(session):
                    raise StoreError("native_context_changed", "原生会话工作目录已变化，请核对执行目录后再继续")
                account = self._select_account(args.get("execution_account_id"), session)
                preferences = self._resolve_task_preferences(session,args)
                args.update(preferences)
                defaults = self._view_defaults
            args["title"] = self.title_generator(args["prompt"], account_home=account["codex_home"] if account else None,
                                                  use_current_account=bool(account and account["id"] == "current"),
                                                  model=args.get("model") or defaults.get("model"),
                                                  auth_store=defaults.get("auth_store") if account and account["id"] == "current" else None)
            if account:
                args["execution_account_id"] = account["id"]
                args["_title_subject"] = account["subject_id"]
        with self.lock:
            if self._closing:
                raise StoreError("unavailable","工作台正在关闭")
            if name in {"project_create", "session_create", "session_update", "session_detail", "task_create", "task_update", "task_detail", "task_start"}:
                self._refresh_native(self.accounts.current() if hasattr(self.accounts, "current") else None)
            result = self._board_dispatch(name, args)
            if name in {"section_create", "project_create", "agent_create", "agent_update", "account_create", "account_default", "account_rename", "model_create", "model_update", "credential_create", "credential_update", "credential_folder_create", "credential_folder_update", "credential_organization_apply"}:
                self._export_settings()
            return result

    def _board_dispatch(self,name,args):
        if name in {"board_state"}:
            return self.board_state()
        if name == "section_create":
            return self.store.create_section(**args)
        if name == "project_create":
            self._section(args.get("section_id"))
            if any(p["name"] == args["name"] or p.get("cwd") == args["cwd"] for p in self._projects()):
                raise StoreError("conflict", "这个项目名称或工作目录已经存在")
            return self.store.create_project(**args)
        if name == "session_create":
            return self._session_view(self._create_session(args))
        if name == "session_update":
            return self._rename_session(args)
        if name == "session_detail":
            identity = args["id"]
            session = next((s for s in self._session_views() if s["id"] == identity), None)
            if session is None:
                raise StoreError("not_found", "会话不存在")
            return {"session": self._session_name_view(session), "tasks": [self._task_view(t, summary=True) for t in self.store.list_tasks() if t.get("session_id") == identity]}
        if name == "task_create":
            title_subject=args.pop("_title_subject",None)
            chosen=self._find_session(args["session_id"])
            account=self._select_account(args.get("execution_account_id"),chosen)
            if title_subject and account["subject_id"]!=title_subject:
                raise StoreError("account_changed","生成标题后账户已变化")
            preferences=self._resolve_task_preferences(chosen,args)
            session=self._bind_session(chosen["id"],account)
            view=self._session_view(session)
            if args.get("project_id") and args["project_id"]!=view["project_id"]:
                raise StoreError("session_boundary","会话不属于所选项目")
            if args.get("section_id") and args["section_id"]!=view["section_id"]:
                raise StoreError("session_boundary","会话不属于所选分区")
            defaults=self._view_defaults
            args.update(preferences, title=safe_display_title(args.get("title") or "",50), project_id=session["project_id"], section_id=session["section_id"],
                        session_id=session["id"],execution_account_id=account["id"],sandbox=defaults["sandbox"],concurrency=defaults["concurrency"],sync_session_defaults=True)
            self._validate_resources(args.get("agent_id"),args.get("resource_paths",[]))
            task=self.store.create_task(**args)
            task=self.store.update_task(task["id"],task["version"],state="ready")
            return self._task_view(task)
        if name == "task_update":
            task_id = args.pop("id")
            if task_id.startswith("native:"):
                raise StoreError("forbidden", "请在 Codex 中编辑原生任务")
            task = self.store.get_task(task_id)
            if "session_id" in args and not args["session_id"]:
                raise StoreError("session_required","任务必须关联会话")
            if args.get("session_id"):
                session = self._find_session(args["session_id"])
                account = self._select_account(args.get("execution_account_id"), session)
                session = self._bind_session(session["id"], account)
                if session["project_id"] != task["project_id"]:
                    raise StoreError("session_boundary", "会话不属于任务项目")
                args.update(session_id=session["id"], section_id=session["section_id"], execution_account_id=account["id"])
            elif "execution_account_id" in args and args["execution_account_id"]:
                bound = self.sessions.get(task["session_id"]) if task.get("session_id") and "session_id" not in args else None
                self._select_account(args["execution_account_id"], bound)
            if "section_id" in args:
                self._section(args["section_id"])
                session = self.sessions.get(args.get("session_id") or task["session_id"])
                if args["section_id"] != session.get("section_id"):
                    raise StoreError("validation", "任务分区必须与会话一致")
            if args.get("execution_account_id") == "current":
                self.accounts.require_ready("current")
            if args.get("state") == task["state"]:
                args.pop("state")
            self._validate_resources(args.get("agent_id", task["agent_id"]), args.get("resource_paths", task["resource_paths"]))
            if "prompt" in args and not args["prompt"].strip():
                raise StoreError("validation", "请填写任务描述")
            if "title" in args:
                if not (args["title"] or "").strip():
                    raise StoreError("validation", "任务标题不能为空")
                args["title"] = safe_display_title(args["title"], 80)
            for key in list(args):
                if key != "version" and key in task and args[key] == task[key]:
                    args.pop(key)
            if task["state"] in {"done", "archived"} and any(key in args for key in ("prompt", "agent_id", "resource_paths", "model", "effort", "session_id", "execution_account_id")):
                args.setdefault("state", "ready")
            if set(args) == {"version"}:
                if args["version"] != task["version"]:
                    raise StoreError("version_conflict", "任务已被其他操作更新")
                return self._task_view(task)
            return self._task_view(self.store.update_task(task_id, _sync_session_defaults=any(k in args for k in ("model","effort","execution_account_id","session_id")), **args))
        if name == "task_detail":
            if args["id"].startswith("native:"):
                raise StoreError("validation", "原生记录是会话，请读取会话详情")
            task = self.store.get_task(args["id"])
            runs = list(reversed(self.store.list_runs(task["id"])))
            task_view=self._task_view(task)
            for run in runs:
                run["events"] = self.store.list_events(run["id"])
                run["native_url"] = "codex://threads/" + run["thread_id"] if run.get("thread_id") and task_view.get("session_native_status") != "missing" else None
                run["location_marker"] = run.get("location_marker")
                run["total_tokens"] = run["input_tokens"] + run["output_tokens"] if run.get("input_tokens") is not None and run.get("output_tokens") is not None else None
            metrics={"run_count":len(runs)}
            for field in ("duration_ms","input_tokens","output_tokens","cached_input_tokens","total_tokens"):
                metrics[field]=sum(run[field] for run in runs) if runs and all(run.get(field) is not None for run in runs) else None
            for item in runs:
                path = self.data_dir / "runs" / item["id"] / "capability-manifest.json"
                item["professional_calls"] = read_capability_events(path) if path.is_file() else []
            return {"task": self._task_view(task), "runs": runs,"metrics":metrics,"last_run":runs[0] if runs else None}
        if name == "task_start":
            if args["id"].startswith("native:"):
                raise StoreError("forbidden", "请在 Codex 中继续原生任务")
            task = self.store.get_task(args["id"])
            if not task["execution_account_id"]:
                raise StoreError("account_required", "请先为任务选择执行账户")
            account = self._ready_account(task["execution_account_id"], task["execution_account_subject"])
            if task.get("session_id"):
                session = self._bind_session(task["session_id"], account)
            else:
                raise StoreError("session_required","请先为任务选择会话")
            if not Path(session["cwd"]).is_dir():
                raise StoreError("workspace_unavailable", "会话工作目录不存在")
            # 执行目录已固定在会话快照；原生导航移除旧项目不应改写或阻断该快照。
            if task["agent_id"]:
                self.models.selected_models(task["agent_id"], require_verified=True)
            if self.native_session_factory and session.get("native_thread_id"):
                client = self.native_session_factory(account)
                try:
                    client.resume_gate(session["native_thread_id"])
                finally:
                    client.close()
            if task["state"] == "backlog":
                task = self.store.update_task(task["id"], task["version"], state="ready")
            return self.runner.start(task["id"])
        if name == "run_cancel":
            self.runner.cancel(args["id"])
            return {"status": "cancellation_requested"}
        raise StoreError("not_found","看板工具不存在")
