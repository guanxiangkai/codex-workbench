"""智谱官方 Coding Plan 只读配额，协议来源 zai-org/zai-coding-plugins。"""
import json
import math
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
if __package__:
    from .minimax_usage_worker import NoRedirect, credential_key
else:
    from minimax_usage_worker import NoRedirect, credential_key

ENDPOINT = 'https://open.bigmodel.cn/api/monitor/usage/quota/limit'


def normalize_usage(data, observed_at):
    if not isinstance(data, dict) or data.get('success') is not True or data.get('code') != 200:
        raise ValueError('provider_rejected')
    limits = data.get('data', {}).get('limits')
    if not isinstance(limits, list) or not 1 <= len(limits) <= 16:
        raise ValueError('invalid_response')
    windows = []
    for index, row in enumerate(limits):
        percentage = row.get('percentage')
        if type(percentage) not in (int, float) or not math.isfinite(percentage) or not 0 <= percentage <= 100:
            raise ValueError('invalid_response')
        reset = row.get('nextResetTime')
        if reset is not None and (type(reset) is not int or not 0 < reset < 100_000_000_000_000):
            raise ValueError('invalid_response')
        # 官方只提供数字窗口枚举，不猜测其含义或重置时间。
        windows.append({'id': f'quota-{index + 1}', 'label': f'套餐额度 · 窗口 {index + 1}',
            'usage': {'used': percentage, 'limit': 100, 'remaining': 100 - percentage, 'unit': '%',
                      'source': ENDPOINT, 'observed_at': observed_at},
            'resets_at': datetime.fromtimestamp(reset / 1000, timezone.utc).isoformat() if reset else None})
    return {'usage': windows[0]['usage'], 'resets_at': windows[0]['resets_at'],
            'usage_windows': windows, 'updated_at': observed_at,
            'api_auth': {'status': 'accepted', 'source': ENDPOINT, 'observed_at': observed_at}}


def read_usage(key):
    request = urllib.request.Request(ENDPOINT, headers={'Authorization': key, 'Accept': 'application/json'}, method='GET')
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError('invalid_response')
        data = json.loads(raw)
        return {'ok': True, 'snapshot': normalize_usage(data, datetime.now(timezone.utc).isoformat())}
    except urllib.error.HTTPError as error:
        return {'ok': False, 'code': 'auth_rejected' if error.code in (401, 403) else 'provider_unavailable'}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {'ok': False, 'code': 'network_failed'}
    except (ValueError, TypeError, AttributeError, OverflowError):
        return {'ok': False, 'code': 'invalid_response'}


if __name__ == '__main__':
    try:
        result = read_usage(credential_key(sys.stdin.buffer.read(65537)))
    except (ValueError, TypeError, AttributeError):
        result = {'ok': False, 'code': 'credential_unavailable'}
    print(json.dumps(result, ensure_ascii=False))
