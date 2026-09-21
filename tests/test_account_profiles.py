import json
import tempfile
import unittest
from pathlib import Path
from codex_workbench.account_profiles import apply_profiles


class ProfileTests(unittest.TestCase):
    def test_identity_binding_and_source_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            path.write_text(json.dumps({'version': 1, 'profiles': [{'view': 'other_accounts', 'identity': ['provider', 'bigmodel', 'main', 'key-a'], 'display_name': 'Official Name', 'avatar_data_uri': 'data:image/jpeg;base64,YQ=='}]}))
            account = {'id': 'main', 'provider_id': 'bigmodel', 'usage_credential_id': 'key-a'}
            result = apply_profiles({'accounts': [account]}, 'other_accounts', path)['accounts'][0]
            self.assertEqual(result['display_name'], 'Official Name')
            self.assertIn('avatar_data_uri', result)
            self.assertNotIn('display_name', apply_profiles({'accounts': [{**account, 'usage_credential_id': 'key-b'}]}, 'other_accounts', path)['accounts'][0])
            self.assertEqual(apply_profiles({'accounts': [{**account, 'display_name': 'New Official'}]}, 'other_accounts', path)['accounts'][0]['display_name'], 'New Official')

    def test_missing_file_keeps_existing(self):
        value = {'accounts': [{'display_name': 'Existing'}]}
        self.assertEqual(apply_profiles(value, 'accounts', Path('/nonexistent/profile')), value)
