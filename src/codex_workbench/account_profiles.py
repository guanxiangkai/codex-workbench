"""首次官网采集的本机账户公开资料；严格绑定账户，不保存登录材料。"""
from datetime import datetime, timezone
import base64
import binascii
import json
import os
import re
from pathlib import Path
import tempfile
import threading
import zlib

MAX_PROFILE_BYTES = 4 * 1024 * 1024
_PROFILE_LOCK = threading.RLock()
_PROFILE_FIELDS = ('display_name', 'avatar_data_uri', 'account_identifier')
_SUBJECT_BINDING = re.compile(r'[0-9a-f]{64}')


def _verified_subject_binding(account):
    """仅接收后台注入的单向主体摘要，绝不把该内部字段投影到页面。"""
    value = account.get('_verified_subject_binding')
    return value if isinstance(value, str) and _SUBJECT_BINDING.fullmatch(value) else None


def profile_identity(view, account):
    """资料必须绑定到已确认的同一账户，不能按显示名称复用。"""
    if view == 'accounts' and account.get('email'):
        # ``current`` 跟随原生登录态切换，必须同时带上已核验主体摘要；其他
        # execution_accounts 的 id 已由登记主体固定，保留既有 identity 使已
        # 确认资料不因这次隔离升级而失配。
        if account.get('id') == 'current':
            binding = _verified_subject_binding(account)
            return ['codex', 'current', account['email'], binding] if binding else None
        return ['codex', account.get('id'), account['email']]
    if view == 'other_accounts' and all(account.get(key) for key in ('provider_id', 'id', 'usage_credential_id')):
        return ['provider', account['provider_id'], account['id'], account['usage_credential_id']]
    return None


def _valid_name(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 200 and '@' not in value


def _valid_avatar(value):
    if not isinstance(value, str) or len(value) > 1024 * 1024:
        return False
    match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})', value)
    if not match:
        return False
    try:
        data = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error):
        return False
    if match[1] == 'png':
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            return False
        offset, kinds = 8, []
        while offset + 12 <= len(data):
            size = int.from_bytes(data[offset:offset + 4], 'big')
            kind = data[offset + 4:offset + 8]
            end = offset + 12 + size
            if end > len(data) or zlib.crc32(data[offset + 4:end - 4]) != int.from_bytes(data[end - 4:end], 'big'):
                return False
            if not kinds and (kind != b'IHDR' or size != 13 or not all(int.from_bytes(data[p:p + 4], 'big') for p in (offset + 8, offset + 12))):
                return False
            kinds.append(kind)
            offset = end
            if kind == b'IEND':
                return size == 0 and offset == len(data) and b'IDAT' in kinds
        return False
    if match[1] == 'jpeg':
        if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
            return False
        offset, frame = 2, False
        while offset + 4 <= len(data):
            if data[offset] != 255:
                return False
            marker = data[offset + 1]
            size = int.from_bytes(data[offset + 2:offset + 4], 'big')
            if size < 2 or offset + 2 + size > len(data):
                return False
            if marker in (0xc0, 0xc1, 0xc2):
                frame = size >= 8 and all(int.from_bytes(data[p:p + 2], 'big') for p in (offset + 5, offset + 7))
            if marker == 0xda:
                return frame and offset + 2 + size < len(data) - 2
            offset += 2 + size
        return False
    return (len(data) >= 30 and data[:4] == b'RIFF' and data[8:12] == b'WEBP'
            and int.from_bytes(data[4:8], 'little') + 8 == len(data)
            and data[12:16] in (b'VP8 ', b'VP8L', b'VP8X')
            and 0 < int.from_bytes(data[16:20], 'little') <= len(data) - 20)


def import_profile(path, view, account, *, display_name=None, avatar_data_uri=None, account_identifier=None,
                   missing_fields=None, observed_at=None):
    """保存用户在官方页面确认过的公开资料；``missing_fields`` 表示已确认不存在并清除旧值。"""
    with _PROFILE_LOCK:
        return _import_profile(path, view, account, display_name=display_name, avatar_data_uri=avatar_data_uri,
                               account_identifier=account_identifier, missing_fields=missing_fields, observed_at=observed_at)


