"""独立模型登记、验证状态和助手绑定的 SQLite 领域边界。"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit
from .model_endpoints import resolve_endpoint


_MODEL_TYPES = frozenset({"reasoning", "multimodal", "speech_to_text", "text_to_speech", "embedding", "rerank"})
_ALL_MODEL_TYPES = _MODEL_TYPES | {"unconfigured"}
_PROTOCOLS = frozenset({"openai-chat", "openai-responses"})
_STATUSES = frozenset({"pending", "validating", "verified", "failed", "paused"})
_VAULT_REF = re.compile(r"vault:[A-Za-z0-9][A-Za-z0-9_.:-]{0,180}")


class ModelRegistryError(ValueError):
    """模型登记的可处理错误，``code`` 供 API 层保持稳定的错误分支。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ModelRegistry:
    """在工作台自有数据库维护模型验证状态，不读取凭据或调用网络。"""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        """初始化独立表；失败模型只能由用户显式重新验证。"""
        self.path = os.fspath(db_path)
        if not Path(self.path).expanduser().resolve().parent.is_dir():
            raise ModelRegistryError("validation", "数据库父目录不存在")
        self._migrate_model_type_constraint()
        self._initialize()

    def create(self, **fields: Any) -> dict[str, Any]:
        """创建待验证模型；新记录会立即进入验证队列。"""
        required = {"name", "model_type", "base_url", "model"}
        optional = {"protocol", "credential_ref", "voice"}
        if set(fields) - required - optional or not required <= set(fields):
            raise ModelRegistryError("validation", "模型字段无效")
        data = self._fields({"protocol": "openai-chat", "credential_ref": "", "voice": "alloy", **fields}, allow_unconfigured=False)
        model_id, now = str(uuid.uuid4()), self._now()
        with self._connection(immediate=True) as conn:
            self._assert_endpoint_unique(conn, data)
            conn.execute("""INSERT INTO provider_models(
                id,name,model_type,base_url,model,protocol,credential_ref,voice,version,validation_status,
                next_check_at,attempt_count,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,1,'pending',NULL,0,?,?)""",
                         (model_id, data["name"], data["model_type"], data["base_url"], data["model"], data["protocol"],
                          data["credential_ref"], data["voice"], now, now))
        return self.get(model_id)

    def update(self, model_id: str, version: int, **fields: Any) -> dict[str, Any]:
        """乐观更新配置并使原验证失效；暂停项保留用户暂停意图，其余重新排队。"""
        self._id(model_id, "模型标识")
        self._version(version)
        if not fields or set(fields) - {"name", "model_type", "base_url", "model", "protocol", "credential_ref", "voice"}:
            raise ModelRegistryError("validation", "模型更新字段无效")
        with self._connection(immediate=True) as conn:
            current = self._require_model(conn, model_id)
            if current["version"] != version:
                raise ModelRegistryError("version_conflict", "模型已被其他操作更新")
            merged = {key: fields.get(key, current[key]) for key in ("name", "model_type", "base_url", "model", "protocol", "credential_ref", "voice")}
            data = self._fields(merged, allow_unconfigured=False)
            self._assert_endpoint_unique(conn, data, exclude_id=model_id)
            now = self._now()
            cursor = conn.execute("""UPDATE provider_models SET name=?,model_type=?,base_url=?,model=?,protocol=?,credential_ref=?,voice=?,
                version=version+1,validation_status=CASE WHEN validation_status='paused' THEN 'paused' ELSE 'pending' END,
                last_checked_at=NULL,last_verified_at=NULL,next_check_at=NULL,
                attempt_count=0,last_error_code=NULL,last_error=NULL,lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE id=? AND version=?""",
                (data["name"], data["model_type"], data["base_url"], data["model"], data["protocol"], data["credential_ref"], data["voice"],
                 now, model_id, version))
            if cursor.rowcount != 1:
                raise ModelRegistryError("version_conflict", "模型已被其他操作更新")
        return self.get(model_id)

    def list_models(self) -> list[dict[str, Any]]:
        """按创建时间读取模型和验证状态，不返回任何凭据值。"""
        with self._connection() as conn:
            return [self._view(dict(row)) for row in conn.execute("SELECT * FROM provider_models ORDER BY created_at,id")]

    def get(self, model_id: str) -> dict[str, Any]:
        """读取一条模型记录。"""
        self._id(model_id, "模型标识")
        with self._connection() as conn:
            return self._view(dict(self._require_model(conn, model_id)))

    def check_endpoint(self, config: dict[str, Any], exclude_id: str | None = None) -> str:
        """只读预检规范化实际请求 endpoint，供解封或写入 Key 前拒绝重复配置。"""
        if exclude_id is not None:
            self._id(exclude_id, "模型标识")
        data = self._fields(dict(config), allow_unconfigured=False)
        with self._connection() as conn:
            self._assert_endpoint_unique(conn, data, exclude_id=exclude_id)
        return self._endpoint_key(data["base_url"], data["model_type"], data["protocol"])

    def queue(self, model_id: str) -> dict[str, Any]:
        """人工请求重新验证；暂停项保持暂停，不能被后台工作者领取。"""
        self._id(model_id, "模型标识")
        with self._connection(immediate=True) as conn:
            current = self._require_model(conn, model_id)
            if current["validation_status"] == "paused":
                raise ModelRegistryError("conflict", "暂停的模型需先恢复后才能验证")
            now = self._now()
            if current["validation_status"] == "validating" and current["lease_until"] is not None and current["lease_until"] > now:
                raise ModelRegistryError("conflict", "模型正在验证中")
            conn.execute("""UPDATE provider_models SET validation_status='pending',next_check_at=NULL,last_error_code=NULL,last_error=NULL,
                lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?""", (now, model_id))
        return self.get(model_id)

    def set_paused(self, model_id: str, paused: bool) -> dict[str, Any]:
        """暂停或恢复验证，不删除模型配置或助手绑定。"""
        self._id(model_id, "模型标识")
        if not isinstance(paused, bool):
            raise ModelRegistryError("validation", "暂停标识必须是布尔值")
        with self._connection(immediate=True) as conn:
            self._require_model(conn, model_id)
            now = self._now()
            if paused:
                conn.execute("UPDATE provider_models SET validation_status='paused',next_check_at=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now, model_id))
            else:
                conn.execute("UPDATE provider_models SET validation_status='pending',next_check_at=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now, model_id))
        return self.get(model_id)

    def claim_due(self, worker_id: str, now: float | None = None, lease_seconds: int = 60) -> dict[str, Any] | None:
        """原子领取一条到期验证；同一时刻只有持有租约令牌的工作者可完成。"""
        self._text(worker_id, "工作者标识", 160)
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 60:
            raise ModelRegistryError("validation", "验证租约至少为 60 秒")
        with self._connection(immediate=True) as conn:
            # 写锁等待也会消耗租约，必须按取得锁后的时间回收和领取。
            timestamp = self._clock(now)
            conn.execute("""UPDATE provider_models SET validation_status='failed',next_check_at=NULL,
                last_error_code='validation_interrupted',last_error='验证中断，请手动重新验证',lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE validation_status='validating' AND (lease_until IS NULL OR lease_until <= ?)""", (timestamp, timestamp))
            row = conn.execute("SELECT * FROM provider_models WHERE validation_status='pending' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            cursor = conn.execute("""UPDATE provider_models SET validation_status='validating',lease_token=?,lease_until=?,
                last_checked_at=?,attempt_count=attempt_count+1,updated_at=? WHERE id=? AND version=? AND
                validation_status='pending'""",
                (token, timestamp + lease_seconds, timestamp, timestamp, row["id"], row["version"]))
            if cursor.rowcount != 1:
                return None
            claimed = dict(self._require_model(conn, row["id"]))
            claimed["lease_token"] = token
            return claimed

    def finish(self, model_id: str, version: int, lease_token: str, success: bool, error_code: str | None = None,
               message: str | None = None, now: float | None = None) -> dict[str, Any] | None:
        """以版本和租约令牌比较并完成验证；过期工作者的结果不会覆盖新状态。"""
        self._id(model_id, "模型标识")
        self._version(version)
        self._text(lease_token, "租约令牌", 80)
        if not isinstance(success, bool):
            raise ModelRegistryError("validation", "验证结果必须是布尔值")
        code, safe_message = self._error(error_code, message, success)
        with self._connection(immediate=True) as conn:
            # 获取写锁时租约可能已经到期；截止时刻与领取端统一为失效。
            timestamp = self._clock(now)
            if success:
                cursor = conn.execute("""UPDATE provider_models SET validation_status='verified',last_verified_at=?,next_check_at=NULL,
                    last_error_code=NULL,last_error=NULL,lease_token=NULL,lease_until=NULL,updated_at=?
                    WHERE id=? AND version=? AND validation_status='validating' AND lease_token=? AND lease_until>?""",
                    (timestamp, timestamp, model_id, version, lease_token, timestamp))
            else:
                cursor = conn.execute("""UPDATE provider_models SET validation_status='failed',next_check_at=NULL,last_error_code=?,last_error=?,
                    lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=? AND version=? AND validation_status='validating' AND lease_token=? AND lease_until>?""",
                    (code, safe_message, timestamp, model_id, version, lease_token, timestamp))
            if cursor.rowcount != 1:
                return None
            return dict(self._require_model(conn, model_id))

    def validate_ids(self, ids: object) -> list[str]:
        """验证助手选择的模型集合至多 20 项且全部已通过实际验证。"""
        normalized = self._ids(ids)
        with self._connection() as conn:
            self._require_verified(conn, normalized)
        return normalized

    def set_bindings(self, assistant_id: str, model_ids: object, connection: sqlite3.Connection | None = None) -> list[str]:
        """替换助手绑定；传入 ``connection`` 时加入调用方事务且不自行提交。"""
        self._id(assistant_id, "助手标识")
        normalized = self._ids(model_ids)
        if connection is not None:
            if not isinstance(connection, sqlite3.Connection):
                raise ModelRegistryError("validation", "绑定事务连接无效")
            self._replace_bindings(connection, assistant_id, normalized)
            return normalized
        with self._connection(immediate=True) as conn:
            self._replace_bindings(conn, assistant_id, normalized)
        return normalized

    def bindings(self, assistant_id: str) -> list[str]:
        """返回助手保存的模型标识，保留失效绑定以便界面提示。"""
        self._id(assistant_id, "助手标识")
        with self._connection() as conn:
            return [row["model_id"] for row in conn.execute("SELECT model_id FROM assistant_model_bindings WHERE assistant_id=? ORDER BY rowid", (assistant_id,))]

    def selected_models(self, assistant_id: str, require_verified: bool = True) -> list[dict[str, Any]]:
        """读取绑定模型；执行前默认拒绝编辑后失效或未验证的绑定。"""
        self._id(assistant_id, "助手标识")
        if not isinstance(require_verified, bool):
            raise ModelRegistryError("validation", "验证要求必须是布尔值")
        with self._connection() as conn:
            records = [dict(row) for row in conn.execute("""SELECT m.* FROM provider_models m
                JOIN assistant_model_bindings b ON b.model_id=m.id WHERE b.assistant_id=? ORDER BY b.rowid""", (assistant_id,))]
            if require_verified and any(record["validation_status"] != "verified" for record in records):
                raise ModelRegistryError("validation", "助手绑定包含未通过验证的模型")
            return records

    def import_legacy(self, assistant_id: str, services: object) -> list[dict[str, Any]]:
        """幂等导入旧服务为暂停的未配置模型，绝不自动探测或绑定。"""
        self._id(assistant_id, "助手标识")
        if not isinstance(services, list) or len(services) > 10:
            raise ModelRegistryError("validation", "旧模型服务必须是最多 10 项的列表")
        created: list[dict[str, Any]] = []
        with self._connection(immediate=True) as conn:
            for service in services:
                if not isinstance(service, dict):
                    raise ModelRegistryError("validation", "旧模型服务格式无效")
                raw = {key: service.get(key, "") for key in ("name", "base_url", "model", "credential_ref")}
                raw.update({"model_type": "unconfigured", "protocol": "openai-chat", "voice": "alloy"})
                data = self._fields(raw, allow_unconfigured=True)
                fingerprint = hashlib.sha256((assistant_id + "\0" + "\0".join(data[key] for key in ("name", "base_url", "model", "credential_ref"))).encode()).hexdigest()
                row = conn.execute("SELECT * FROM provider_models WHERE legacy_fingerprint=?", (fingerprint,)).fetchone()
                if row is None:
                    model_id, timestamp = str(uuid.uuid4()), self._now()
                    conn.execute("""INSERT INTO provider_models(id,name,model_type,base_url,model,protocol,credential_ref,voice,version,
                        validation_status,attempt_count,created_at,updated_at,legacy_fingerprint) VALUES (?,?,?,?,?,?,?,?,1,'paused',0,?,?,?)""",
                        (model_id, data["name"], data["model_type"], data["base_url"], data["model"], data["protocol"], data["credential_ref"], data["voice"], timestamp, timestamp, fingerprint))
                    row = self._require_model(conn, model_id)
                created.append(dict(row))
        return created

    def _initialize(self) -> None:
        with self._connection(immediate=True) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS provider_models(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, model_type TEXT NOT NULL CHECK(model_type IN ('reasoning','multimodal','speech_to_text','text_to_speech','embedding','rerank','unconfigured')),
                base_url TEXT NOT NULL, model TEXT NOT NULL, protocol TEXT NOT NULL CHECK(protocol IN ('openai-chat','openai-responses')),
                credential_ref TEXT NOT NULL DEFAULT '', voice TEXT NOT NULL DEFAULT 'alloy', version INTEGER NOT NULL,
                validation_status TEXT NOT NULL CHECK(validation_status IN ('pending','validating','verified','failed','paused')),
                last_checked_at REAL, last_verified_at REAL, next_check_at REAL, attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT, last_error TEXT, lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                legacy_fingerprint TEXT UNIQUE
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS provider_models_due ON provider_models(validation_status,next_check_at)")
            conn.execute("""CREATE TABLE IF NOT EXISTS assistant_model_bindings(
                assistant_id TEXT NOT NULL, model_id TEXT NOT NULL REFERENCES provider_models(id), PRIMARY KEY(assistant_id,model_id)
            )""")
            now = self._now()
            conn.execute("UPDATE provider_models SET next_check_at=NULL WHERE next_check_at IS NOT NULL")
            conn.execute("""UPDATE provider_models SET validation_status='failed',last_error_code='validation_interrupted',
                last_error='验证中断，请手动重新验证',lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE validation_status='validating' AND (lease_until IS NULL OR lease_until <= ?)""", (now, now))

    def _migrate_model_type_constraint(self) -> None:
        """为既有工作台数据库加入 rerank 枚举值，同时保留已绑定模型。"""
        raw = sqlite3.connect(self.path, isolation_level=None)
        try:
            row = raw.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='provider_models'").fetchone()
            if row is None or "'rerank'" in (row[0] or ""):
                return
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("BEGIN IMMEDIATE")
            bindings = list(raw.execute("SELECT assistant_id,model_id FROM assistant_model_bindings"))
            raw.execute("DROP TABLE assistant_model_bindings")
            raw.execute("ALTER TABLE provider_models RENAME TO provider_models_old")
            raw.execute("""CREATE TABLE provider_models(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, model_type TEXT NOT NULL CHECK(model_type IN ('reasoning','multimodal','speech_to_text','text_to_speech','embedding','rerank','unconfigured')),
                base_url TEXT NOT NULL, model TEXT NOT NULL, protocol TEXT NOT NULL CHECK(protocol IN ('openai-chat','openai-responses')),
                credential_ref TEXT NOT NULL DEFAULT '', voice TEXT NOT NULL DEFAULT 'alloy', version INTEGER NOT NULL,
                validation_status TEXT NOT NULL CHECK(validation_status IN ('pending','validating','verified','failed','paused')),
                last_checked_at REAL, last_verified_at REAL, next_check_at REAL, attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT, last_error TEXT, lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                legacy_fingerprint TEXT UNIQUE
            )""")
            raw.execute("INSERT INTO provider_models SELECT * FROM provider_models_old")
            raw.execute("DROP TABLE provider_models_old")
            raw.execute("CREATE INDEX IF NOT EXISTS provider_models_due ON provider_models(validation_status,next_check_at)")
            raw.execute("""CREATE TABLE assistant_model_bindings(
                assistant_id TEXT NOT NULL, model_id TEXT NOT NULL REFERENCES provider_models(id), PRIMARY KEY(assistant_id,model_id)
            )""")
            raw.executemany("INSERT INTO assistant_model_bindings(assistant_id,model_id) VALUES (?,?)", bindings)
            if raw.execute("PRAGMA foreign_key_check").fetchone():
                raise ModelRegistryError("migration_invalid", "模型迁移的绑定校验未通过")
            raw.execute("COMMIT")
        except BaseException:
            if raw.in_transaction:
                raw.execute("ROLLBACK")
            raise
        finally:
            raw.close()

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

    def _fields(self, fields: dict[str, Any], allow_unconfigured: bool) -> dict[str, str]:
        expected = {"name", "model_type", "base_url", "model", "protocol", "credential_ref", "voice"}
        if set(fields) != expected:
            raise ModelRegistryError("validation", "模型字段无效")
        data = {key: self._clean(fields[key], key, 4096) for key in expected}
        self._text(data["name"], "模型名称", 160)
        if data["model_type"] != "unconfigured" or data["model"]:
            self._text(data["model"], "模型标识", 240)
        self._text(data["voice"], "语音", 120)
        if data["model_type"] not in (_ALL_MODEL_TYPES if allow_unconfigured else _MODEL_TYPES):
            raise ModelRegistryError("validation", "模型类型无效")
        if data["protocol"] not in _PROTOCOLS:
            raise ModelRegistryError("validation", "模型协议无效")
        if data["model_type"] in {"speech_to_text", "text_to_speech", "embedding", "rerank"} and data["protocol"] != "openai-chat":
            raise ModelRegistryError("validation", "音频、向量和重排序模型只支持 OpenAI Chat 兼容协议")
        self._url(data["base_url"])
        if data["credential_ref"] and (not _VAULT_REF.fullmatch(data["credential_ref"]) or data["credential_ref"][6:].lower().startswith(("sk-", "bearer", "eyj"))):
            raise ModelRegistryError("validation", "凭据只能填写 vault:<entry-ref> 保险库引用，不能填写 API Key 明文")
        return data

    @staticmethod
    def _url(value: str) -> None:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ModelRegistryError("validation", "接口地址需为 HTTP(S) 地址，不能带账号、密码、查询参数或片段")

    @staticmethod
    def _endpoint_key(base_url: str, model_type: str, protocol: str) -> str:
        """按探针追加能力路径的规则归一化接口地址，避免 endpoint 与 base URL 重复登记。"""
        try: return resolve_endpoint(base_url, model_type, protocol)
        except ValueError as error: raise ModelRegistryError("validation", "接口路径与模型能力不匹配") from error

    def _assert_endpoint_unique(self, conn: sqlite3.Connection, data: dict[str, str], exclude_id: str | None = None) -> None:
        """同一规范化实际接口只能登记一条；历史无效或未配置记录保留但不参与唯一键。"""
        candidate = self._endpoint_key(data["base_url"], data["model_type"], data["protocol"])
        for row in conn.execute("SELECT id,base_url,model_type,protocol FROM provider_models"):
            if row["id"] == exclude_id:continue
            try: existing=self._endpoint_key(row["base_url"],row["model_type"],row["protocol"])
            except ModelRegistryError:continue
            if existing == candidate:
                raise ModelRegistryError("duplicate_endpoint", "该接口已存在，请编辑已有模型 Key")

    @classmethod
    def _view(cls, row: dict[str, Any]) -> dict[str, Any]:
        try:endpoint=cls._endpoint_key(row["base_url"],row["model_type"],row["protocol"])
        except ModelRegistryError:endpoint=None
        return {**row,"effective_endpoint":endpoint}

    @staticmethod
    def _error(code: object, message: object, success: bool) -> tuple[str | None, str | None]:
        if success:
            if code is not None or message is not None:
                raise ModelRegistryError("validation", "成功验证不能附带错误信息")
            return None, None
        safe_code = "validation_failed" if code is None else ModelRegistry._clean(code, "错误码", 80)
        safe_message = "验证失败" if message is None else ModelRegistry._clean(message, "错误消息", 240)
        if any(marker in safe_message.lower() for marker in ("bearer ", "api_key", "password", "token=")):
            raise ModelRegistryError("validation", "错误消息不能包含认证信息")
        return safe_code, safe_message

    @staticmethod
    def _ids(ids: object) -> list[str]:
        if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(item, str) for item in ids):
            raise ModelRegistryError("validation", "模型选择必须是最多 20 个模型标识")
        result = list(dict.fromkeys(ids))
        if len(result) != len(ids):
            raise ModelRegistryError("validation", "模型选择不能重复")
        for model_id in result:
            ModelRegistry._id(model_id, "模型标识")
        return result

    @staticmethod
    def _require_verified(conn: sqlite3.Connection, ids: list[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT id,validation_status FROM provider_models WHERE id IN ({placeholders})", ids).fetchall()
        found = {row[0]: row[1] for row in rows}
        if any(found.get(model_id) != "verified" for model_id in ids):
            raise ModelRegistryError("validation", "只能绑定已通过验证的模型")

    @classmethod
    def _replace_bindings(cls, conn: sqlite3.Connection, assistant_id: str, ids: list[str]) -> None:
        cls._require_verified(conn, ids)
        conn.execute("DELETE FROM assistant_model_bindings WHERE assistant_id=?", (assistant_id,))
        conn.executemany("INSERT INTO assistant_model_bindings(assistant_id,model_id) VALUES (?,?)", ((assistant_id, model_id) for model_id in ids))

    @staticmethod
    def _require_model(conn: sqlite3.Connection, model_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM provider_models WHERE id=?", (model_id,)).fetchone()
        if row is None:
            raise ModelRegistryError("not_found", "模型不存在")
        return row

    @staticmethod
    def _clock(now: float | None) -> float:
        if now is None:
            return time.time()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ModelRegistryError("validation", "时间必须是秒级时间戳")
        return float(now)

    @staticmethod
    def _now() -> float:
        """返回持久化验证状态使用的当前 Unix 秒级时间戳。"""
        return time.time()

    @staticmethod
    def _version(value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ModelRegistryError("validation", "版本号无效")

    @staticmethod
    def _id(value: object, label: str) -> None:
        if not isinstance(value, str):
            raise ModelRegistryError("validation", f"{label}无效")
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError):
            raise ModelRegistryError("validation", f"{label}无效") from None

    @staticmethod
    def _text(value: object, label: str, maximum: int) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ModelRegistryError("validation", f"{label}无效")

    @staticmethod
    def _clean(value: object, label: str, maximum: int) -> str:
        if not isinstance(value, str) or len(value) > maximum:
            raise ModelRegistryError("validation", f"{label}无效")
        return value.strip()
