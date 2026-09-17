"""官方 CLI 的隔离协议探针：空账户目录、本机假模型，不访问用户登录或真实模型。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import os
import signal
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class LocalModel:
    """只返回合成文本，保存调用次数及上下文标记，绝不保存请求正文。"""

    def __init__(self, label: str, failures=()):
        self.failures = list(failures)
        self.label = label
        self.calls = 0
        self.saw_previous = False
        self.saw_auth = False
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers.get('Content-Length', '0'))
                if size > 4 * 1024 * 1024:
                    self.send_error(413)
                    return
                body = self.rfile.read(size)
                model.calls += 1
                failure = model.failures.pop(0) if model.failures else None
                if failure == "reset":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                model.saw_auth |= bool(self.headers.get('Authorization'))
                model.saw_previous |= b'PROBE_A_COMPLETED' in body
                text = f'PROBE_{model.label}_COMPLETED'
                item = {'id': 'msg_probe', 'type': 'message', 'role': 'assistant',
                        'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
                response = {'id': 'resp_probe', 'object': 'response', 'created_at': int(time.time()),
                            'status': 'completed', 'model': 'routing-probe', 'output': [item],
                            'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}}
                events = [
                    ('response.created', {'response': {**response, 'status': 'in_progress', 'output': []}}),
                    ('response.output_item.added', {'output_index': 0, 'item': {**item, 'status': 'in_progress', 'content': []}}),
                    ('response.content_part.added', {'output_index': 0, 'item_id': 'msg_probe', 'content_index': 0,
                                                     'part': {'type': 'output_text', 'text': '', 'annotations': []}}),
                    ('response.output_text.delta', {'output_index': 0, 'item_id': 'msg_probe', 'content_index': 0, 'delta': text}),
                    ('response.output_text.done', {'output_index': 0, 'item_id': 'msg_probe', 'content_index': 0, 'text': text}),
                    ('response.output_item.done', {'output_index': 0, 'item': item}),
                    ('response.completed', {'response': response}),
                ]
                payload = ''.join(f'event: {name}\ndata: {json.dumps({"type": name, **value})}\n\n' for name, value in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                if failure == "stream":
                    self.wfile.write(payload.split(b"event: response.output_text.delta")[0]);self.wfile.flush()
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        """返回仅监听本机回环的模型地址。"""
        return f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        """停止本次合成模型服务。"""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class ProbeRpc:
    """真实 stdio 协议客户端，支持响应与通知分流，拒绝所有工具请求。"""

    async def start(self, cli: Path, home: Path, database: Path, model: LocalModel):
        # 使用白名单环境和临时 HOME；不能继承当前桌面的认证、MCP 或代理环境。
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT') if key in os.environ}
        env.update(HOME=str(home), CODEX_HOME=str(home))
        settings = {'sqlite_home': str(database), 'model_provider': 'probe', 'model': 'routing-probe',
                    'model_providers.probe.name': 'Local protocol probe',
                    'model_providers.probe.base_url': model.url,
                    'model_providers.probe.wire_api': 'responses',
                    'model_providers.probe.requires_openai_auth': False,
                    'check_for_update_on_startup': False, 'analytics.enabled': False, 'features.apps': False}
        args = [arg for key, value in settings.items() for arg in ('-c', f'{key}={json.dumps(value)}')]
        self.process = await asyncio.create_subprocess_exec(str(cli), *args, 'app-server', '--listen', 'stdio://',
            cwd=home, env=env, stdin=-1, stdout=-1, stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024 * 1024, start_new_session=True)
        self.pending = {}
        self.events = asyncio.Queue()
        self.sequence = 0
        self.reader = asyncio.create_task(self._read())
        self.rejected_tools = 0
        await self.call('initialize', {'clientInfo': {'name': 'workbench_protocol_probe', 'version': '1'},
                                       'capabilities': {'experimentalApi': True}})
        await self._send({'method': 'initialized'})
        return self

    async def _send(self, value):
        self.process.stdin.write((json.dumps(value) + '\n').encode())
        await self.process.stdin.drain()

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if 'id' in message and 'method' in message:
                    self.rejected_tools += 1
                    await self._send({'id': message['id'], 'error': {'code': -32601, 'message': 'Probe rejects tools'}})
                elif 'id' in message:
                    target = self.pending.get(message['id'])
                    if target and not target.done():
                        target.set_result(message)
                else:
                    await self.events.put(message)
        finally:
            for target in self.pending.values():
                if not target.done():
                    target.set_exception(RuntimeError('probe transport closed'))

    async def call(self, method: str, params: dict):
        """调用真实协议；错误只返回官方错误码，不打印请求、响应正文。"""
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._send({'id': request_id, 'method': method, 'params': params})
        try:
            response = await asyncio.wait_for(future, 25)
        finally:
            self.pending.pop(request_id, None)
        if 'error' in response:
            raise RuntimeError(f"{method}: RPC {response['error'].get('code')}")
        return response['result']

    async def turn(self, thread_id: str):
        """发送合成输入并等待准确的 turn/completed，不触发真实模型消费。"""
        result = await self.call('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': 'Protocol probe. Reply once.'}]})
        turn_id = result['turn']['id']
        async with asyncio.timeout(30):
            while True:
                message = await self.events.get()
                params = message.get('params', {})
                if message.get('method') == 'turn/completed' and params.get('threadId') == thread_id and params.get('turn', {}).get('id') == turn_id:
                    return params['turn']['status']

    async def close(self):
        """关闭本次 CLI 进程组，绝不触及桌面正在运行的进程。"""
        if not hasattr(self, 'process'):
            return
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except TimeoutError:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
        await self.reader


async def probe(cli: Path) -> dict:
    """验证共享元数据、分页、跨运行目录恢复及归档；返回不含原始记录的结果。"""
    checks = {}
    limitations = {}
    model_a, model_b = LocalModel('A'), LocalModel('B')
    try:
        async with AsyncExitStack() as cleanup:
            temporary = cleanup.enter_context(tempfile.TemporaryDirectory(prefix='workbench-native-probe-'))
            root = Path(temporary).resolve()
            a, b, db = root / 'a', root / 'b', root / 'state'
            for folder in (a, b, db):
                folder.mkdir(mode=0o700)
            one = ProbeRpc(); cleanup.push_async_callback(one.close); await one.start(cli, a, db, model_a)
            two = ProbeRpc(); cleanup.push_async_callback(two.close); await two.start(cli, b, db, model_b)
            project = (await one.call('project/create', {'idempotencyKey': 'probe', 'name': 'Synthetic probe', 'roots': []}))['project']
            checks['shared_project'] = any(p['id'] == project['id'] for p in (await two.call('project/list', {}))['data'])
            started = await one.call('thread/start', {'cwd': str(root), 'projectId': project['id'], 'model': 'routing-probe', 'ephemeral': False})
            thread_id = started['thread']['id']
            checks['first_turn_completed'] = await one.turn(thread_id) == 'completed'
            checks['first_backend_only'] = model_a.calls == 1 and model_b.calls == 0
            await one.close()
            # 不复制任何会话或认证文件；仅按官方线程 ID 从共享的临时状态库恢复。
            resumed = await two.call('thread/resume', {'threadId': thread_id, 'excludeTurns': True})
            checks['same_thread_after_resume'] = resumed['thread']['id'] == thread_id
            checks['second_turn_completed'] = await two.turn(thread_id) == 'completed'
            checks['new_backend_used'] = model_b.calls == 1 and model_a.calls == 1
            checks['previous_context_received'] = model_b.saw_previous
            page = await two.call('thread/list', {'useStateDbOnly': True, 'limit': 1})
            checks['visible_in_list'] = any(t['id'] == thread_id for t in page['data'])
            try:
                await two.call('thread/archive', {'threadId': thread_id})
                limitations['archive_from_execution_home'] = 'supported'
            except RuntimeError as error:
                limitations['archive_from_execution_home'] = str(error)
            await two.close()
            owner = ProbeRpc(); cleanup.push_async_callback(owner.close); await owner.start(cli, a, db, model_a)
            await owner.call('thread/archive', {'threadId': thread_id})
            archived = await owner.call('thread/list', {'useStateDbOnly': True, 'archived': True})
            checks['archive_using_storage_owner'] = any(t['id'] == thread_id for t in archived['data'])
            checks['no_auth_headers'] = not (model_a.saw_auth or model_b.saw_auth)
            checks['no_tool_execution'] = one.rejected_tools == two.rejected_tools == 0
    finally:
        model_a.close(); model_b.close()
    return {'checks': checks, 'protocol_checks_passed': all(checks.values()), 'limitations': limitations,
            'native_account_takeover_passed': False,
            'scope': 'Official CLI + local synthetic provider; not ChatGPT OAuth or native UI acceptance'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    options = parser.parse_args()
    print(json.dumps(asyncio.run(probe(options.cli.resolve())), ensure_ascii=False))
