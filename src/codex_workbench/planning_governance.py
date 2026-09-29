"""Evidence-bound governance records for planning tasks.

This module deliberately does not read instruction files or apply edits.  The
caller supplies a manifest collected through its authorised execution context;
we retain only canonical metadata and hashes so a later review can establish
which version was actually evaluated.
"""
from __future__ import annotations

import hashlib
import difflib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .store import StoreError


MAX_JSON_BYTES = 96_000
MAX_CASES = 12
MAX_TEXT = 12_000
_PROTECTED_KINDS = frozenset({"permission", "security", "authorization"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text_sha(value: str) -> str:
    """Hash original UTF-8 text; unlike _sha, this must not canonicalise it."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: Any, label: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise StoreError("validation", f"{label}无效")
    return value.strip()


def _raw_text(value: Any, label: str, limit: int = MAX_TEXT) -> str:
    """Validate content without normalising it before its source hash is recorded."""
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise StoreError("validation", f"{label}无效")
    return value


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StoreError("validation", f"{label}必须是对象")
    if len(_canonical(value).encode("utf-8")) > MAX_JSON_BYTES:
        raise StoreError("limit", f"{label}过大")
    return value


class Governance:
    """Persist governance evidence while keeping task, scope and run boundaries explicit."""

    def __init__(self, db: sqlite3.Connection):
        if not isinstance(db, sqlite3.Connection):
            raise TypeError("Governance requires an sqlite3.Connection")
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS planning_governance_manifests (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(task_id, run_id, manifest_hash));
                CREATE INDEX IF NOT EXISTS planning_governance_manifests_task
                    ON planning_governance_manifests(task_id, created_at);
                CREATE TABLE IF NOT EXISTS planning_governance_suites (
                    task_id TEXT PRIMARY KEY, suite_hash TEXT NOT NULL, scope TEXT NOT NULL,
                    baseline_manifest_hash TEXT, payload TEXT NOT NULL,
                    frozen_sequence INTEGER NOT NULL, frozen_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS planning_governance_candidates (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, scope TEXT NOT NULL,
                    baseline_manifest_hash TEXT NOT NULL, payload TEXT NOT NULL,
                    budget TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS planning_governance_candidates_task
                    ON planning_governance_candidates(task_id, created_at);
                CREATE TABLE IF NOT EXISTS planning_governance_feedback (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, candidate_id TEXT,
                    root_cause TEXT NOT NULL, independence_key TEXT NOT NULL,
                    source_kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS planning_governance_feedback_task
                    ON planning_governance_feedback(task_id, root_cause, independence_key);
                CREATE TABLE IF NOT EXISTS planning_governance_evaluations (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                    suite_hash TEXT NOT NULL, payload TEXT NOT NULL, summary TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS planning_governance_evaluations_candidate
                    ON planning_governance_evaluations(task_id, candidate_id, created_at);
                CREATE TABLE IF NOT EXISTS planning_governance_evaluation_attempts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                    suite_hash TEXT NOT NULL, baseline_manifest_hash TEXT NOT NULL,
                    state TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(task_id, candidate_id, suite_hash, baseline_manifest_hash));
            """)
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(planning_governance_suites)")}
            if "frozen_sequence" not in columns:
                self.db.execute("ALTER TABLE planning_governance_suites ADD COLUMN frozen_sequence INTEGER")
                self.db.execute("UPDATE planning_governance_suites SET frozen_sequence=rowid WHERE frozen_sequence IS NULL")
            if "baseline_manifest_hash" not in columns:
                self.db.execute("ALTER TABLE planning_governance_suites ADD COLUMN baseline_manifest_hash TEXT")

    def record_manifest(self, task_id: str, run_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        """Record hashes of caller-supplied rule metadata; paths are never dereferenced here."""
        task_id, run_id = _text(task_id, "任务 ID", 200), _text(run_id, "运行 ID", 200)
        manifest = _mapping(manifest, "规则清单")
        rules = manifest.get("rules")
        if not isinstance(rules, list) or not rules or len(rules) > 200:
            raise StoreError("validation", "规则清单必须包含有限规则列表")
        normalized, seen_ids = [], set()
        for rule in rules:
            rule = _mapping(rule, "规则项")
            rule_id = _text(rule.get("id"), "规则 ID", 300)
            if rule_id in seen_ids:
                raise StoreError("validation", "规则 ID 不能重复")
            seen_ids.add(rule_id)
            item = {"id": rule_id,
                    "scope": _text(rule.get("scope"), "规则范围", 120),
                    "source": _text(rule.get("source", "caller"), "规则来源", 300)}
            content_hash = rule.get("content_hash")
            raw_content = rule.get("content")
            if raw_content is not None:
                raw_content = _raw_text(raw_content, "规则内容", MAX_TEXT)
                computed_hash = _text_sha(raw_content)
                if content_hash is not None and _text(content_hash, "规则哈希", 128) != computed_hash:
                    raise StoreError("validation", "规则原文与哈希不一致")
                content_hash = computed_hash
            elif content_hash is None:
                raise StoreError("validation", "规则必须提供原文或哈希")
            item["content_hash"] = _text(content_hash, "规则哈希", 128)
            item["kind"] = _text(rule.get("kind", "instruction"), "规则类型", 80)
            # The collector is authoritative about discovery/load state.  Do not
            # infer that a discovered rule was loaded by the runtime.
            load_state = _text(rule.get("load_state", "unknown"), "加载状态", 40)
            if load_state not in {"discovered", "available", "injected", "loaded", "unknown", "load_failed"}:
                raise StoreError("validation", "不支持的规则加载状态")
            item["load_state"] = load_state
            evidence_ref = rule.get("evidence_ref")
            if evidence_ref is not None and not isinstance(evidence_ref, (str, dict, list)):
                raise StoreError("validation", "规则证据引用无效")
            item["evidence_ref"] = evidence_ref
            byte_count = rule.get("byte_count")
            if byte_count is not None and (not isinstance(byte_count, int) or byte_count < 0):
                raise StoreError("validation", "规则字节数无效")
            if raw_content is not None:
                actual_bytes = len(raw_content.encode("utf-8"))
                if byte_count is not None and byte_count != actual_bytes:
                    raise StoreError("validation", "规则字节数与原文不一致")
                byte_count = actual_bytes
            item["byte_count"] = byte_count
            normalized.append(item)
        value = {"rules": sorted(normalized, key=lambda x: x["id"]),
                 "collector": _text(manifest.get("collector", "caller"), "收集方", 120),
                 "runtime": manifest.get("runtime")}
        manifest_hash = _sha(value)
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM planning_governance_manifests WHERE task_id=? AND run_id=? AND manifest_hash=?",
                                  (task_id, run_id, manifest_hash)).fetchone()
            if row is None:
                record_id = str(uuid.uuid4())
                self.db.execute("INSERT INTO planning_governance_manifests VALUES(?,?,?,?,?,?)",
                                (record_id, task_id, run_id, manifest_hash, _canonical(value), _now()))
                row = self.db.execute("SELECT * FROM planning_governance_manifests WHERE id=?", (record_id,)).fetchone()
        return self._manifest(row)

    def freeze_suite(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Freeze independently selected behaviour cases before any candidate can exist."""
        task_id, payload = _text(task_id, "任务 ID", 200), _mapping(payload, "验收套件")
        scope = self._scope(payload.get("scope"))
        cases = payload.get("cases")
        if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
            raise StoreError("validation", "验收套件必须包含有限案例列表")
        normalized, seen = [], set()
        for case in cases:
            case = _mapping(case, "验收案例")
            case_id = _text(case.get("id"), "案例 ID", 160)
            if case_id in seen:
                raise StoreError("validation", "案例 ID 不能重复")
            seen.add(case_id)
            expected = _mapping(case.get("expected"), "案例预期")
            required = self._phrases(expected.get("required", []), "必须出现")
            forbidden = self._phrases(expected.get("forbidden", []), "不得出现")
            if not required and not forbidden:
                raise StoreError("validation", "案例至少需要一个必须或禁止条件")
            if set(required) & set(forbidden):
                raise StoreError("validation", "同一案例的必须和禁止条件不能重叠")
            case_input = _mapping(case.get("input"), "案例输入")
            serialized_input = _canonical(case_input)
            if len(serialized_input) > MAX_TEXT:
                raise StoreError("limit", "案例输入超过 12000 字符")
            normalized.append({"id": case_id, "input": case_input,
                               "expected": {"required": required, "forbidden": forbidden}})
        baseline_manifest_hash = payload.get("baseline_manifest_hash")
        baseline = payload.get("baseline")
        if (baseline_manifest_hash is None) != (baseline is None):
            raise StoreError("validation", "冻结基线必须同时提供清单哈希和规则快照")
        normalized_baseline = None
        if baseline is not None:
            baseline = _mapping(baseline, "冻结基线")
            baseline_text = _raw_text(baseline.get("text"), "冻结规则原文")
            normalized_baseline = {"rule_id": _text(baseline.get("rule_id"), "冻结规则 ID", 300),
                                   "text": baseline_text,
                                   "content_hash": _text(baseline.get("content_hash"), "冻结规则哈希", 128)}
            if normalized_baseline["content_hash"] != _text_sha(baseline_text):
                raise StoreError("validation", "冻结规则原文与哈希不一致")
            baseline_manifest_hash = _text(baseline_manifest_hash, "基线规则清单哈希", 128)
        value = {"scope": scope, "cases": normalized,
                 "selection": _text(payload.get("selection", "independent"), "案例选择方式", 120),
                 "baseline": normalized_baseline}
        suite_hash = _sha(value)
        with self.lock, self.db:
            if normalized_baseline is not None:
                manifest = self.db.execute("SELECT payload FROM planning_governance_manifests WHERE task_id=? AND manifest_hash=?",
                                           (task_id, baseline_manifest_hash)).fetchone()
                if manifest is None:
                    raise StoreError("validation", "冻结基线必须属于当前任务已记录的规则清单")
                manifest_rules = json.loads(manifest["payload"])["rules"]
                matched = next((rule for rule in manifest_rules if rule["id"] == normalized_baseline["rule_id"]), None)
                if matched is None or matched["content_hash"] != normalized_baseline["content_hash"]:
                    raise StoreError("validation", "冻结规则不属于指定的基线清单")
            existing = self.db.execute("SELECT * FROM planning_governance_suites WHERE task_id=?", (task_id,)).fetchone()
            candidate = self.db.execute("SELECT 1 FROM planning_governance_candidates WHERE task_id=? LIMIT 1", (task_id,)).fetchone()
            if existing is not None:
                if existing["suite_hash"] != suite_hash:
                    raise StoreError("conflict", "验收套件已冻结，不能修改")
                return self._suite(existing)
            if candidate is not None:
                raise StoreError("conflict", "候选已创建，不能再冻结或替换验收套件")
            sequence = self.db.execute("SELECT COALESCE(MAX(frozen_sequence), 0) + 1 FROM planning_governance_suites").fetchone()[0]
            self.db.execute("INSERT INTO planning_governance_suites(task_id,suite_hash,scope,baseline_manifest_hash,payload,frozen_sequence,frozen_at) VALUES(?,?,?,?,?,?,?)",
                            (task_id, suite_hash, _canonical(scope), baseline_manifest_hash, _canonical(value), sequence, _now()))
            return self._suite(self.db.execute("SELECT * FROM planning_governance_suites WHERE task_id=?", (task_id,)).fetchone())

    def propose(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a reviewable candidate without applying it to any instruction file."""
        task_id, payload = _text(task_id, "任务 ID", 200), _mapping(payload, "规则候选")
        scope = self._scope(payload.get("scope"))
        with self.lock, self.db:
            suite = self.db.execute("SELECT * FROM planning_governance_suites WHERE task_id=?", (task_id,)).fetchone()
            if suite is None:
                raise StoreError("conflict", "必须先冻结独立验收套件")
            if _canonical(scope) != suite["scope"]:
                raise StoreError("validation", "候选范围必须与冻结套件的权威项目范围一致")
            baseline = _text(payload.get("baseline_manifest_hash"), "基线规则哈希", 128)
            manifest = self.db.execute("SELECT 1 FROM planning_governance_manifests WHERE task_id=? AND manifest_hash=?",
                                       (task_id, baseline)).fetchone()
            if manifest is None:
                raise StoreError("validation", "候选必须绑定当前任务已记录的基线规则清单")
            proposal = _mapping(payload.get("proposal"), "候选修改")
            suite_value = json.loads(suite["payload"])
            frozen_baseline = suite_value.get("baseline")
            if suite["baseline_manifest_hash"] is not None and baseline != suite["baseline_manifest_hash"]:
                raise StoreError("validation", "候选基线清单必须与冻结套件一致")
            if frozen_baseline is not None:
                required_fields = {"rule_id", "replacement_text", "rationale"}
                if set(proposal) != required_fields:
                    raise StoreError("validation", "候选必须只包含 rule_id、replacement_text 和 rationale")
                if _text(proposal["rule_id"], "候选规则 ID", 300) != frozen_baseline["rule_id"]:
                    raise StoreError("validation", "候选只能修改冻结基线中的规则")
                replacement = _text(proposal["replacement_text"], "候选替换文本")
                rationale = _text(proposal["rationale"], "候选理由", 4000)
                proposal = {"rule_id": frozen_baseline["rule_id"], "replacement_text": replacement, "rationale": rationale}
                diff = "\n".join(difflib.unified_diff(frozen_baseline["text"].splitlines(), replacement.splitlines(),
                                                       fromfile="baseline", tofile="candidate", lineterm=""))
            else:
                # Compatibility for historic suites without a source-text snapshot.
                diff = None
            evidence_ids = payload.get("feedback_ids", [])
            if not isinstance(evidence_ids, list) or len(evidence_ids) > 100:
                raise StoreError("validation", "候选证据无效")
            feedback = self._feedback_rows(task_id, evidence_ids)
            user_correction = bool(payload.get("user_correction"))
            independent = {row["independence_key"] for row in feedback}
            if not user_correction and len(independent) < 2:
                raise StoreError("validation", "候选需要两个独立原因证据；明确用户纠正可单例提出")
            if user_correction and not any(row["source_kind"] == "user_correction" for row in feedback):
                raise StoreError("validation", "单例候选必须绑定明确用户纠正")
            budget = self._budget(payload.get("budget", {}), proposal)
            candidate_id = str(uuid.uuid4())
            value = {"proposal": proposal, "feedback_ids": evidence_ids, "user_correction": user_correction,
                     "suite_hash": suite["suite_hash"], "source_rule_hash": frozen_baseline["content_hash"] if frozen_baseline else None,
                     "diff": diff}
            self.db.execute("INSERT INTO planning_governance_candidates VALUES(?,?,?,?,?,?,?,?)",
                            (candidate_id, task_id, _canonical(scope), baseline, _canonical(value), _canonical(budget), "proposed", _now()))
            return self._candidate(self.db.execute("SELECT * FROM planning_governance_candidates WHERE id=?", (candidate_id,)).fetchone())

    def feedback(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Store a bounded failure observation and a deduplicable common-cause key."""
        task_id, payload = _text(task_id, "任务 ID", 200), _mapping(payload, "反馈")
        source_kind = _text(payload.get("source_kind"), "反馈来源", 80)
        if source_kind not in {"run", "user_correction", "review", "evaluation"}:
            raise StoreError("validation", "不支持的反馈来源")
        root_cause = _text(payload.get("root_cause"), "共同根因", 240)
        independence = _text(payload.get("independence_key"), "独立性标识", 240)
        candidate_id = payload.get("candidate_id")
        if candidate_id is not None:
            candidate_id = _text(candidate_id, "候选 ID", 80)
            if self.db.execute("SELECT 1 FROM planning_governance_candidates WHERE id=? AND task_id=?", (candidate_id, task_id)).fetchone() is None:
                raise StoreError("not_found", "候选不存在或不属于当前任务")
        outcome = self._outcome(payload.get("outcome", {}))
        value = {"summary": _text(payload.get("summary"), "反馈摘要", 4000), "outcome": outcome,
                 "source_ref": payload.get("source_ref")}
        record_id = str(uuid.uuid4())
        with self.lock, self.db:
            self.db.execute("INSERT INTO planning_governance_feedback VALUES(?,?,?,?,?,?,?,?)",
                            (record_id, task_id, candidate_id, root_cause, independence, source_kind, _canonical(value), _now()))
            return self._feedback(self.db.execute("SELECT * FROM planning_governance_feedback WHERE id=?", (record_id,)).fetchone())

    def preview_evaluation(self, task_id: str, candidate_id: str) -> dict[str, Any]:
        """Return the strictly minimal external-evaluation packet, without expected results."""
        task_id, candidate_id = _text(task_id, "任务 ID", 200), _text(candidate_id, "候选 ID", 80)
        with self.lock:
            suite = self.db.execute("SELECT * FROM planning_governance_suites WHERE task_id=?", (task_id,)).fetchone()
            candidate = self.db.execute("SELECT * FROM planning_governance_candidates WHERE id=? AND task_id=?", (candidate_id, task_id)).fetchone()
            if suite is None or candidate is None:
                raise StoreError("not_found", "验收套件或候选不存在")
            suite_value, candidate_value = json.loads(suite["payload"]), json.loads(candidate["payload"])
            baseline = suite_value.get("baseline")
            if baseline is None or suite["baseline_manifest_hash"] is None:
                raise StoreError("conflict", "历史套件没有可外发的冻结基线")
            if suite["baseline_manifest_hash"] != candidate["baseline_manifest_hash"]:
                raise StoreError("conflict", "候选与冻结套件的基线不一致")
            proposal = candidate_value["proposal"]
            if proposal.get("rule_id") != baseline["rule_id"]:
                raise StoreError("conflict", "候选没有指向冻结基线规则")
            return {"suite_hash": suite["suite_hash"], "baseline_manifest_hash": suite["baseline_manifest_hash"],
                    "baseline": {"rule_id": baseline["rule_id"], "text": baseline["text"], "content_hash": baseline["content_hash"]},
                    "candidate": {"rule_id": proposal["rule_id"], "replacement_text": proposal["replacement_text"], "rationale": proposal["rationale"]},
                    "cases": [{"id": case["id"], "input": case["input"]} for case in suite_value["cases"]]}

    def evaluate(self, task_id: str, candidate_id: str, evaluator: Callable[[dict[str, Any], dict[str, Any]], Any],
                 finalize_guard: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
        """Run frozen cases and optionally guard the final write under this instance's lock.

        ``finalize_guard`` receives a stable identity mapping while ``self.lock``
        is held: task_id, candidate_id, suite_hash, frozen_sequence,
        baseline_manifest_hash, baseline_rule_hash, and candidate_payload_hash.
        It must return ``True`` to permit adoption; a false result or exception
        records an unknown, non-adoptable evaluation instead of a passed result.
        """
        task_id, candidate_id = _text(task_id, "任务 ID", 200), _text(candidate_id, "候选 ID", 80)
        if not callable(evaluator):
            raise StoreError("validation", "必须提供实际评测回调")
        if finalize_guard is not None and not callable(finalize_guard):
            raise StoreError("validation", "最终写入守卫必须是可调用对象")
        with self.lock, self.db:
            suite = self.db.execute("SELECT * FROM planning_governance_suites WHERE task_id=?", (task_id,)).fetchone()
            candidate = self.db.execute("SELECT * FROM planning_governance_candidates WHERE id=? AND task_id=?", (candidate_id, task_id)).fetchone()
            if suite is None or candidate is None:
                raise StoreError("not_found", "验收套件或候选不存在")
            suite_value, candidate_value = json.loads(suite["payload"]), json.loads(candidate["payload"])
            suite_hash, candidate_payload = suite["suite_hash"], candidate["payload"]
            frozen_baseline = suite_value.get("baseline")
            if frozen_baseline is None:
                raise StoreError("conflict", "历史套件没有可执行的冻结规则快照")
            if suite["baseline_manifest_hash"] != candidate["baseline_manifest_hash"]:
                raise StoreError("conflict", "候选与冻结套件的基线不一致")
            existing_attempt = self.db.execute(
                "SELECT * FROM planning_governance_evaluation_attempts WHERE task_id=? AND candidate_id=? AND suite_hash=? AND baseline_manifest_hash=?",
                (task_id, candidate_id, suite_hash, candidate["baseline_manifest_hash"])).fetchone()
            if existing_attempt is not None:
                if existing_attempt["state"] == "completed":
                    previous = self.db.execute(
                        "SELECT * FROM planning_governance_evaluations WHERE task_id=? AND candidate_id=? AND suite_hash=? ORDER BY created_at DESC LIMIT 1",
                        (task_id, candidate_id, suite_hash)).fetchone()
                    if previous is not None:
                        return self._evaluation(previous)
                raise StoreError("conflict", "评测已启动或中断，状态未知；不会自动重复调用评测器")
            attempt_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO planning_governance_evaluation_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                            (attempt_id, task_id, candidate_id, suite_hash, candidate["baseline_manifest_hash"], "started",
                             _canonical({"reason": "external_evaluator_pending"}), _now(), None))
        results = []
        try:
            for case in suite_value["cases"]:
                results.append(self._evaluate_case(case, "baseline", {"rule_id": frozen_baseline["rule_id"],
                                                                          "text": frozen_baseline["text"],
                                                                          "content_hash": frozen_baseline["content_hash"]}, evaluator))
                results.append(self._evaluate_case(case, "candidate", candidate_value["proposal"], evaluator))
            summary = self._evaluation_summary(results)
        except BaseException:
            with self.lock, self.db:
                self.db.execute("UPDATE planning_governance_evaluation_attempts SET state=?,detail=?,completed_at=? WHERE id=?",
                                ("unknown", _canonical({"reason": "evaluation_interrupted"}), _now(), attempt_id))
            raise
        record_id = str(uuid.uuid4())
        with self.lock, self.db:
            current_suite = self.db.execute("SELECT suite_hash FROM planning_governance_suites WHERE task_id=?", (task_id,)).fetchone()
            current_candidate = self.db.execute("SELECT payload,baseline_manifest_hash FROM planning_governance_candidates WHERE id=? AND task_id=?", (candidate_id, task_id)).fetchone()
            if current_suite is None or current_suite["suite_hash"] != suite_hash or current_candidate is None or current_candidate["payload"] != candidate_payload or current_candidate["baseline_manifest_hash"] != candidate["baseline_manifest_hash"]:
                self.db.execute("UPDATE planning_governance_evaluation_attempts SET state=?,detail=?,completed_at=? WHERE id=?",
                                ("unknown", _canonical({"reason": "candidate_or_baseline_changed"}), _now(), attempt_id))
                raise StoreError("conflict", "评测期间候选或冻结基线已变化，结果未写入")
            guard_result = {"state": "not_configured"}
            if finalize_guard is not None:
                guard_context = {
                    "task_id": task_id,
                    "candidate_id": candidate_id,
                    "suite_hash": suite_hash,
                    "frozen_sequence": suite["frozen_sequence"],
                    "baseline_manifest_hash": candidate["baseline_manifest_hash"],
                    "baseline_rule_hash": frozen_baseline["content_hash"],
                    "candidate_payload_hash": _sha(json.loads(candidate_payload)),
                }
                try:
                    if finalize_guard(guard_context) is True:
                        guard_result = {"state": "accepted"}
                    else:
                        guard_result = {"state": "rejected"}
                except Exception as exc:
                    guard_result = {"state": "rejected", "error": type(exc).__name__}
            if guard_result["state"] == "rejected":
                summary = {**summary, "candidate_state": "unknown", "adoptable": False,
                           "finalize_guard": guard_result}
            else:
                summary = {**summary, "finalize_guard": guard_result}
            self.db.execute("INSERT INTO planning_governance_evaluations VALUES(?,?,?,?,?,?,?)",
                            (record_id, task_id, candidate_id, suite_hash, _canonical(results), _canonical(summary), _now()))
            self.db.execute("UPDATE planning_governance_candidates SET state=? WHERE id=?", (summary["candidate_state"], candidate_id))
            self.db.execute("UPDATE planning_governance_evaluation_attempts SET state=?,detail=?,completed_at=? WHERE id=?",
                            ("completed", _canonical({"evaluation_id": record_id}), _now(), attempt_id))
            return {"id": record_id, "attempt_id": attempt_id, "task_id": task_id, "candidate_id": candidate_id, "suite_hash": suite["suite_hash"],
                    "results": results, "summary": summary}

    def detail(self, task_id: str) -> dict[str, Any]:
        task_id = _text(task_id, "任务 ID", 200)
        with self.lock:
            manifest_rows = self.db.execute("SELECT * FROM planning_governance_manifests WHERE task_id=? ORDER BY created_at,id", (task_id,)).fetchall()
            suite = self.db.execute("SELECT * FROM planning_governance_suites WHERE task_id=?", (task_id,)).fetchone()
            candidates = self.db.execute("SELECT * FROM planning_governance_candidates WHERE task_id=? ORDER BY created_at,id", (task_id,)).fetchall()
            feedback = self.db.execute("SELECT * FROM planning_governance_feedback WHERE task_id=? ORDER BY created_at,id", (task_id,)).fetchall()
            evaluations = self.db.execute("SELECT * FROM planning_governance_evaluations WHERE task_id=? ORDER BY created_at,id", (task_id,)).fetchall()
        feedback_values = [self._feedback(row) for row in feedback]
        causes: dict[str, dict[str, Any]] = {}
        for item in feedback_values:
            group = causes.setdefault(item["root_cause"], {"root_cause": item["root_cause"], "observations": 0, "reported_independence_keys": set(), "feedback_ids": []})
            group["observations"] += 1
            group["reported_independence_keys"].add(item["independence_key"])
            group["feedback_ids"].append(item["id"])
        return {"manifests": [self._manifest(row) for row in manifest_rows], "suite": self._suite(suite) if suite else None,
                "candidates": [self._candidate(row) for row in candidates], "feedback": feedback_values,
                "causes": [{**group, "reported_independence_keys": sorted(group["reported_independence_keys"]),
                            "reported_independence_count": len(group["reported_independence_keys"]),
                            "independence_verified": False,
                            "independence_note": "调用方报告的去重标识；不同会话不会自动证明为独立根因"} for group in causes.values()],
                "evaluations": [self._evaluation(row) for row in evaluations], "metrics": self._metrics(feedback_values, evaluations)}

    def _scope(self, value: Any) -> dict[str, Any]:
        value = _mapping(value, "权威项目范围")
        return {"project_id": _text(value.get("project_id"), "项目范围", 200),
                "scope_id": _text(value.get("scope_id"), "范围 ID", 200),
                "kind": _text(value.get("kind", "project"), "范围类型", 80)}

    @staticmethod
    def _phrases(value: Any, label: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 30:
            raise StoreError("validation", f"{label}无效")
        return [_text(item, label, 400) for item in value]

    def _feedback_rows(self, task_id: str, ids: list[Any]) -> list[sqlite3.Row]:
        if not ids:
            return []
        normalized = [_text(item, "反馈 ID", 80) for item in ids]
        if len(set(normalized)) != len(normalized):
            raise StoreError("validation", "反馈证据不能重复")
        marks = ",".join("?" for _ in normalized)
        rows = self.db.execute(f"SELECT * FROM planning_governance_feedback WHERE task_id=? AND id IN ({marks})", [task_id, *normalized]).fetchall()
        if len(rows) != len(normalized):
            raise StoreError("validation", "候选证据必须属于当前任务")
        return rows

    def _budget(self, value: Any, proposal: dict[str, Any]) -> dict[str, Any]:
        value = _mapping(value, "预算")
        constraints = value.get("constraints", [])
        removals = value.get("remove_ids", [])
        if not isinstance(constraints, list) or not isinstance(removals, list):
            raise StoreError("validation", "预算约束无效")
        protected = set()
        content: list[tuple[str, str]] = []
        for item in constraints:
            item = _mapping(item, "预算约束项")
            item_id, kind = _text(item.get("id"), "约束 ID", 160), _text(item.get("kind"), "约束类型", 80)
            content.append((item_id, _text(item.get("content"), "约束内容", MAX_TEXT)))
            if kind in _PROTECTED_KINDS:
                protected.add(item_id)
        removal_ids = {_text(item, "移除约束", 160) for item in removals}
        if protected & removal_ids:
            raise StoreError("validation", "候选不能以预算为由删除权限或安全约束")
        texts = content + [("proposal", text) for text in self._strings(proposal)]
        normalized: dict[str, list[str]] = {}
        polarity: dict[str, dict[str, list[str]]] = {}
        for item_id, text in texts:
            key = " ".join(text.casefold().split())
            normalized.setdefault(key, []).append(item_id)
            sign, remainder = self._mechanical_polarity(key)
            polarity.setdefault(remainder, {}).setdefault(sign, []).append(item_id)
        duplicate_exact = [ids for ids in normalized.values() if len(ids) > 1]
        opposite_candidates = [{"positive_ids": signs["positive"], "negative_ids": signs["negative"]}
                               for signs in polarity.values() if "positive" in signs and "negative" in signs]
        estimate_bytes = len(("".join(text for _, text in content) + _canonical(proposal)).encode("utf-8"))
        tokenizer = value.get("tokenizer")
        if tokenizer is not None:
            tokenizer = _text(tokenizer, "分词器", 120)
        return {"estimate": True, "method": "utf8_bytes_divided_by_4", "estimated_bytes": estimate_bytes,
                "estimated_tokens": (estimate_bytes + 3) // 4,
                "tokenizer": {"known": tokenizer is not None, "name": tokenizer},
                "mechanical_checks": {"duplicate_exact": duplicate_exact, "opposite_text_candidates": opposite_candidates,
                                      "semantic_proof": "not_available"},
                "protected_constraint_ids": sorted(protected), "remove_ids": sorted(removal_ids)}

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [item for child in value.values() for item in Governance._strings(child)]
        if isinstance(value, list):
            return [item for child in value for item in Governance._strings(child)]
        return []

    @staticmethod
    def _mechanical_polarity(text: str) -> tuple[str, str]:
        for prefix in ("not ", "no ", "不要", "不得", "禁止"):
            if text.startswith(prefix):
                return "negative", text[len(prefix):].strip()
        return "positive", text

    @staticmethod
    def _outcome(value: Any) -> dict[str, Any]:
        value = _mapping(value, "结果指标")
        allowed = {"completed", "rework", "user_intervention", "regression", "latency_ms", "tokens"}
        unknown = set(value) - allowed
        if unknown:
            raise StoreError("validation", "结果指标包含不支持字段")
        result = {}
        for key, item in value.items():
            if key in {"completed", "rework", "user_intervention", "regression"}:
                if not isinstance(item, bool):
                    raise StoreError("validation", "布尔结果指标无效")
            elif not isinstance(item, int) or item < 0:
                raise StoreError("validation", "数值结果指标无效")
            result[key] = item
        return result

    def _evaluate_case(self, case: dict[str, Any], variant_name: str, variant: dict[str, Any], evaluator: Callable[[dict[str, Any], dict[str, Any]], Any]) -> dict[str, Any]:
        try:
            raw = evaluator(case["input"], {"name": variant_name, "value": variant})
            if not isinstance(raw, dict) or raw.get("actual") is not True:
                return {"case_id": case["id"], "variant": variant_name, "state": "unknown", "reason": "evaluator_not_actual", "output_hash": None}
            text = raw.get("text")
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                return {"case_id": case["id"], "variant": variant_name, "state": "unknown", "reason": "evaluator_output_unavailable", "output_hash": None}
            missing = [item for item in case["expected"]["required"] if item not in text]
            forbidden = [item for item in case["expected"]["forbidden"] if item in text]
            return {"case_id": case["id"], "variant": variant_name, "state": "passed" if not missing and not forbidden else "failed",
                    "missing": missing, "forbidden": forbidden, "output_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        except Exception as exc:  # the evaluator is external; its failure is evidence, never success
            return {"case_id": case["id"], "variant": variant_name, "state": "unknown", "reason": "evaluator_exception",
                    "error_type": type(exc).__name__, "output_hash": None}

    @staticmethod
    def _evaluation_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
        by_case: dict[str, dict[str, dict[str, Any]]] = {}
        for item in results:
            by_case.setdefault(item["case_id"], {})[item["variant"]] = item
        for pair in by_case.values():
            baseline, candidate = pair["baseline"], pair["candidate"]
            regression = baseline["state"] == "passed" and candidate["state"] == "failed"
            improvement = baseline["state"] == "failed" and candidate["state"] == "passed"
            comparison = {"regression": regression, "improvement": improvement,
                          "known": baseline["state"] != "unknown" and candidate["state"] != "unknown"}
            baseline["comparison"] = comparison
            candidate["comparison"] = comparison
        candidate = [item["state"] for item in results if item["variant"] == "candidate"]
        baseline = [item["state"] for item in results if item["variant"] == "baseline"]
        if any(item == "failed" for item in candidate): state = "failed"
        elif any(item == "unknown" for item in candidate) or any(item == "unknown" for item in baseline): state = "unknown"
        else: state = "passed"
        return {"candidate_state": state, "baseline": {key: sum(item["state"] == key and item["variant"] == "baseline" for item in results) for key in ("passed", "failed", "unknown")},
                "candidate": {key: sum(item["state"] == key and item["variant"] == "candidate" for item in results) for key in ("passed", "failed", "unknown")},
                "adoptable": state == "passed"}

    @staticmethod
    def _manifest(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "task_id": row["task_id"], "run_id": row["run_id"], "manifest_hash": row["manifest_hash"], "manifest": json.loads(row["payload"]), "created_at": row["created_at"]}

    @staticmethod
    def _suite(row: sqlite3.Row) -> dict[str, Any]:
        value = json.loads(row["payload"])
        baseline = value.get("baseline")
        # Keep executable source text inside the immutable suite record, but never
        # expose it through the normal detail API.
        safe_baseline = None if baseline is None else {"rule_id": baseline["rule_id"], "content_hash": baseline["content_hash"]}
        safe_cases = [{"id": case["id"], "checks_count": len(case["expected"]["required"]) + len(case["expected"]["forbidden"])}
                      for case in value["cases"]]
        return {"task_id": row["task_id"], "suite_hash": row["suite_hash"], "scope": value["scope"], "cases": safe_cases,
                "baseline_manifest_hash": row["baseline_manifest_hash"], "baseline": safe_baseline,
                "frozen_sequence": row["frozen_sequence"], "frozen_at": row["frozen_at"]}

    @staticmethod
    def _candidate(row: sqlite3.Row) -> dict[str, Any]:
        value = json.loads(row["payload"])
        return {"id": row["id"], "task_id": row["task_id"], "scope": json.loads(row["scope"]), "baseline_manifest_hash": row["baseline_manifest_hash"],
                **value, "budget": json.loads(row["budget"]), "state": row["state"], "created_at": row["created_at"]}

    @staticmethod
    def _feedback(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "task_id": row["task_id"], "candidate_id": row["candidate_id"], "root_cause": row["root_cause"],
                "independence_key": row["independence_key"], "source_kind": row["source_kind"], **json.loads(row["payload"]), "created_at": row["created_at"]}

    @staticmethod
    def _evaluation(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "task_id": row["task_id"], "candidate_id": row["candidate_id"], "suite_hash": row["suite_hash"],
                "results": json.loads(row["payload"]), "summary": json.loads(row["summary"]), "created_at": row["created_at"]}

    @staticmethod
    def _metrics(feedback: list[dict[str, Any]], evaluations: list[sqlite3.Row]) -> dict[str, Any]:
        values = [item["outcome"] for item in feedback]
        result = {"observations": len(values), "evaluations": len(evaluations)}
        for key in ("completed", "rework", "user_intervention", "regression"):
            known = [value[key] for value in values if key in value]
            true_count = sum(item is True for item in known)
            result[key] = {"true": true_count, "false": sum(item is False for item in known),
                           "known": len(known), "unknown": len(values) - len(known),
                           "true_ratio": true_count / len(known) if known else None}
        for key in ("latency_ms", "tokens"):
            numbers = [value[key] for value in values if key in value]
            result[f"{key}_average"] = sum(numbers) / len(numbers) if numbers else None
        return result
