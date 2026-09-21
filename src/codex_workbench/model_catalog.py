"""由 Codex 维护的公开模型配置；密钥仅引用保险库，不在目录里保存。"""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from .model_contracts import PROFILES

FIELDS={'id','name','service_name','model_type','base_url','model','model_source','credential_id','updated_at','network_scope','api_profile','provider_id','provider_name'}
TYPES={'image_generation','video_generation','reasoning','multimodal','speech_to_text','text_to_speech','embedding','rerank'}
PROVIDER_ID=re.compile(r'[a-z][a-z0-9_-]{0,127}\Z')

def configured_models(path):
    """读取模型目录及其单模型分片，拒绝秘密字段和重复标识。"""
    path=Path(path)
    directory=path.parent
    items=[]
    if path.exists():
        items.extend(_legacy_models(path))
    if directory.exists():
        for fragment in sorted(directory.glob('*.json')):
            if fragment==path:continue
            items.append(_fragment_model(fragment))
    if len(items)>1000:raise ValueError('模型目录格式无效')
    result=[];seen=set()
    for item in items:
        model=_model(item)
        if model['id'] in seen:raise ValueError('模型标识或类型无效')
        seen.add(model['id']);result.append(model)
    return result


def _load(path, message):
    if path.stat().st_size>1024*1024:raise ValueError(message+'超过大小限制')
    try:return json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:raise ValueError(message+'不是有效 JSON') from error


def _legacy_models(path):
    data=_load(path,'模型目录')
    if not isinstance(data,dict) or set(data)!={'version','models'} or data['version']!=1 or not isinstance(data['models'],list):raise ValueError('模型目录格式无效')
    return data['models']


def _fragment_model(path):
    data=_load(path,'模型分片')
    if not isinstance(data,dict) or set(data)!={'version','model'} or data['version']!=1:raise ValueError('模型分片格式无效')
    return data['model']


def _model(item):
    if not isinstance(item,dict) or set(item)-FIELDS:raise ValueError('模型配置包含未声明字段')
    if any(not isinstance(item.get(key),str) or not item[key].strip() or len(item[key])>512 for key in ('id','name','service_name','model_type','base_url','updated_at')):raise ValueError('模型配置不完整')
    if item.get('credential_id') is not None and (not isinstance(item['credential_id'],str) or not item['credential_id'].strip() or len(item['credential_id'])>512):raise ValueError('模型凭据引用无效')
    if item['model_type'] not in TYPES:raise ValueError('模型标识或类型无效')
    if item.get('model_source') not in ('explicit','server_default'):raise ValueError('模型来源未声明')
    if item['model_source']=='explicit' and (not isinstance(item.get('model'),str) or not item['model'].strip()):raise ValueError('模型名称未配置')
    if item['model_source']=='server_default' and item.get('model') not in (None,''):raise ValueError('默认模型不能同时声明模型名称')
    provider_id,provider_name=item.get('provider_id'),item.get('provider_name')
    if (provider_id is None)!=(provider_name is None):raise ValueError('模型供应商信息必须同时登记')
    if provider_id is not None and (not isinstance(provider_id,str) or not PROVIDER_ID.fullmatch(provider_id) or not isinstance(provider_name,str) or not provider_name.strip() or len(provider_name)>512):raise ValueError('模型供应商信息无效')
    if item.get('network_scope') not in (None,'internal','external'):raise ValueError('模型网络范围无效')
    if item.get('api_profile') is not None and (not isinstance(item['api_profile'],str) or item['api_profile'] not in PROFILES):raise ValueError('模型接口协议无效')
    endpoint=urlsplit(item['base_url'])
    if endpoint.scheme not in ('http','https') or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:raise ValueError('模型地址不能包含认证信息')
    if item.get('api_profile') and endpoint.path.rstrip('/')!=PROFILES[item['api_profile']]['path']:raise ValueError('模型协议与接口路径不匹配')
    return {**item,'model':item.get('model') or '', 'source':'model_catalog','validation_status':'pending','last_checked_at':None,**({'configuration_status':'missing_credential'} if not item.get('credential_id') else {})}
