"""仅供隔离网关进程使用的官方文件认证适配器；用户已明确授权限定账户的内存消费。"""
from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time

from .account_runtime import current_account_home, validate_account_home
from .codex_rpc import AccountRpc
from .gateway_routes import GatewayError
from .model_gateway import Authorization

UPSTREAM = 'https://chatgpt.com/backend-api/codex'


class OfficialAuth:
    """固定目录和主体，不接受请求指定认证路径；续期交给官方 CLI，不自行使用 refresh token。"""

    def __init__(self, home: Path, subject: str, codex: str, *, current=False, rpc_factory=AccountRpc):
        checked = current_account_home() if current else validate_account_home(str(home))
        if Path(checked) != home.resolve() or not subject:
            raise ValueError('认证目录或已授权主体不符')
        self.home, self.subject, self.codex = Path(checked), subject, codex
        self.current, self.rpc_factory = current, rpc_factory
        self.lock = threading.Lock()
        self._recent_ingress = []
        directory = self.home.stat()
        self.directory_identity = (directory.st_dev, directory.st_ino)

    def _snapshot(self):
        directory_fd = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.fstat(directory_fd)
            if (directory.st_dev, directory.st_ino) != self.directory_identity or directory.st_uid != os.getuid():
                raise ValueError()
            fd = os.open('auth.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                        or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 128 * 1024):
                    raise ValueError()
                value = json.loads(stream.read(128 * 1024 + 1))
                after = os.fstat(stream.fileno())
            latest = os.stat('auth.json', dir_fd=directory_fd, follow_symlinks=False)
            signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if signature(before) != signature(after) or signature(after) != signature(latest):
                raise GatewayError('auth_changed_during_read', '官方登录刚发生变化，请重试', 409)
        finally:
            os.close(directory_fd)
        tokens = value.get('tokens') or {}
        token = tokens.get('access_token')
        if value.get('auth_mode') != 'chatgpt' or not isinstance(token, str) or tokens.get('account_id') != self.subject:
            raise ValueError()
        # 只消费 access_token；refresh_token 不返回、不复制、不由网关自行刷新。
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if (claims.get('https://api.openai.com/auth') or {}).get('chatgpt_account_id') != self.subject:
            raise ValueError()
        expiry = claims.get('exp')
        if type(expiry) not in (int, float):
            raise ValueError()
        return token, expiry

    def authorize(self) -> Authorization:
        """在同一锁内核验主体、检查有效期并冻结认证头；所有失败仅返回脱敏错误。"""
        with self.lock:
            try:
                token, expiry = self._snapshot()
                if expiry - time.time() < 180:
                    self._remember(token, expiry)
                    with self.rpc_factory(self.codex, str(self.home), current=self.current) as rpc:
                        account = (rpc.request('account/read', {'refreshToken': True}) or {}).get('account')
                        if not isinstance(account, dict) or account.get('type') != 'chatgpt':
                            raise ValueError()
                    token, expiry = self._snapshot()
                if expiry - time.time() < 30:
                    raise ValueError()
                return Authorization(self.subject, {'Authorization': 'Bearer ' + token,
                                     'Chatgpt-Account-Id': self.subject, 'Originator': 'codex_cli_rs'})
            except GatewayError:
                raise
            except Exception:
                raise GatewayError('official_auth_unavailable', '指定账户的官方登录不可用或身份不符', 401) from None

    def accepts(self, header: str) -> bool:
        """入口不触发续期；同主体轮换仅容许已核验旧令牌最多 60 秒，退出或换号立即拒绝。"""
        with self.lock:
            try:
                token, expiry = self._snapshot()
                if expiry <= time.time() or not isinstance(header,str):return False
                candidate = hashlib.sha256(header.encode()).digest()
                expected = hashlib.sha256(('Bearer '+token).encode()).digest()
                if hmac.compare_digest(candidate,expected):
                    self._remember(token,expiry)
                    return True
                return any(until>time.time() and hmac.compare_digest(candidate,digest) for digest,until in self._recent_ingress)
            except Exception:
                self._recent_ingress.clear()
                return False

    def _remember(self, token, expiry):
        digest=hashlib.sha256(('Bearer '+token).encode()).digest()
        self._recent_ingress=[item for item in self._recent_ingress if item[1]>time.time() and item[0]!=digest][-1:]
        self._recent_ingress.append((digest,min(expiry,time.time()+60)))
