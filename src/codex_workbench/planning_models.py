"""Quota-aware, bounded auxiliary calls through registered vault bindings.

The router supplies candidates only. It cannot execute tools, change accounts,
approve delivery, or expand the caller's data boundary.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


class ModelCallError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _protocol_for(model):
    """Translate an explicit catalog profile; specialised full endpoints need no guess."""
    protocol = model.get('protocol')
    if protocol in {'openai-chat', 'openai-responses'}:
        return protocol
    if model.get('api_profile') == 'openai_chat':
        return 'openai-chat'
    model_type = model.get('model_type')
    path = urlsplit(str(model.get('base_url', ''))).path.rstrip('/')
    required = {'embedding': '/embeddings', 'rerank': '/rerank'}.get(model_type)
    if required and path.endswith(required):
        # capability_worker requires this protocol label, but these request shapes
        # use the registered specialised endpoint and do not derive a chat path.
        return 'openai-chat'
    return None


def visible_answer(value):
    if not isinstance(value, str):
        raise ModelCallError('response_invalid')
    value = re.sub(r'<think>.*?</think>', '', value, flags=re.S | re.I).strip()
    if '<think' in value.lower() or not value:
        raise ModelCallError('empty_answer')
    return value


def invoke_registered(model, request, timeout=125):
    """Only non-secret config/request files; credential arrives on worker stdin."""
    reference = model.get('credential_id') or model.get('credential_ref') or ''
    reference = reference.removeprefix('vault:')
    if not reference:
        raise ModelCallError('credential_missing')
    protocol = _protocol_for(model)
    if protocol is None:
        raise ModelCallError('manifest_invalid')
    config = {'model_type': model['model_type'], 'model': model['model'],
              'base_url': model['base_url'], 'protocol': protocol,
              'credential_ref': 'vault:' + reference}
    with tempfile.TemporaryDirectory(prefix='workbench-aux-') as directory:
        root = Path(directory)
        config_path, request_path = root / 'model.json', root / 'request.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        config_path.chmod(0o600)
        request_path.chmod(0o600)
        command = [str(Path.home() / '.codex/scripts/key-vault/key-vault.sh'), 'exec-stdin', reference,
                   sys.executable, '-I', str(Path(__file__).with_name('capability_worker.py')),
                   str(config_path), str(request_path)]
        try:
            response = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, timeout=timeout,
                                      env={k:v for k,v in os.environ.items() if k not in {
                                          'OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN','PYTHONPATH','PYTHONHOME'}})
        except subprocess.TimeoutExpired as exc:
            raise ModelCallError('timeout') from exc
        except OSError as exc:
            raise ModelCallError('worker_unavailable') from exc
        if response.returncode or len(response.stdout) > 4 * 1024 * 1024:
            raise ModelCallError('worker_failed')
        try:
            result = json.loads(response.stdout)
        except (ValueError, UnicodeError) as exc:
            raise ModelCallError('response_invalid') from exc
        if not isinstance(result, dict):
            raise ModelCallError('response_invalid')
        if not result.get('success'):
            # Never forward provider bodies or errors containing request material.
            safe = {'timeout','rate_limited','unauthorized','forbidden','request_invalid',
                    'response_invalid','credential_missing','credential_target','credential_format',
                    'http_error','network_error','model_not_allowed','manifest_invalid'}
            raise ModelCallError(result.get('code') if result.get('code') in safe else 'provider_failed')
        if request['tool'] == 'reasoning_chat':
            result['text'] = visible_answer(result.get('text'))
        return result


class PlanningModels:
    def __init__(self, models_provider, accounts_provider=None, invoke=None, record=None, enabled=True):
        self.models_provider = models_provider
        self.accounts_provider = accounts_provider or (lambda: [])
        self.invoke = invoke or invoke_registered
        self.record = record
        self.enabled = enabled
        self.events = []
        self.telemetry_db = None
        self.telemetry_lock = None

    def bind_telemetry(self, db, lock=None):
        """Persist route metadata only; requests, answers and credentials never enter telemetry."""
        self.telemetry_db, self.telemetry_lock = db, lock
        db.execute("""CREATE TABLE IF NOT EXISTS planning_model_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, capability TEXT NOT NULL, purpose TEXT NOT NULL,
            task_id TEXT,
            model_id TEXT NOT NULL, provider_id TEXT NOT NULL, success INTEGER NOT NULL,
            code TEXT, fallback_reason TEXT, elapsed_ms INTEGER NOT NULL, usage TEXT, cost REAL, route TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        try:
            db.execute("ALTER TABLE planning_model_calls ADD COLUMN fallback_reason TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE planning_model_calls ADD COLUMN task_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE planning_model_calls ADD COLUMN cost REAL")
        except sqlite3.OperationalError:
            pass
        db.execute("CREATE INDEX IF NOT EXISTS planning_model_calls_route ON planning_model_calls(capability,purpose,model_id,created_at)")
        db.execute("""CREATE TABLE IF NOT EXISTS planning_model_evaluations (
            call_id INTEGER PRIMARY KEY REFERENCES planning_model_calls(id),
            quality TEXT, adopted INTEGER, correction_count INTEGER, reason TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK (quality IS NULL OR quality IN ('accepted','rejected','needs_correction')),
            CHECK (adopted IS NULL OR adopted IN (0,1)),
            CHECK (correction_count IS NULL OR correction_count >= 0)
        )""")

    def _recent_latencies(self, capability, purpose):
        if self.telemetry_db is None:
            return {}
        rows = self.telemetry_db.execute("""SELECT model_id,elapsed_ms FROM planning_model_calls
            WHERE capability=? AND purpose=? AND success=1 ORDER BY id DESC LIMIT 100""",
            (capability, purpose)).fetchall()
        values = {}
        for row in rows:
            values.setdefault(row['model_id'], []).append(row['elapsed_ms'])
        return {model_id: round(sum(samples[:10]) / min(len(samples), 10)) for model_id, samples in values.items()}

    def _persist(self, event, capability):
        if self.telemetry_db is None:
            return
        cursor = self.telemetry_db.execute("""INSERT INTO planning_model_calls
            (capability,purpose,task_id,model_id,provider_id,success,code,fallback_reason,elapsed_ms,usage,cost,route,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (capability, event['purpose'], event.get('task_id'), event['model_id'],
            event['provider_id'], int(event['success']), event['code'], event.get('fallback_reason'), event['elapsed_ms'],
            json.dumps(event['usage'], separators=(',', ':')) if event['usage'] is not None else None, event.get('cost'),
            json.dumps(event['route'], separators=(',', ':')), datetime.now(timezone.utc).isoformat()))
        event['call_id'] = cursor.lastrowid

    def record_evaluation(self, call_id, *, task_id, quality=None, adopted=None, correction_count=None, reason):
        """Attach user or review evidence to one recorded model call.

        This intentionally cannot create a call record: quality, adoption and
        corrections are unknown until a caller supplies evidence for an actual
        invocation.
        """
        if self.telemetry_db is None:
            raise ModelCallError('telemetry_unavailable')
        if isinstance(call_id, bool) or not isinstance(call_id, int) or call_id < 1:
            raise ValueError('call_id_invalid')
        if not isinstance(task_id, str) or not task_id:
            raise ValueError('task_id_invalid')
        if quality not in {None, 'accepted', 'rejected', 'needs_correction'}:
            raise ValueError('quality_invalid')
        if adopted is not None and not isinstance(adopted, bool):
            raise ValueError('adoption_invalid')
        if (correction_count is not None and (isinstance(correction_count, bool)
                                              or not isinstance(correction_count, int)
                                              or correction_count < 0)):
            raise ValueError('correction_count_invalid')
        if quality is None and adopted is None and correction_count is None:
            raise ValueError('evaluation_empty')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError('reason_invalid')
        exists = self.telemetry_db.execute('SELECT 1 FROM planning_model_calls WHERE id=? AND task_id=?',
                                           (call_id, task_id)).fetchone()
        if exists is None:
            raise ValueError('call_not_found')
        with self.telemetry_db:
            self.telemetry_db.execute("""INSERT INTO planning_model_evaluations
                (call_id,quality,adopted,correction_count,reason,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(call_id) DO UPDATE SET quality=excluded.quality, adopted=excluded.adopted,
                correction_count=excluded.correction_count, reason=excluded.reason, updated_at=excluded.updated_at""",
                (call_id, quality, None if adopted is None else int(adopted), correction_count, reason.strip(),
                 datetime.now(timezone.utc).isoformat()))

    def stats(self, capability=None, purpose=None, task_id=None):
        """Return aggregate route evidence without request/answer material."""
        if self.telemetry_db is None:
            return []
        where, values = [], []
        if capability is not None:
            where.append('capability=?'); values.append(capability)
        if purpose is not None:
            where.append('purpose=?'); values.append(purpose)
        if task_id is not None:
            where.append('task_id=?'); values.append(task_id)
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        rows = self.telemetry_db.execute("""SELECT c.id,c.capability,c.purpose,c.task_id,c.model_id,c.provider_id,
            c.success,c.elapsed_ms,c.usage,c.cost,c.fallback_reason,e.quality,e.adopted,e.correction_count
            FROM planning_model_calls c LEFT JOIN planning_model_evaluations e ON e.call_id=c.id""" + clause +
            " ORDER BY c.capability,c.purpose,c.model_id,c.id", values).fetchall()
        groups = {}
        for row in rows:
            value = dict(row)
            key = tuple(value[name] for name in ('capability', 'purpose', 'task_id', 'model_id', 'provider_id'))
            groups.setdefault(key, []).append(value)
        return [self._stats_group(group) for group in groups.values()]

    def task_calls(self, task_id):
        """Return safe, persisted call/evaluation evidence for a task's review UI."""
        if self.telemetry_db is None:
            return []
        if not isinstance(task_id, str) or not task_id:
            raise ValueError('task_id_invalid')
        rows = self.telemetry_db.execute("""SELECT c.id AS call_id,c.capability,c.purpose,c.task_id,c.model_id,
            c.provider_id,c.success,c.code,c.fallback_reason,c.elapsed_ms,c.usage,c.cost,c.route,c.created_at,
            e.quality,e.adopted,e.correction_count,e.reason,e.updated_at
            FROM planning_model_calls c LEFT JOIN planning_model_evaluations e ON e.call_id=c.id
            WHERE c.task_id=? ORDER BY c.id DESC""", (task_id,)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            try:
                value['usage'] = json.loads(value['usage']) if value['usage'] is not None else None
            except (TypeError, ValueError):
                value['usage'] = None
            try:
                value['route'] = json.loads(value['route'])
            except (TypeError, ValueError):
                value['route'] = None
            result.append(value)
        return result

    @staticmethod
    def _stats_group(rows):
        first = rows[0]
        result = {name: first[name] for name in ('capability', 'purpose', 'task_id', 'model_id', 'provider_id')}
        result['calls'] = len(rows)
        result['successes'] = sum(row['success'] for row in rows)
        result['average_latency_ms'] = round(sum(row['elapsed_ms'] for row in rows) / len(rows))
        result['total_elapsed_ms'] = sum(row['elapsed_ms'] for row in rows)
        result['fallbacks'] = sum(row['fallback_reason'] is not None for row in rows)
        qualities = [row['quality'] for row in rows]
        adoptions = [row['adopted'] for row in rows]
        corrections = [row['correction_count'] for row in rows]
        # A partial review is evidence about only those calls, not a pass rate for
        # the group.  Keep the aggregate unknown until every call is evaluated.
        result['quality_pass_rate'] = (round(sum(value == 'accepted' for value in qualities) / len(rows), 4)
                                       if all(value is not None for value in qualities) else None)
        result['adoption_rate'] = (round(sum(adoptions) / len(rows), 4)
                                   if all(value is not None for value in adoptions) else None)
        result['correction_count'] = sum(corrections) if all(value is not None for value in corrections) else None
        usages = []
        for row in rows:
            try:
                usage = json.loads(row['usage']) if row['usage'] is not None else None
            except (TypeError, ValueError):
                usage = None
            usages.append(usage if isinstance(usage, dict) else None)
        for field in ('input_tokens', 'output_tokens', 'total_tokens'):
            values = [usage.get(field) if usage is not None else None for usage in usages]
            result[field] = (sum(values) if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                                for value in values) else None)
        costs = [row['cost'] for row in rows]
        result['cost'] = (sum(costs) if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                            for value in costs) else None)
        return result

    @staticmethod
    def _instant(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, AttributeError):
            return None

    @classmethod
    def _quota(cls, account, now):
        """Return conservative quota state without treating absent observations as fresh."""
        windows = account.get('usage_windows') or [{'usage': account.get('usage', {}), 'resets_at': account.get('resets_at')}]
        ratios, resets, freshness = [], [], []
        observed = cls._instant(account.get('updated_at') or account.get('observed_at'))
        for window in windows:
            usage = window.get('usage') or {}
            reset = cls._instant(window.get('resets_at'))
            timestamp = cls._instant(usage.get('observed_at')) or observed
            is_fresh = bool(timestamp and (now - timestamp).total_seconds() <= 300)
            freshness.append(is_fresh)
            remaining, limit = usage.get('remaining'), usage.get('limit')
            ratio = None
            if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
                ratio = max(0, min(1, remaining / limit))
            # A zero observation that predates its reset is no longer evidence of exhaustion.
            if ratio == 0 and reset and reset <= now:
                ratio = None
            if ratio is not None and is_fresh:
                ratios.append(ratio)
            if reset and reset > now:
                resets.append((reset - now).total_seconds())
        state = 'fresh' if freshness and all(freshness) else 'unknown'
        return (min(ratios) if state == 'fresh' and ratios else None, min(resets) if resets else None, state)

    def candidates(self, capability='reasoning', *, external_allowed=False, purpose='auxiliary'):
        if not self.enabled or not external_allowed:
            return []
        try:
            accounts = self.accounts_provider() or []
            if isinstance(accounts, dict):
                accounts = accounts.get('accounts', [])
        except Exception:
            accounts = []
        quotas = {}
        now = datetime.now(timezone.utc)
        for account in accounts:
            provider_id = account.get('provider_id')
            if provider_id:
                quotas[provider_id] = self._quota(account, now)
        latencies = self._recent_latencies(capability, purpose)
        result = []
        for model in self.models_provider() or []:
            if (model.get('provider_id') not in {'minimax', 'bigmodel'} or model.get('model_type') != capability
                    or model.get('validation_status') != 'verified' or not model.get('model')
                    or not _protocol_for(model) or model.get('registered') is False
                    or not (model.get('credential_id') or model.get('credential_ref'))):
                continue
            url = urlsplit(model.get('base_url', ''))
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                continue
            ratio, reset, freshness = quotas.get(model['provider_id'], (None, None, 'unknown'))
            if ratio is not None and ratio <= 0:
                continue
            candidate = dict(model)
            candidate['_route'] = {'remaining_ratio': ratio, 'reset_in_seconds': reset,
                                   'freshness': freshness,
                                   'reason': 'quota_available' if ratio is not None else 'quota_unknown',
                                   'recent_latency_ms': latencies.get(model['id'])}
            result.append(candidate)
        # Fresh usable quota comes first.  MiniMax is the preferred auxiliary provider;
        # BigModel/GLM remains capability-matched rather than being invented as fallback.
        result.sort(key=lambda m: (m['_route']['remaining_ratio'] is None,
                                  m['provider_id'] != 'minimax',
                                  not ((m['_route']['remaining_ratio'] or 0) >= .2 and (m['_route']['reset_in_seconds'] or float('inf')) < 21600),
                                  -(m['_route']['remaining_ratio'] or 0),
                                  m['_route']['recent_latency_ms'] is None,
                                  m['_route']['recent_latency_ms'] or float('inf'),
                                  m['provider_id'] != 'minimax', m['id']))
        return result

    def call(self, capability, payload, *, external_allowed=False, purpose='auxiliary', task_id=None, allow_fallback=True):
        models = self.candidates(capability, external_allowed=external_allowed, purpose=purpose)
        if not models:
            raise ModelCallError('data_boundary' if not external_allowed else 'model_unavailable')
        tried = set()
        last = 'model_unavailable'
        for model in models:
            if model['provider_id'] in tried or len(tried) >= (2 if allow_fallback else 1):
                continue
            tried.add(model['provider_id'])
            started = time.monotonic()
            event = {'model_id': model['id'], 'provider_id': model['provider_id'], 'purpose': purpose,
                     'task_id': task_id, 'route': model['_route'], 'success': False, 'code': None,
                     'usage': None, 'cost': None}
            if last != 'model_unavailable':
                event['fallback_reason'] = last
            try:
                tool = {'reasoning': 'reasoning_chat', 'embedding': 'embedding', 'rerank': 'rerank'}[capability]
                result = self.invoke(model, {'tool': tool, 'model_id': model['model'], **payload})
                if not isinstance(result, dict) or result.get('success') is False:
                    raise ModelCallError('response_invalid')
                if capability == 'reasoning':
                    result['text'] = visible_answer(result.get('text'))
                event['success'] = True
                event['usage'] = {k:result[k] for k in ('input_tokens','output_tokens','total_tokens') if result.get(k) is not None} or None
                if isinstance(result.get('cost'), (int, float)) and not isinstance(result.get('cost'), bool):
                    event['cost'] = result['cost']
                response = {**result, 'model_id': model['id'], 'route': model['_route']}
            except ModelCallError as exc:
                last = event['code'] = exc.code
            except Exception:
                last = event['code'] = 'provider_failed'
            finally:
                event['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                self.events.append(event)
                del self.events[:-100]
                self._persist(event, capability)
                if self.record:
                    self.record(event)
            if event['success']:
                response['model_call_id'] = event.get('call_id')
                return response
            if not allow_fallback:
                raise ModelCallError(last)
        raise ModelCallError(last)
