"""不同服务的账户字段必须对应，不把 S3 双密钥写成密码或通用 Key。"""
import json,unittest
from codex_workbench.configuration_schema import configuration_schema
from codex_workbench.credential_schema import PAYLOAD_FIELDS,CredentialSchemaError,normalize_payload,build_payload_index,probe_structure

class ConfigurationSchemaTests(unittest.TestCase):
 def test_templates_only_reference_supported_fields_and_return_independent_copies(self):
  schema=configuration_schema();self.assertLessEqual(set(schema['fields']),PAYLOAD_FIELDS)
  for kind in schema['types']:
   for section in kind['sections']:
    self.assertLessEqual(set(section['fields']),set(schema['fields']))
  schema['types'].clear();self.assertGreater(len(configuration_schema()['types']),0)
 def test_database_nacos_s3_and_dify_fields_are_distinct(self):
  kinds={k['id']:{f for section in k['sections'] for f in section['fields']} for k in configuration_schema()['types']}
  self.assertTrue({'database_type','database','host','port','account','password'}<=kinds['database'])
  self.assertTrue({'endpoint','namespace','group','account','password'}<=kinds['nacos'])
  self.assertTrue({'endpoint','access_key','secret_key','bucket','region'}<=kinds['s3']);self.assertNotIn('password',kinds['s3']);self.assertNotIn('key',kinds['s3'])
  self.assertTrue({'endpoint','app_id','account','password','key'}<=kinds['dify'])
 def test_values_and_port_type_are_preserved_without_index_leakage(self):
  cases=[{'database_type':'PostgreSQL','database':'fixture-db','host':'db.example.invalid','port':5432,'account':'fixture-user','password':'synthetic-password'}, {'endpoint':'https://nacos.example.invalid','namespace':'fixture-ns','group':'fixture-group','account':'fixture-user','password':'synthetic-password'}, {'endpoint':'https://s3.example.invalid','access_key':'synthetic-access','secret_key':'synthetic-secret','bucket':'fixture-bucket','region':'fixture-region'}, {'endpoint':'https://dify.example.invalid','app_id':'fixture-app','account':'fixture-user','key':'synthetic-dify-key'}]
  for value in cases:
   self.assertEqual(value,normalize_payload(value));index=build_payload_index(value);self.assertEqual(sorted(value),index['has_fields'])
   self.assertNotIn('synthetic-',json.dumps(index));self.assertNotIn('example.invalid',json.dumps(index))
  self.assertEqual('credential',build_payload_index(cases[2])['kind'])
 def test_known_s3_aliases_preserve_two_fields(self):
  index=probe_structure(json.dumps({'AWS_ACCESS_KEY_ID':'synthetic-access','AWS_SECRET_ACCESS_KEY':'synthetic-secret'}).encode())
  self.assertEqual(['access_key','secret_key'],index['has_fields']);self.assertEqual('credential',index['kind'])

 def test_common_services_are_grouped_once_and_redis_has_dedicated_fields(self):
  schema=configuration_schema();types={t['id']:t for t in schema['types']};grouped=[item for group in schema['groups'] for item in group['types']]
  self.assertEqual(len(grouped),len(set(grouped)));self.assertEqual(set(types),set(grouped))
  self.assertTrue({'redis','mongodb','kafka','rabbitmq','rocketmq','search','ssh','registry','kubernetes','gitlab','jenkins','smtp'}<=set(types))
  redis={f for section in types['redis']['sections'] for f in section['fields']}
  self.assertTrue({'database_index','connection_mode','sentinel_master','account','password'}<=redis)
  self.assertNotIn('database_type',redis)
  self.assertNotIn('Redis',schema['fields']['database_type']['options'])
  self.assertTrue(schema['fields']['connection_uri']['sensitive']);self.assertTrue(schema['fields']['kubeconfig']['sensitive']);self.assertTrue(schema['fields']['extra_config']['sensitive'])
 def test_redis_index_preserves_integer_zero_and_rejects_wrong_types(self):
  value={'host':'redis.example.invalid','database_index':0,'account':'fixture-user','password':'synthetic-password'}
  self.assertEqual(value,normalize_payload(value));index=build_payload_index(value)
  self.assertEqual('integer',index['field_types']['database_index']);self.assertNotIn('synthetic-password',json.dumps(index))
  for invalid in [-1,True,'0',1.5,2147483648]:
   with self.assertRaises(CredentialSchemaError):normalize_payload({'database_index':invalid})
 def test_multiline_and_extensible_configuration_remain_encrypted_payload_fields(self):
  value={'brokers':'one.example.invalid:9092\ntwo.example.invalid:9092','extra_config':'CUSTOM_SECRET=synthetic-only\nCUSTOM_FLAG=enabled'}
  self.assertEqual(value,normalize_payload(value));index=build_payload_index(value)
  self.assertEqual(['brokers','extra_config'],index['has_fields']);self.assertNotIn('CUSTOM_SECRET',json.dumps(index));self.assertNotIn('synthetic-only',json.dumps(index))
