"""Version-bound advisor handoffs and task execution ownership.

All writes use Planning's transaction and authorization. Packets contain only
the bounded, redacted material selected by the caller, never credentials or
repository dumps. Receiving or accepting advice does not execute it.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
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
        self.db.execute('INSERT INTO planning_handoffs VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?)',
                        (key, task_id, purpose, fingerprint, raw, sha, 'prepared', stamp, stamp))
        return self.packet(task_id, key, fingerprint)

    def packet(self, task_id, packet_id, fingerprint):
        row = self.db.execute('SELECT * FROM planning_handoffs WHERE id=? AND task_id=?', (packet_id, task_id)).fetchone()
        if not row:
            raise StoreError('not_found', '交接记录不属于当前任务')
        value = dict(row)
        value['packet'] = json.loads(value['packet'])
        value['response'] = json.loads(value['response']) if value['response'] else None
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
        self.db.execute("UPDATE planning_handoffs SET state='sending',owner_pid=?,updated_at=? WHERE id=?", (owner_pid, instant(), packet_id))
        return packet

    def finish(self, task_id, packet_id, response=None, error=None):
        row = self.db.execute('SELECT state FROM planning_handoffs WHERE id=? AND task_id=?', (packet_id, task_id)).fetchone()
        if not row or row['state'] != 'sending':
            raise StoreError('conflict', '交接状态已变化')
        if response is not None:
            if (not isinstance(response, dict) or response.get('decision') not in {'continue', 'correct', 'stop'}
                    or not isinstance(response.get('suggestions'), list) or len(response['suggestions']) > 20
                    or any(not isinstance(s, str) or not s.strip() or len(s) > 2000 for s in response['suggestions'])):
                raise StoreError('validation', '建议结果格式无效')
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
            self.db.execute("UPDATE planning_handoffs SET state='unknown',error='interrupted',owner_pid=NULL,updated_at=? WHERE id=?", (instant(), row['id']))

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
