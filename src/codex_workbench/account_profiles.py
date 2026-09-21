"""首次官网采集的本机账户公开资料；严格绑定账户，不保存登录材料。"""
import json
import re
from pathlib import Path


def apply_profiles(value, view, path):
    if view not in ('accounts', 'other_accounts'):
        return value
    try:
        path = Path(path)
        if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            return value
        document = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('profiles'), list):
            return value
    except (OSError, ValueError):
        return value
    accounts = []
    for account in value.get('accounts', []):
        item = dict(account)
        identity = (['codex', item.get('id'), item.get('email')] if view == 'accounts' and item.get('email')
                    else ['provider', item.get('provider_id'), item.get('id'), item.get('usage_credential_id')] if view == 'other_accounts' else None)
        for profile in document['profiles']:
            if not isinstance(profile, dict) or identity is None or profile.get('view') != view or profile.get('identity') != identity:
                continue
            name = profile.get('display_name')
            # 实时官方值优先，首次页面资料用于填补缺失。
            current = item.get('display_name') or (item.get('name') if item.get('name_source') == 'official' else None)
            if not current and isinstance(name, str) and 0 < len(name.strip()) <= 200 and '@' not in name:
                item['display_name'] = name.strip()
                if view == 'accounts':
                    item.update(name=name.strip(), name_source='official_page')
            avatar = profile.get('avatar_data_uri')
            if not item.get('avatar_data_uri') and isinstance(avatar, str) and len(avatar) <= 1024 * 1024 and re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+', avatar):
                item['avatar_data_uri'] = avatar
            item['profile_observed_at'] = profile.get('observed_at')
            break
        accounts.append(item)
    return {**value, 'accounts': accounts}
