"""Version-bound advisor handoffs and task execution ownership.

All writes use Planning's transaction and authorization. Packets contain only
the bounded, redacted material selected by the caller, never credentials or
repository dumps. Receiving or accepting advice does not execute it.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import sqlite3
from pathlib import Path
import uuid
from datetime import datetime, timezone

from .store import StoreError


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def instant():
    return datetime.now(timezone.utc).isoformat()


class PlanningCoordination:
    def __init__(self, db):
        self.db = db
        db.executescript('''
            CREATE TABLE IF NOT EXISTS planning_handoffs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, purpose TEXT NOT NULL,
                fingerprint TEXT NOT NULL, packet TEXT NOT NULL, packet_sha256 TEXT NOT NULL,
                state TEXT NOT NULL, response TEXT, response_sha256 TEXT, error TEXT,
                owner_pid INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                provider_operation_id TEXT, status_query_ref TEXT, side_effect_state TEXT,
                last_event_id TEXT, operation_key TEXT, reconciliation_source TEXT,
                status_evidence TEXT,
                UNIQUE(task_id,purpose,packet_sha256)
            );
            CREATE TABLE IF NOT EXISTS planning_advice_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, packet_id TEXT NOT NULL,
                suggestion_index INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS planning_work_ownership (
                task_id TEXT PRIMARY KEY, plan TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        ''')
        for column in ("provider_operation_id", "status_query_ref", "side_effect_state", "last_event_id",
                       "operation_key", "reconciliation_source", "status_evidence"):
            try:
                db.execute(f"ALTER TABLE planning_handoffs ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass

    def prepare(self, task_id, fingerprint, packet, purpose='delivery_review'):
        raw = encoded(packet)
        if len(raw) > 24000:
            raise StoreError('limit', '交接材料超过上限，请缩小任务卡或证据摘要')
        sha = digest(packet)
        row = self.db.execute('SELECT id FROM planning_handoffs WHERE task_id=? AND purpose=? AND packet_sha256=?',
                              (task_id, purpose, sha)).fetchone()
        if row:
            return self.packet(task_id, row['id'], fingerprint)
        key, stamp = str(uuid.uuid4()), instant()
        self.db.execute('''INSERT INTO planning_handoffs
            (id,task_id,purpose,fingerprint,packet,packet_sha256,state,created_at,updated_at,side_effect_state,operation_key)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (key, task_id, purpose, fingerprint, raw, sha, 'prepared', stamp, stamp, 'none', sha))
        return self.packet(task_id, key, fingerprint)

    def packet(self, task_id, packet_id, fingerprint):
        row = self.db.execute('SELECT * FROM planning_handoffs WHERE id=? AND task_id=?', (packet_id, task_id)).fetchone()
        if not row:
            raise StoreError('not_found', '交接记录不属于当前任务')
        value = dict(row)
        value['packet'] = json.loads(value['packet'])
        value['response'] = json.loads(value['response']) if value['response'] else None
        value['reconciliation_source'] = json.loads(value['reconciliation_source']) if value['reconciliation_source'] else None
        value['stale'] = value['fingerprint'] != fingerprint
        value['decisions'] = [dict(r) for r in self.db.execute(
            'SELECT suggestion_index,decision,reason,created_at FROM planning_advice_decisions WHERE packet_id=? ORDER BY id', (packet_id,))]
        value.pop('owner_pid', None)
        return value

    def begin(self, task_id, packet_id, fingerprint, owner_pid):
        packet = self.packet(task_id, packet_id, fingerprint)
        if packet['stale']:
            raise StoreError('version_conflict', '交接材料已过期，请按当前任务重新准备')
        if packet['state'] != 'prepared':
            raise StoreError('conflict', '该材料已提交；结果未知时先核对记录，不自动重发')
        self.db.execute("UPDATE planning_handoffs SET state='sending',owner_pid=?,side_effect_state='attempted',updated_at=? WHERE id=?", (owner_pid, instant(), packet_id))
        return packet

    def cancel_prepared(self, task_id, packet_id, fingerprint, reason):
        packet = self.packet(task_id, packet_id, fingerprint)
        if packet['stale'] or packet['state'] != 'prepared':
            raise StoreError('conflict', '只有尚未发送的交接材料可以安全取消')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise StoreError('validation', '取消原因无效')
        self.db.execute("UPDATE planning_handoffs SET state='cancelled',error=?,updated_at=? WHERE id=?", (reason.strip(), instant(), packet_id))
        return self.packet(task_id, packet_id, fingerprint)

    def reconcile(self, task_id, packet_id, fingerprint, *, provider_operation_id, status_query_ref,
                  side_effect_state, last_event_id=None, source=None, status_evidence=None):
        """Persist a bounded manual status check; it never follows a reference or accepts a response."""
        packet = self.packet(task_id, packet_id, fingerprint)
        if packet['stale'] or packet['state'] not in {'sending', 'unknown'}:
            raise StoreError('conflict', '只有发送中或结果未知的交接可核对')
        values = (provider_operation_id, status_query_ref, side_effect_state)
        if any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in values):
            raise StoreError('validation', '外部操作核对字段无效')
        if '://' in status_query_ref or '/' in status_query_ref:
            raise StoreError('validation', '状态查询引用必须是已登记的引用标识，不能是 URL 或路径')
        if side_effect_state not in {'unknown', 'submitted', 'completed', 'failed'}:
            raise StoreError('validation', '外部副作用状态无效')
        if packet.get('provider_operation_id') and packet['provider_operation_id'] != provider_operation_id.strip():
            raise StoreError('idempotency_conflict', '外部操作标识与已记录操作不一致')
        if packet.get('status_query_ref') and packet['status_query_ref'] != status_query_ref.strip():
            raise StoreError('idempotency_conflict', '状态查询引用与已记录操作不一致')
        if not isinstance(source, dict) or not source or not isinstance(status_evidence, str) or not status_evidence.strip() or len(status_evidence) > 4000:
            raise StoreError('validation', '人工核对需要来源引用和状态证据')
        self.db.execute('''UPDATE planning_handoffs SET state='unknown',provider_operation_id=?,status_query_ref=?,
            side_effect_state=?,last_event_id=?,reconciliation_source=?,status_evidence=?,owner_pid=NULL,updated_at=? WHERE id=?''',
            (provider_operation_id.strip(), status_query_ref.strip(), side_effect_state, last_event_id,
             encoded(source), status_evidence.strip(), instant(), packet_id))
        return self.packet(task_id, packet_id, fingerprint)

    @staticmethod
    def _validate_response(response):
        if (not isinstance(response, dict) or response.get('decision') not in {'continue', 'correct', 'stop'}
                or not isinstance(response.get('suggestions'), list) or len(response['suggestions']) > 20
                or any(not isinstance(s, str) or not s.strip() or len(s) > 2000 for s in response['suggestions'])):
            raise StoreError('validation', '建议结果格式无效')

    def reconcile_response(self, task_id, packet_id, fingerprint, *, provider_operation_id, response, source, status_evidence):
        """Accept a returned structured response only after a recorded server-side completion."""
        packet = self.packet(task_id, packet_id, fingerprint)
        if packet['stale'] or packet['state'] != 'unknown' or packet.get('side_effect_state') != 'completed':
            raise StoreError('conflict', '只有已核对完成但结果未知的交接可恢复响应')
        if not isinstance(provider_operation_id, str) or not provider_operation_id.strip() or packet.get('provider_operation_id') != provider_operation_id.strip():
            raise StoreError('idempotency_conflict', '外部操作标识与已记录操作不一致')
        if not isinstance(source, dict) or not source or not isinstance(status_evidence, str) or not status_evidence.strip() or len(status_evidence) > 4000:
            raise StoreError('validation', '恢复响应需要来源引用和完成状态证据')
        self._validate_response(response)
        if packet.get('response') is not None:
            if packet['response'] == response:
                return packet
            raise StoreError('idempotency_conflict', '已记录的外部响应与本次响应不一致')
        self.db.execute('''UPDATE planning_handoffs SET state='received',response=?,response_sha256=?,
            reconciliation_source=?,status_evidence=?,owner_pid=NULL,updated_at=? WHERE id=?''',
            (encoded(response), digest(response), encoded(source), status_evidence.strip(), instant(), packet_id))
        return self.packet(task_id, packet_id, fingerprint)

    def finish(self, task_id, packet_id, response=None, error=None):
        row = self.db.execute('SELECT state FROM planning_handoffs WHERE id=? AND task_id=?', (packet_id, task_id)).fetchone()
        if not row or row['state'] != 'sending':
            raise StoreError('conflict', '交接状态已变化')
        if response is not None:
            self._validate_response(response)
        self.db.execute('UPDATE planning_handoffs SET state=?,response=?,response_sha256=?,error=?,owner_pid=NULL,updated_at=? WHERE id=?',
                        ('received' if response is not None else 'unknown', encoded(response) if response is not None else None,
                         digest(response) if response is not None else None, error, instant(), packet_id))

    def decide(self, task_id, packet_id, fingerprint, index, decision, reason):
        packet = self.packet(task_id, packet_id, fingerprint)
        if packet['stale'] or packet['state'] != 'received':
            raise StoreError('version_conflict', '建议过期或未收到完整结果，不能采纳')
        suggestions = packet['response']['suggestions']
        if type(index) is not int or index < 0 or index >= len(suggestions):
            raise StoreError('validation', '建议不存在')
        if decision not in {'accepted', 'rejected', 'deferred'} or not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise StoreError('validation', '请选择采纳、拒绝或暂缓，并填写理由')
        previous = self.db.execute('SELECT decision,reason FROM planning_advice_decisions WHERE packet_id=? AND suggestion_index=? ORDER BY id DESC LIMIT 1', (packet_id, index)).fetchone()
        if previous and previous['decision'] == decision and previous['reason'] == reason.strip():
            return
        self.db.execute('INSERT INTO planning_advice_decisions(packet_id,suggestion_index,decision,reason,created_at) VALUES(?,?,?,?,?)',
                        (packet_id, index, decision, reason.strip(), instant()))

    def recover(self, is_alive):
        for row in self.db.execute("SELECT id,owner_pid FROM planning_handoffs WHERE state='sending'").fetchall():
            if row['owner_pid'] is not None and is_alive(row['owner_pid']):
                continue
            self.db.execute("UPDATE planning_handoffs SET state='unknown',side_effect_state='unknown',error='interrupted',owner_pid=NULL,updated_at=? WHERE id=?", (instant(), row['id']))

    @staticmethod
    def normalize_plan(plan):
        if not isinstance(plan, dict) or set(plan) - {'owner', 'paths', 'resources', 'dependencies'}:
            raise StoreError('validation', '并行任务边界无效')
        owner = plan.get('owner', '')
        if not isinstance(owner, str) or len(owner) > 200:
            raise StoreError('validation', '负责人无效')
        result = {'owner': owner.strip()}
        for field in ('paths', 'resources', 'dependencies'):
            values = plan.get(field, [])
            if not isinstance(values, list) or len(values) > 50 or any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in values):
                raise StoreError('validation', '任务边界字段无效')
            result[field] = sorted(set(v.strip() for v in values))
        paths = []
        for path in result['paths']:
            path = path.replace('\\', '/')
            if path.startswith('/') or any(p == '..' for p in path.split('/')) or any(c in path for c in '*?['):
                raise StoreError('validation', '文件范围应为项目相对路径或目录，不支持通配符和上级目录')
            paths.append(posixpath.normpath(path).rstrip('/'))
        result['paths'] = sorted(set(paths))
        return result

    def plan(self, task_id):
        row = self.db.execute('SELECT plan FROM planning_work_ownership WHERE task_id=?', (task_id,)).fetchone()
        return json.loads(row['plan']) if row else {'owner': '', 'paths': [], 'resources': [], 'dependencies': []}

    def save_plan(self, task_id, plan, verify_task):
        plan = self.normalize_plan(plan)
        def visit(key, seen):
            if key == task_id or key in seen:
                raise StoreError('validation', '任务依赖不能形成循环')
            verify_task(key)
            for dep in self.plan(key)['dependencies']:
                visit(dep, seen | {key})
        for dependency in plan['dependencies']:
            visit(dependency, set())
        self.db.execute('INSERT INTO planning_work_ownership VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET plan=excluded.plan,updated_at=excluded.updated_at',
                        (task_id, encoded(plan), instant()))

    @staticmethod
    def overlap(left, right):
        return any(a == '.' or b == '.' or a == b or a.startswith(b + '/') or b.startswith(a + '/') for a in left for b in right)

    def readiness(self, task_id, project_id, dependency_ready):
        plan, blockers = self.plan(task_id), []
        for dep in plan['dependencies']:
            if not dependency_ready(dep):
                blockers.append({'kind': 'dependency', 'task_id': dep, 'reason': '上游任务尚未通过当前交付验收'})
        project = self.db.execute('SELECT cwd FROM projects WHERE id=?', (project_id,)).fetchone()
        cwd = Path(project['cwd']).resolve() if project else None
        # A run's current state is the lease authority. No second scheduler.
        for row in self.db.execute("SELECT t.id,t.project_id,pr.cwd FROM tasks t JOIN planning_tasks p ON p.task_id=t.id JOIN projects pr ON pr.id=t.project_id WHERE t.state='running' AND t.id<>? AND p.deleted_at IS NULL", (task_id,)):
            other = self.plan(row['id'])
            resources = sorted(set(plan['resources']) & set(other['resources']))
            same_checkout = row['project_id'] == project_id or (cwd is not None and Path(row['cwd']).resolve() == cwd)
            files = same_checkout and self.overlap(plan['paths'], other['paths'])
            if resources or files:
                blockers.append({'kind': 'ownership', 'task_id': row['id'], 'resources': resources, 'reason': '执行中的任务占用重叠文件或共享资源'})
        return {'plan': plan, 'blockers': blockers, 'ready': not blockers, 'enforcement': 'declared_boundaries'}

    def detail(self, task_id, fingerprint):
        ids = self.db.execute('SELECT id FROM planning_handoffs WHERE task_id=? ORDER BY created_at DESC LIMIT 20', (task_id,)).fetchall()
        return {'handoffs': [self.packet(task_id, r['id'], fingerprint) for r in ids], 'ownership': self.plan(task_id)}
