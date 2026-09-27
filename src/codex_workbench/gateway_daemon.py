"""隔离的本机账户网关进程；仅模型请求经过此进程，工作台页面与 MCP 不取得认证。"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading

from .gateway_auth import OfficialAuth, UPSTREAM
from .account_runtime import validate_account_home
from .readonly_sources import connection
from .gateway_routes import GatewayError, RouteStore, WorkbenchAccounts
from .model_gateway import ModelGateway, Upstream
from .native_compatibility import file_digest
from .runtime import secure_directory


_ACCOUNT_ID = re.compile(r'[A-Za-z0-9_.:@-]{1,160}\Z')


def _authorized_accounts(settings: dict) -> list[dict]:
    """校验持久化白名单与账户声明精确一致，拒绝默认扩展账户。"""
    account_ids=settings.get('authorized_account_ids')
    declared=settings.get('accounts')
    if (not isinstance(account_ids,list) or not 1 <= len(account_ids) <= 8 or 'current' not in account_ids
            or len(account_ids)!=len(set(account_ids))
            or any(not isinstance(account_id,str) or not _ACCOUNT_ID.fullmatch(account_id) for account_id in account_ids)
            or not isinstance(declared,list) or len(declared)!=len(account_ids)):
        raise ValueError('网关授权账户配置无效')
    by_id={}
    for account in declared:
        if not isinstance(account,dict):
            raise ValueError('网关授权账户配置无效')
        account_id=account.get('id')
        home=account.get('home')
        subject_id=account.get('subject_id')
        if (not isinstance(account_id,str) or not _ACCOUNT_ID.fullmatch(account_id) or account_id in by_id
                or not isinstance(home,str) or not Path(home).is_absolute()
                or not isinstance(subject_id,str) or not _ACCOUNT_ID.fullmatch(subject_id)):
            raise ValueError('网关授权账户配置无效')
        by_id[account_id]=account
    if set(by_id)!=set(account_ids):
        raise ValueError('网关授权账户配置无效')
    return [by_id[account_id] for account_id in account_ids]


class RegisteredUpstreams:
    """显式启用后，按工作台已确认登记动态获取账户，不缓存默认选择或认证头。"""

    def __init__(self, db, cli, authorized, *, broker_factory=OfficialAuth):
        self.db, self.cli, self.factory = Path(db), str(cli), broker_factory
        self.authorized = {a['id']: a for a in authorized}
        self.lock = threading.Lock()
        self.cache = {}

    def __call__(self, target):
        with connection(self.db) as db:
            row = db.execute('SELECT id,codex_home,subject_id FROM execution_accounts WHERE id=?',
                             (target.account_id,)).fetchone()
        if row is None or row['subject_id'] != target.subject_id:
            raise GatewayError('account_identity_changed', '所选账户登记已变化，请重试', 409)
        declared = self.authorized.get(target.account_id)
        if target.account_id == 'current':
            if not declared or declared['subject_id'] != target.subject_id:
                raise GatewayError('account_identity_changed', '主账户登录身份已变化', 409)
            home = declared['home']
        else:
            try:
                home = validate_account_home(row['codex_home'])
                managed_root = (self.db.parent/'accounts').resolve()
                explicit = declared and Path(declared['home']).resolve() == Path(home) and declared['subject_id'] == target.subject_id
                if not explicit and Path(home).parent != managed_root:
                    raise ValueError('账户不在工作台管理目录')
            except ValueError:
                raise GatewayError('account_home_invalid', '所选账户登录目录不可用', 409) from None
        key = (target.account_id, home, target.subject_id)
        with self.lock:
            if key not in self.cache:
                broker = self.factory(Path(home), target.subject_id, self.cli, current=target.account_id == 'current')
                # 每次调用 authorize 都重新验证官方登录；这里只缓存有界的适配器。
                self.cache = {k: v for k, v in self.cache.items() if k[0] != target.account_id}
                if len(self.cache) >= 64:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = Upstream(UPSTREAM, broker.authorize)
            return self.cache[key]


def atomic_json(path: Path, value: dict):
    """原子更新本机非秘密状态；不会记录请求内容或认证材料。"""
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class UpgradeCheck:
    """CLI 制品变化时先运行无账户的本机协议回归；同一制品复用已通过证据。"""

    def __init__(self, root: Path, cli: Path, probe: Path):
        self.root, self.cli, self.probe = root, cli, probe
        self.lock = threading.Lock()
        self.stamp = None

    def __call__(self):
        with self.lock:
            inputs=[self.cli,self.probe,self.probe.parent/'tests/native_protocol_probe.py',
                    Path(__file__).parent/'gateway_routes.py',Path(__file__).parent/'model_gateway.py']
            stamp=tuple((i.st_dev,i.st_ino,i.st_size,i.st_mtime_ns) for i in (p.stat() for p in inputs))
            if stamp == self.stamp:return
            digest = file_digest(self.cli)
            source = {name: file_digest(Path(__file__).parent/name) for name in ('gateway_routes.py','model_gateway.py')}
            probe_digest=hashlib.sha256(self.probe.read_bytes()+(self.probe.parent/'tests/native_protocol_probe.py').read_bytes()+sys.version.encode()).hexdigest()
            try: previous = json.loads((self.root/'compatibility.json').read_text())
            except (OSError, ValueError): previous = {}
            if previous.get('passed') and previous.get('cli_sha256') == digest and previous.get('source_sha256') == source and previous.get('probe_sha256')==probe_digest:
                self.stamp=stamp;return
            env = {k:os.environ[k] for k in ('PATH','LANG','TMPDIR') if k in os.environ}
            result = subprocess.run([sys.executable, str(self.probe), '--cli',str(self.cli)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=90)
            try: evidence = json.loads(result.stdout)
            except (ValueError, UnicodeError): evidence = {}
            if (result.returncode or evidence.get('passed') is not True or evidence.get('cli_sha256')!=digest
                    or evidence.get('source_sha256') != source or evidence.get('probe_sha256')!=probe_digest or file_digest(self.cli) != digest):
                atomic_json(self.root/'status.json', {'ready':False,'reason':'cli_compatibility_check_failed'})
                raise GatewayError('cli_compatibility_check_failed','Codex 更新兼容检查未通过，尚未转发请求',503)
            atomic_json(self.root/'compatibility.json',evidence)
            self.stamp=stamp


def serve(root: Path):
    """使用明确配置的账户策略、官方上游和单实例端口。"""
    root = secure_directory(root)
    lease = (root/'gateway.lock').open('a+')
    try: fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('gateway_already_running')
    settings = json.loads((root/'settings.json').read_text())
    cli = Path(settings['cli'])
    accounts = WorkbenchAccounts(root.parent/'workbench.sqlite3')
    authorized = _authorized_accounts(settings)
    policy = settings.get('account_policy', 'fixed')
    if policy not in ('fixed', 'registered_accounts'):
        raise ValueError('网关账户策略无效')
    brokers={a['id']:OfficialAuth(Path(a['home']),a['subject_id'],str(cli),current=a['id']=='current')
             for a in authorized if policy == 'fixed' or a['id'] == 'current'}
    checker=UpgradeCheck(root,cli,Path(settings['probe']))
    checker()
    resolver = RegisteredUpstreams(root.parent/'workbench.sqlite3', cli, authorized) if policy == 'registered_accounts' else None
    def authenticate(header):
        checker()
        return brokers['current'].accepts(header)
    server=ModelGateway(RouteStore(root),accounts,{key:Upstream(UPSTREAM,broker.authorize) for key,broker in brokers.items()},
                        secrets.token_urlsafe(32),port=settings['port'],authenticate=authenticate,resolve_upstream=resolver)
    atomic_json(root/'status.json',{'ready':True,'pid':os.getpid(),'port':server.server_port,'authorized_accounts':list(brokers),
                                  'account_policy':policy,'provider':'workbench_gateway','transport':'http_sse','native_ui_verified':False})
    def stop(signum,frame): threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try: server.serve_forever()
    finally:
        server.server_close();lease.close()
        atomic_json(root/'status.json',{'ready':False,'reason':'stopped'})


def main():
    """LaunchAgent 的固定入口，异常只写脱敏状态。"""
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True,type=Path)
    args=parser.parse_args()
    try:serve(args.root)
    except Exception:
        try:atomic_json(args.root/'status.json',{'ready':False,'reason':'startup_failed'})
        except Exception:pass
        raise SystemExit(1)


if __name__=='__main__':main()
