"""按页面读取刷新外部账户用量；不持久化秘密、业务目录或调度任务。"""
import json
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .minimax_usage_worker import ENDPOINT
from .bigmodel_usage_worker import ENDPOINT as BIGMODEL_ENDPOINT

MESSAGES = {
    'auth_rejected': '用量刷新失败：供应商 拒绝了当前密钥，保留上次数据。',
    'provider_rejected': '用量刷新失败：供应商 未返回可用套餐数据，保留上次数据。',
    'network_failed': '用量刷新失败：暂时无法连接 供应商，保留上次数据。',
    'invalid_response': '用量刷新失败：供应商 返回的数据格式异常，保留上次数据。',
    'credential_unavailable': '用量刷新失败：无法读取已关联的密钥，保留上次数据。',
    'provider_unavailable': '用量刷新失败：供应商 服务暂不可用，保留上次数据。',
}


def fetch_usage(credential_id, provider='minimax'):
    """固定消费者经保险库 stdin 使用密钥；父进程只取得经过筛选的用量。"""
    if not isinstance(credential_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', credential_id):
        return {'ok': False, 'code': 'credential_unavailable'}
    command = Path.home() / '.codex/scripts/key-vault/key-vault.sh'
    workers={'minimax':'minimax_usage_worker.py','bigmodel':'bigmodel_usage_worker.py'}
    if provider not in workers:return {'ok': False, 'code': 'provider_unavailable'}
    worker = Path(__file__).with_name(workers[provider])
    try:
        result = subprocess.run([str(command), 'exec-stdin', credential_id, sys.executable, str(worker)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=False)
        if result.returncode or len(result.stdout) > 32768:
            return {'ok': False, 'code': 'credential_unavailable'}
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or type(response.get('ok')) is not bool:
            return {'ok': False, 'code': 'invalid_response'}
        return response
    except subprocess.TimeoutExpired:
        return {'ok': False, 'code': 'network_failed'}
    except (OSError, ValueError):
        return {'ok': False, 'code': 'credential_unavailable'}


class AccountUsage:
    """按显式账户和密钥引用隔离缓存；并发查询共享同一在途请求。"""

    def __init__(self, fetcher=fetch_usage, bigmodel_fetcher=None, data_dir=None):
        self.fetcher = fetcher
        self.bigmodel_fetcher = bigmodel_fetcher or (lambda credential: fetch_usage(credential,'bigmodel'))
        self.lock = threading.Lock()
        self.inflight = {}
        self.snapshots = OrderedDict()
        self.cache_path = Path(data_dir) / 'account-usage-cache.json' if data_dir is not None else None
        self._load_cache()

    @staticmethod
    def _binding(account):
        provider = account.get('provider_id')
        account_id = account.get('id')
        credential = account.get('usage_credential_id')
        if provider not in ('minimax', 'bigmodel') or not isinstance(account_id, str) or not isinstance(credential, str) or not credential:
            return None
        # Never put a vault reference in the process key or runtime cache.
        return provider, account_id, hashlib.sha256(credential.encode('utf-8')).hexdigest()

    @staticmethod
    def _is_catalog_newer(account, snapshot):
        catalog_at, snapshot_at = account.get('updated_at'), snapshot.get('updated_at') if isinstance(snapshot, dict) else None
        if not isinstance(catalog_at, str) or not isinstance(snapshot_at, str):
            return False
        try:
            return datetime.fromisoformat(snapshot_at.replace('Z', '+00:00')) < datetime.fromisoformat(catalog_at.replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return False

    def _load_cache(self):
        if self.cache_path is None:
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding='utf-8'))
            entries = raw.get('entries') if isinstance(raw, dict) and raw.get('version') == 1 else None
            if not isinstance(entries, list):
                return
            for entry in entries[-64:]:
                if not isinstance(entry, dict):
                    continue
                provider, account_id, credential_hash, snapshot = (entry.get('provider_id'), entry.get('account_id'), entry.get('credential_hash'), entry.get('snapshot'))
                usage = snapshot.get('usage') if isinstance(snapshot, dict) else None
                windows = snapshot.get('usage_windows') if isinstance(snapshot, dict) else None
                usage_valid = usage is None or isinstance(usage, dict)
                windows_valid = windows is None or (isinstance(windows, list) and all(
                    isinstance(window, dict) and isinstance(window.get('usage'), dict) for window in windows))
                has_quota = isinstance(usage, dict) or isinstance(windows, list)
                valid_snapshot = isinstance(snapshot, dict) and isinstance(snapshot.get('updated_at'), str) and usage_valid and windows_valid and has_quota
                if provider in ('minimax', 'bigmodel') and isinstance(account_id, str) and re.fullmatch(r'[0-9a-f]{64}', str(credential_hash)) and valid_snapshot:
                    self.snapshots[(provider, account_id, credential_hash)] = snapshot
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # A runtime cache is expendable. Corruption must never block page reads.
            self.snapshots.clear()

    def _save_cache(self):
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            payload = {'version': 1, 'entries': [
                {'provider_id': provider, 'account_id': account_id, 'credential_hash': credential_hash, 'snapshot': snapshot}
                for (provider, account_id, credential_hash), snapshot in self.snapshots.items()
            ]}
            encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{self.cache_path.name}.', suffix='.tmp', dir=self.cache_path.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, 'wb') as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.cache_path)
                os.chmod(self.cache_path, 0o600)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        except (OSError, TypeError, ValueError):
            # Quota collection is still useful when its local cache cannot be saved.
            return

    def _remember(self, binding, snapshot):
        self.snapshots[binding] = deepcopy(snapshot)
        self.snapshots.move_to_end(binding)
        while len(self.snapshots) > 64:
            self.snapshots.popitem(last=False)

    def cached(self, account):
        """页面读取只使用已取得的快照，不等待供应商网络请求。"""
        binding = self._binding(account)
        if binding is None:
            return {}
        with self.lock:
            previous = self.snapshots.get(binding, {})
            if previous and self._is_catalog_newer(account, previous):
                return {}
            return deepcopy(previous)

    def refresh(self, account):
        """返回用量覆盖字段，失败不清空上次成功的额度或伪造新的用量时间。"""
        credential = account.get('usage_credential_id')
        provider=account.get('provider_id')
        binding = self._binding(account)
        if binding is None:
            return {}
        with self.lock:
            future = self.inflight.get(binding)
            owner = future is None
            if owner:
                future = Future()
                self.inflight[binding] = future
        if not owner:
            return deepcopy(future.result(timeout=20))
        try:
            try:
                response = (self.bigmodel_fetcher if provider=='bigmodel' else self.fetcher)(credential)
            except Exception:
                response = {'ok': False, 'code': 'provider_unavailable'}
            checked = datetime.now(timezone.utc).isoformat()
            with self.lock:
                if response.get('ok') is True:
                    snapshot = deepcopy(response['snapshot'])
                    result = {**snapshot, 'usage_refresh': {'state': 'ok', 'observed_at': checked}}
                    self._remember(binding, result)
                    self._save_cache()
                else:
                    previous = self.snapshots.get(binding, {})
                    # 人工登记的更新数据优先于进程里更早的成功快照。
                    if previous and self._is_catalog_newer(account, previous):
                        previous = {}
                    code = response.get('code')
                    result = {**deepcopy(previous), 'usage_refresh': {'state': 'failed', 'observed_at': checked,
                        'message': MESSAGES.get(code, MESSAGES['provider_unavailable'])}}
                    if code == 'auth_rejected':
                        result['api_auth'] = {'status': 'rejected', 'source': BIGMODEL_ENDPOINT if provider=='bigmodel' else ENDPOINT, 'observed_at': checked}
                    result['usage_refresh']['code'] = code or 'provider_unavailable'
                    self._remember(binding, result)
            future.set_result(deepcopy(result))
            return result
        except Exception:
            # 意外失败也不允许带出异常中的秘密值。
            result = {'usage_refresh': {'state': 'failed', 'observed_at': datetime.now(timezone.utc).isoformat(),
                       'message': MESSAGES['provider_unavailable']}}
            future.set_result(result)
            return result
        finally:
            with self.lock:
                self.inflight.pop(binding, None)
