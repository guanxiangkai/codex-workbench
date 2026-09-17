"""跨会话列表的覆盖、边界、续页与筛选契约。"""
import unittest
from codex_workbench.execution_pages import ExecutionPages

class ExecutionPageTests(unittest.TestCase):
    def setUp(self):
        self.now=0;self.reader=ExecutionPages(lambda:self.now)
        self.sessions=[{'id':str(i)} for i in range(6)]
        self.calls=[]
    def read(self,session,cursor):
        self.calls.append((session['id'],cursor))
        return [{'id':session['id']+str(cursor)}], 'older' if cursor is None else None
    def test_covers_all_sessions_before_older_pages_without_loss(self):
        rows,token=self.reader.read(self.sessions,None,(None,None),'account',self.read)
        self.assertEqual(len(rows),4);self.assertEqual([x[0] for x in self.calls],['0','1','2','3'])
        while token:
            more,token=self.reader.read(self.sessions,token,(None,None),'account',self.read);rows+=more
        self.assertEqual(len({x['id'] for x in rows}),12)
        self.assertEqual([x[0] for x in self.calls[:6]],['0','1','2','3','4','5'])
    def test_cursor_is_bound_to_account_and_filter(self):
        _,token=self.reader.read(self.sessions,None,('section',None),'account',self.read)
        for scope,account in [(('other',None),'account'),(('section',None),'other')]:
            with self.assertRaises(ValueError):self.reader.read(self.sessions,token,scope,account,self.read)
    def test_deleted_sessions_are_not_read(self):
        _,token=self.reader.read(self.sessions,None,(None,None),'account',self.read)
        self.calls=[]
        self.reader.read([],token,(None,None),'account',self.read)
        self.assertEqual(self.calls,[])
    def test_retry_uses_same_cursor_and_expiration_is_explicit(self):
        _,token=self.reader.read(self.sessions,None,(None,None),'account',self.read)
        first,_=self.reader.read(self.sessions,token,(None,None),'account',self.read)
        second,_=self.reader.read(self.sessions,token,(None,None),'account',self.read)
        self.assertEqual(first,second)
        self.now=901
        with self.assertRaises(ValueError):self.reader.read(self.sessions,token,(None,None),'account',self.read)
    def test_empty_scope_has_no_more_pages(self):
        self.assertEqual(self.reader.read([],None,(None,None),'account',self.read),([],None))
    def test_failed_read_does_not_consume_cursor(self):
        _,token=self.reader.read(self.sessions,None,(None,None),'account',self.read)
        def fail(*args):raise ValueError('unavailable')
        with self.assertRaises(ValueError):self.reader.read(self.sessions,token,(None,None),'account',fail)
        rows,_=self.reader.read(self.sessions,token,(None,None),'account',self.read)
        self.assertEqual(rows[0]['id'],'4None')
