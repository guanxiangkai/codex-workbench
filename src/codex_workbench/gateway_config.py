"""接入原生 openai 端点；保持线程 provider 和其他配置，撤回时只处理受管内容。"""
from __future__ import annotations

import json
import re
import tomllib

ENDPOINT = 'http://127.0.0.1:18742/v1'
ENDPOINT_BLOCK = ('# BEGIN CODEX WORKBENCH ENDPOINT\n'
                  f'openai_base_url = "{ENDPOINT}"\n'
                  '# END CODEX WORKBENCH ENDPOINT\n')
TRANSPORT_BEGIN = '# BEGIN CODEX WORKBENCH TRANSPORT\n'
TRANSPORT_END = '# END CODEX WORKBENCH TRANSPORT\n'


def enable_builtin(text: str) -> str:
    """只接管官方直连；不覆盖其他提供方、端点或无法安全定位的配置写法。"""
    parsed = tomllib.loads(text)
    if parsed.get('model_provider', 'openai') != 'openai':
        raise ValueError('已有其他 provider，未覆盖')
    if ENDPOINT_BLOCK in text:
        if (text.count(ENDPOINT_BLOCK) == 1 and TRANSPORT_BEGIN in text
                and parsed.get('openai_base_url') == ENDPOINT
                and parsed.get('features', {}).get('enable_request_compression') is False):
            disable_builtin(text)
            return text
        raise ValueError('受管端点已变化，未覆盖')
    if parsed.get('openai_base_url') is not None or 'CODEX WORKBENCH ENDPOINT' in text or TRANSPORT_BEGIN in text:
        raise ValueError('已有其他端点或接入块，未覆盖')
    features = parsed.get('features', {})
    headers = list(re.finditer(r'^\[features\][ \t]*(?:#.*)?\n', text, re.M))
    if len(headers) > 1 or (features and not headers):
        raise ValueError('features 写法不能安全定位，未覆盖')
    if headers:
        start = headers[0].end()
        next_table = re.search(r'^\s*\[', text[start:], re.M)
        end = start + next_table.start() if next_table else len(text)
        block = text[start:end]
        match = re.search(r'^[ \t]*enable_request_compression[ \t]*=[ \t]*(?:true|false)[ \t]*(?:#[^\n]*)?(?:\n|$)', block, re.M)
        if 'enable_request_compression' in features and not match:
            raise ValueError('压缩设置不能安全定位，未覆盖')
        previous = match.group() if match else ''
        at = start + match.start() if match else start
    else:
        text += '\n[features]\n'
        at = len(text)
        previous = ''
    transport = (TRANSPORT_BEGIN + '# previous = ' + json.dumps(previous) + '\n'
                 'enable_request_compression = false\n' + TRANSPORT_END)
    result = ENDPOINT_BLOCK + text[:at] + transport + text[at + len(previous):]
    expected = dict(parsed)
    expected['openai_base_url'] = ENDPOINT
    expected['features'] = {**features, 'enable_request_compression': False}
    if tomllib.loads(result) != expected:
        raise ValueError('接入会改变其他配置，未覆盖')
    return result


def disable_builtin(text: str) -> str:
    """恢复接入前的压缩选项；用户的其他编辑保持原样。"""
    if text.count(ENDPOINT_BLOCK) != 1 or text.count(TRANSPORT_BEGIN) != 1 or text.count(TRANSPORT_END) != 1:
        raise ValueError('接入块已变化，未自动修改')
    pattern = re.escape(TRANSPORT_BEGIN) + r'# previous = ([^\n]+)\nenable_request_compression = false\n' + re.escape(TRANSPORT_END)
    match = re.search(pattern, text)
    if match is None:
        raise ValueError('传输配置已变化，未自动修改')
    previous = json.loads(match.group(1))
    if not isinstance(previous, str) or (previous and not re.fullmatch(r'[ \t]*enable_request_compression[ \t]*=[ \t]*(?:true|false)[ \t]*(?:#[^\n]*)?\n?', previous)):
        raise ValueError('恢复选项无效，未自动修改')
    result = (text[:match.start()] + previous + text[match.end():]).replace(ENDPOINT_BLOCK, '', 1)
    expected = tomllib.loads(text)
    del expected['openai_base_url']
    if previous:
        expected['features']['enable_request_compression'] = tomllib.loads(previous)['enable_request_compression']
    else:
        expected['features'].pop('enable_request_compression')
    if tomllib.loads(result) != expected:
        raise ValueError('撤回会改变其他配置，未自动修改')
    return result
