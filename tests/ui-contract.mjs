import {readFileSync} from 'node:fs';
import {strict as assert} from 'node:assert';
import {webcrypto} from 'node:crypto';
import {runInNewContext} from 'node:vm';

const source=readFileSync(new URL('../ui/app.html',import.meta.url),'utf8');
const script=source.match(/<script>([\s\S]*)<\/script>/)?.[1];
assert.ok(script);
const plain=value=>JSON.parse(JSON.stringify(value));
function boot(page='agents',embedded=false,fetchImpl=async()=>({ok:true,json:async()=>({structuredContent:{}})}),navigation=null){
 const target={innerHTML:''};
 const document={activeElement:null,hidden:false,title:'',querySelector:s=>s==='#app'?target:null,querySelectorAll:()=>[],getElementById:()=>null,addEventListener(){},createElement:()=>({setAttribute(){},remove(){}}),body:{appendChild(){}}};
 const window={addEventListener(){},open:()=>({}),innerWidth:1440,innerHeight:900,...(navigation?{__workbenchNavigation:navigation}:{})},parent=embedded?{postMessage(){}}:window;
 runInNewContext(script.replace('__WORKBENCH_INITIAL_PAGE__',page),{window,parent,document,navigator:{clipboard:{writeText:async()=>{}}},TextEncoder,FormData:class {},crypto:webcrypto,setTimeout:()=>0,clearTimeout(){},btoa:v=>Buffer.from(v,'binary').toString('base64'),atob:v=>Buffer.from(v,'base64').toString('binary'),fetch:fetchImpl});
 return {target,api:window.__workbenchTest};
}
for(const tool of ['workbench_state','agent_create','agent_update','model_list','model_key_prepare','model_create','model_update','model_validate','model_pause','credential_list','credential_folder_create','credential_update','credential_key_prepare','credential_create','account_create','account_login','account_status','account_default','account_rename'])assert.match(script,new RegExp("['\\\"]"+tool+"['\\\"]"),'missing '+tool);
for(const protocol of ['ui/initialize','ui/notifications/initialized','tools/call','ui/open-link'])assert.ok(script.includes("'"+protocol+"'"));
assert.match(script,/appInfo:\{name:'codex-workbench-ui',version:'0\.9\.1'\}/);assert.match(script,/protocolVersion:'2026-01-26'/);
assert.match(script,/document\.hidden/);assert.match(script,/openTopPopover\(\)/);assert.match(script,/revision!==loadRevision/);

