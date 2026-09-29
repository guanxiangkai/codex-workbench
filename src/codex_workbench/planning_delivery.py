"""Delivery contracts for planning work.

This module deliberately owns no task, asset, or runner authority.  Callers
validate the task and advance its optimistic version in their existing SQLite
transaction; this module stores the immutable delivery record around it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from .store import StoreError


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _error(code, message):
    raise StoreError(code, message)


class PlanningDelivery:
    """Persist delivery review data using a Planning instance's db and lock.

    A mutation receives ``verify_task(task_id, expected_version)`` and
    ``advance_task(task)``.  The former must apply the parent service's access
    policy; the latter must increment ``tasks.version`` on this same connection.
    Pass ``commit=False`` when the caller owns the surrounding transaction.
    """

    def __init__(self, db, lock=None):
        self.db = db
        self.lock = lock or threading.RLock()
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS planning_delivery_cards (
                    task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS planning_delivery_anchors (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                    asset_sha256 TEXT NOT NULL, run_id TEXT, message_id TEXT,
                    descriptor_kind TEXT NOT NULL, descriptor TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS planning_delivery_annotations (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, anchor_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                    position TEXT NOT NULL, change_text TEXT NOT NULL, preserve TEXT NOT NULL,
                    acceptance TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(task_id, operation_id)
                );
                CREATE TABLE IF NOT EXISTS planning_delivery_evidence (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, criterion_id TEXT NOT NULL,
                    executor_state TEXT NOT NULL, validation_state TEXT NOT NULL,
                    user_acceptance TEXT NOT NULL, usage TEXT, source_ref TEXT,
                    summary TEXT, check_name TEXT, result TEXT, manual_validation INTEGER NOT NULL DEFAULT 0,
                    card_revision INTEGER, card_sha256 TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS planning_delivery_checkpoints (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, round INTEGER NOT NULL,
                    evidence_sha256 TEXT NOT NULL, decision TEXT NOT NULL,
                    suggestions TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(task_id, round), UNIQUE(task_id, evidence_sha256)
                );
                CREATE TABLE IF NOT EXISTS planning_delivery_checkpoint_evidence (
                    checkpoint_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
                    PRIMARY KEY(checkpoint_id, evidence_id), UNIQUE(evidence_id)
                );
                CREATE TABLE IF NOT EXISTS planning_delivery_knowledge_candidates (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, scope TEXT NOT NULL,
                    title TEXT NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                    source_ref TEXT NOT NULL, card_revision INTEGER NOT NULL, card_sha256 TEXT NOT NULL,
                    verified_at TEXT NOT NULL, applicability TEXT NOT NULL,
                    relation_type TEXT, direction TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(task_id, scope, content_sha256, source_ref, card_revision)
                );
            """)
            # Early builds keyed operation_id globally.  Preserve rows while
            # scoping replay identity to its task, as the public contract does.
            legacy_unique = any(
                [column['name'] for column in self.db.execute(f"PRAGMA index_info({index['name']})")] == ['operation_id']
                for index in self.db.execute("PRAGMA index_list(planning_delivery_annotations)") if index['unique'])
            if legacy_unique:
                self.db.executescript("""
                    CREATE TABLE planning_delivery_annotations_next (
                        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, anchor_id TEXT NOT NULL,
                        operation_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                        position TEXT NOT NULL, change_text TEXT NOT NULL, preserve TEXT NOT NULL,
                        acceptance TEXT NOT NULL, created_at TEXT NOT NULL,
                        UNIQUE(task_id, operation_id)
                    );
                    INSERT INTO planning_delivery_annotations_next
                        SELECT id,task_id,anchor_id,operation_id,payload_sha256,position,change_text,preserve,acceptance,created_at
                        FROM planning_delivery_annotations;
                    DROP TABLE planning_delivery_annotations;
                    ALTER TABLE planning_delivery_annotations_next RENAME TO planning_delivery_annotations;
                """)
            for column, definition in (("source_ref", "TEXT"), ("summary", "TEXT"), ("check_name", "TEXT"),
                                       ("result", "TEXT"), ("manual_validation", "INTEGER NOT NULL DEFAULT 0"),
                                       ("card_revision", "INTEGER"), ("card_sha256", "TEXT")):
                try:
                    self.db.execute(f"ALTER TABLE planning_delivery_evidence ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            self.db.commit()

    @staticmethod
    def _text(value, field, limit=10000, *, empty=False):
        if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
            _error("validation", f"{field}无效")
        return value.strip()

    @classmethod
    def _strings(cls, value, field, *, limit=100, item_limit=4000):
        if not isinstance(value, list) or len(value) > limit:
            _error("validation", f"{field}必须是有限文本列表")
        return [cls._text(item, field, item_limit) for item in value]

    @classmethod
    def normalize_card(cls, card):
        if not isinstance(card, dict) or set(card) != {"goal", "scope", "preserve", "acceptance", "facts", "assumptions", "original"}:
            _error("validation", "任务卡字段不完整")
        acceptance = card["acceptance"]
        if not isinstance(acceptance, list) or not acceptance or len(acceptance) > 100:
            _error("validation", "验收条件无效")
        normalized_acceptance = []
        seen = set()
        for criterion in acceptance:
            if not isinstance(criterion, dict) or set(criterion) != {"id", "text"}:
                _error("validation", "验收条件必须包含 id 和 text")
            criterion_id = cls._text(criterion["id"], "验收条件标识", 160)
            if criterion_id in seen:
                _error("validation", "验收条件标识重复")
            seen.add(criterion_id)
            normalized_acceptance.append({"id": criterion_id, "text": cls._text(criterion["text"], "验收条件", 4000)})
        return {
            "goal": cls._text(card["goal"], "目标", 10000),
            "scope": cls._strings(card["scope"], "范围"),
            "preserve": cls._strings(card["preserve"], "保留项"),
            "acceptance": normalized_acceptance,
            "facts": cls._strings(card["facts"], "事实"),
            "assumptions": cls._strings(card["assumptions"], "假设"),
            "original": cls._text(card["original"], "原始需求", 100000),
        }

    def _mutate(self, task_id, expected_version, verify_task, advance_task, action, *, commit):
        with self.lock:
            savepoint = "planning_delivery_" + uuid.uuid4().hex
            self.db.execute(f"SAVEPOINT {savepoint}")
            try:
                task = verify_task(task_id, expected_version)
                result = action(task)
                advanced = advance_task(task)
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            if commit:
                self.db.commit()
            result["task_version"] = advanced["version"] if isinstance(advanced, dict) else advanced
            return result

    def save_card(self, task_id, expected_version, card, *, verify_task, advance_task, commit=True):
        payload = self.normalize_card(card)
        def action(_task):
            row = self.db.execute("SELECT revision FROM planning_delivery_cards WHERE task_id=?", (task_id,)).fetchone()
            revision = (row[0] if row else 0) + 1
            stamp = _now()
            self.db.execute("""INSERT INTO planning_delivery_cards(task_id,revision,payload,created_at,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,updated_at=excluded.updated_at""",
                            (task_id, revision, _json(payload), stamp, stamp))
            return {"revision": revision, "card": payload}
        return self._mutate(task_id, expected_version, verify_task, advance_task, action, commit=commit)

    @staticmethod
    def _context_id(context, field, task_id, *, optional=False):
        if context is None and optional:
            return None
        if not isinstance(context, dict) or not isinstance(context.get("id"), str) or not context["id"]:
            _error("validation", f"{field}上下文无效")
        if context.get("task_id") != task_id:
            _error("ownership", f"{field}不属于任务")
        return context["id"]

    @classmethod
    def _descriptor(cls, descriptor):
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("type"), str):
            _error("validation", "锚点描述无效")
        kind = descriptor["type"]
        if kind == "text_quote":
            if set(descriptor) != {"type", "quote", "start", "end"} or not isinstance(descriptor["start"], int) or not isinstance(descriptor["end"], int) or descriptor["start"] < 0 or descriptor["end"] <= descriptor["start"]:
                _error("validation", "文本引用锚点无效")
            return kind, {"type": kind, "quote": cls._text(descriptor["quote"], "引用", 5000), "start": descriptor["start"], "end": descriptor["end"]}
        if kind == "component":
            if set(descriptor) != {"type", "component", "region"}:
                _error("validation", "组件锚点无效")
            return kind, {"type": kind, "component": cls._text(descriptor["component"], "组件", 500), "region": cls._text(descriptor["region"], "组件区域", 500)}
        if kind == "region":
            if set(descriptor) != {"type", "page", "x", "y", "width", "height"} or not isinstance(descriptor["page"], int) or descriptor["page"] < 1:
                _error("validation", "区域锚点无效")
            values = [descriptor[key] for key in ("x", "y", "width", "height")]
            if any(not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 or value > 1 for value in values) or descriptor["width"] == 0 or descriptor["height"] == 0 or descriptor["x"] + descriptor["width"] > 1 or descriptor["y"] + descriptor["height"] > 1:
                _error("validation", "区域锚点无效")
            return kind, descriptor
        _error("validation", "不支持的锚点描述类型")

    def create_anchor(self, task_id, expected_version, asset, run, message, descriptor, *, verify_task, advance_task, commit=True):
        if not isinstance(asset, dict) or not isinstance(asset.get("id"), str) or not isinstance(asset.get("sha256"), str) or len(asset["sha256"]) != 64:
            _error("validation", "资产上下文无效")
        run_id = self._context_id(run, "运行", task_id, optional=True)
        message_id = self._context_id(message, "消息", task_id, optional=True)
        kind, normalized = self._descriptor(descriptor)
        anchor = {"id": str(uuid.uuid4()), "task_id": task_id, "asset_id": asset["id"], "asset_sha256": asset["sha256"], "run_id": run_id, "message_id": message_id, "descriptor_kind": kind, "descriptor": normalized}
        def action(_task):
            self.db.execute("INSERT INTO planning_delivery_anchors VALUES(?,?,?,?,?,?,?,?,?)", (*[anchor[key] for key in ("id", "task_id", "asset_id", "asset_sha256", "run_id", "message_id", "descriptor_kind")], _json(normalized), _now()))
            return anchor.copy()
        return self._mutate(task_id, expected_version, verify_task, advance_task, action, commit=commit)

    def require_current_anchor(self, anchor_id, task_id, asset, run, message):
        row = self.db.execute("SELECT * FROM planning_delivery_anchors WHERE id=? AND task_id=?", (anchor_id, task_id)).fetchone()
        if row is None:
            _error("not_found", "锚点不存在")
        anchor = dict(row)
        if not isinstance(asset, dict) or asset.get("id") != anchor["asset_id"] or asset.get("sha256") != anchor["asset_sha256"]:
            _error("stale_anchor", "资产内容已变化，锚点已失效")
        if self._context_id(run, "运行", task_id, optional=True) != anchor["run_id"] or self._context_id(message, "消息", task_id, optional=True) != anchor["message_id"]:
            _error("stale_anchor", "运行或消息上下文已变化，锚点已失效")
        anchor["descriptor"] = json.loads(anchor["descriptor"])
        return anchor

    def add_annotation(self, task_id, expected_version, anchor_id, asset, run, message, *, position, change_text, preserve, acceptance, operation_id, verify_task, advance_task, commit=True):
        if not isinstance(position, dict) or not position or any(not isinstance(key, str) or not isinstance(value, (str, int, float, bool, type(None))) for key, value in position.items()):
            _error("validation", "注释位置无效")
        operation_id = self._text(operation_id, "操作标识", 160)
        payload = {"anchor_id": anchor_id, "position": position, "change_text": self._text(change_text, "修改说明", 10000), "preserve": self._strings(preserve, "保留项"), "acceptance": self._strings(acceptance, "关联验收条件")}
        digest = hashlib.sha256(_json(payload).encode()).hexdigest()
        with self.lock:
            prior = self.db.execute("SELECT id,payload_sha256 FROM planning_delivery_annotations WHERE task_id=? AND operation_id=?", (task_id, operation_id)).fetchone()
            if prior:
                if prior["payload_sha256"] != digest:
                    _error("idempotency_conflict", "操作标识已用于不同请求")
                return {"id": prior["id"], "idempotent": True, "task_version": expected_version}
        def action(_task):
            self.require_current_anchor(anchor_id, task_id, asset, run, message)
            annotation_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO planning_delivery_annotations VALUES(?,?,?,?,?,?,?,?,?,?)", (annotation_id, task_id, anchor_id, operation_id, digest, _json(position), payload["change_text"], _json(payload["preserve"]), _json(payload["acceptance"]), _now()))
            return {"id": annotation_id, "idempotent": False}
        return self._mutate(task_id, expected_version, verify_task, advance_task, action, commit=commit)

    def _card_criterion(self, task_id, criterion_id):
        row = self.db.execute("SELECT revision,payload FROM planning_delivery_cards WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            _error("validation", "任务尚未保存验收卡")
        card = json.loads(row["payload"])
        if criterion_id not in {item["id"] for item in card["acceptance"]}:
            _error("validation", "验收条件不属于当前任务卡")
        return row["revision"], hashlib.sha256(_json(card).encode()).hexdigest()

    def _source_ref(self, task_id, source_ref):
        if not isinstance(source_ref, dict) or source_ref.get("kind") not in {"asset", "run"} or not isinstance(source_ref.get("id"), str):
            _error("validation", "证据来源必须是本任务资产或运行")
        if source_ref["kind"] == "asset":
            row = self.db.execute("SELECT id,sha256 FROM assets WHERE id=?", (source_ref["id"],)).fetchone()
            linked = self.db.execute("SELECT 1 FROM asset_refs WHERE asset_id=? AND task_id=?", (source_ref["id"], task_id)).fetchone()
            if row is None or linked is None or source_ref.get("sha256") != row["sha256"]:
                _error("stale_anchor", "资产证据不存在、未关联或哈希已变化")
            return {"kind": "asset", "id": row["id"], "sha256": row["sha256"]}
        row = self.db.execute("SELECT id,state FROM runs WHERE id=? AND task_id=?", (source_ref["id"], task_id)).fetchone()
        if row is None:
            _error("validation", "运行证据不存在或不属于任务")
        return {"kind": "run", "id": row["id"], "state": row["state"]}

    def record_evidence(self, task_id, expected_version, *, criterion_id, executor_state, validation_state, source_ref, summary, check_name, result, manual_validation=False, usage=None, verify_task, advance_task, commit=True):
        criterion_id = self._text(criterion_id, "验收条件标识", 160)
        if executor_state not in {"unknown", "queued", "running", "completed", "failed"} or validation_state not in {"unknown", "passed", "failed"}:
            _error("validation", "执行或验证状态无效")
        if not isinstance(manual_validation, bool):
            _error("validation", "人工检查标识无效")
        if usage is not None and not isinstance(usage, dict):
            _error("validation", "用量必须为对象或未知")
        def action(_task):
            revision, card_sha256 = self._card_criterion(task_id, criterion_id)
            source = self._source_ref(task_id, source_ref)
            if validation_state == "passed" and executor_state != "completed":
                _error("validation", "通过验证必须对应已完成执行")
            if validation_state == "passed" and source["kind"] == "run" and source["state"] != "review":
                _error("validation", "运行尚未进入 review，不能作为通过证据")
            evidence_id = str(uuid.uuid4())
            self.db.execute("""INSERT INTO planning_delivery_evidence(id,task_id,criterion_id,executor_state,validation_state,user_acceptance,usage,source_ref,summary,check_name,result,manual_validation,card_revision,card_sha256,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (evidence_id, task_id, criterion_id, executor_state, validation_state, "pending", _json(usage) if usage is not None else None, _json(source), self._text(summary, "证据摘要", 10000), self._text(check_name, "检查名称", 500), self._text(result, "检查结果", 10000), int(manual_validation), revision, card_sha256, _now()))
            return {"id": evidence_id, "executor_state": executor_state, "validation_state": validation_state, "user_acceptance": "pending", "usage": usage, "source_ref": source, "manual_validation": manual_validation}
        return self._mutate(task_id, expected_version, verify_task, advance_task, action, commit=commit)

    def accept_evidence(self, task_id, expected_version, evidence_id, *, accepted, verify_task, advance_task, commit=True):
        if not isinstance(accepted, bool):
            _error("validation", "用户验收值无效")
        def action(_task):
            row = self.db.execute("SELECT * FROM planning_delivery_evidence WHERE id=? AND task_id=?", (evidence_id, task_id)).fetchone()
            if row is None:
                _error("not_found", "交付证据不存在")
            if accepted and row["validation_state"] != "passed":
                _error("unverified_acceptance", "没有通过验证的证据不能验收")
            if accepted:
                revision, card_sha256 = self._card_criterion(task_id, row["criterion_id"])
                if row["card_revision"] != revision or row["card_sha256"] != card_sha256:
                    _error("stale_evidence", "任务卡已修订，必须重新验证")
                self._source_ref(task_id, json.loads(row["source_ref"]))
            state = "accepted" if accepted else "rejected"
            self.db.execute("UPDATE planning_delivery_evidence SET user_acceptance=? WHERE id=?", (state, evidence_id))
            return {"id": evidence_id, "user_acceptance": state}
        return self._mutate(task_id, expected_version, verify_task, advance_task, action, commit=commit)

    def checkpoint(self, task_id, expected_version, evidence_ids, *, decision, suggestions, permissions_requested=False, verify_task, advance_task, commit=True):
        if permissions_requested or decision == "grant_permission":
            _error("permission_denied", "监督检查点不能授予权限")
        if decision not in {"continue", "correct", "stop"}:
            _error("validation", "监督决策无效")
        if not isinstance(evidence_ids, list) or not evidence_ids or len(evidence_ids) > 100 or any(not isinstance(value, str) or not value for value in evidence_ids):
            _error("validation", "监督检查点需要真实证据")
        suggestions = self._strings(suggestions, "修订建议", limit=20)
        evidence_ids = sorted(set(evidence_ids))
        def action(_task):
            placeholders = ",".join("?" for _ in evidence_ids)
            rows = self.db.execute(f"SELECT id,validation_state,criterion_id,card_revision,card_sha256,source_ref FROM planning_delivery_evidence WHERE task_id=? AND id IN ({placeholders})", (task_id, *evidence_ids)).fetchall()
            if len(rows) != len(evidence_ids) or any(row["validation_state"] == "unknown" for row in rows):
                _error("validation", "监督检查点需要已记录的验证证据")
            for row in rows:
                revision, card_sha256 = self._card_criterion(task_id, row["criterion_id"])
                if row["card_revision"] != revision or row["card_sha256"] != card_sha256:
                    _error("stale_evidence", "监督证据对应的任务卡已修订")
                self._source_ref(task_id, json.loads(row["source_ref"]))
            used = self.db.execute("SELECT count(*) FROM planning_delivery_checkpoints WHERE task_id=?", (task_id,)).fetchone()[0]
            if used >= 2:
                _error("correction_limit", "最多允许两轮修订检查")
            digest = hashlib.sha256(_json(evidence_ids).encode()).hexdigest()
            if self.db.execute("SELECT 1 FROM planning_delivery_checkpoints WHERE task_id=? AND evidence_sha256=?", (task_id, digest)).fetchone():
                _error("evidence_loop", "不能使用同一组证据重复检查")
            if self.db.execute(f"SELECT 1 FROM planning_delivery_checkpoint_evidence WHERE evidence_id IN ({placeholders})", evidence_ids).fetchone():
                _error("evidence_loop", "证据已用于先前检查，不能形成重复证据循环")
            checkpoint_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO planning_delivery_checkpoints VALUES(?,?,?,?,?,?,?)", (checkpoint_id, task_id, used + 1, digest, decision, _json(suggestions), _now()))
            self.db.executemany("INSERT INTO planning_delivery_checkpoint_evidence VALUES(?,?)", [(checkpoint_id, evidence_id) for evidence_id in evidence_ids])
            return {"id": checkpoint_id, "round": used + 1, "decision": decision, "suggestions": suggestions, "starts_runner": False}
        return self._mutate(task_id, expected_version, verify_task, advance_task, action, commit=commit)

    def metrics(self, task_id):
        rows = self.db.execute("SELECT usage FROM planning_delivery_evidence WHERE task_id=?", (task_id,)).fetchall()
        known = [json.loads(row["usage"]) for row in rows if row["usage"] is not None]
        metric = {"evidence_count": len(rows), "usage": known or None,
                  "rework_count": None, "first_round_acceptance": "unknown",
                  "correction_acceptance": "unknown", "runtime_ms": None,
                  "stopped": None, "failed": None,
                  "first_draft_attribution": "unknown", "user_correction": "unknown"}
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "planning_changes" in tables:
            # Only an explicit rework-prepared event is a counted rework. An annotation alone
            # may be a note, and supplement messages do not establish who authored a correction.
            metric["rework_count"] = self.db.execute("SELECT count(*) FROM planning_changes WHERE task_id=? AND kind='rework_prepared'", (task_id,)).fetchone()[0]
        if {"planning_delivery_checkpoints", "planning_delivery_checkpoint_evidence"} <= tables:
            rounds = self.db.execute("""SELECT c.round,e.user_acceptance FROM planning_delivery_checkpoints c
                JOIN planning_delivery_checkpoint_evidence ce ON ce.checkpoint_id=c.id
                JOIN planning_delivery_evidence e ON e.id=ce.evidence_id
                WHERE c.task_id=? ORDER BY c.round""", (task_id,)).fetchall()
            by_round = {}
            for row in rounds:
                by_round.setdefault(row["round"], []).append(row["user_acceptance"])
            def result(values):
                if not values:
                    return "unknown"
                if any(value == "rejected" for value in values):
                    return "rejected"
                if all(value == "accepted" for value in values):
                    return "accepted"
                return "pending"
            metric["first_round_acceptance"] = result(by_round.get(1))
            metric["correction_acceptance"] = result(by_round.get(2))
        if "runs" in tables:
            runs = self.db.execute("SELECT state,created_at,finished_at FROM runs WHERE task_id=?", (task_id,)).fetchall()
            if runs:
                durations = []
                for row in runs:
                    try:
                        started = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                        finished = datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00")) if row["finished_at"] else None
                    except (TypeError, ValueError):
                        finished = None
                    if finished is None:
                        durations = None
                        break
                    durations.append(max(0, round((finished - started).total_seconds() * 1000)))
                metric["runtime_ms"] = sum(durations) if durations is not None else None
                states = {row["state"] for row in runs}
                metric["failed"] = "failed" in states
                metric["stopped"] = bool(states & {"cancelled", "interrupted"})
                if "planning_delivery_checkpoints" in tables and self.db.execute(
                        "SELECT 1 FROM planning_delivery_checkpoints WHERE task_id=? AND decision='stop'", (task_id,)).fetchone():
                    metric["stopped"] = True
        return metric

    def detail(self, task_id):
        card = self.db.execute("SELECT revision,payload,updated_at FROM planning_delivery_cards WHERE task_id=?", (task_id,)).fetchone()
        card_value = None if card is None else {"revision": card["revision"], "updated_at": card["updated_at"], "card": json.loads(card["payload"])}
        anchors = []
        for row in self.db.execute("SELECT * FROM planning_delivery_anchors WHERE task_id=? ORDER BY created_at,id", (task_id,)):
            value = dict(row); value["descriptor"] = json.loads(value["descriptor"]); anchors.append(value)
        annotations = []
        for row in self.db.execute("SELECT * FROM planning_delivery_annotations WHERE task_id=? ORDER BY created_at,id", (task_id,)):
            value = dict(row)
            for key in ("position", "preserve", "acceptance"):
                value[key] = json.loads(value[key])
            annotations.append(value)
        evidence = []
        for row in self.db.execute("SELECT * FROM planning_delivery_evidence WHERE task_id=? ORDER BY created_at,id", (task_id,)):
            value = dict(row)
            value["usage"] = json.loads(value["usage"]) if value["usage"] is not None else None
            value["source_ref"] = json.loads(value["source_ref"]) if value["source_ref"] else None
            value["manual_validation"] = bool(value["manual_validation"])
            evidence.append(value)
        checkpoints = []
        for row in self.db.execute("SELECT * FROM planning_delivery_checkpoints WHERE task_id=? ORDER BY round", (task_id,)):
            value = dict(row); value["suggestions"] = json.loads(value["suggestions"]); checkpoints.append(value)
        candidates = []
        for row in self.db.execute("SELECT * FROM planning_delivery_knowledge_candidates WHERE task_id=? ORDER BY created_at,id", (task_id,)):
            value = dict(row)
            value["source"] = json.loads(value.pop("source_ref"))
            value["sha256"] = value.pop("content_sha256")
            value["applicability"] = json.loads(value["applicability"])
            value["reviewed"] = False
            value["local_only"] = True
            candidates.append(value)
        return {"card": card_value, "anchors": anchors, "annotations": annotations, "evidence": evidence,
                "checkpoints": checkpoints, "knowledge_candidates": candidates, "metrics": self.metrics(task_id)}

    def save_knowledge_candidates(self, task_id, candidates):
        """Persist only unreviewed local candidates derived from accepted evidence."""
        if not isinstance(candidates, list):
            _error("validation", "知识候选无效")
        saved = []
        with self.lock, self.db:
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("status") != "candidate" or candidate.get("reviewed") is not False:
                    _error("validation", "知识候选必须待审核")
                source = candidate.get("source")
                applicability = candidate.get("applicability")
                if (not isinstance(source, dict) or not isinstance(candidate.get("scope"), str)
                        or not isinstance(candidate.get("title"), str) or not isinstance(candidate.get("content"), str)
                        or not isinstance(candidate.get("sha256"), str) or not isinstance(candidate.get("card_revision"), int)
                        or not isinstance(candidate.get("card_sha256"), str) or not isinstance(candidate.get("verified_at"), str)
                        or applicability != {"status": "unknown", "reason": "pending_confirmation"}):
                    _error("validation", "知识候选来源或适用性无效")
                identity = _json({"task_id": task_id, "scope": candidate["scope"], "sha256": candidate["sha256"],
                                  "source": source, "card_revision": candidate["card_revision"]})
                candidate_id = hashlib.sha256(identity.encode()).hexdigest()
                self.db.execute("""INSERT OR IGNORE INTO planning_delivery_knowledge_candidates
                    (id,task_id,scope,title,content,content_sha256,source_ref,card_revision,card_sha256,verified_at,applicability,relation_type,direction,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (candidate_id, task_id, candidate["scope"], candidate["title"], candidate["content"], candidate["sha256"],
                     _json(source), candidate["card_revision"], candidate["card_sha256"], candidate["verified_at"],
                     _json(applicability), candidate.get("relation_type"), candidate.get("direction"), "candidate", _now()))
                row = self.db.execute("SELECT * FROM planning_delivery_knowledge_candidates WHERE id=?", (candidate_id,)).fetchone()
                value = dict(row)
                value["source"] = json.loads(value.pop("source_ref")); value["sha256"] = value.pop("content_sha256")
                value["applicability"] = json.loads(value["applicability"]); value["reviewed"] = False; value["local_only"] = True
                saved.append(value)
        return saved
