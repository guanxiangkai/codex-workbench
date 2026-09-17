"""密钥保险库的非秘密目录、分类和目标绑定元数据。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .credential_schema import CredentialSchemaError, build_model_key_index, validate_structure, describe_structure


_ENTRY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_VAULT_RE = re.compile(r"vault:([A-Za-z0-9][A-Za-z0-9._-]{0,127})\Z")
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\Z")
_UNSET = object()

# 仅依据保险库清单中公开的 entryId 前缀归类。规则不得根据标签、显示名或
# 密文内容猜测条目的用途，也不表达该凭证是否仍然有效。
_ORGANIZATION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("account.gitea.", ("开发平台", "代码托管")),
    ("account.github.", ("开发平台", "代码托管")),
    ("github.", ("开发平台", "代码托管")),
    ("ssh.git.", ("开发平台", "代码托管")),
    ("account.npm.", ("开发平台", "软件包")),
    ("account.business-platform.", ("业务系统", "共享平台")),
    ("account.business-application.", ("业务系统", "应用账户")),
    ("account.business-component.", ("业务系统", "组件接口")),
    ("account.business-workflow.", ("业务系统", "工作流服务")),
    ("account.business-procurement.", ("业务系统", "采购管理")),
    ("account.document-digitization.", ("AI 服务", "文档解析")),
    ("huggingface.", ("AI 服务", "模型资源")),
    ("proposal-ppt-image-", ("AI 服务", "PPT 生成")),
    ("proposal-ppt-reasoning-", ("AI 服务", "PPT 生成")),
    ("api.", ("AI 服务", "API 服务")),
    ("ssh.", ("基础设施", "主机与 SSH")),
    ("database.", ("基础设施", "数据库与中间件")),
    ("redis.", ("基础设施", "数据库与中间件")),
    ("account.proxmox.", ("基础设施", "虚拟化与存储")),
    ("account.seaweedfs.", ("基础设施", "虚拟化与存储")),
    ("tls.", ("基础设施", "证书与安全")),
    ("system.", ("基础设施", "系统账户")),
    ("config.", ("基础设施", "配置快照")),
    ("account.feishu.", ("协作", "飞书")),
    ("feishu.", ("协作", "飞书")),
)
_ORGANIZATION_RULE_VERSION = "entry-prefix-v2"


class CredentialCatalogError(ValueError):
    """凭证目录的固定安全错误，不携带保险库命令或秘密输出。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> None:
    raise CredentialCatalogError(code, message)


