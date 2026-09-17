"""模型网关契约验收：全部使用合成身份和本机假模型，不访问真实认证或远端模型。"""
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from uuid import uuid4

from codex_workbench.gateway_routes import AccountTarget, GatewayError, RouteStore, WorkbenchAccounts
from codex_workbench.model_gateway import Authorization, ModelGateway, Upstream, decode_body

TOKEN = 'synthetic-local-test-key-not-a-real-account-token'


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.routes = RouteStore(self.root / 'gateway')
        self.default = 'a'
        self.subjects = {'a': 'subject-a', 'b': 'subject-b'}
        self.seen = []
        self.reply_status = 200
        self.stream_started = threading.Event()
        self.stream_finish = threading.Event()
        self.slow = False
        self.cancel_mode = False
        self.upstream_closed = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_POST(self):
                data = self.rfile.read(int(self.headers['Content-Length']))
                owner.seen.append((self.path, json.loads(data), self.headers.get('Authorization')))
                self.send_response(owner.reply_status)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.send_header('Set-Cookie', 'synthetic=never-forward')
                self.send_header('X-Codex-Turn-State', 'synthetic-state')
                self.end_headers()
                self.wfile.write(b'data: {"type":"response.created"}\n\n')
                self.wfile.flush()
                owner.stream_started.set()
                if owner.cancel_mode:
                    readable, _, _ = select.select([self.connection], [], [], 3)
                    if readable and self.connection.recv(1) == b'':
                        owner.upstream_closed.set()
                    return
                if owner.slow: owner.stream_finish.wait(4)
                self.wfile.write(b'data: {"type":"response.completed"}\n\n')
                self.wfile.flush()

        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.upstream.daemon_threads = True
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close_upstream)
        self.transports = {a: Upstream(f'http://127.0.0.1:{self.upstream.server_port}/v1',
                           lambda a=a: Authorization('subject-' + a, {'Authorization': 'Bearer upstream-' + a}))
                           for a in self.subjects}
        self.server = ModelGateway(self.routes, self.accounts, self.transports, TOKEN)
        self.serving = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.serving.start()
        self.addCleanup(self._close_gateway)

    def _close_gateway(self):
        self.stream_finish.set()
        self.server.shutdown(); self.server.server_close(); self.serving.join(2)

    def _close_upstream(self):
        self.upstream.shutdown(); self.upstream.server_close(); self.thread.join(2)

    def accounts(self, account_id=None):
        key = account_id or self.default
        return AccountTarget(key, self.subjects[key])

    def body(self, thread_id=None, root=None, **extra):
        key = thread_id or str(uuid4())
        return {'model': 'synthetic', 'client_metadata': {'thread_id': key, 'session_id': root or key},
                'input': [{'role': 'user', 'content': 'synthetic probe'}], 'stream': True, **extra}

    def request(self, body, path='/v1/responses', headers=None, compressed=False):
        raw = json.dumps(body).encode()
        defaults = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'}
        if compressed:
            raw = gzip.compress(raw); defaults['Content-Encoding'] = 'gzip'
        defaults.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=6)
        conn.request('POST', path, raw, defaults)
        response = conn.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        conn.close()
        return result

    def error_code(self, result):
        return json.loads(result[1])['error']['code']

    def test_default_change_and_restart_preserve_binding(self):
        first = self.body()
        self.assertEqual(self.request(first)[0], 200)
        self.default = 'b'
        self.server.routes = RouteStore(self.root / 'gateway')
        self.assertEqual(self.request(first)[0], 200)
        self.assertEqual(self.request(self.body())[0], 200)
        self.assertEqual([x[2] for x in self.seen], ['Bearer upstream-a', 'Bearer upstream-a', 'Bearer upstream-b'])

    def test_prewarms_do_not_bind_or_infer(self):
        body = self.body(generate=False)
        self.assertEqual(self.error_code(self.request(body)), 'prewarm_unsupported')
        self.assertIsNone(self.routes.lookup(body['client_metadata']['thread_id']))
        self.assertEqual(self.seen, [])

    def test_child_inherits_root_not_new_default(self):
        root = self.body(); self.request(root)
        self.default = 'b'
        self.assertEqual(self.request(self.body(root=root['client_metadata']['thread_id']))[0], 200)
        self.assertEqual(self.seen[-1][2], 'Bearer upstream-a')

    def test_orphan_child_rejected(self):
        self.assertEqual(self.error_code(self.request(self.body(root=str(uuid4())))), 'parent_route_missing')
        self.assertEqual(self.seen, [])

    def test_identity_change_rejected(self):
        body = self.body(); self.request(body)
        self.subjects['a'] = 'different-person'
        self.assertEqual(self.error_code(self.request(body)), 'account_identity_changed')
        self.assertEqual(len(self.seen), 1)

    def test_upstream_subject_must_match(self):
        self.subjects['a'] = 'different-person'
        body = self.body()
        self.assertEqual(self.error_code(self.request(body)), 'upstream_identity_mismatch')
        self.assertIsNone(self.routes.lookup(body['client_metadata']['thread_id']))

    def test_auth_snapshot_cannot_be_changed_after_identity_check(self):
        headers = {'Authorization': 'Bearer upstream-a'}
        snapshot = Authorization('subject-a', headers)
        headers['Authorization'] = 'Bearer upstream-b'
        self.server.upstreams['a'] = Upstream(self.transports['a'].base_url, lambda: snapshot)
        self.assertEqual(self.request(self.body())[0], 200)
        self.assertEqual(self.seen[0][2], 'Bearer upstream-a')
        with self.assertRaises(TypeError): snapshot.headers['Authorization'] = 'different'

    def test_missing_account_transport_never_falls_back(self):
        self.default = 'b'; del self.server.upstreams['b']
        self.assertEqual(self.error_code(self.request(self.body())), 'account_transport_unavailable')
        self.assertEqual(self.seen, [])

    def test_quota_error_does_not_retry_or_switch_account(self):
        self.reply_status = 429
        result = self.request(self.body())
        self.assertEqual(result[0], 429)
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0][2], 'Bearer upstream-a')

    def test_full_protocol_fields_preserved_and_auth_replaced(self):
        body = self.body(tools=[{'type': 'function', 'name': 'test', 'parameters': {'type': 'object'}}],
            reasoning={'effort': 'ultra'}, include=['reasoning.encrypted_content'], service_tier='priority')
        body['input'].append({'type': 'function_call_output', 'call_id': 'synthetic-call', 'output': 'synthetic-result'})
        result = self.request(body, compressed=True)
        self.assertEqual(result[0], 200)
        self.assertEqual(self.seen[0][1], body)
        self.assertNotIn('Set-Cookie', result[2])
        self.assertEqual(result[2]['X-Codex-Turn-State'], 'synthetic-state')
        self.assertEqual(self.seen[0][2], 'Bearer upstream-a')

    def test_unbound_previous_response_and_compact_rejected(self):
        self.assertEqual(self.error_code(self.request(self.body(previous_response_id='synthetic-response'))), 'existing_thread_unbound')
        self.assertEqual(self.error_code(self.request(self.body(), path='/v1/responses/compact')), 'existing_thread_unbound')
        self.assertEqual(self.seen, [])

    def test_bound_compact_and_previous_response_forwarded(self):
        body = self.body(); self.request(body)
        body['previous_response_id'] = 'synthetic-response'
        self.assertEqual(self.request(body)[0], 200)
        self.assertEqual(self.request(body, path='/v1/responses/compact')[0], 200)
        self.assertEqual(self.seen[-1][0], '/v1/responses/compact')

    def test_missing_metadata_does_not_guess_from_header(self):
        body = self.body(); body.pop('client_metadata')
        self.assertEqual(self.error_code(self.request(body, headers={'session_id': str(uuid4())})), 'thread_metadata_required')
        self.assertEqual(self.seen, [])

    def test_web_origins_hosts_and_missing_auth_rejected(self):
        for headers in ({'Origin': 'https://example.com'}, {'Host': 'example.com'}, {'Authorization': ''}):
            self.assertIn(self.request(self.body(), headers=headers)[0], (401, 403))
        self.assertEqual(self.seen, [])

    def test_unknown_endpoint_rejected(self):
        self.assertEqual(self.request(self.body(), path='/v1/arbitrary')[0], 404)

    def test_same_thread_concurrency_rejected_until_completion(self):
        self.slow = True
        body = self.body(); result = []
        thread = threading.Thread(target=lambda: result.append(self.request(body)))
        thread.start()
        self.assertTrue(self.stream_started.wait(2))
        try:
            self.assertEqual(self.error_code(self.request(body)), 'thread_busy')
        finally:
            self.stream_finish.set(); thread.join(6)
        self.assertEqual(result[0][0], 200)
        self.assertEqual(self.request(body)[0], 200)

    def test_first_stream_chunk_arrives_before_upstream_finishes(self):
        self.slow = True
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        conn.request('POST', '/v1/responses', json.dumps(self.body()),
                     {'Authorization': 'Bearer '+TOKEN, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        try:
            self.assertEqual(response.readline(), b'data: {"type":"response.created"}\n')
        finally:
            self.stream_finish.set(); response.read(); conn.close()

    def test_workbench_default_is_read_from_authoritative_table(self):
        path = self.root / 'workbench.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE preferences(singleton INTEGER, default_execution_account_id TEXT)')
            db.execute('CREATE TABLE execution_accounts(id TEXT, subject_id TEXT)')
            db.execute("INSERT INTO execution_accounts VALUES('b','subject-b')")
            db.execute("INSERT INTO preferences VALUES(1,'b')")
        self.assertEqual(WorkbenchAccounts(path)(), AccountTarget('b','subject-b'))
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM preferences')
        with self.assertRaises(GatewayError): WorkbenchAccounts(path)()

    def test_client_disconnect_closes_silent_upstream(self):
        self.cancel_mode = True
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        conn.request('POST', '/v1/responses', json.dumps(self.body()),
                     {'Authorization': 'Bearer '+TOKEN, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        self.assertEqual(response.readline(), b'data: {"type":"response.created"}\n')
        response.close(); conn.close()
        self.assertTrue(self.upstream_closed.wait(1.5), '客户端取消后静默上游应及时断开')

    def test_redirect_is_not_followed(self):
        self.reply_status = 302
        self.assertEqual(self.request(self.body())[0], 502)
        self.assertEqual(len(self.seen), 1)

    def test_invalid_json_encoding_and_root_change_rejected(self):
        self.assertEqual(self.request(self.body(), headers={'Content-Encoding': 'unknown'})[0], 415)
        body = self.body(); self.request(body)
        body['client_metadata']['session_id'] = str(uuid4())
        self.assertEqual(self.error_code(self.request(body)), 'thread_owner_mismatch')

    def test_ambiguous_json_is_rejected_before_routing(self):
        for raw in (b'{"a":1,"a":2}', b'{"nested":{"a":1,"a":2}}', b'{"a":NaN}', b'[]'):
            with self.assertRaises(GatewayError): decode_body(raw, '')


if __name__ == '__main__':
    unittest.main()
