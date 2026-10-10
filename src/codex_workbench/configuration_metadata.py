"""读取 iCloud 的公开展示元数据；不恢复秘密结构，不写运行数据库。"""
import json
from pathlib import Path


DISPLAY_TYPES = {
    'account': '账户登录', 'api': '接口凭证', 'network': '网络配置',
    'host': '主机访问', 'archive': '导入资料', 'deployment': '部署配置',
    'security': '安全凭证', 'general': '通用配置',
}


def read_organization(path):
    """只投影登记的展示字段；历史 kind/has_fields 不构成密文结构证据。"""
    if path is None:
        return {'entries': {}, 'folders': []}
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(2_000_001)
    except FileNotFoundError:
        return {'entries': {}, 'folders': []}
    try:
        if len(raw) > 2_000_000:
            raise ValueError()
        source = json.loads(raw).get('credential_organization', {})
        entries, folders = source.get('entries', []), source.get('folders', [])
        if not isinstance(entries, list) or not isinstance(folders, list) or max(len(entries), len(folders)) > 5000:
            raise ValueError()
        result = {'entries': {}, 'folders': []}
        for item in entries:
            identifier = item['entry_id']
            if not isinstance(identifier, str) or not identifier or identifier in result['entries']:
                raise ValueError()
            value = {k: item[k] for k in ('label', 'description', 'folder_id', 'color', 'organization_source')
                     if isinstance(item.get(k), str)}
            tags = item.get('tags', [])
            if not isinstance(tags, list) or len(tags) > 12 or any(not isinstance(t, str) for t in tags):
                raise ValueError()
            value['tags_json'] = json.dumps(tags, ensure_ascii=False)
            if item.get('display_type') in DISPLAY_TYPES:
                value['display_type'] = item['display_type']
            result['entries'][identifier] = value
        seen = set()
        for item in folders:
            identifier, name = item['id'], item['name']
            if not isinstance(identifier, str) or not identifier or identifier in seen or not isinstance(name, str):
                raise ValueError()
            seen.add(identifier)
            result['folders'].append({'id': identifier, 'name': name,
                                      **{k: item.get(k) for k in ('parent_id', 'color')
                                         if item.get(k) is None or isinstance(item.get(k), str)}})
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('配置展示信息格式无效，请检查工作台配置') from None
