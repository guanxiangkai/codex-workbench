"""官方 CLI 断连恢复验收；合成模型主动断连，无用户认证或真实模型调用。"""
import asyncio,json,secrets,tempfile,threading,sys
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).parent/'src'))
sys.path.insert(0,str(Path(__file__).parent/'tests'))
from codex_workbench.model_gateway import ModelGateway,Upstream,Authorization
from codex_workbench.gateway_routes import RouteStore,AccountTarget
from native_protocol_probe import LocalModel,ProbeRpc

async def scenario(failure,retries):
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);home=root/'home';home.mkdir();state=root/'state';state.mkdir()
        model=LocalModel('A',[failure]);token=secrets.token_urlsafe(32)
        (home/'config.toml').write_text('[model_providers.probe]\nrequest_max_retries = '+str(retries[0])+'\nstream_max_retries = '+str(retries[1])+'\n[model_providers.probe.http_headers]\nAuthorization = '+json.dumps('Bearer '+token)+'\n')
        accounts=lambda key=None:AccountTarget('a','synthetic-a')
        gateway=ModelGateway(RouteStore(root/'routes'),accounts,{'a':Upstream(model.url,lambda:Authorization('synthetic-a'))},token)
        thread=threading.Thread(target=gateway.serve_forever,daemon=True);thread.start();rpc=ProbeRpc()
        try:
            await rpc.start(Path('/Applications/ChatGPT.app/Contents/Resources/codex'),home,state,gateway)
            tid=(await rpc.call('thread/start',{'cwd':str(root),'model':'routing-probe'}))['thread']['id']
            status=await rpc.turn(tid)
            return {'fault':failure,'retry_policy':list(retries),'status':status,'upstream_calls':model.calls,'account_unchanged':gateway.routes.lookup(tid)['account_id']=='a','tools_executed':rpc.rejected_tools}
        finally:
            await rpc.close();gateway.shutdown();gateway.server_close();thread.join(2);model.close()

async def main():
    results=[]
    for failure,retries in [('reset',(0,0)),('reset',(4,5)),('stream',(4,5))]:
        value=await scenario(failure,retries);results.append(value);print(json.dumps(value),flush=True)
    passed=results[0]['status']!='completed' and all(x['status']=='completed' and x['upstream_calls']==2 and x['account_unchanged'] for x in results[1:])
    print(json.dumps({'passed':passed}));return passed
if __name__=='__main__':raise SystemExit(0 if asyncio.run(main()) else 1)
