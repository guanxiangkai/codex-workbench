"""首次官网采集的本机账户公开资料；严格绑定账户，不保存登录材料。"""
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import tempfile


def profile_identity(view, account):
    """资料必须绑定到已确认的同一账户，不能按显示名称复用。"""
    if view == 'accounts' and account.get('email'):
        return ['codex', account.get('id'), account.get('email')]
    if view == 'other_accounts' and all(account.get(key) for key in ('provider_id', 'id', 'usage_credential_id')):
        return ['provider', account['provider_id'], account['id'], account['usage_credential_id']]
    return None


def _valid_name(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 200 and '@' not in value


def _valid_avatar(value):
    return isinstance(value, str) and len(value) <= 1024 * 1024 and bool(
        re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+', value))


def import_profile(path, view, account, *, display_name=None, avatar_data_uri=None, account_identifier=None, observed_at=None):
    """保存用户在官方页面确认过的公开资料；不接受客户端声明的认证状态。"""
    identity = profile_identity(view, account)
    if identity is None:
        raise ValueError('账户身份尚未确认，不能导入资料')
    if display_name is not None and not _valid_name(display_name):
        raise ValueError('显示名称无效')
    if avatar_data_uri is not None and not _valid_avatar(avatar_data_uri):
        raise ValueError('头像必须是受支持的本地图片 data URI')
    if account_identifier is not None and (not isinstance(account_identifier, str) or not re.fullmatch(r'[A-Za-z0-9._@+-]{1,200}', account_identifier)):
        raise ValueError('官方账户标识无效')
    if display_name is None and avatar_data_uri is None:
        raise ValueError('至少提供一个公开资料字段')
    if observed_at is not None:
        try:
            parsed = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
        except (AttributeError, ValueError) as error:
            raise ValueError('资料确认时间无效') from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('资料确认时间必须带时区')
    path = Path(path)
    if path.is_symlink():
        raise ValueError('账户资料不能为符号链接')
    try:
        document = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'version': 1, 'profiles': []}
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError('账户资料存储不可用') from error
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('profiles'), list):
        raise ValueError('账户资料存储格式无效')
    previous = next((item for item in document['profiles'] if isinstance(item, dict)
                     and item.get('view') == view and item.get('identity') == identity), {})
    profiles = [item for item in document['profiles'] if isinstance(item, dict)
                and not (item.get('view') == view and item.get('identity') == identity)]
    profile = {'view': view, 'identity': identity, 'source': 'official_page',
               'observed_at': observed_at or datetime.now(timezone.utc).isoformat()}
    if display_name is not None:
        profile['display_name'] = display_name.strip()
    if avatar_data_uri is not None:
        profile['avatar_data_uri'] = avatar_data_uri
    if account_identifier is not None:
        profile['account_identifier'] = account_identifier
    for key in ('display_name', 'avatar_data_uri', 'account_identifier'):
        if key not in profile and key in previous:
            profile[key] = previous[key]
    profiles.append(profile)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.account-profile-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump({'version': 1, 'profiles': profiles}, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return profile


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
        identity = profile_identity(view, item)
        for profile in document['profiles']:
            if (not isinstance(profile, dict) or identity is None or profile.get('view') != view
                    or profile.get('identity') != identity or profile.get('source') != 'official_page'):
                continue
            name = profile.get('display_name')
            # 官网资料是明确确认过的显示快照；后台额度不能覆盖它。
            if _valid_name(name):
                item['display_name'] = name.strip()
                if view == 'accounts':
                    item.update(name=name.strip(), name_source='official_page')
            avatar = profile.get('avatar_data_uri')
            if _valid_avatar(avatar):
                item['avatar_data_uri'] = avatar
            if isinstance(profile.get('account_identifier'), str):
                item['account_identifier'] = profile['account_identifier']
            item['profile_observed_at'] = profile.get('observed_at')
            item['profile_status'] = {'source': 'official_page', 'name': 'available' if _valid_name(name) else 'missing',
                                      'avatar': 'available' if _valid_avatar(avatar) else 'missing'}
            break
        accounts.append(item)
    return {**value, 'accounts': accounts}
