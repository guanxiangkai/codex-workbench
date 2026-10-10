import json
import base64
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codex_workbench.account_profiles import apply_profiles, import_profile, _valid_avatar

AVATAR = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='


class ProfileTests(unittest.TestCase):
    def test_identity_binding_and_source_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            path.write_text(json.dumps({'version': 1, 'profiles': [{'view': 'other_accounts', 'identity': ['provider', 'bigmodel', 'main', 'key-a'], 'source':'official_page', 'display_name': 'Official Name', 'avatar_data_uri': AVATAR}]}))
            account = {'id': 'main', 'provider_id': 'bigmodel', 'usage_credential_id': 'key-a'}
            result = apply_profiles({'accounts': [account]}, 'other_accounts', path)['accounts'][0]
            self.assertEqual(result['display_name'], 'Official Name')
            self.assertIn('avatar_data_uri', result)
            self.assertNotIn('display_name', apply_profiles({'accounts': [{**account, 'usage_credential_id': 'key-b'}]}, 'other_accounts', path)['accounts'][0])
            self.assertEqual(apply_profiles({'accounts': [{**account, 'display_name': 'New Official'}]}, 'other_accounts', path)['accounts'][0]['display_name'], 'Official Name')

    def test_missing_file_keeps_existing(self):
        value = {'accounts': [{'display_name': 'Existing'}]}
        self.assertEqual(apply_profiles(value, 'accounts', Path('/nonexistent/profile')), value)

    def test_import_requires_bound_identity_and_keeps_avatar_local(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            account = {'id':'main','provider_id':'bigmodel','usage_credential_id':'key-a'}
            profile = import_profile(path, 'other_accounts', account, display_name='Official Name',
                                     avatar_data_uri=AVATAR)
            self.assertEqual('official_page', profile['source'])
            self.assertEqual('Official Name', apply_profiles({'accounts':[account]}, 'other_accounts', path)['accounts'][0]['display_name'])
            with self.assertRaises(ValueError):
                import_profile(path, 'other_accounts', {'id':'main','provider_id':'bigmodel'}, display_name='No binding')

    def test_partial_manual_update_preserves_other_confirmed_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profiles.json'
            account={'id':'current','email':'a@example.invalid'}
            import_profile(path,'accounts',account,display_name='Alice',avatar_data_uri=AVATAR,account_identifier='subject-123')
            import_profile(path,'accounts',account,display_name='Alice New')
            profile=apply_profiles({'accounts':[account]},'accounts',path)['accounts'][0]
            self.assertEqual('Alice New',profile['display_name'])
            self.assertEqual('subject-123',profile['account_identifier'])
            self.assertEqual(AVATAR,profile['avatar_data_uri'])
            self.assertEqual(0o600,path.stat().st_mode & 0o777)

    def test_explicitly_missing_field_clears_but_omitted_field_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            account = {'id': 'current', 'email': 'a@example.invalid'}
            import_profile(path, 'accounts', account, display_name='Alice', avatar_data_uri=AVATAR,
                           account_identifier='subject-123')
            import_profile(path, 'accounts', account, missing_fields=['display_name', 'avatar_data_uri'])
            profile = apply_profiles({'accounts': [{**account, 'display_name': 'Old profile', 'name': 'Old native name',
                                                    'name_source': 'official', 'avatar_data_uri': AVATAR}]}, 'accounts', path)['accounts'][0]
            self.assertNotIn('display_name', profile)
            self.assertNotIn('name', profile)
            self.assertNotIn('name_source', profile)
            self.assertNotIn('avatar_data_uri', profile)
            self.assertEqual('subject-123', profile['account_identifier'])
            import_profile(path, 'accounts', account, account_identifier='subject-456')
            preserved = apply_profiles({'accounts': [{**account, 'display_name': 'Old profile', 'name': 'Old native name',
                                                      'name_source': 'official', 'avatar_data_uri': AVATAR}]}, 'accounts', path)['accounts'][0]
            self.assertEqual('subject-456', preserved['account_identifier'])
            self.assertNotIn('display_name', preserved)
            self.assertNotIn('name', preserved)
            self.assertNotIn('avatar_data_uri', preserved)
            with self.assertRaises(ValueError):
                import_profile(path, 'accounts', account, display_name='Alice', missing_fields=['display_name'])

    def test_invalid_base64_and_image_containers_are_rejected(self):
        self.assertTrue(_valid_avatar(AVATAR))
        for value in ('====', 'a===b', 'YQ==', base64.b64encode(b'\x89PNG\r\n\x1a\nnot an image').decode()):
            self.assertFalse(_valid_avatar('data:image/png;base64,' + value))
        self.assertFalse(_valid_avatar(AVATAR.replace('image/png', 'image/jpeg')))

    def test_identifier_only_observation_records_missing_display_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            account = {'id': 'one', 'email': 'one@example.invalid'}
            import_profile(path, 'accounts', account, account_identifier='official-one')
            result = apply_profiles({'accounts': [account]}, 'accounts', path)['accounts'][0]
            self.assertEqual('official-one', result['account_identifier'])
            self.assertEqual({'source': 'official_page', 'name': 'missing', 'avatar': 'missing'}, result['profile_status'])
            with self.assertRaises(ValueError):
                import_profile(path, 'accounts', account)

    def test_aggregate_limit_rejects_without_losing_existing_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            import_profile(path, 'accounts', {'id':'one','email':'one@example.invalid'}, display_name='One')
            before = path.read_bytes()
            with patch('codex_workbench.account_profiles.MAX_PROFILE_BYTES', len(before) + 1):
                with self.assertRaises(ValueError):
                    import_profile(path, 'accounts', {'id':'two','email':'two@example.invalid'}, display_name='Two')
                self.assertEqual(before, path.read_bytes())
                self.assertEqual('One', apply_profiles({'accounts':[{'id':'one','email':'one@example.invalid'}]}, 'accounts', path)['accounts'][0]['display_name'])

    def test_concurrent_imports_preserve_every_account(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            def save(index):
                return import_profile(path, 'accounts', {'id':str(index),'email':f'{index}@example.invalid'}, display_name=f'Account {index}')
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(save, range(32)))
            self.assertEqual(32, len(json.loads(path.read_bytes())['profiles']))