def _import_profile(path, view, account, *, display_name=None, avatar_data_uri=None, account_identifier=None,
                    missing_fields=None, observed_at=None):
    identity = profile_identity(view, account)
    if identity is None:
        raise ValueError('账户身份尚未确认，不能导入资料')
    if display_name is not None and not _valid_name(display_name):
        raise ValueError('显示名称无效')
    if avatar_data_uri is not None and not _valid_avatar(avatar_data_uri):
        raise ValueError('头像必须是受支持的本地图片 data URI')
    if account_identifier is not None and (not isinstance(account_identifier, str) or not re.fullmatch(r'[A-Za-z0-9._@+-]{1,200}', account_identifier)):
        raise ValueError('官方账户标识无效')
    if missing_fields is None:
        missing = set()
    elif isinstance(missing_fields, list) and all(isinstance(field, str) for field in missing_fields):
        missing = set(missing_fields)
        if not missing <= set(_PROFILE_FIELDS):
            raise ValueError('资料缺失字段无效')
    else:
        raise ValueError('资料缺失字段无效')
    provided = {'display_name': display_name, 'avatar_data_uri': avatar_data_uri, 'account_identifier': account_identifier}
    if any(provided[field] is not None for field in missing):
        raise ValueError('资料字段不能同时存在和缺失')
    if all(value is None for value in provided.values()) and not missing:
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
    if path.exists() and path.stat().st_size > MAX_PROFILE_BYTES:
        raise ValueError('账户资料超过存储上限')
    try:
        document = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'version': 1, 'profiles': []}
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError('账户资料存储不可用') from error
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('profiles'), list):
        raise ValueError('账户资料存储格式无效')
    def matching(item):
        if not isinstance(item, dict) or item.get('view') != view:
            return False
        if item.get('identity') == identity:
            return True
        # 仅当旧资料自己已经保存过相同的内部主体摘要时，才允许从旧
        # 三段 Codex identity 迁移；同邮箱本身不是主体证明。
        legacy = identity[:3] if view == 'accounts' and len(identity) == 4 else None
        return item.get('identity') == legacy and item.get('verified_subject_binding') == identity[3]
    previous = next((item for item in document['profiles'] if matching(item)), {})
    profiles = [item for item in document['profiles'] if isinstance(item, dict)
                and not matching(item)]
    previous_missing = previous.get('missing_fields') if isinstance(previous.get('missing_fields'), list) else []
    # 官方页面明确不存在的结论是持久资料的一部分；后续只更新另一个字段
    # 不能让原始 RPC 值再次回退显示。只有本次获得该字段的新确认值才解除。
    missing = (set(field for field in previous_missing if field in _PROFILE_FIELDS) | missing) - {
        field for field, value in provided.items() if value is not None
    }
    profile = {'view': view, 'identity': identity, 'source': 'official_page',
               'observed_at': observed_at or datetime.now(timezone.utc).isoformat()}
    if missing:
        profile['missing_fields'] = sorted(missing)
    if display_name is not None:
        profile['display_name'] = display_name.strip()
    if avatar_data_uri is not None:
        profile['avatar_data_uri'] = avatar_data_uri
    if account_identifier is not None:
        profile['account_identifier'] = account_identifier
    for key in _PROFILE_FIELDS:
        if key not in profile and key not in missing and key in previous:
            profile[key] = previous[key]
    profiles.append(profile)
    payload = json.dumps({'version': 1, 'profiles': profiles}, ensure_ascii=False).encode('utf-8')
    if len(payload) > MAX_PROFILE_BYTES:
        raise ValueError('账户资料超过存储上限，请缩小头像后重试')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.account-profile-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(payload)
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
    clean={**value,'accounts':[{key:item for key,item in account.items() if key != '_verified_subject_binding'} for account in value.get('accounts',[])]}
    try:
        path = Path(path)
        if path.is_symlink() or path.stat().st_size > MAX_PROFILE_BYTES:
            return clean
        document = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('profiles'), list):
            return clean
    except (OSError, ValueError):
        return clean
    accounts = []
    for account in value.get('accounts', []):
        item = dict(account)
        identity = profile_identity(view, item)
        item.pop('_verified_subject_binding', None)
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
            missing = profile.get('missing_fields')
            if isinstance(missing, list):
                for field in missing:
                    if field in _PROFILE_FIELDS:
                        item.pop(field, None)
                        if view == 'accounts' and field == 'display_name':
                            # ``name`` 是原生账户昵称的投影，不能绕开资料页
                            # 对显示名称“明确不存在”的确认。
                            item.pop('name', None)
                            item.pop('name_source', None)
            item['profile_observed_at'] = profile.get('observed_at')
            item['profile_status'] = {'source': 'official_page', 'name': 'available' if _valid_name(name) else 'missing',
                                      'avatar': 'available' if _valid_avatar(avatar) else 'missing'}
            break
        accounts.append(item)
    return {**value, 'accounts': accounts}
