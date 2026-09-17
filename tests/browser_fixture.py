"""浏览器联调专用：合成账户、助手与模型服务，不调用真实账户或模型。"""
import json
import sys
import shlex
import copy
import hashlib
import html
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codex_workbench.account_runtime import current_account_home
from codex_workbench.api import PreviewServer, Handler
from codex_workbench.service import Workbench
from codex_workbench.model_registry import ModelRegistry
from codex_workbench.model_probe import probe_model
from codex_workbench.credentials import CredentialCatalog
from codex_workbench.ui_release import UiRelease
from tests.media_fixtures import MP3


class FixtureAccounts:
    def __init__(self):
        self.store = None
        self.epoch_file = None

    def current(self, refresh=False):
        account = self.store.register_current_account("当前测试账户", current_account_home(), "fixture-subject")
        return {"account": {**account, "isCurrent": True, "email": "fixture@example.invalid"}, "identity_id": "fixture-subject",
                "login": {"status": "ready"}, "usage": {"plan": "pro", "resetCredits": 2, "subscriptionExpiresAt": None,
                "limits": {"codex": {"primary": {"windowKind": "week", "remainingPercent": 70-(int(self.epoch_file.read_text()) if self.epoch_file else 0), "resetsAt": "2026-09-18T00:00:00+00:00"}}}},
                "models": [{"id": "fixture-model", "model": "fixture-model", "displayName": "测试模型", "isDefault": True,
                            "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}], "defaultReasoningEffort": "medium"}],
                "taskDefaults": {"model": "fixture-model", "effort": "medium", "concurrency": 1, "sandbox": "read-only"}}

    def require_ready(self, account_id, expected_subject=None):
        if account_id != "current" or expected_subject not in {None, "fixture-subject"}:
            raise ValueError("合成账户不匹配")

    def status(self, account_id, refresh=True):
        if account_id == "current":
            return self.current()
        saved = next(a for a in self.store.list_execution_accounts() if a["id"] == account_id)
        return {"account": saved, "login": {"status": "not_logged_in"}}

    def close(self):
        pass






class FixtureModel(BaseHTTPRequestHandler):
    """先失败一次再成功的本机合成模型，可从浏览器手动重新验证。"""

    attempts = {}
    authenticated_models = set()

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            request = json.loads(body)
        except ValueError:
            request = {"model": "fixture-transcription"}
        model = request.get("model", "fixture")
        if model in self.authenticated_models and self.headers.get("Authorization") != "Bearer synthetic-workbench-key":
            self.send_response(401)
            self.end_headers()
            return
        self.attempts[model] = self.attempts.get(model, 0) + 1
        if self.attempts[model] == 1 or model == "always-fail":
            self.send_response(503)
            self.end_headers()
            return
        content_type = "application/json"
        if self.path.endswith("/audio/speech"):
            response = MP3
            content_type = "audio/mpeg"
        elif self.path.endswith("/audio/transcriptions"):
            response = {"text": "Test."}
        elif self.path.endswith("/rerank"):
            response = {"results": [{"index": 0, "relevance_score": 0.95}, {"index": 1, "relevance_score": 0.12}]}
        elif self.path.endswith("/embeddings"):
            response = {"data": [{"embedding": [0.1, 0.2]}]}
        elif self.path.endswith("/responses"):
            response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Red"}]}]}
        else:
            response = {"choices": [{"message": {"content": "Red"}}]}
        payload = response if isinstance(response, bytes) else json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FixtureKeyVault:
    """仅接受固定合成 Key 的内存替身，拒绝保存真实凭据。"""

    def __init__(self, entries_file=None):
        self.values = {}
        self.entries_file = entries_file

    def store(self, key, context):
        if key != "synthetic-workbench-key":
            raise ValueError("测试页面仅允许预设的合成 Key")
        reference = "vault:fixture-key-" + str(len(self.values) + 1)
        self.values[reference] = key
        if self.entries_file:
            entries = json.loads(self.entries_file.read_text())
            entries.append({"entryId": reference.removeprefix("vault:"), "revision": 1})
            self.entries_file.write_text(json.dumps(entries))
        FixtureModel.authenticated_models.add(context["name"])
        return reference


class FixturePayloadVault:
    """通用凭证测试仅接收固定的合成密码与 Key，在内存保存。"""
    def __init__(self, entries_file):
        self.entries_file=entries_file;self.saved=[]
    def store(self,payload,context):
        if payload.get("password") not in {None,"","synthetic-password"} or payload.get("key") not in {None,"","synthetic-workbench-key"}:
            raise ValueError("测试页面只允许合成凭证")
        self.saved.append(copy.deepcopy(payload))
        identity="fixture-credential-"+str(len(self.saved))
        entries=json.loads(self.entries_file.read_text());entries.append({"entryId":identity,"revision":1});self.entries_file.write_text(json.dumps(entries))
        return "vault:"+identity


class FixtureUiRelease(UiRelease):
    """通过只含整数的测试文件模拟完整发布，不修改真实界面源码。"""
    def __init__(self,epoch_file):
        self.epoch_file=epoch_file
        super().__init__(source_mode=True)
    @property
    def revision(self):
        base=super().revision
        return hashlib.sha256((base+':'+self.epoch_file.read_text()).encode()).hexdigest()
    def manifest(self,page,known_revision=None):
        value=super().manifest(page)
        revision=self.revision
        if known_revision==revision:return {'revision':revision,'unchanged':True}
        value['html']=value['html'].replace(value['revision'],revision)
        value['revision']=revision
        return value

class FixtureHandler(Handler):
    """标准 MCP App parent bridge 的隔离浏览器替身，只允许同一资源。"""
    def do_GET(self):
        if self.path!='/embedded':return super().do_GET()
        if not self._allowed():return self._respond(403,{'error':'origin_denied'})
        manifest=self.server.client.manifest('accounts');uri=manifest['resource_uri'];page=manifest['html'].replace('__WORKBENCH_RESOURCE_URI__',uri)
        host_script="""const frame=document.querySelector('iframe'),stats=document.querySelector('#stats');window.addEventListener('message',async event=>{if(event.source!==frame.contentWindow||event.data?.jsonrpc!=='2.0')return;const request=event.data;if(!request.id)return;let result,error;try{if(request.method==='ui/initialize'){stats.dataset.initializes=String(Number(stats.dataset.initializes)+1);result={}}else if(request.method==='tools/call'){if(request.params.name==='workbench_state')stats.dataset.polls=String(Number(stats.dataset.polls)+1);const response=await fetch('/rpc',{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Request':'1'},body:JSON.stringify({name:request.params.name,arguments:request.params.arguments})});result=await response.json()}else if(request.method==='resources/read'){if(request.params.uri!==SCOPE)throw Error('scope denied');stats.dataset.resources=String(Number(stats.dataset.resources)+1);const response=await fetch('/accounts');result={contents:[{uri:SCOPE,mimeType:'text/html;profile=mcp-app',text:await response.text()}]}}else throw Error('unsupported');}catch(e){error={code:-32601,message:e.message}}event.source.postMessage({jsonrpc:'2.0',id:request.id,...(error?{error}:{result})},'*')});""".replace('SCOPE',json.dumps(uri))
        body=('<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;height:100%;overflow:hidden}iframe{width:100%;height:100%;border:0}</style></head><body><div id="stats" hidden data-initializes="0" data-resources="0" data-polls="0"></div><iframe id="fixture-app" sandbox="allow-scripts" srcdoc="'+html.escape(page,quote=True)+'"></iframe><script>'+host_script+'</script></body></html>').encode()
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)

class FixturePreviewServer(PreviewServer):
    def __init__(self,address,client):
        super().__init__(address,client);self.RequestHandlerClass=FixtureHandler


if __name__ == "__main__":
    root = Path(tempfile.mkdtemp(prefix="workbench-browser-v7-")).resolve()
    data = root / "data"; data.mkdir(mode=0o700)
    chosen = root / "selected-workspace"; chosen.mkdir()
    accounts = FixtureAccounts()
    registry = ModelRegistry(data / "workbench.sqlite3")
    model_server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel)
    threading.Thread(target=model_server.serve_forever, daemon=True).start()
    model_base_url = f"http://127.0.0.1:{model_server.server_port}/v1"
    entries_file = root / "vault-metadata.json"
    entries_file.write_text(json.dumps([{"entryId": "fixture-archived-entry", "revision": 1}]))
    command = root / "fake-vault.sh"
    command.write_text("#!/bin/sh\nif [ \"$1\" = status ]; then echo '{\"ready\":true,\"masterKeyAvailable\":true}'; elif [ \"$1\" = list ]; then cat " + shlex.quote(str(entries_file)) + "; else exit 64; fi\n")
    command.chmod(0o700)
    credentials = CredentialCatalog(data / "workbench.sqlite3", command, poll_seconds=0)
    vault = FixtureKeyVault(entries_file)
    def fixture_probe(model, cancel):
        if model["base_url"].rstrip("/") not in {model_base_url,*[model_base_url+suffix for suffix in ("/chat/completions","/responses","/rerank","/embeddings","/audio/speech","/audio/transcriptions")]}:
            return {"success": False, "code": "fixture_target", "message": "测试页面只允许本机合成模型地址"}
        return probe_model(model, token=vault.values.get(model.get("credential_ref")), cancel_event=cancel)
    board = Workbench(data, root / "resources", account_service=accounts, model_registry=registry, model_probe=fixture_probe, key_vault=vault, enable_model_validation=True, credential_catalog=credentials, credential_vault=FixturePayloadVault(entries_file), ui_source_mode=True)
    accounts.store = board.store
    epoch_file=root/'ui-epoch';epoch_file.write_text('0');accounts.epoch_file=epoch_file;board.ui_release=FixtureUiRelease(epoch_file)
    server = FixturePreviewServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 18742), board)
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "root": str(root), "modelBaseUrl": model_base_url, "uiEpochFile":str(epoch_file)}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close(); board.close(); model_server.shutdown(); model_server.server_close()
