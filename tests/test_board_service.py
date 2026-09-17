"""恢复看板的业务闭环，合成账户/执行器，不读取实际登录或会话。"""
import tempfile,time,threading,uuid,sqlite3
from pathlib import Path
from readonly_fixture import Fixture as ReadonlyFixture
import unittest
from codex_workbench.service import Workbench
from codex_workbench.executor import Execution
from codex_workbench.store import Store,StoreError
from tests.fakes import MemoryCredentials,fake_title

class Accounts:
    def __init__(self):self.store=None
    def require_ready(self,identity,expected_subject=None):
        assert identity==self.account['id']
        assert expected_subject in (None,'fixture-subject')
    def status(self,identity,refresh=False):
        return {'account':self.account,'login':{'status':'ready'},'identity_id':'fixture-subject'}
    def system_defaults(self):return {'model':'fixture-model','effort':'high','sandbox':'read-only','concurrency':1}
    def close(self):pass

class Executor:
    def __init__(self):self.outcome='review';self.requests=[]
    def execute(self,request,cancel,on_event):
        self.requests.append(request);on_event('thread',str(uuid.uuid4()))
        return Execution(self.outcome,result='合成执行结果',error='合成失败原因' if self.outcome=='failed' else '',input_tokens=12,output_tokens=3)

class BoardTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ReadonlyFixture();self.board=self.fixture.board
        self.addCleanup(self.fixture.close)
    def test_board_initialization_is_isolated_from_other_pages(self):
        self.board.call('workbench_state',{'view':'models'});self.assertEqual([],self.fixture.native.calls)
        self.assertEqual('execution_turn',self.board.call('board_state',{})['status']['task_unit'])
    def test_every_terminal_outcome_is_completed_without_guessing_metrics(self):
        from codex_workbench.readonly_sources import NativeRead
        from test_readonly_service import SESSION
        for result in ['completed','failed','interrupted','cancelled']:
            task=NativeRead.turn_view({'id':'t','status':result},SESSION)
            self.assertEqual('done',task['state']);self.assertEqual(result,task['result']);self.assertIsNone(task['total_tokens'])
    def test_task_write_and_archive_channels_are_removed(self):
        for name in ['task_create','task_update','task_start','run_cancel','session_update']:
            with self.subTest(name=name),self.assertRaises(ValueError):self.board.call(name,{})
    def test_unknown_session_is_rejected_before_turn_read(self):
        with self.assertRaises(ValueError):self.board.call('board_state',{'session_id':'other'})
        self.assertEqual(['snapshot'],self.fixture.native.calls)


class StateMigrationTests(unittest.TestCase):
    def test_legacy_terminal_states_become_done_and_runs_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'db';store=Store(path);account=store.create_execution_account('合成账户',str(root));store.set_default_execution_account(account['id']);project=store.create_project('项目',str(root));ids=[]
            for status in ('review','failed','cancelled'):
                t=store.create_task(project['id'],status);t=store.update_task(t['id'],t['version'],state='ready');r=store.claim(t['id']);store.finish(r['id'],status,result='保留结果');ids.append((t['id'],status))
            with sqlite3.connect(path) as db:
                sql=db.execute("select sql from sqlite_master where name='tasks'").fetchone()[0]
                indexes=[r[0] for r in db.execute("select sql from sqlite_master where tbl_name='tasks' and type='index' and sql is not null")]
                legacy=sql.replace('"tasks"','tasks').replace('CREATE TABLE tasks','CREATE TABLE old_tasks',1).replace("'done','archived'","'review','done','failed','cancelled'")
                db.execute(legacy);db.execute('insert into old_tasks select * from tasks');db.execute('drop table tasks');db.execute('alter table old_tasks rename to tasks')
                for q in indexes:db.execute(q)
                for identity,status in ids:db.execute('update tasks set state=? where id=?',(status,identity))
                db.commit()
            restored=Store(path)
            for identity,status in ids:
                self.assertEqual('done',restored.get_task(identity)['state']);self.assertEqual(status,restored.list_runs(identity)[0]['state']);self.assertEqual('保留结果',restored.list_runs(identity)[0]['result'])
            with sqlite3.connect(path) as db:self.assertEqual([],db.execute('pragma foreign_key_check').fetchall())
