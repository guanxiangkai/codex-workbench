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
    """保留 Coding Plan 积分和周期；未知枚举不推断，缺少重置值不造时间。"""
    # 不同 Coding Plan 网关版本可能省略 success，或把 code 序列化为字符串。
    # 只在字段存在时校验，真正的结构仍由 limits 严格校验，避免放宽到任意响应。
    if not isinstance(data, dict):
        raise ValueError('provider_rejected')
    if 'success' in data and data.get('success') is not True:
        raise ValueError('provider_rejected')
    if 'code' in data and data.get('code') not in (200, '200'):
        raise ValueError('provider_rejected')
    payload = data.get('data')
    if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
        payload = payload['data']
    limits = payload.get('limits') if isinstance(payload, dict) else None
    if not isinstance(limits, list) or not 1 <= len(limits) <= 16:
        raise ValueError('invalid_response')
    windows = []
    identifiers = set()
    for index, row in enumerate(limits):
        if not isinstance(row, dict):
            raise ValueError('invalid_response')
        percentage = row.get('percentage')
        if type(percentage) not in (int, float) or not math.isfinite(percentage) or not 0 <= percentage <= 100:
            raise ValueError('invalid_response')
        reset = row.get('nextResetTime')
        if reset is not None and (type(reset) is not int or not 0 < reset < 100_000_000_000_000):
            raise ValueError('invalid_response')
        # 经官方套餐页与同账户配额接口对照：unit=3/number=5 为五小时，6/1 为周。
        # 不按数组顺序推断周期；其他类型和枚举保持未知窗口。
        identifier, label = f'quota-{index + 1}', f'套餐额度 · 未识别窗口 {index + 1}'
        if row.get('type') == 'CREDIT_LIMIT' and type(row.get('unit')) is int and type(row.get('number')) is int:
            known = {(3, 5): ('coding-five-hour', 'Coding Plan · 5 小时'),
                     (6, 1): ('coding-week', 'Coding Plan · 本周')}
            identifier, label = known.get((row['unit'], row['number']), (identifier, label))
        if identifier in identifiers:
            raise ValueError('invalid_response')
        identifiers.add(identifier)
        usage = {'used': percentage, 'limit': 100, 'remaining': 100 - percentage, 'unit': '%'}
        # usage 在此接口是积分上限，currentValue 才是已用量；不要将百分比当积分。
        credit_fields = ('currentValue', 'usage', 'remaining')
        if row.get('type') == 'CREDIT_LIMIT' and any(field in row for field in credit_fields):
            used, limit, remaining = (row.get(field) for field in credit_fields)
            if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                   for value in (used, limit, remaining)):
                raise ValueError('invalid_response')
            # 官方积分整数分别取整时可能相差 1；保留原值，不造出修正后的余额。
            if used > limit or remaining > limit or not math.isclose(used + remaining, limit, rel_tol=0, abs_tol=1):
                raise ValueError('invalid_response')
            usage = {'used': used, 'limit': limit, 'remaining': remaining, 'unit': '积分'}
            if used + remaining != limit:
                usage['rounding_difference'] = limit - used - remaining
        usage.update({'source': ENDPOINT, 'observed_at': observed_at})
        windows.append({'id': identifier, 'label': label, 'usage': usage,
            'resets_at': datetime.fromtimestamp(reset / 1000, timezone.utc).isoformat() if reset else None})
    windows.sort(key=lambda window: {'coding-five-hour': 0, 'coding-week': 1}.get(window['id'], 2))
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
