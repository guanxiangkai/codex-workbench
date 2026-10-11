"""只从两家官方 OpenAPI 的请求模型枚举发现接口，不执行网页内容。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

from .model_catalog import configured_models, registered_models, _model
from .model_endpoints import resolve_endpoint


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 文档地址和生产路径来自官方契约，响应不能指定凭据目的地。
MINIMAX = 'https://platform.minimax.cn/docs/api-reference/'
SOURCES = {
    'minimax': [
        (MINIMAX+'text/api/openapi-chat-openai.json', '/v1/chat/completions', 'reasoning'),
        (MINIMAX+'speech/t2a/api/openapi.json', '/v1/t2a_v2', 'text_to_speech'),
        (MINIMAX+'speech/speech-to-text/api/openapi.json', '/v1/speech_to_text', 'speech_to_text'),
        (MINIMAX+'image/generation/api/text-to-image.json', '/v1/image_generation', 'image_generation'),
        (MINIMAX+'video/generation/api/v2-video-generation.json', '/v2/video_generation', 'video_generation'),
    ],
    'bigmodel': [('https://docs.bigmodel.cn/openapi/openapi.json', path, kind) for path, kind in (
        ('/paas/v4/chat/completions', 'reasoning'),
        ('/paas/v4/audio/speech', 'text_to_speech'),
        ('/paas/v4/audio/transcriptions', 'speech_to_text'),
        ('/paas/v4/images/generations', 'image_generation'),
        ('/paas/v4/videos/generations', 'video_generation'),
        ('/paas/v4/embeddings', 'embedding'),
        ('/paas/v4/rerank', 'rerank'),
        ('/paas/v4/layout_parsing', 'multimodal'),
    )],
}
ORIGINS = {'minimax': 'https://api.minimax.cn', 'bigmodel': 'https://open.bigmodel.cn/api'}
NAMES = {'minimax': 'MiniMax', 'bigmodel': '智谱 GLM'}
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


def fetch_document(url):
    """定长、超时、无重定向地读取固定官方来源；不附带账户凭据。"""
    if url not in {source for sources in SOURCES.values() for source, _, _ in sources}:
        raise ValueError('非官方模型来源')
    with build_opener(_NoRedirect()).open(Request(url, headers={'Accept': 'application/json'}), timeout=15) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise ValueError('模型文档过大')
    value = json.loads(body)
    if not isinstance(value, dict) or not isinstance(value.get('paths'), dict):
        raise ValueError('官方文档格式变化')
    return value


def _schemas(document, schema, depth=0):
    if depth > 12 or not isinstance(schema, dict):
        raise ValueError('模型请求结构无效')
    if '$ref' in schema:
        ref = schema['$ref']
        if not isinstance(ref, str) or not ref.startswith('#/components/schemas/'):
            raise ValueError('不读取外部引用')
        target = document
        for part in ref[2:].split('/'):
            target = target[part]
        yield from _schemas(document, target, depth+1)
    elif 'properties' in schema:
        yield schema
    else:
        variants = schema.get('oneOf') or schema.get('anyOf') or schema.get('allOf')
        if not isinstance(variants, list) or not variants:
            raise ValueError('缺少请求模型定义')
        for variant in variants:
            # 音频聊天需要独立输入协议，不把它误当成普通文本聊天。
            if variant.get('title') == '音频模型':
                continue
            for resolved in _schemas(document, variant, depth+1):
                yield {**resolved, '_title': variant.get('title', '')}


def parse_models(document, provider, path, kind):
    """只登记请求体中明确的模型值；不猜测标题、响应字段或任意 URL。"""
    content = document['paths'][path]['post']['requestBody']['content']
    result = []
    for media in content.values():
        for schema in _schemas(document, media['schema']):
            prop = schema.get('properties', {}).get('model', {})
            identifiers = prop.get('enum')
            if not identifiers:
                identifiers = [prop.get('default'), prop.get('example')]
                examples = media.get('examples', {}).values()
                identifiers += [example.get('value', {}).get('model') for example in examples]
                identifiers += [schema.get('example', {}).get('model')]
            for identifier in identifiers:
                if identifier is None:
                    continue
                if not isinstance(identifier, str) or not MODEL_ID.fullmatch(identifier):
                    raise ValueError('官方模型标识无效')
                if any(marker in identifier.lower() for marker in ('preview', 'experimental', 'deprecated')):
                    continue
                capability = 'multimodal' if schema.get('_title') == '视觉模型' or (provider == 'minimax' and identifier.startswith('MiniMax-M3')) else kind
                result.append({'model': identifier, 'model_type': capability, 'base_url': ORIGINS[provider]+path})
    if not result:
        raise ValueError('未找到明确的稳定模型标识')
    return result


def discover(provider, fetch=fetch_document, *, cancelled=lambda:False):
    """每家供应商完整解析成功后才返回候选；某个来源失败不会截断覆盖目录。"""
    documents = {}; result = {}
    for url, path, kind in SOURCES[provider]:
        if cancelled():raise InterruptedError('发现已停止')
        if url not in documents:
            documents[url] = fetch(url)
        for model in parse_models(documents[url], provider, path, kind):
            result[(model['model'], model['base_url'])] = model
    return list(result.values())


def register_models(resources_dir, provider, candidates, *, data_dir=None):
    """仅新增公开模型分片；同供应商同一官方接口有唯一引用时才复用。"""
    directory = Path(resources_dir)/'models'
    existing = (registered_models(resources_dir,data_dir) if data_dir is not None
                else configured_models(directory/'catalog.json'))
    endpoints = []
    for model in existing:
        endpoint = model.get('base_url')
        if model.get('source') != 'model_catalog':
            try: endpoint = resolve_endpoint(endpoint, model.get('model_type'), model.get('protocol') or 'openai-chat')
            except (ValueError, TypeError): endpoint = None
        endpoints.append((model, endpoint))
    keys = {(m.get('provider_id'), m.get('model'), endpoint) for m, endpoint in endpoints}
    allowed = {ORIGINS[provider]+path for _, path, _ in SOURCES[provider]}
    additions = []
    for candidate in candidates:
        if candidate.get('base_url') not in allowed or not MODEL_ID.fullmatch(candidate.get('model', '')):
            raise ValueError('模型目标不在官方白名单')
        key = (provider, candidate['model'], candidate['base_url'])
        if key in keys:
            continue
        refs = {m.get('credential_id') for m, endpoint in endpoints if m.get('provider_id') == provider
                and endpoint == candidate['base_url'] and m.get('credential_id') and m.get('validation_status') != 'paused'}
        reference = next(iter(refs)) if len(refs) == 1 else None
        digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
        item = {'id': provider+'-'+digest, 'name': candidate['model'], 'service_name': NAMES[provider],
                **candidate, 'model_source': 'explicit', 'provider_id': provider, 'provider_name': NAMES[provider],
                'network_scope': 'external', 'credential_id': reference,
                'updated_at': datetime.now(timezone.utc).isoformat()}
        _model(item)
        additions.append(item); keys.add(key)
    if len(existing)+len(additions) > 1000:
        raise ValueError('模型目录已达到上限')
    directory.mkdir(parents=True, exist_ok=True)
    for item in additions:
        fd, temporary = tempfile.mkstemp(prefix='.discover-', dir=directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 1, 'model': item}, stream, ensure_ascii=False, indent=2)
                stream.flush(); os.fsync(stream.fileno())
            # 同名分片绝不覆盖；进程级刷新锁保证本工作台不会并发登记。
            os.link(temporary, directory/('discovered-'+item['id']+'.json'))
        finally:
            os.unlink(temporary)
    return len(additions)
