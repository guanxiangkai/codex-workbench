"""验证逐轮归属、累计值不重复计数、追加读取与身份边界。"""
import json
from pathlib import Path
import tempfile
import unittest
from codex_workbench.turn_metadata import TurnMetadata

class TurnMetadataTests(unittest.TestCase):
    def test_incremental_usage_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);(home/'sessions').mkdir();p=home/'sessions'/'test.jsonl'
            def append(kind,payload):
                with p.open('a') as f:f.write(json.dumps({'type':kind,'payload':payload})+'\n')
            append('session_meta',{'id':'thread'})
            append('turn_context',{'turn_id':'one','model':'model-a','effort':'high'})
            append('token_usage_record',{'thread_id':'thread','turn_id':'one','turn_token_usage':{'total_tokens':12},'thread_token_usage':{'total_tokens':9000}})
            reader=TurnMetadata(home);first=reader.read(p,'thread')
            self.assertEqual(12,first['one']['total_tokens']);self.assertEqual('model-a',first['one']['model']);self.assertNotIn('account',first['one'])
            self.assertEqual(first,reader.read(p,'thread'))
            append('token_usage_record',{'thread_id':'thread','turn_id':'one','turn_token_usage':{'total_tokens':20}})
            append('turn_context',{'turn_id':'two','model':'model-b','effort':'low'})
            append('token_usage_record',{'thread_id':'wrong','turn_id':'two','turn_token_usage':{'total_tokens':800}})
            result=reader.read(p,'thread');self.assertEqual(20,result['one']['total_tokens']);self.assertEqual('model-b',result['two']['model']);self.assertNotIn('total_tokens',result['two'])
            self.assertEqual({},reader.read(p,'wrong'))
            outside=home/'outside.jsonl';outside.write_text(p.read_text());self.assertEqual({},reader.read(outside,'thread'))
    def test_partial_line_and_replaced_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);(home/'sessions').mkdir();p=home/'sessions'/'test.jsonl'
            p.write_text(json.dumps({'type':'session_meta','payload':{'id':'thread'}})+'\n')
            event=json.dumps({'type':'turn_context','payload':{'turn_id':'one','model':'a'}})
            with p.open('a') as f:f.write(event[:20])
            reader=TurnMetadata(home);self.assertEqual({},reader.read(p,'thread'))
            with p.open('a') as f:f.write(event[20:]+'\n')
            self.assertEqual('a',reader.read(p,'thread')['one']['model'])
            p.write_text(json.dumps({'type':'session_meta','payload':{'id':'other'}})+'\n');self.assertEqual({},reader.read(p,'thread'))
