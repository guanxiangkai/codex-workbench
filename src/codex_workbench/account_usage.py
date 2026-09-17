"""按页面读取刷新外部账户用量；不持久化秘密、业务目录或调度任务。"""
import json
import re
import subprocess
import sys
import threading
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .minimax_usage_worker import ENDPOINT

MESSAGES = {
    'auth_rejected': '用量刷新失败：MiniMax 拒绝了当前密钥，保留上次数据。',
    'provider_rejected': '用量刷新失败：MiniMax 未返回可用套餐数据，保留上次数据。',
    'network_failed': '用量刷新失败：暂时无法连接 MiniMax，保留上次数据。',
    'invalid_response': '用量刷新失败：MiniMax 返回的数据格式异常，保留上次数据。',
    'credential_unavailable': '用量刷新失败：无法读取已关联的密钥，保留上次数据。',
    'provider_unavailable': '用量刷新失败：MiniMax 服务暂不可用，保留上次数据。',
}


def fetch_usage(credential_id):
    """固定消费者经保险库 stdin 使用密钥；父进程只取得经过筛选的用量。"""
    if not isinstance(credential_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', credential_id):
        return {'ok': False, 'code': 'credential_unavailable'}
    command = Path.home() / '.codex/scripts/key-vault/key-vault.sh'
    worker = Path(__file__).with_name('minimax_usage_worker.py')
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

    def __init__(self, fetcher=fetch_usage):
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.inflight = {}
        self.snapshots = OrderedDict()

    def refresh(self, account):
        """返回用量覆盖字段，失败不清空上次成功的额度或伪造新的用量时间。"""
        credential = account.get('usage_credential_id')
        if account.get('provider_id') != 'minimax' or not credential:
            return {}
        binding = (account['id'], credential)
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
                response = self.fetcher(credential)
            except Exception:
                response = {'ok': False, 'code': 'provider_unavailable'}
            checked = datetime.now(timezone.utc).isoformat()
            with self.lock:
                if response.get('ok') is True:
                    snapshot = deepcopy(response['snapshot'])
                    self.snapshots[binding] = snapshot
                    self.snapshots.move_to_end(binding)
                    while len(self.snapshots) > 64:
                        self.snapshots.popitem(last=False)
                    result = {**snapshot, 'usage_refresh': {'state': 'ok', 'observed_at': checked}}
                else:
                    previous = self.snapshots.get(binding, {})
                    # 人工登记的更新数据优先于进程里更早的成功快照。
                    if previous and account.get('updated_at'):
                        if datetime.fromisoformat(previous['updated_at']) < datetime.fromisoformat(account['updated_at']):
                            previous = {}
                    code = response.get('code')
                    result = {**deepcopy(previous), 'usage_refresh': {'state': 'failed', 'observed_at': checked,
                        'message': MESSAGES.get(code, MESSAGES['provider_unavailable'])}}
                    if code == 'auth_rejected':
                        result['api_auth'] = {'status': 'rejected', 'source': ENDPOINT, 'observed_at': checked}
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
