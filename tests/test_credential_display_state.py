"""格式状态与字段映射独立，过期证据不可继续用来展示类型。"""
import json
import unittest
from codex_workbench.credentials import CredentialCatalog
from codex_workbench.credential_schema import probe_structure

class CredentialDisplayStateTests(unittest.TestCase):
    def saved(self,raw):
        return {'revision':1,'generation':2,'source':'probe','structure_json':json.dumps(probe_structure(raw))}
    def test_known_text_is_still_unmapped(self):
        value=CredentialCatalog._structure_view(self.saved(b'synthetic text'),1)
        self.assertEqual(('known','unmapped','unknown'),(value['format_status'],value['mapping_status'],value['kind']))
        self.assertEqual([],value['has_fields'])
    def test_stale_format_is_not_presented_as_current(self):
        value=CredentialCatalog._structure_view(self.saved(b'synthetic text'),2)
        self.assertEqual(('unavailable','stale'),(value['format_status'],value['mapping_status']))
    def test_partial_mapping_is_distinct_from_unknown_format(self):
        value=CredentialCatalog._structure_view(self.saved(b'{"account":"synthetic","extra":1}'),1)
        self.assertEqual(('known','partial'),(value['format_status'],value['mapping_status']))
