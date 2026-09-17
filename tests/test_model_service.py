"""独立模型库到技能助手及验证重试的本机集成验证。"""
import json
import tempfile
import threading
import time
from readonly_fixture import Fixture as ReadonlyFixture
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_workbench.agent_resources import ResourceError
from codex_workbench.catalog import validate
from codex_workbench.model_registry import ModelRegistry
from codex_workbench.service import Workbench
from tests.test_service import FakeAccounts
from tests.fakes import MemoryCredentials


class ModelServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ReadonlyFixture();self.board=self.fixture.board
        self.addCleanup(self.fixture.close)
    def test_model_state_has_no_validator_or_network_probe(self):
        self.board.call('workbench_state',{'view':'models'});self.assertFalse(hasattr(self.board,'validator'));self.assertEqual([],self.fixture.native.calls)
    def test_no_legacy_import_or_binding_changes(self):
        self.board.call('model_list',{});self.assertEqual([],list(self.fixture.root.iterdir()))
    def test_old_verification_state_is_preserved(self):
        import sqlite3
        path=self.fixture.root/'workbench.sqlite3'
        with sqlite3.connect(path) as c:
            c.execute('create table provider_models(id text,name text,base_url text,validation_status text)')
            c.execute("insert into provider_models values('m','模型','https://example.invalid/v1','failed')")
        self.assertEqual('failed',self.board.call('model_list',{})['models'][0]['validation_status'])
        with sqlite3.connect(path) as c:self.assertEqual('failed',c.execute('select validation_status from provider_models').fetchone()[0])
    def test_model_endpoint_query_is_not_public(self):
        import sqlite3
        with sqlite3.connect(self.fixture.root/'workbench.sqlite3') as c:
            c.execute('create table provider_models(id text,name text,base_url text)');c.execute("insert into provider_models values('m','模型','https://example.invalid?token=synthetic')")
        self.assertEqual('',self.board.call('model_list',{})['models'][0]['base_url'])
    def test_missing_model_is_an_error_not_a_created_default(self):
        with self.assertRaises(ValueError):self.board.call('model_detail',{'id':'missing'})
        self.assertEqual([],list(self.fixture.root.iterdir()))
    def test_query_rejects_unknown_options(self):
        with self.assertRaises(ValueError):self.board.call('model_list',{'validate':True})



if __name__ == "__main__":
    unittest.main()