const {api}=boot();assert.ok(api);
assert.equal(api.normalizePage('agents'),'agents');assert.equal(api.normalizePage('accounts'),'accounts');assert.equal(api.normalizePage('board'),'board');assert.equal(api.normalizePage('unknown'),'board');assert.equal(api.getState().page,'agents');
assert.deepEqual([...api.nav().matchAll(/data-page="([^"]+)"/g)].map(match=>match[1]),['board','agents','accounts']);
assert.match(api.agents(),/技能助手/);assert.match(api.accounts(),/凭证中心/);

api.setState({selected:null,providerModels:[],agents:[{id:'ag1',name:'助手',description:'说明'}]});
const agentForm=api.agentForm(null),agentEditForm=api.agentForm({id:'ag1',name:'助手',description:'说明',directory:'/absolute/private/repo'});
for(const text of ['agent-dialog','dialog-icon','name="name"','textarea name="description"','agent-model-control','required-mark'])assert.match(agentForm,new RegExp(text));assert.doesNotMatch(agentEditForm,/配置仓库目录|absolute\/private\/repo|资源文件|新建文本资源|data-resource-read|data-agent-directory/);assert.deepEqual([...api.agents().matchAll(/data-agents-tab="([^"]+)"/g)].map(match=>match[1]),['assistants','models']);assert.match(api.agents(),/助手/);

const folders=[{id:'f1',name:'工作',parent_id:null},{id:'f2',name:'模型',parent_id:'f1'}],entries=[{id:'e1',title:'推理凭证',folder_id:'f2',tags:['生产'],kind:'credential',structure_status:'indexed',field_types:{description:'string',password:'string',endpoint:'string',key:'string'},has_fields:['description','password','endpoint','key'],updated_at:3}];
api.setState({credentialFolders:folders,credentials:entries,credentialFolderFilter:'all',credentialQuery:'',selected:null});
const credentialCreate=api.credentialForm(null),credentialEdit=api.credentialForm({id:'e1',title:'已有',folder_id:'f2',tags:['生产']});
for(const text of ['dialog-icon','credential-dialog','required-mark','name="title"','name="description"','name="account"','name="password"','name="ip"','name="remote_path"','name="port"','name="endpoint"','name="key"'])assert.match(credentialCreate,new RegExp(text));for(const removedField of ['name="description"','name="password"','name="key"'])assert.doesNotMatch(credentialEdit,new RegExp(removedField));
assert.deepEqual(plain(api.credentialUpdatePayload({title:'改名',folder_id:'f2',tags:'生产、AI'})),{title:'改名',folder_id:'f2',tags:['生产','AI']});
const entryHtml=api.credentialEntryCard(entries[0]);for(const text of ['推理凭证','工作 / 模型','说明','密码','接口 / URL','Key','编辑条目'])assert.match(entryHtml,new RegExp(text));assert.doesNotMatch(entryHtml,/reference|secret-value/);assert.deepEqual(plain(api.credentialFolderOptions()).map(item=>item.name),['根目录','工作','工作 / 模型']);
const formatCards={
 indexed:{id:'indexed',title:'字段契约',kind:'credential',structure_status:'indexed',has_fields:['port','private-name-value','__proto__'],field_types:{port:'integer'},base_urls:['https://private-value.invalid']},
 unindexed:{id:'unindexed',title:'未探测',kind:'api_key',has_fields:['key'],structure_status:'unindexed'},
 text:{id:'text',title:'原文',structure_status:'unrecognized',structure_format:'text',format_status:'known'},
 json:{id:'json',title:'记录',structure_status:'unrecognized',structure_format:'json_object',format_status:'known'},
 stale:{id:'stale',title:'已变化',structure_status:'stale',structure_format:'text',format_status:'unavailable'},
 error:{id:'error',title:'错误',structure_status:'error'}
};
const indexed=api.credentialEntryCard(formatCards.indexed);assert.match(indexed,/端口 · 整数/);assert.doesNotMatch(indexed,/private-name-value|private-value.invalid|__proto__/);
const unindexed=api.credentialEntryCard(formatCards.unindexed);assert.match(unindexed,/类型未识别/);assert.match(unindexed,/字段信息待识别/);assert.doesNotMatch(unindexed,/API Key|已加密字段/);
const textCard=api.credentialEntryCard(formatCards.text);assert.match(textCard,/加密文本/);assert.doesNotMatch(textCard,/内容格式未识别|字段待映射/);
const jsonCard=api.credentialEntryCard(formatCards.json);assert.match(jsonCard,/结构化记录/);assert.match(jsonCard,/字段待映射/);
assert.match(api.credentialEntryCard(formatCards.stale),/内容已变化，字段待更新/);assert.match(api.credentialEntryCard(formatCards.error),/字段结构校验失败/);
api.setState({credentials:[{id:'root',title:'根项',tags:['公共']},{id:'child',title:'子项',folder_id:'f2',tags:['生产']},{id:'other',title:'其他',folder_id:'f1',tags:['测试']}],credentialFolderFilter:'f1',credentialQuery:''});assert.deepEqual(plain(api.credentialVisibleEntries()).map(entry=>entry.id),['child','other']);api.setState({credentialFolderFilter:'root'});assert.deepEqual(plain(api.credentialVisibleEntries()).map(entry=>entry.id),['root']);api.setState({credentialFolderFilter:'all',credentialQuery:'生产',credentialTab:'vault'});assert.deepEqual(plain(api.credentialVisibleEntries()).map(entry=>entry.id),['child']);assert.deepEqual([...api.accounts().matchAll(/data-credential-tab="([^"]+)"/g)].map(match=>match[1]),['accounts','vault']);assert.match(api.credentialVault(),/搜索标题或标签/);
assert.doesNotMatch(script,/credentialOptionsFor|model-credential|raw\.credential_id/);assert.match(script,/credential_key_prepare',\{title\}|payload_envelope:envelope/);

const keyPair=await webcrypto.subtle.generateKey({name:'RSA-OAEP',modulusLength:2048,publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['wrapKey','unwrapKey']);
const publicKey=Buffer.from(await webcrypto.subtle.exportKey('spki',keyPair.publicKey)).toString('base64');
const secretText=api.credentialPayloadText({description:'中文说明',account:'sample-user',password:'secret',ip:'192.0.2.10',remote_path:'/srv',port:'443',endpoint:'https://api.example.invalid',key:'key-value'});const envelope=plain(await api.createKeyEnvelope(secretText,{id:'session-1',public_key:publicKey}));assert.ok(!JSON.stringify(envelope).includes('secret'));
const aes=await webcrypto.subtle.unwrapKey('raw',Buffer.from(envelope.wrapped_key,'base64'),keyPair.privateKey,{name:'RSA-OAEP'},{name:'AES-GCM',length:256},false,['decrypt']);const decrypted=await webcrypto.subtle.decrypt({name:'AES-GCM',iv:Buffer.from(envelope.iv,'base64'),additionalData:new TextEncoder().encode('session-1')},aes,Buffer.from(envelope.ciphertext,'base64'));assert.deepEqual(JSON.parse(new TextDecoder().decode(decrypted)),JSON.parse(secretText));
assert.equal(api.credentialPayloadText({}),'{}');assert.equal(api.credentialSecretPayload({port:'443'}).port,443);assert.throws(()=>api.credentialSecretPayload({port:'0'}),/1 到 65535/);

assert.deepEqual(plain(api.modelPayload({name:'ranker',model_type:'rerank',base_url:'https://rank.invalid',credential_id:'drop'})),{name:'ranker',model_type:'rerank',base_url:'https://rank.invalid'});const modelForm=api.modelForm(null);for(const text of ['model-dialog','模型名称','API 地址','type="password" name="key"'])assert.match(modelForm,new RegExp(text));for(const type of ['文本推理','多模态','语音识别','语音合成','向量','重排序'])assert.match(modelForm,new RegExp(type));assert.match(api.modelForm({id:'voice',model_type:'text_to_speech',voice:'nova'}),/name="voice" value="nova"/);
for(const [status,label] of [['pending','待验证'],['validating','验证中'],['verified','通过'],['failed','失败'],['paused','已暂停']])assert.match(api.providerModelRow({id:status,name:status,model_type:'reasoning',base_url:'https://models.invalid',validation_status:status}),new RegExp(label));
assert.match(api.providerModelRow({id:'failed',name:'失败模型',model_type:'rerank',base_url:'https://models.invalid',validation_status:'failed',last_error:'bad key'}),/请手动验证/);assert.match(api.providerModelRow({id:'failed',name:'失败模型',model_type:'rerank',base_url:'https://models.invalid',validation_status:'failed'}),/data-model-validate="failed"/);assert.match(api.providerModelRow({id:'verified',name:'已验证模型',model_type:'rerank',base_url:'https://models.invalid',validation_status:'verified'}),/立即验证/);assert.match(api.providerModelRow({id:'paused',name:'暂停模型',model_type:'rerank',base_url:'https://models.invalid',validation_status:'paused'}),/恢复/);
api.setState({providerModels:[{id:'verified',name:'可用',model_type:'reasoning',base_url:'https://one.invalid',validation_status:'verified'},{id:'failed',name:'失败',model_type:'embedding',base_url:'https://two.invalid',validation_status:'failed'}]});assert.deepEqual(plain(api.verifiedModelTree()).map(group=>group.type),['reasoning']);assert.deepEqual(plain(api.providerModelItems({items:[{id:'wrapped'}]})),[{id:'wrapped'}]);
assert.equal(api.accountDisplayName({display_name:'自定义别名',username:'official-user',source:'official'}),'自定义别名');assert.equal(api.accountDisplayName({display_name:'当前 Codex 账户',username:'official-user',source:'official'}),'official-user');assert.equal(api.accountDisplayName({display_name:'当前Codex账户',email:'sample@example.invalid'}),'Codex');for(const [status,label] of [['expired','授权已过期'],['failed','登录失败'],['identity_mismatch','账户不匹配']])assert.equal(api.loginText(status),label);
api.setState({accounts:[{id:'current',display_name:'当前 Codex 账户',expected_email:'sample@example.invalid',username:'official-user',source:'official',isCurrent:true,status:'pending',usage:{plan:'pro',limits:{codex:{primary:{remainingPercent:72}}}}},{id:'default',display_name:'默认账户',status:'ready',isDefault:true}],current_account_id:'current'});const currentCard=api.accountCard(api.currentAccount());for(const text of ['official-user','sample@example.invalid','>当前<','等待授权','Pro','额度剩余','72%'])assert.match(currentCard,new RegExp(text));assert.doesNotMatch(currentCard,/当前 Codex 账户<\/span>/);assert.match(api.accountCard(api.getState().accounts[1]),/默认执行账户/);const accountForm=api.accountForm();for(const text of ['添加 Codex 账户','name="name"','账户名称','account-create-dialog'])assert.match(accountForm,new RegExp(text));assert.doesNotMatch(accountForm,/账户类型|GitHub|username|网页登录/);

for(const embedded of [false,true])for(const page of ['agents','accounts']){const {target}=boot(page,embedded);assert.equal(target.innerHTML.includes('page-links'),!embedded)}
for(const page of ['board','unknown'])assert.equal(boot(page).api.getState().page,'board');
assert.equal(boot('agents',false,undefined,{page:'board'}).api.getState().page,'board');
assert.equal(boot('accounts',true,undefined,{page:'board'}).api.getState().page,'accounts','embedded explicit accounts page must remain locked');
const fetchCalls=[];boot('agents',false,async(...args)=>{fetchCalls.push(args);return {ok:true,json:async()=>({structuredContent:{}})}});await new Promise(resolve=>setImmediate(resolve));assert.equal(fetchCalls[0]?.[0],'/rpc');assert.equal(fetchCalls[0]?.[1]?.headers?.['X-Workbench-Request'],'1');
assert.match(script,/role="dialog" aria-modal="true"|isVisibleControl|expected_sha256/);assert.match(source,/\.notice\{z-index:300\}|\.required-mark\{color:#c83b3b/);assert.match(script,/refreshProviderModels\(false\).*openSelection\('agent'/s);assert.match(source,/body:has\(\.modal\)\{overflow:hidden/);
console.log('Skills assistants and accounts/credentials navigation, form, encryption, and board action contracts passed');
{
const api=boot('board').api;
assert.equal(api.normalizePage('board'),'board');assert.equal(api.normalizePage('unknown'),'board');
assert.deepEqual([...api.nav().matchAll(/data-page="([^"]+)"/g)].map(m=>m[1]),['board','agents','accounts']);
api.applySnapshot({sections:[{id:'s',name:'分区'}],projects:[{id:'p',name:'项目',section_id:'s'}],sessions:[{id:'c',title:'会话',project_id:'p',section_id:'s'}],tasks:[{id:'backlog',state:'backlog',title:'待办',session_id:'c'},{id:'ready',state:'ready',title:'待执行',session_id:'c'},{id:'running',state:'running',title:'运行中',session_id:'c'},{id:'done',state:'done',title:'完成',session_id:'c',last_run:{state:'failed',error:'失败原因'}},{id:'archived',state:'archived',title:'归档',session_id:'c'}],accounts:[],agents:[],models:[]});
assert.deepEqual(JSON.parse(JSON.stringify(api.taskCounts(api.filteredTasks()))),{backlog:1,ready:1,running:1,done:1,archived:1});
assert.match(api.board(),/待处理/);assert.match(api.board(),/执行中/);assert.doesNotMatch(api.board(),/待审核|失败<\/b>|已取消<\/b>/);assert.match(api.board(),/失败：失败原因/);
assert.match(api.boardSelectors(),/name="section_filter"/);assert.match(api.taskForm(null),/name="session_id"/);const detail={...api.getState().tasks[3],runs:[{state:'failed',error:'失败原因',duration_ms:1234,input_tokens:2,output_tokens:3,cached_input_tokens:1,total_tokens:5,native_url:'codex://threads/run',location_marker:'turn-2'}],metrics:{duration_ms:1234,input_tokens:2,output_tokens:3,cached_input_tokens:1,total_tokens:5}};const detailHtml=api.taskDetailView(detail);for(const text of ['失败原因','累计运行指标','运行记录','1.2 秒','总 Token','定位标记','打开 Codex 会话'])assert.match(detailHtml,new RegExp(text));
console.log('Board UI contract passed');

}

{
 const {api}=boot('board');api.setState({accounts:[{id:'a',name:'默认执行',isDefault:true,login:{status:'ready'}},{id:'b',name:'会话账户',login:{status:'ready'}}],taskDefaults:{model:'m1',effort:'high'},models:[{id:'m1',model:'m1',supportedReasoningEfforts:[{reasoningEffort:'high'}]},{id:'m2',model:'m2',supportedReasoningEfforts:[{reasoningEffort:'medium'}]}],sessions:[{id:'s1',title:'会话',model:'m2',effort:'medium',execution_account_id:'b'}],selected:null,sessionFilter:''});
 assert.match(api.taskForm(null),/name="execution_account_id"[^>]*value="a"/);
 api.setState({sessionFilter:'s1'});const html=api.taskForm(null);assert.match(html,/name="execution_account_id"[^>]*value="b"/);assert.match(html,/name="model"[^>]*value="m2"/);assert.match(html,/name="effort"[^>]*value="medium"/);
}

{
 const {api}=boot('board'),sources=[];api.setState({tasks:[]});const counts={backlog:0,ready:0,running:0,done:0,archived:0};
 for(const state of Object.keys(counts)){const html=api.taskColumn(state,state,[],counts);assert.match(html,new RegExp('data-status-icon="'+state+'"'));const match=html.match(/src="(data:image\/svg\+xml[^"]+)"/);assert.ok(match);sources.push(match[1])}
 assert.equal(new Set(sources).size,5,'each task status must have its own icon');
}
