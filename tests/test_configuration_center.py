import unittest
from codex_workbench.configuration_center import configuration_entries

class ConfigurationCenterTests(unittest.TestCase):
 def test_model_credentials_are_excluded_without_mutating_sources(self):
  entries=[{'id':'v','label':'Model Key','folder_id':'ai','tags':[]}, {'id':'db','label':'Database','folder_id':'data','tags':['PostgreSQL','核心服务']}]
  result=configuration_entries(entries,[{'id':'m','name':'M','credential_id':'v'}])
  self.assertEqual(['db'],[x['id'] for x in result])
  self.assertEqual('postgresql',result[0]['service_type']);self.assertEqual('data',result[0]['folder_id'])
  self.assertEqual(2,len(entries));self.assertNotIn('service_type',entries[1])
 def test_no_name_guess_and_ambiguous_tags_do_not_select_random_type(self):
  result=configuration_entries([{'id':'a','label':'生产 Redis','tags':[]},{'id':'b','tags':['Redis','PostgreSQL']}],[])
  self.assertTrue(all(x['service_type'] is None for x in result))
 def test_models_without_credentials_never_become_configuration_cards(self):
  models=[{'id':'m1','name':'A','base_url':'https://example.invalid'}, {'id':'m2','name':'B','base_url':'https://example.invalid','credential_id':'missing'}]
  self.assertEqual([],configuration_entries([],models))
