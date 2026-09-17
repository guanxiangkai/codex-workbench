"""原生账户接入的更新检查；协议通过不能替代真实桌面验收。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


# 只比较接入实际依赖的字段；文案和无关可选字段变化不算破坏。
FIELDS = {
    'InitializeParams': ['clientInfo', 'capabilities'],
    'InitializeResponse': ['codexHome'],
    'ThreadStartParams': ['cwd', 'projectId', 'model', 'modelProvider', 'ephemeral'],
    'ThreadStartResponse': ['thread.id', 'thread.path', 'thread.projectId'],
    'TurnStartParams': ['threadId', 'input'],
    'TurnStartResponse': ['turn.id', 'turn.status'],
    'ThreadResumeParams': ['threadId', 'path', 'excludeTurns'],
    'ThreadResumeResponse': ['thread.id', 'thread.path'],
    'ThreadReadParams': ['threadId', 'includeTurns'],
    'ThreadListParams': ['cursor', 'limit', 'archived', 'useStateDbOnly'],
    'ThreadListResponse': ['nextCursor', 'backwardsCursor'],
    'ThreadArchiveParams': ['threadId'],
    'ProjectCreateParams': ['idempotencyKey', 'name', 'roots'],
    'ProjectListParams': ['cursor', 'limit'],
    'ThreadStartedNotification': ['thread.id', 'thread.path'],
    'ServerRequestResolvedNotification': ['requestId', 'threadId'],
}
METHODS = {'initialize', 'thread/start', 'turn/start', 'thread/resume', 'thread/read',
           'thread/list', 'thread/archive', 'project/create', 'project/list'}
NATIVE_CASES = {'new_task_default', 'prewarm_default_change', 'existing_thread_owner',
                'tool_callback_routing', 'inactive_archive', 'restart_resume', 'primary_identity_unchanged'}
PROTOCOL_CASES = {'shared_project', 'first_turn_completed', 'first_backend_only', 'same_thread_after_resume',
                  'second_turn_completed', 'new_backend_used', 'previous_context_received', 'visible_in_list',
                  'archive_using_storage_owner', 'no_auth_headers', 'no_tool_execution'}


def file_digest(path: Path) -> str:
    """按块计算版本制品摘要，不读取用户状态或登录材料。"""
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve(schema: dict, node: dict) -> dict:
    while '$ref' in node:
        reference = node['$ref']
        if not reference.startswith('#/definitions/'):
            raise ValueError('Unsupported schema reference')
        node = schema['definitions'][reference.rsplit('/', 1)[1]]
    return node


def _shape(schema: dict, node, seen=frozenset()):
    if isinstance(node, list):
        return [_shape(schema, item, seen) for item in node]
    if not isinstance(node, dict):
        return node
    if '$ref' in node:
        reference = node['$ref']
        if reference in seen:
            return {'recursive': reference}
        return _shape(schema, _resolve(schema, node), seen | {reference})
    return {key: ({name: _shape(schema, field, seen) for name, field in value.items()}
                  if key == 'properties' else _shape(schema, value, seen)) for key, value in node.items()
            if key not in {'description', 'title', 'examples', '$schema', 'definitions'}}


def protocol_contract(directory: Path) -> dict:
    """提取官方生成 schema 的相关形状；缺文件或缺字段直接拒绝。"""
    contract = {}
    for name, fields in FIELDS.items():
        paths = list(directory.rglob(name + '.json'))
        if len(paths) != 1:
            raise ValueError(f'Ambiguous or absent schema: {name}')
        schema = json.loads(paths[0].read_text())
        selected = {}
        for field in fields:
            node = schema
            for part in field.split('.'):
                node = _resolve(schema, node).get('properties', {}).get(part)
                if node is None:
                    raise ValueError(f'Missing protocol field: {name}.{field}')
            selected[field] = _shape(schema, node)
        # 新增必填入参会破坏现有调用，即使其不是此前选择的字段。
        if name.endswith('Params'):
            selected['$required'] = sorted(schema.get('required', []))
        contract[name] = selected
    requests = json.loads((directory / 'ClientRequest.json').read_text())
    methods = {method for variant in requests.get('oneOf', [])
               for method in variant.get('properties', {}).get('method', {}).get('enum', [])}
    missing = METHODS - methods
    if missing:
        raise ValueError('Missing protocol methods: ' + ', '.join(sorted(missing)))
    contract['methods'] = sorted(METHODS)
    return contract


def evaluate_upgrade(baseline: dict, observed: dict, checks: dict,
                     native_acceptance: dict | None = None, *, artifacts: dict | None = None) -> dict:
    """同时要求协议、隔离行为与准确版本的原生验收；任何缺证据均不放行。"""
    changes = sorted(key for key in baseline.keys() | observed.keys() if baseline.get(key) != observed.get(key))
    behavior_ok = all(checks.get(case) is True for case in PROTOCOL_CASES)
    native = native_acceptance or {}
    # 两个制品摘要都须准确匹配；版本号相同也不能沿用被替换代码的验收。
    hashes = ('cli_sha256', 'desktop_sha256')
    artifact_match = bool(artifacts) and all(
        isinstance(artifacts.get(key), str) and len(artifacts[key]) == 64
        and artifacts[key] == native.get('artifacts', {}).get(key) for key in hashes)
    native_ok = artifact_match and all(native.get('cases', {}).get(case) is True for case in NATIVE_CASES)
    return {'protocol_compatible': not changes, 'changed_contracts': changes,
            'isolated_behavior_passed': behavior_ok, 'native_acceptance_passed': native_ok,
            'native_routing_ready': not changes and behavior_ok and native_ok}
