"""Codex 账户低频资料与高频额度分片；只保存公开字段和登记绑定。"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

PROFILE = ('email', 'plan', 'name', 'name_source', 'profile_observed_at')
USAGE = ('remaining_percent', 'resets_at', 'reset_cards', 'observed_at')
ANALYSIS = ('status', 'confidence', 'predicted_reset_at', 'reset_card_likelihood',
            'evidence', 'observed_at', 'stale', 'error', 'model', 'scope',
            'confidence_kind', 'primary_verified', 'event_type', 'announced_reset_at',
            'last_manual_reset_at', 'summary', 'source_ids', 'coverage_incomplete',
            'history', 'history_summary', 'signal', 'predicted_reset_window',
            'confidence_breakdown', 'prediction_sources', 'prediction_candidates',
            'last_success_at', 'last_attempt_at')


class AccountSnapshot:
    def __init__(self, directory):
        self.directory = Path(directory)

    @staticmethod
    def binding(account):
        return {k: account.get(k) for k in ('id', 'codex_home', 'subject_id', 'expected_email')}

    def path(self, account, part):
        key = hashlib.sha256(str(account['id']).encode()).hexdigest()
        return self.directory / 'account-snapshots' / key / (part + '.json')

    @staticmethod
    def read(path):
        try:
            if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
                return {}
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, account, part):
        value = self.read(self.path(account, part))
        return value.get('data', {}) if value.get('binding') == self.binding(account) else {}

    def write(self, account, part, data):
        path = self.path(account, part)
        if path.parent.is_symlink() or path.parent.parent.is_symlink() or path.is_symlink():
            raise ValueError('账户快照不能为符号链接')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {'version': 1, 'binding': self.binding(account), 'data': data}
        if self.read(path) == payload:
            return
        fd, temporary = tempfile.mkstemp(prefix='.account-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(payload, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def migrate(self, account):
        """首次从已确认邮箱对应的历史公开快照恢复；不迁移登录状态。"""
        if self.path(account, 'profile').exists() or not account.get('subject_id'):
            return
        profiles = self.read(self.directory / 'account-profiles.json').get('profiles', [])
        emails = {p['identity'][2] for p in profiles if isinstance(p, dict)
                  and p.get('view') == 'accounts' and isinstance(p.get('identity'), list)
                  and len(p['identity']) == 3 and p['identity'][:2] == ['codex', account['id']]}
        if account.get('expected_email'):
            emails = {account['expected_email']}
        if not emails:
            return
        contexts = self.read(self.directory / 'snapshots.json').get('contexts', {})
        entries = [v.get('accounts', {}) for v in contexts.values() if isinstance(v, dict)]
        for entry in sorted(entries, key=lambda e: e.get('updated_at', ''), reverse=True):
            for item in entry.get('data', {}).get('accounts', []):
                if item.get('id') == account['id'] and item.get('email') in emails and item.get('login_status') == 'ready':
                    self.save_profile(account, item, observed_at=item.get('profile_observed_at') or item.get('observed_at'))
                    self.save_usage(account, item)
                    return

    def cached(self, account):
        profile = self.get(account, 'profile')
        if profile.get('invalidated'):
            return {}
        usage = self.get(account, 'usage')
        analysis = self.get(account, 'analysis')
        result = {**{k: v for k, v in profile.items() if k in PROFILE},
                  **{k: v for k, v in usage.items() if k in USAGE}}
        if analysis:
            result['reset_analysis'] = analysis
        return result

    def fresh_identity(self, account, seconds):
        if not account.get('subject_id'):
            return None
        profile = self.get(account, 'profile')
        try:
            fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(profile['checked_at'])).total_seconds() < seconds
        except (KeyError, ValueError, TypeError):
            fresh = False
        if fresh and profile.get('email') and not profile.get('invalidated'):
            return {'type': 'chatgpt', 'email': profile['email'], 'planType': profile.get('plan'),
                    'name': profile.get('name') if profile.get('name_source') == 'official' else None}
        return None

    def save_profile(self, account, item, observed_at=None):
        now = datetime.now(timezone.utc).isoformat()
        data = self.get(account, 'profile')
        data = {k: v for k, v in data.items() if k in PROFILE}
        for key in PROFILE:
            if item.get(key) is not None and not (key in ('name', 'name_source') and item.get('name_source') == 'unavailable'):
                data[key] = item[key]
        data['checked_at'] = observed_at or now
        self.write(account, 'profile', data)

    def save_usage(self, account, item):
        # None 表示该字段此次未取得，0 是有效的新额度。
        data = self.get(account, 'usage')
        data.update({k: item[k] for k in USAGE if item.get(k) is not None})
        self.write(account, 'usage', data)

    def save_analysis(self, account, analysis):
        """保存独立预测；分析失败时调用方可保留旧值而不覆盖官方用量。"""
        if not isinstance(analysis, dict):
            return
        allowed = {key: analysis[key] for key in ANALYSIS if key in analysis}
        if allowed:
            self.write(account, 'analysis', allowed)

    def invalidate(self, account):
        self.write(account, 'profile', {'invalidated': True})
        self.write(account, 'usage', {})
        self.write(account, 'analysis', {})
