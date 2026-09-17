#!/usr/bin/env python3
"""用当前官方 CLI 验收自研网关：模拟账户、本机模型、无真实认证和模型消费。"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime
import json
import hashlib
from pathlib import Path
import secrets
import sys
import tempfile
import threading

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'tests'))

from codex_workbench.gateway_routes import AccountTarget, RouteStore
from codex_workbench.model_gateway import Authorization, ModelGateway, Upstream
from codex_workbench.native_compatibility import file_digest
from native_protocol_probe import LocalModel, ProbeRpc


async def probe(cli: Path) -> dict:
    """以真实 app-server 走网关验证归属和上下文；返回脱敏布尔证据。"""
    with tempfile.TemporaryDirectory(prefix='workbench-gateway-probe-') as temp:
        root = Path(temp).resolve()
        home = root / 'home'; home.mkdir(mode=0o700)
        state = root / 'state'; state.mkdir()
        route_dir = root / 'routes'
        model_a, model_b = LocalModel('A'), LocalModel('B')
        default = ['a']
        token = secrets.token_urlsafe(32)
        (home / 'config.toml').write_text('[model_providers.probe.http_headers]\nAuthorization = '
                                         + json.dumps('Bearer ' + token) + '\n')
        accounts = lambda key=None: AccountTarget(key or default[0], 'synthetic-' + (key or default[0]))
        upstreams = {'a': Upstream(model_a.url, lambda: Authorization('synthetic-a')),
                     'b': Upstream(model_b.url, lambda: Authorization('synthetic-b'))}
        gateway = ModelGateway(RouteStore(route_dir), accounts, upstreams, token)
        server = threading.Thread(target=gateway.serve_forever, daemon=True); server.start()
        try:
            async with AsyncExitStack() as cleanup:
                rpc = ProbeRpc(); cleanup.push_async_callback(rpc.close)
                await rpc.start(cli, home, state, gateway)
                first = (await rpc.call('thread/start', {'cwd': str(root), 'model': 'routing-probe'}))['thread']['id']
                checks = {'no_model_call_before_turn': model_a.calls == model_b.calls == 0}
                checks['first_turn_completed'] = await rpc.turn(first) == 'completed'
                checks['first_default_account_only'] = model_a.calls == 1 and model_b.calls == 0
                default[0] = 'b'
                # 重建路由库对象模拟进程内缓存丢失，归属只能由 SQLite 恢复。
                gateway.routes = RouteStore(route_dir)
                checks['existing_turn_completed'] = await rpc.turn(first) == 'completed'
                checks['existing_account_preserved'] = model_a.calls == 2 and model_b.calls == 0
                second = (await rpc.call('thread/start', {'cwd': str(root), 'model': 'routing-probe'}))['thread']['id']
                checks['new_turn_completed'] = await rpc.turn(second) == 'completed'
                checks['new_default_account_only'] = model_a.calls == 2 and model_b.calls == 1
                checks['same_account_context_received'] = model_a.saw_previous
                checks['no_cross_account_context'] = not model_b.saw_previous
                checks['thread_metadata_matches_routes'] = (
                    gateway.routes.lookup(first)['account_id'] == 'a' and gateway.routes.lookup(second)['account_id'] == 'b')
                checks['no_upstream_auth_material'] = not model_a.saw_auth and not model_b.saw_auth
                checks['no_tools_executed'] = rpc.rejected_tools == 0
            return {'schema_version': 1, 'cli_sha256': file_digest(cli), 'checks': checks,
                    'verified_at': datetime.now(UTC).isoformat(),
                    'source_sha256': {name: file_digest(Path(__file__).resolve().parent / 'src/codex_workbench' / name)
                                      for name in ('gateway_routes.py', 'model_gateway.py')},
                    'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()+(Path(__file__).parent/'tests/native_protocol_probe.py').read_bytes()+sys.version.encode()).hexdigest(),
                    'passed': all(checks.values()), 'native_desktop_verified': False,
                    'subscription_auth_verified': False, 'native_routing_enabled': False,
                    'transport': 'HTTP Responses / SSE',
                    'scope': 'installed CLI, synthetic local upstreams; no user auth or real model usage'}
        finally:
            gateway.shutdown(); gateway.server_close(); server.join(2)
            model_a.close(); model_b.close()


def main():
    """按需验收；报告使用排他创建，不能覆盖现有证据或修改生产配置。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, default=Path('/Applications/ChatGPT.app/Contents/Resources/codex'))
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = asyncio.run(probe(args.cli))
    payload = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.report:
        with args.report.open('x') as stream:
            stream.write(payload)
    print(payload)
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
