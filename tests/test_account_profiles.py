import json
import tempfile
import unittest
from pathlib import Path
from codex_workbench.account_profiles import apply_profiles, import_profile


class ProfileTests(unittest.TestCase):
    def test_identity_binding_and_source_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            path.write_text(json.dumps({'version': 1, 'profiles': [{'view': 'other_accounts', 'identity': ['provider', 'bigmodel', 'main', 'key-a'], 'source':'official_page', 'display_name': 'Official Name', 'avatar_data_uri': 'data:image/jpeg;base64,YQ=='}]}))
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
                                     avatar_data_uri='data:image/png;base64,YQ==')
            self.assertEqual('official_page', profile['source'])
            self.assertEqual('Official Name', apply_profiles({'accounts':[account]}, 'other_accounts', path)['accounts'][0]['display_name'])
            with self.assertRaises(ValueError):
                import_profile(path, 'other_accounts', {'id':'main','provider_id':'bigmodel'}, display_name='No binding')

    def test_partial_manual_update_preserves_other_confirmed_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profiles.json'
            account={'id':'current','email':'a@example.invalid'}
            import_profile(path,'accounts',account,display_name='Alice',avatar_data_uri='data:image/png;base64,YQ==',account_identifier='subject-123')
            import_profile(path,'accounts',account,display_name='Alice New')
            profile=apply_profiles({'accounts':[account]},'accounts',path)['accounts'][0]
            self.assertEqual('Alice New',profile['display_name'])
            self.assertEqual('subject-123',profile['account_identifier'])
            self.assertEqual('data:image/png;base64,YQ==',profile['avatar_data_uri'])
            self.assertEqual(0o600,path.stat().st_mode & 0o777)
