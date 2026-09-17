"""隔离进程通过 stdin 接收密钥，只返回白名单内的 MiniMax 用量。"""
import json
import math
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ENDPOINT = 'https://www.minimaxi.com/v1/token_plan/remains'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """固定认证接收方，拒绝重定向转发。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_usage(data, observed_at):
    """将官方百分比和毫秒时间映射成两个窗口，不使用为零的计数字段推断用量。"""
    base = data.get('base_resp') if isinstance(data, dict) else None
    if not isinstance(base, dict) or type(base.get('status_code')) is not int or base['status_code'] != 0:
        raise ValueError('upstream_rejected')
    records = data.get('model_remains')
    if not isinstance(records, list):
        raise ValueError('invalid_response')
    general = [row for row in records if isinstance(row, dict) and row.get('model_name') == 'general']
    if len(general) != 1:
        raise ValueError('invalid_response')
    row = general[0]
    windows = []
    for identifier, label, prefix, start, end in (
        ('general-five-hour', '通用额度 · 5 小时', 'current_interval', 'start_time', 'end_time'),
        ('general-week', '通用额度 · 本周', 'current_weekly', 'weekly_start_time', 'weekly_end_time'),
    ):
        remaining = row.get(prefix + '_remaining_percent')
        if type(remaining) not in (int, float) or not math.isfinite(remaining) or not 0 <= remaining <= 100:
            raise ValueError('invalid_response')
        starts, ends = row.get(start), row.get(end)
        if type(starts) is not int or type(ends) is not int or not 0 < starts < ends < 100_000_000_000_000:
            raise ValueError('invalid_response')
        if identifier == 'general-five-hour' and ends - starts != 18_000_000:
            label = '通用额度 · 当前窗口'
        windows.append({'id': identifier, 'label': label,
            'usage': {'used': 100 - remaining, 'limit': 100, 'remaining': remaining, 'unit': '%',
                      'source': ENDPOINT, 'observed_at': observed_at},
            'resets_at': datetime.fromtimestamp(ends / 1000, timezone.utc).isoformat()})
    return {'usage': windows[0]['usage'], 'resets_at': windows[0]['resets_at'],
            'usage_windows': windows, 'updated_at': observed_at,
            'api_auth': {'status': 'accepted', 'source': ENDPOINT, 'observed_at': observed_at}}


def read_usage(key):
    """仅发起一次官方 GET；上游错误与异常正文均不返回父进程。"""
    request = urllib.request.Request(ENDPOINT, headers={
        'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}, method='GET')
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError('invalid_response')
            data = json.loads(raw)
            base = data.get('base_resp') if isinstance(data, dict) else None
            if isinstance(base, dict) and base.get('status_code') != 0:
                return {'ok': False, 'code': 'auth_rejected' if base.get('status_code') == 1004 else 'provider_rejected'}
            snapshot = normalize_usage(data, datetime.now(timezone.utc).isoformat())
            return {'ok': True, 'snapshot': snapshot}
    except urllib.error.HTTPError as error:
        return {'ok': False, 'code': 'auth_rejected' if error.code in (401, 403) else 'provider_unavailable'}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {'ok': False, 'code': 'network_failed'}
    except (ValueError, TypeError, AttributeError, OverflowError):
        return {'ok': False, 'code': 'invalid_response'}


def credential_key(raw):
    """兼容保险库的原始 Key 与含 api_key 的 JSON 载荷，仅在消费者内解码。"""
    if len(raw) > 65536:
        raise ValueError('payload_too_large')
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        payload = raw.decode('utf-8').strip()
    key = payload.get('api_key') if isinstance(payload, dict) else payload
    if not isinstance(key, str) or not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('invalid_key')
    return key


def main():
    """受控消费保险库载荷，所有失败均输出固定错误码，不回显密钥。"""
    try:
        raw = sys.stdin.buffer.read(65537)
        key = credential_key(raw)
        result = read_usage(key)
    except (ValueError, TypeError, AttributeError):
        result = {'ok': False, 'code': 'credential_unavailable'}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
