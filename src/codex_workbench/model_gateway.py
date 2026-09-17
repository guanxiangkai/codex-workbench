"""本机 Responses HTTP 网关；透传模型协议，账户认证由独立的上游适配器负责。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import gzip
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
import ssl
import time
import select
import sqlite3
import threading
from typing import Callable
from types import MappingProxyType
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from .gateway_routes import AccountTarget, GatewayError, RouteStore, thread_key

MAX_BODY = 32 * 1024 * 1024
PATHS = {'/v1/responses': '/responses', '/v1/responses/compact': '/responses/compact'}
REQUEST_HEADERS = {'openai-beta', 'x-codex-turn-state', 'x-codex-turn-metadata',
                   'x-codex-beta-features', 'x-codex-version', 'session_id', 'conversation_id'}
RESPONSE_HEADERS = {'content-type', 'content-encoding', 'x-request-id', 'retry-after',
                    'x-codex-turn-state', 'openai-processing-ms'}


@dataclass(frozen=True)
class Authorization:
    """单次授权的不可变快照；身份和请求头必须由认证器在同一临界区取得。"""

    subject_id: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        # 防止认证器后续刷新原字典，导致已核验的主体与实际请求头不一致。
        if not isinstance(self.subject_id, str) or not self.subject_id:
            raise GatewayError('upstream_identity_missing', '上游未确认执行身份', 409)
        if any(not isinstance(k, str) or not isinstance(v, str) or '\n' in k+v or '\r' in k+v
               for k, v in self.headers.items()):
            raise GatewayError('upstream_auth_invalid', '上游认证格式无效', 503)
        object.__setattr__(self, 'headers', MappingProxyType(dict(self.headers)))


def resolve_proxy(base_url: str):
    """每次请求按当前代理及绕过规则选路，不缓存开关，不改变在途连接。"""
    url = urlsplit(base_url)
    if url.scheme != 'https':
        return None
    proxies = getproxies()
    proxy = proxies.get('https') or proxies.get('all')
    if not proxy or proxy_bypass(url.netloc):
        return None
    try:
        target = urlsplit(proxy)
        if (target.scheme != 'http' or target.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or target.username or target.password or target.query or target.fragment
                or target.path not in ('', '/') or target.port == 0
                or any(c in proxy for c in ('\r', '\n'))):
            raise ValueError()
        target.port  # 在发送任何请求前校验端口，异常中不暴露代理配置。
    except ValueError:
        raise GatewayError('proxy_not_supported', '当前代理不是已支持的本机 HTTP 隧道代理', 503) from None
    return target


def connection_error(error, stage):
    """仅输出固定网络诊断，不包含请求、认证、代理配置或异常原文。"""
    if isinstance(error,ssl.SSLCertVerificationError):
        return GatewayError('upstream_certificate_invalid','官方模型服务的 TLS 证书校验失败',502)
    if isinstance(error,(TimeoutError,socket.timeout)):
        return GatewayError('upstream_'+stage+'_timeout','模型网络连接超时；未切换账户',504)
    reason=('dns' if isinstance(error,socket.gaierror) else
            'refused' if isinstance(error,ConnectionRefusedError) else
            'reset' if isinstance(error,(ConnectionResetError,BrokenPipeError)) else
            'tls' if isinstance(error,ssl.SSLError) else 'closed')
    message={'connect':'模型网络连接未能建立，请稍后重试',
             'send':'发送模型请求时连接中断，未自动重发',
             'response':'等待模型响应时连接中断，未自动重发',
             'stream':'接收模型结果时连接中断，未自动重发'}[stage]
    return GatewayError('upstream_'+stage+'_'+reason,message,502)


def connect_upstream(base_url):
    """仅在模型请求发送前重建连接；每次重读系统网络设置，最多三次。"""
    url=urlsplit(base_url)
    for attempt in range(3):
        if attempt:time.sleep((.25,.75)[attempt-1])
        proxy=resolve_proxy(base_url)
        client_type=http.client.HTTPSConnection if url.scheme=='https' else http.client.HTTPConnection
        conn=client_type(proxy.hostname if proxy else url.hostname,
                         (proxy.port or 80) if proxy else url.port,timeout=5)
        if proxy:conn.set_tunnel(url.hostname,url.port or 443)
        try:
            # connect() 只进行 TCP、CONNECT 与 TLS 握手，不发送模型正文和授权头。
            conn.connect()
            conn.sock.settimeout(300)
            return conn
        except ssl.SSLCertVerificationError as error:
            conn.close()
            raise connection_error(error,'connect') from None
        except (OSError,http.client.HTTPException) as error:
            conn.close()
            if attempt==2:raise connection_error(error,'connect') from None


@dataclass(frozen=True)
class Upstream:
    """经验证的上游连接；单次认证快照不拆分获取、不接受客户端指定目标。"""

    base_url: str
    authorize: Callable[[], Authorization] = field(repr=False)

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if (url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path.endswith('/')
                or (url.scheme == 'http' and url.hostname not in {'127.0.0.1', '::1'})):
            raise ValueError('模型上游必须使用 HTTPS 或本机回环地址，且不含认证和查询参数')
        if any(c in self.base_url for c in ('\r', '\n')):
            raise ValueError('模型上游地址无效')

    @contextmanager
    def request(self, suffix: str, body: bytes, headers: dict[str, str], authorization: Authorization,
                downstream: socket.socket | None = None):
        """原样转发正文，不重写 model/tools/推理内容，不跟随重定向，已发送的请求不自动重试。"""
        url = urlsplit(self.base_url)
        conn = connect_upstream(self.base_url)
        stopped = threading.Event()
        watcher = None
        try:
            outgoing = {k: v for k, v in headers.items() if k.lower() in REQUEST_HEADERS}
            outgoing.update(authorization.headers)
            outgoing.update({'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                             'Accept': 'text/event-stream, application/json', 'Accept-Encoding': 'identity'})
            upstream_socket = conn.sock
            def watch_disconnect():
                # 上游静默时也要传播取消，不能等待下一个 SSE chunk 才发现客户端离开。
                while not stopped.is_set():
                    try:
                        readable, _, _ = select.select([downstream], [], [], .1)
                        if readable and downstream.recv(1, socket.MSG_PEEK) == b'':
                            upstream_socket.shutdown(socket.SHUT_RDWR)
                            return
                        if readable:
                            stopped.wait(.1)
                    except (OSError, ValueError):
                        return
            if downstream is not None:
                watcher = threading.Thread(target=watch_disconnect, daemon=True)
                watcher.start()
            stage='send'
            try:
                conn.request('POST', url.path + suffix, body=body, headers=outgoing)
                stage='response'
                response=conn.getresponse()
                stage='stream'
                yield response
            except (OSError,http.client.HTTPException) as error:
                raise connection_error(error,stage) from None
        finally:
            stopped.set()
            conn.close()
            if watcher is not None:
                watcher.join(.2)


def decode_body(raw: bytes, encoding: str) -> dict:
    """有界读取用于归属判断的元数据；实际向上游发送原始 JSON 内容。"""
    try:
        if encoding == 'gzip':
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                decoded = stream.read(MAX_BODY + 1)
        elif encoding in ('', 'identity'):
            decoded = raw
        else:
            raise GatewayError('encoding_unsupported', '此网关版本不支持该请求压缩格式', 415)
        if len(decoded) > MAX_BODY:
            raise GatewayError('request_too_large', '模型请求超过网关大小上限', 413)
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate JSON field')
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError('non-finite JSON value')
        body = json.loads(decoded, object_pairs_hook=unique_fields, parse_constant=invalid_constant)
        if not isinstance(body, dict):
            raise ValueError()
        return body
    except (ValueError, OSError, EOFError, RecursionError) as exc:
        if isinstance(exc, GatewayError):
            raise
        raise GatewayError('invalid_json', '模型请求必须是有效的 JSON 对象') from None


class ModelGateway(ThreadingHTTPServer):
    """仅监听 IPv4 loopback，以独立本机令牌认证；不提供 CORS、账户修改或 UI 接口。"""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, routes: RouteStore, accounts: Callable, upstreams: dict[str, Upstream],
                 token: str, *, port=0, authenticate=None):
        if not isinstance(token, str) or len(token) < 32 or not token.isascii() or not token.isprintable():
            raise ValueError('网关必须使用独立且足够长的本机访问令牌')
        self.routes, self.accounts, self.upstreams = routes, accounts, dict(upstreams)
        self._token = token
        self.authenticate = authenticate
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(('127.0.0.1', port), GatewayHandler)

    @property
    def url(self) -> str:
        """返回可配置为单一固定 provider 的 Responses 根地址。"""
        return f'http://127.0.0.1:{self.server_port}/v1'

    def target(self, account: AccountTarget) -> tuple[Upstream, Authorization]:
        """只允许明确登记的认证上游；缺失时禁止借用主账户。"""
        upstream = self.upstreams.get(account.account_id)
        if upstream is None:
            raise GatewayError('account_transport_unavailable', '所选账户尚未接入模型认证通道', 503)
        authorization = upstream.authorize()
        if not isinstance(authorization, Authorization):
            raise GatewayError('upstream_auth_invalid', '上游认证适配器未返回有效授权', 503)
        return upstream, authorization

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        """不把原始模型请求、HTTP 头或认证异常打印到运行日志。"""


class GatewayHandler(BaseHTTPRequestHandler):
    """接收真实模型请求，保持流式输出，并将断线传播为上游连接关闭。"""

    protocol_version = 'HTTP/1.1'
    server_version = 'WorkbenchGateway/1'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        self._started = False

    def log_message(self, *args):
        """不记录请求路径或请求行，避免异常请求将秘密带入日志。"""

    def _authorize(self):
        hosts = self.headers.get_all('Host', [])
        if hosts != [f'127.0.0.1:{self.server.server_port}'] or self.headers.get('Origin'):
            raise GatewayError('origin_denied', '模型网关不接受网页来源请求', 403)
        auth = self.headers.get_all('Authorization', [])
        expected = 'Bearer ' + self.server._token
        valid = len(auth) == 1 and (self.server.authenticate(auth[0]) if self.server.authenticate
                                   else hmac.compare_digest(auth[0].encode(), expected.encode()))
        if not valid:
            raise GatewayError('gateway_auth_required', '模型网关认证失败', 401)

    def _error(self, error: GatewayError):
        if not self._started:
            body = json.dumps({'error': {'type': 'workbench_gateway_error',
                    'code': error.code, 'message': str(error)}}, ensure_ascii=True).encode()
            self.send_response(error.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        try:
            if self.path == '/health' and not self.headers.get('Origin') and self.headers.get_all('Host',[]) == [f'127.0.0.1:{self.server.server_port}']:
                body=b'{"ready":true,"protocol":1}'
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                return
            self._authorize()
            raise GatewayError('http_only', '模型网关使用 HTTP Responses；请禁用 provider 的 WebSocket', 405)
        except GatewayError as exc:
            self._error(exc)

    def do_POST(self):
        try:
            self._authorize()
            if self.path not in PATHS:
                raise GatewayError('endpoint_not_supported', '不支持此模型接口', 404)
            if self.headers.get('Upgrade') or self.headers.get('Transfer-Encoding'):
                raise GatewayError('transport_not_supported', '请求须为带长度的 HTTP JSON', 400)
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise GatewayError('length_required', '模型请求必须提供有效长度', 411)
            size = int(lengths[0])
            if not 0 < size <= MAX_BODY:
                raise GatewayError('request_too_large', '模型请求超过网关大小上限', 413)
            if self.headers.get_content_type() != 'application/json':
                raise GatewayError('content_type_required', '模型请求必须使用 application/json', 415)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise GatewayError('incomplete_request', '模型请求未完整送达')
            encoding = self.headers.get('Content-Encoding', '').lower()
            body = decode_body(raw, encoding)
            metadata = body.get('client_metadata') or {}
            key = thread_key(metadata.get('thread_id') if isinstance(metadata, dict) else None)
            with self.server.routes.executing(key):
                selected = []
                def verify(account):
                    upstream, authorization = self.server.target(account)
                    selected.append((upstream, authorization))
                    return authorization.subject_id
                self.server.routes.bind(body, self.server.accounts, verify, compact=self.path.endswith('/compact'))
                upstream, authorization = selected[0]
                # 压缩正文只解码一次给 HTTP 上游；其余字段、类型和协议结构原样保留。
                forwarded = raw if encoding in ('', 'identity') else gzip.decompress(raw)
                with upstream.request(PATHS[self.path], forwarded, dict(self.headers), authorization, self.connection) as response:
                    if not 200 <= response.status < 300:
                        status = response.status if response.status in {400, 401, 403, 408, 409, 413, 429} else 502
                        # 仅识别固定协议错误，不转发可能包含请求内容的上游报错正文。
                        reason='rejected'
                        try:
                            failure=json.loads(response.read(64*1024));error=failure.get('error',failure)
                            message=error.get('message','') if isinstance(error,dict) else str(error)
                            for field in ('max_output_tokens','temperature','top_p','store','stream','instructions','service_tier','prompt_cache_retention','context_management','reasoning','input','tools','model','metadata'):
                                if field in message and any(word in message.lower() for word in ('unsupported','not supported','required','invalid','must')):
                                    reason='invalid_'+field;break
                        except (ValueError,AttributeError):pass
                        raise GatewayError('upstream_'+reason, '所选账户的模型请求失败；未切换账户或重试', status)
                    self.send_response(response.status)
                    for name, value in response.getheaders():
                        if name.lower() in RESPONSE_HEADERS:
                            self.send_header(name, value)
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self._started = True
                    while chunk := response.read1(64 * 1024):
                        self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
                        self.wfile.flush()
                    self.wfile.write(b'0\r\n\r\n')
                    self.wfile.flush()
                    self.close_connection = True
        except GatewayError as exc:
            self._error(exc)
        except (TimeoutError, socket.timeout):
            self._error(GatewayError('upstream_timeout', '模型连接超时；未切换账户', 504))
        except (OSError, http.client.HTTPException, sqlite3.Error, ValueError):
            self._error(GatewayError('gateway_unavailable', '模型网关连接暂时不可用；未切换账户', 502))