def _text(value: Any, maximum: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _error("metadata_invalid", f"{field} 无效")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _error("metadata_invalid", f"{field} 无效")
    return value.strip()


def _tags(value: Any) -> list[str]:
    """统一标签校验，避免预校验与登记使用不同的去重规则。"""
    if not isinstance(value, list) or len(value) > 12:
        _error("metadata_invalid", "标签无效")
    normalized = [_text(tag, 80, "标签") for tag in value]
    if len(set(normalized)) != len(normalized):
        _error("metadata_invalid", "标签不能重复")
    return normalized


def _url(value: Any) -> str:
    value = _text(value, 4096, "目标地址")
    try:
        parsed = urlsplit(value)
    except ValueError:
        _error("metadata_invalid", "目标地址无效")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        _error("metadata_invalid", "目标地址无效")
    return value.rstrip("/")


class CredentialCatalog:
    """合并只读保险库清单与本机 SQLite 组织信息；不会读取或移动任何密文。"""

    def __init__(self, db_path: str | Path, vault_command: Path | None = None, poll_seconds: float = 60) -> None:
        self.db_path = Path(db_path)
        self.command = vault_command or Path.home() / ".codex/scripts/key-vault/key-vault.sh"
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or poll_seconds < 0:
            _error("metadata_invalid", "轮询间隔无效")
        self.poll_seconds = float(poll_seconds)
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as conn:
            # 先取得写锁再检查旧表列，避免多个入口并发升级重复添加同一列。
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS credential_folders(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT REFERENCES credential_folders(id), color TEXT
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS credential_organizer(
                entry_id TEXT PRIMARY KEY, label TEXT, folder_id TEXT REFERENCES credential_folders(id),
                tags_json TEXT NOT NULL DEFAULT '[]', color TEXT, kind TEXT NOT NULL DEFAULT 'other', base_urls_json TEXT NOT NULL DEFAULT '[]', has_fields_json TEXT NOT NULL DEFAULT '[]', organization_source TEXT
            )""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(credential_organizer)")}
            if "has_fields_json" not in columns:
                conn.execute("ALTER TABLE credential_organizer ADD COLUMN has_fields_json TEXT NOT NULL DEFAULT '[]'")
            if "organization_source" not in columns:
                conn.execute("ALTER TABLE credential_organizer ADD COLUMN organization_source TEXT")
            # 组织目录与载荷结构是独立事实；旧 kind/has_fields 列保留但不作为结构证据。
            conn.execute("""CREATE TABLE IF NOT EXISTS credential_structure(
                entry_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision > 0),
                generation INTEGER NOT NULL CHECK(generation > 0), source TEXT NOT NULL CHECK(source IN ('created','probe')),
                structure_json TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS credential_targets(
                entry_id TEXT PRIMARY KEY, target_digest TEXT NOT NULL
            )""")
            # COALESCE 令根目录的 NULL 父级也参与唯一性约束。旧库若已有同级
            # 重名目录，不迁移、不合并；后续整理事务会稳定复用最早的目录。
            try:
                conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS credential_folders_parent_name_unique
                    ON credential_folders(COALESCE(parent_id, ''), name)""")
            except sqlite3.IntegrityError:
                pass

    def _run(self, operation: str) -> Any:
        if operation not in {"list", "status"} or not self.command.is_file() or self.command.is_symlink():
            _error("vault_unavailable", "保险库目录暂时不可用")
        try:
            result = subprocess.run([str(self.command), operation], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, check=False, timeout=5)
        except subprocess.TimeoutExpired:
            _error("vault_timeout", "读取保险库目录超时")
        except OSError:
            _error("vault_unavailable", "保险库目录暂时不可用")
        if result.returncode:
            _error("vault_unavailable", "无法读取保险库目录")
        try:
            return json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _error("vault_unavailable", "保险库目录返回格式无效")

    @staticmethod
    def _vault_entries(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            _error("vault_unavailable", "保险库目录返回格式无效")
        entries: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("entryId"), str) or not _ENTRY_RE.fullmatch(item["entryId"]):
                _error("vault_unavailable", "保险库目录返回格式无效")
            record = {"entry_id": item["entryId"]}
            for source, target in (("revision", "revision"), ("createdAt", "created_at"), ("updatedAt", "updated_at")):
                if source in item:
                    if source == "revision" and (not isinstance(item[source], int) or isinstance(item[source], bool) or item[source] < 1):
                        _error("vault_unavailable", "保险库目录返回格式无效")
                    if source != "revision" and not isinstance(item[source], str):
                        _error("vault_unavailable", "保险库目录返回格式无效")
                    record[target] = item[source]
            entries.append(record)
        return entries

    @staticmethod
    def _vault_status(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("ready"), bool):
            _error("vault_unavailable", "保险库状态返回格式无效")
        result = {"ready": value["ready"]}
        if isinstance(value.get("entries"), int) and not isinstance(value["entries"], bool) and value["entries"] >= 0:
            result["entries"] = value["entries"]
        if isinstance(value.get("masterKeyAvailable"), bool):
            result["master_key_available"] = value["masterKeyAvailable"]
        return result

    def _snapshot(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if not force and self._cache is not None and now - self._cache[0] < self.poll_seconds:
                return self._cache[1]
            status = self._vault_status(self._run("status"))
            entries = self._vault_entries(self._run("list"))
            snapshot = {"status": status, "entries": entries}
            self._cache = (now, snapshot)
            return snapshot

    def _folders(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            return [dict(row) for row in conn.execute("SELECT id,name,parent_id,color FROM credential_folders ORDER BY name,id")]

    @staticmethod
    def _decode_list(value: Any, maximum: int, field: str) -> list[str]:
        try:
            result = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            _error("metadata_invalid", "本机凭证元数据无效")
        if not isinstance(result, list) or len(result) > maximum or any(not isinstance(item, str) for item in result):
            _error("metadata_invalid", "本机凭证元数据无效")
        return result

    def _entry_views(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        with self._connection() as conn:
            saved = {row["entry_id"]: dict(row) for row in conn.execute("SELECT * FROM credential_organizer")}
            structures = {row["entry_id"]: dict(row) for row in conn.execute("SELECT * FROM credential_structure")}
        views = []
        for vault in snapshot["entries"]:
            item = saved.get(vault["entry_id"])
            if item is None:
                view = {"id": vault["entry_id"], "reference": "vault:" + vault["entry_id"], "label": vault["entry_id"],
                        "folder_id": None, "tags": [], "color": None,
                        "organization_source": "unorganized"}
            else:
                view = {"id": vault["entry_id"], "reference": "vault:" + vault["entry_id"],
                        "label": item["label"] or vault["entry_id"], "folder_id": item["folder_id"],
                        "tags": self._decode_list(item["tags_json"], 12, "标签"), "color": item["color"],
                        }
                view["organization_source"] = item.get("organization_source") or "manual"
            view.update(self._structure_view(structures.get(vault["entry_id"]), vault.get("revision")))
            views.append({**view, **{key: value for key, value in vault.items() if key != "entry_id"}})
        return views

    @staticmethod
    def _structure_view(saved: dict[str, Any] | None, revision: int | None) -> dict[str, Any]:
        """仅暴露与当前密文修订一致的索引；旧字段和名称不能补足证据。"""
        empty = {"kind": "unknown", "has_fields": [], "field_types": {}, "has_unknown_fields": False,
                 "structure_status": "unindexed", "structure_source": None, "structure_format": None,
                 "structure_error": None, "structure_revision": None, "structure_generation": 0,
                 "format_status": "unavailable", "mapping_status": "unindexed"}
        if saved is None:
            return empty
        generation = saved["generation"]
        if type(generation) is not int or generation < 1 or type(saved["revision"]) is not int or saved["revision"] < 1 or saved["source"] not in {"created", "probe"}:
            return {**empty, "structure_status": "error", "structure_error": "invalid_schema", "mapping_status": "error"}
        empty.update(structure_source=saved["source"], structure_revision=saved["revision"], structure_generation=generation)
        if revision != saved["revision"]:
            return {**empty, "structure_status": "stale", "mapping_status": "stale"}
        try:
            serialized = saved["structure_json"]
            if not isinstance(serialized, str) or len(serialized) > 65536:
                raise CredentialSchemaError()
            structure = validate_structure(json.loads(serialized))
        except (ValueError, TypeError, RecursionError):
            return {**empty, "structure_status": "error", "structure_error": "invalid_schema", "mapping_status": "error"}
        return {**empty, **structure, **describe_structure(structure)}

    def list(self) -> dict[str, Any]:
        """返回保险库非秘密条目与本地层级组织信息，受 TTL 缓存约束。"""
        snapshot = self._snapshot()
        return {"folders": self._folders(), "entries": self._entry_views(snapshot), "status": dict(snapshot["status"])}

    def _folder_exists(self, folder_id: str | None) -> None:
        if folder_id is None:
            return
        if not isinstance(folder_id, str):
            _error("folder_invalid", "凭证目录不存在")
        with self._connection() as conn:
            if conn.execute("SELECT 1 FROM credential_folders WHERE id=?", (folder_id,)).fetchone() is None:
                _error("folder_invalid", "凭证目录不存在")

    def _folder_depth(self, parent_id: str | None, own_id: str | None = None) -> int:
        depth, seen = 0, set()
        with self._connection() as conn:
            while parent_id is not None:
                if parent_id == own_id or parent_id in seen:
                    _error("folder_cycle", "凭证目录不能形成循环")
                seen.add(parent_id)
                row = conn.execute("SELECT parent_id FROM credential_folders WHERE id=?", (parent_id,)).fetchone()
                if row is None:
                    _error("folder_invalid", "凭证目录不存在")
                depth += 1
                parent_id = row["parent_id"]
        return depth

    def _subtree_height(self, folder_id: str) -> int:
        """返回目录至最深后代的边数，移动父级时也必须保留八层上限。"""
        with self._connection() as conn:
            row = conn.execute("""WITH RECURSIVE descendants(id,depth) AS (
                SELECT id,0 FROM credential_folders WHERE id=?
                UNION ALL
                SELECT child.id,descendants.depth+1 FROM credential_folders child JOIN descendants ON child.parent_id=descendants.id
            ) SELECT MAX(depth) AS height FROM descendants""", (folder_id,)).fetchone()
        return int(row["height"] or 0)

    def folder_create(self, name: str, parent_id: str | None = None, color: str | None = None) -> dict[str, Any]:
        """创建深度不超过八层的本机组织目录，不触及保险库密文文件。"""
        name = _text(name, 160, "目录名称")
        if color is not None and (not isinstance(color, str) or not _COLOR_RE.fullmatch(color)):
            _error("metadata_invalid", "颜色无效")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id,name,parent_id,color FROM credential_folders WHERE name=? AND parent_id IS ? ORDER BY id LIMIT 1", (name, parent_id)).fetchone()
            if row is not None:
                return dict(row)
            if parent_id is not None and conn.execute("SELECT 1 FROM credential_folders WHERE id=?", (parent_id,)).fetchone() is None:
                _error("folder_invalid", "凭证目录不存在")
            depth, current_parent = 0, parent_id
            while current_parent is not None:
                row = conn.execute("SELECT parent_id FROM credential_folders WHERE id=?", (current_parent,)).fetchone()
                if row is None:
                    _error("folder_invalid", "凭证目录不存在")
                depth += 1
                current_parent = row["parent_id"]
            if depth >= 8:
                _error("folder_depth", "凭证目录最多八层")
            folder = {"id": str(uuid.uuid4()), "name": name, "parent_id": parent_id, "color": color}
            try:
                conn.execute("INSERT INTO credential_folders(id,name,parent_id,color) VALUES (?,?,?,?)", tuple(folder.values()))
            except sqlite3.IntegrityError:
                row = conn.execute("SELECT id,name,parent_id,color FROM credential_folders WHERE name=? AND parent_id IS ? ORDER BY id LIMIT 1", (name, parent_id)).fetchone()
                if row is None:
                    raise
                return dict(row)
        return folder

    def folder_update(self, folder_id: str, *, name: str | object = _UNSET, parent_id: str | None | object = _UNSET,
                      color: str | None | object = _UNSET) -> dict[str, Any]:
        """更新目录名称、父级或颜色，并拒绝循环和超过八层的移动。"""
        with self._connection() as conn:
            row = conn.execute("SELECT id,name,parent_id,color FROM credential_folders WHERE id=?", (folder_id,)).fetchone()
        if row is None:
            _error("folder_invalid", "凭证目录不存在")
        result = dict(row)
        if name is not _UNSET:
            result["name"] = _text(name, 160, "目录名称")
        if parent_id is not _UNSET:
            self._folder_exists(parent_id)
            parent_depth = self._folder_depth(parent_id, folder_id)
            if parent_depth + 1 + self._subtree_height(folder_id) > 8:
                _error("folder_depth", "凭证目录最多八层")
            result["parent_id"] = parent_id
        if color is not _UNSET:
            if color is not None and (not isinstance(color, str) or not _COLOR_RE.fullmatch(color)):
                _error("metadata_invalid", "颜色无效")
            result["color"] = color
        with self._connection() as conn:
            try:
                conn.execute("UPDATE credential_folders SET name=?,parent_id=?,color=? WHERE id=?", (result["name"], result["parent_id"], result["color"], folder_id))
            except sqlite3.IntegrityError:
                _error("folder_exists", "同级凭证目录已存在")
        return result

    def _known_entry(self, entry_id: str, *, force: bool = False) -> None:
        if not isinstance(entry_id, str) or not _ENTRY_RE.fullmatch(entry_id):
            _error("entry_invalid", "凭证条目不存在")
        if entry_id not in {item["entry_id"] for item in self._snapshot(force)["entries"]}:
            _error("entry_invalid", "凭证条目不存在")

    def entry_update(self, entry_id: str, *, label: str | None | object = _UNSET, folder_id: str | None | object = _UNSET,
                     tags: list[str] | object = _UNSET, color: str | None | object = _UNSET) -> dict[str, Any]:
        """只更新本机条目组织信息；条目必须已存在于保险库只读清单。"""
        self._known_entry(entry_id, force=True)
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM credential_organizer WHERE entry_id=?", (entry_id,)).fetchone()
        current = dict(row) if row else {"entry_id": entry_id, "label": None, "folder_id": None, "tags_json": "[]", "color": None, "kind": "other", "base_urls_json": "[]", "has_fields_json": "[]", "organization_source": None}
        if label is not _UNSET:
            current["label"] = None if label is None else _text(label, 160, "凭证标签")
        if folder_id is not _UNSET:
            self._folder_exists(folder_id)
            current["folder_id"] = folder_id
        if tags is not _UNSET:
            normalized = _tags(tags)
            current["tags_json"] = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        if color is not _UNSET:
            if color is not None and (not isinstance(color, str) or not _COLOR_RE.fullmatch(color)):
                _error("metadata_invalid", "颜色无效")
            current["color"] = color
        # 任何显式编辑都归用户所有，后续批量整理不得覆盖其目录、名称或标签。
        current["organization_source"] = "manual"
        with self._connection() as conn:
            conn.execute("""INSERT INTO credential_organizer(entry_id,label,folder_id,tags_json,color,kind,base_urls_json,has_fields_json,organization_source) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET label=excluded.label,folder_id=excluded.folder_id,tags_json=excluded.tags_json,color=excluded.color,kind=excluded.kind,base_urls_json=excluded.base_urls_json,has_fields_json=excluded.has_fields_json,organization_source=excluded.organization_source""", (current["entry_id"],current["label"],current["folder_id"],current["tags_json"],current["color"],current["kind"],current["base_urls_json"],current["has_fields_json"],current["organization_source"]))
        return next(item for item in self.list()["entries"] if item["id"] == entry_id)

    @staticmethod
    def _organization_path(entry_id: str) -> tuple[str, ...] | None:
        """按公开 entryId 前缀返回建议目录；历史包装只移除前缀，不推断状态。"""
        historical = entry_id.startswith("quarantine.")
        if historical:
            entry_id = entry_id.removeprefix("quarantine.")
        for prefix, path in _ORGANIZATION_RULES:
            if entry_id.startswith(prefix):
                return path
        return ("待整理",) if historical else None

    @staticmethod
    def _is_customized(item: dict[str, Any] | None) -> bool:
        """判断既有元数据是否来自用户或业务写入，避免批量整理覆盖它。"""
        if item is None:
            return False
        return any((item["folder_id"] is not None, item["label"] is not None, item["color"] is not None,
                    item["kind"] != "other", item["base_urls_json"] != "[]", item["has_fields_json"] != "[]",
                    item["tags_json"] != "[]"))

    def _organization_candidates(self, snapshot: dict[str, Any], saved: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        if saved is None:
            with self._connection() as conn:
                saved = {row["entry_id"]: dict(row) for row in conn.execute("SELECT * FROM credential_organizer")}
        candidates = []
        for vault in snapshot["entries"]:
            entry_id = vault["entry_id"]
            path = self._organization_path(entry_id)
            saved_item = saved.get(entry_id)
            source = saved_item.get("organization_source") if saved_item else None
            if path is None:
                state = "unclassified"
            elif source == "suggestion":
                state = "already_applied"
            elif self._is_customized(saved_item):
                state = "preserved"
            else:
                state = "ready"
            candidates.append({"id": entry_id, "reference": "vault:" + entry_id, "suggested_path": list(path) if path else None,
                               "reason": "entry_prefix" if path else None, "state": state,
                               "can_apply": state == "ready"})
        return candidates

    def organization_suggestions(self) -> dict[str, Any]:
        """返回可解释的本机整理建议；字段不包含秘密或任何可用性结论。

        返回项中的 ``suggested_path`` 是待创建的目录层级，``reason`` 固定为
        ``entry_prefix``，``state`` 为 ready、already_applied、preserved 或
        unclassified，``can_apply`` 表示该建议能否安全批量落地。
        """
        return {"rules_version": _ORGANIZATION_RULE_VERSION,
                "entries": self._organization_candidates(self._snapshot(force=True))}

    def _ensure_folder_path(self, conn: sqlite3.Connection, path: tuple[str, ...]) -> dict[str, Any]:
        """在调用方持有的写事务中复用或创建同级唯一的目录路径。"""
        parent_id = None
        for name in path:
            row = conn.execute("SELECT id,name,parent_id,color FROM credential_folders WHERE name=? AND parent_id IS ? ORDER BY id LIMIT 1", (name, parent_id)).fetchone()
            if row is None:
                folder = {"id": str(uuid.uuid4()), "name": name, "parent_id": parent_id, "color": None}
                try:
                    conn.execute("INSERT INTO credential_folders(id,name,parent_id,color) VALUES (?,?,?,?)", tuple(folder.values()))
                except sqlite3.IntegrityError:
                    row = conn.execute("SELECT id,name,parent_id,color FROM credential_folders WHERE name=? AND parent_id IS ? ORDER BY id LIMIT 1", (name, parent_id)).fetchone()
                    if row is None:
                        raise
                    folder = dict(row)
            else:
                folder = dict(row)
            parent_id = folder["id"]
        return folder

    @staticmethod
    def _insert_suggested_organization(conn: sqlite3.Connection, entry_id: str, folder_id: str) -> bool:
        """仅在条目仍未组织时登记建议来源，返回本次是否实际写入。"""
        result = conn.execute("""INSERT INTO credential_organizer(entry_id,label,folder_id,tags_json,color,kind,base_urls_json,has_fields_json,organization_source)
            VALUES (?,NULL,?,'[]',NULL,'other','[]','[]','suggestion') ON CONFLICT(entry_id) DO NOTHING""", (entry_id, folder_id))
        return result.rowcount == 1

    def apply_organization_suggestions(self, entry_ids: list[str] | None = None) -> dict[str, Any]:
        """批量写入建议目录到本机元数据，幂等且从不覆盖已有自定义组织信息。"""
        if entry_ids is not None:
            if not isinstance(entry_ids, list) or len(entry_ids) > 256 or any(not isinstance(entry_id, str) or not _ENTRY_RE.fullmatch(entry_id) for entry_id in entry_ids) or len(set(entry_ids)) != len(entry_ids):
                _error("entry_invalid", "凭证条目不存在")
            selected = set(entry_ids)
        else:
            selected = None
        snapshot = self._snapshot(force=True)
        known = {entry["entry_id"] for entry in snapshot["entries"]}
        if selected is not None and not selected <= known:
            _error("entry_invalid", "凭证条目不存在")
        applied, skipped = [], []
        with self._connection() as conn:
            # 在写锁内重新读取本机元数据，避免 suggestions 预览后手工编辑被覆盖。
            conn.execute("BEGIN IMMEDIATE")
            saved = {row["entry_id"]: dict(row) for row in conn.execute("SELECT * FROM credential_organizer")}
            for candidate in self._organization_candidates(snapshot, saved):
                if selected is not None and candidate["id"] not in selected:
                    continue
                if not candidate["can_apply"]:
                    skipped.append({key: candidate[key] for key in ("id", "reference", "state", "suggested_path")})
                    continue
                folder = self._ensure_folder_path(conn, tuple(candidate["suggested_path"]))
                if self._insert_suggested_organization(conn, candidate["id"], folder["id"]):
                    applied.append({**{key: candidate[key] for key in ("id", "reference", "suggested_path", "reason")}, "folder_id": folder["id"]})
                else:
                    skipped.append({key: candidate[key] for key in ("id", "reference", "state", "suggested_path")})
            folders = [dict(row) for row in conn.execute("SELECT id,name,parent_id,color FROM credential_folders ORDER BY name,id")]
        return {"rules_version": _ORGANIZATION_RULE_VERSION, "applied": applied, "skipped": skipped, "folders": folders}

    def index_structure(self, entry_id: str, revision: int, structure: dict[str, Any], *,
                        expected_generation: int, source: str = "probe") -> dict[str, Any]:
        """登记受控消费者的无值索引；以保险库修订号和本机 generation 做冲突检查。

        仅主代理获准执行结构探测后调用本入口；方法本身只运行 status/list。
        调用方须在消费前记录 revision，消费后由本方法强制刷新清单再核对；
        跨进程保险库写入不在 SQLite 事务内，后续清单 revision 漂移会隐藏索引。
        """
        if not isinstance(entry_id, str) or not _ENTRY_RE.fullmatch(entry_id) or type(revision) is not int or revision < 1:
            _error("structure_invalid", "凭证结构索引无效")
        if type(expected_generation) is not int or expected_generation < 0 or not isinstance(source, str) or source not in {"created", "probe"}:
            _error("structure_invalid", "凭证结构索引无效")
        try:
            normalized = validate_structure(structure)
        except CredentialSchemaError:
            _error("structure_invalid", "凭证结构索引无效")
        snapshot = self._snapshot(force=True)
        entry = next((item for item in snapshot["entries"] if item["entry_id"] == entry_id), None)
        if entry is None:
            _error("entry_invalid", "凭证条目不存在")
        if entry.get("revision") != revision:
            _error("structure_stale", "凭证修订已变化，请重新探测结构")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT generation FROM credential_structure WHERE entry_id=?", (entry_id,)).fetchone()
            generation = row["generation"] if row else 0
            # 损坏的令牌仅可由持有新探测证据的调用按 generation=0 重建；
            # 同一写锁下若其他调用已修复为有效令牌，正常 CAS 会拒绝旧请求。
            if type(generation) is not int or generation < 0:
                generation = 0
            if generation != expected_generation:
                _error("structure_conflict", "凭证结构索引已变化，请重新读取")
            conn.execute("""INSERT INTO credential_structure(entry_id,revision,generation,source,structure_json)
                VALUES (?,?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET revision=excluded.revision,
                generation=excluded.generation,source=excluded.source,structure_json=excluded.structure_json""",
                (entry_id, revision, generation + 1, source, json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))))
        return next(item for item in self.list()["entries"] if item["id"] == entry_id)

    def validate_payload_metadata(self, title: str, folder_id: str | None = None,
                                  tags: list[str] | None = None) -> dict[str, Any]:
        """写保险库前只读校验全部组织信息；不保证跨保险库与 SQLite 原子提交。"""
        title = _text(title, 160, "凭证标题")
        normalized_tags = _tags([] if tags is None else tags)
        if folder_id is not None:
            _text(folder_id, 128, "凭证目录")
        self._folder_exists(folder_id)
        return {"title": title, "folder_id": folder_id, "tags": normalized_tags}

    def register_payload(self, reference: str, title: str, structure: dict[str, Any], folder_id: str | None = None, tags: list[str] | None = None) -> dict:
        """登记刚创建的版本化凭证及其结构证据，不读取加密字段值。"""
        if not isinstance(reference,str) or not _VAULT_RE.fullmatch(reference):
            _error("entry_invalid","凭证引用无效")
        try:
            normalized = validate_structure(structure)
        except CredentialSchemaError:
            _error("structure_invalid", "凭证结构索引无效")
        if normalized["structure_format"] != "workbench_credential_v1" or normalized["structure_status"] != "indexed":
            _error("structure_invalid", "新凭证结构与写入契约不一致")
        metadata = self.validate_payload_metadata(title, folder_id, tags)
        entry_id = reference.removeprefix("vault:")
        self.index_structure(entry_id, 1, normalized, expected_generation=0, source="created")
        return self.entry_update(entry_id,label=metadata["title"],folder_id=metadata["folder_id"],tags=metadata["tags"])

    def register_key(self, reference: str, label: str, base_url: str, *, created: bool) -> dict[str, Any]:
        """仅登记刚成功写入的模型 Key；目标只存摘要且不进入列表或设置导出。"""
        match = _VAULT_RE.fullmatch(reference) if isinstance(reference, str) else None
        if match is None:
            _error("entry_invalid", "凭证引用无效")
        if created is not True:
            _error("structure_invalid", "仅可登记已完成写入的新凭证")
        target_digest = hashlib.sha256(_url(base_url).encode("utf-8")).hexdigest()
        entry_id = match.group(1)
        self.index_structure(entry_id, 1, build_model_key_index(), expected_generation=0, source="created")
        with self._connection() as conn:
            conn.execute("INSERT INTO credential_targets(entry_id,target_digest) VALUES (?,?)", (entry_id, target_digest))
            # 模型登记只补缺失标题，保留已有自定义目录、标签和名称。
            conn.execute("INSERT INTO credential_organizer(entry_id,label) VALUES (?,?) ON CONFLICT(entry_id) DO NOTHING", (entry_id, _text(label, 160, "凭证标签")))
        return next(item for item in self.list()["entries"] if item["id"] == entry_id)

    def resolve_key(self, entry_id: str, base_url: str) -> str:
        """返回匹配目标的非秘密 vault 引用；未绑定或跨目标时明确拒绝。"""
        entry = next((item for item in self.list()["entries"] if item["id"] == entry_id), None)
        if entry is None:
            _error("entry_invalid", "凭证条目不存在")
        if entry["kind"] != "api_key":
            _error("key_kind_invalid", "所选凭证不是 API Key")
        with self._connection() as conn:
            target = conn.execute("SELECT target_digest FROM credential_targets WHERE entry_id=?", (entry_id,)).fetchone()
            legacy = conn.execute("SELECT base_urls_json FROM credential_organizer WHERE entry_id=?", (entry_id,)).fetchone()
        # 已有本机绑定留在原位，绝不经普通列表或外部目录导出回传。
        old_urls = self._decode_list(legacy["base_urls_json"], 10, "目标地址") if legacy else []
        if target is None and not old_urls:
            _error("target_binding_required", "该 API Key 尚未绑定目标地址")
        normalized = _url(base_url)
        matches = hashlib.sha256(normalized.encode("utf-8")).hexdigest() == target["target_digest"] if target else normalized in old_urls
        if not matches:
            _error("target_mismatch", "该 API Key 不适用于当前目标地址")
        return entry["reference"]

    def metadata_export(self) -> dict[str, Any]:
        """仅导出组织资料；结构索引与目标绑定留在本机，不向外部目录复制。"""
        with self._connection() as conn:
            folders = [dict(row) for row in conn.execute("SELECT id,name,parent_id,color FROM credential_folders ORDER BY name,id")]
            entries = []
            for row in conn.execute("SELECT entry_id,label,folder_id,tags_json,color,organization_source FROM credential_organizer ORDER BY entry_id"):
                entries.append({"entry_id": row["entry_id"], "label": row["label"], "folder_id": row["folder_id"],
                                "tags": self._decode_list(row["tags_json"], 12, "标签"), "color": row["color"],
                                "organization_source": row["organization_source"] or "manual"})
        return {"folders": folders, "entries": entries}
