import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import {webcrypto} from 'node:crypto';
const source=fs.readFileSync(new URL('../ui/readonly.js',import.meta.url),'utf8').replace('window.__workbenchReadonlyTest={','window.__fieldTest={detail,readSecret,s};window.__workbenchReadonlyTest={');
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
function boot({embedded=false,bootstrap=null,page='agents',fieldPayload=null}={}){
 const events={},docEvents={},controls=new Map(),requests=[],messages=[],timers=new Map();let timerId=0;
 let html='',renders=0;const root={get innerHTML(){return html;},set innerHTML(value){html=value;renders++;},get renders(){return renders;}};
 const document={hidden:false,activeElement:null,addEventListener:(k,v)=>docEvents[k]=v,querySelectorAll:()=>[],getElementById(id){if(id==='app')return root;if(!root.innerHTML.includes(`id="${id}"`))return null;if(!controls.has(id))controls.set(id,{addEventListener(k,v){this[k]=v;},focus(){}});return controls.get(id);}};
 const window={addEventListener:(k,v)=>events[k]=v};const parent=embedded?{postMessage:m=>messages.push(m)}:window;window.parent=parent;
 const context={window,parent,document,INITIAL_PAGE:page,WORKBENCH_MODULES:[{id:'assets',name:'资源中心',group:'能力与知识'},{id:'agents',name:'技能助手',group:'能力与知识'},{id:'board',name:'执行记录',group:'工作'},{id:'models',name:'模型目录',group:'能力与知识'},{id:'config',name:'配置中心',group:'账户与配置'},{id:'other_accounts',name:'其他账户',group:'账户与配置'}],READONLY_ICONS:{},ConfigurationCrypto:{async prepare(entry){return {arguments:{id:entry.id},privateKey:{}};},async decrypt(){return fieldPayload;}},WORKBENCH_BOOTSTRAP:bootstrap,URL,console,AbortController,DOMException,crypto:webcrypto,history:{replaceState(){}},setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),fetch:(url,options)=>new Promise((resolve,reject)=>{const r={url,options,resolve:body=>resolve({ok:true,json:async()=>body}),response:resolve,reject};requests.push(r);options.signal?.addEventListener('abort',()=>reject(options.signal.reason||new DOMException('Aborted','AbortError')),{once:true});})};
 vm.runInNewContext(source,context);
 return {fields:window.__fieldTest,root,requests,messages,timers,controls,async visibility(hidden){document.hidden=hidden;docEvents.visibilitychange();await flush();},async event(name,args={}){events[name]?.(args);await flush();},async hostResult(result){await this.event('message',{source:parent,data:{jsonrpc:'2.0',method:'ui/notifications/tool-result',params:result}});},async reply(message,result){await this.event('message',{source:parent,data:{jsonrpc:'2.0',id:message.id,result}});},async expire(){for(const [id,{fn}] of [...timers]){timers.delete(id);fn();}await flush();}};
}
const data=name=>({context:'test-account',revision:name,reset:true,unchanged:false,data:{skills:[{id:name,name,enabled:true}]}});
// 隐藏窗口不应丢弃公共目录读取，返回时复用尚在执行的请求。
const hidden=boot();await flush();assert.match(hidden.root.innerHTML,/正在读取/);
await hidden.visibility(true);hidden.requests[0].resolve(data('hidden-result'));await flush();
assert.match(hidden.root.innerHTML,/hidden-result/);assert.doesNotMatch(hidden.root.innerHTML,/正在读取/);
await hidden.visibility(false);assert.equal(hidden.requests.length,2);
await hidden.visibility(true);await hidden.visibility(false);assert.equal(hidden.requests.length,2);
hidden.requests[1].resolve(data('fresh'));await flush();assert.match(hidden.root.innerHTML,/fresh/);
assert.doesNotMatch(hidden.root.innerHTML,/>只读</);
// 离开页面时请求取消并解除忙状态；从页面缓存恢复后可重新读取。
const restored=boot();await flush();await restored.event('pagehide');assert.doesNotMatch(restored.root.innerHTML,/aria-busy="true"/);
await restored.event('pageshow',{persisted:true});assert.equal(restored.requests.length,2);restored.requests[1].resolve(data('restored'));await flush();assert.match(restored.root.innerHTML,/restored/);
// 超时结束加载，显示重试；重试成功可以恢复。
const stalled=boot();await flush();await stalled.expire();assert.doesNotMatch(stalled.root.innerHTML,/正在读取/);assert.match(stalled.root.innerHTML,/超时/);
stalled.controls.get('retry').click();await flush();stalled.requests.at(-1).resolve(data('retry-ok'));await flush();assert.match(stalled.root.innerHTML,/retry-ok/);
// 乱序响应不能覆盖新请求；响应体挂起也受超时约束。
const stale=boot();await flush();await stale.event('pagehide');await stale.event('pageshow',{persisted:true});stale.requests[1].resolve(data('latest'));stale.requests[0].resolve(data('obsolete'));await flush();assert.match(stale.root.innerHTML,/latest/);assert.doesNotMatch(stale.root.innerHTML,/obsolete/);
const bodyStall=boot();await flush();bodyStall.requests[0].response({ok:true,json:()=>new Promise(()=>{})});await flush();await bodyStall.expire();assert.match(bodyStall.root.innerHTML,/超时/);assert.doesNotMatch(bodyStall.root.innerHTML,/aria-busy="true"/);
const host=boot({embedded:true});await flush();await host.reply(host.messages[0],{});const call=host.messages.find(x=>x.method==='tools/call');assert(call);
await host.visibility(true);await host.reply(call,data('host-result'));assert.match(host.root.innerHTML,/host-result/);
await host.visibility(false);await host.expire();assert.match(host.root.innerHTML,/超时/);assert.doesNotMatch(host.root.innerHTML,/aria-busy="true"/);
console.log('UI 加载：隐藏/返回、页面恢复、超时重试、乱序响应、宿主桥接通过');
assert.equal(host.timers.size,0);assert.equal(stalled.timers.size,0);assert.equal(bodyStall.timers.size,0);

const cached=boot();await flush();cached.requests[0].resolve(data('cached-result'));await flush();const rendered=cached.root.renders;
await cached.visibility(true);await cached.visibility(false);assert.match(cached.root.innerHTML,/cached-result/);assert.doesNotMatch(cached.root.innerHTML,/正在读取/);
const request=JSON.parse(cached.requests[1].options.body);assert.equal(request.name,'workbench_sync');assert.equal(request.arguments.revision,'cached-result');
cached.requests[1].resolve({context:'test-account',revision:'cached-result',unchanged:true});await flush();assert.equal(cached.root.renders,rendered);
await cached.visibility(true);await cached.visibility(false);assert.equal(JSON.parse(cached.requests.at(-1).options.body).arguments.refresh,false);
cached.requests.at(-1).resolve({context:'another-account',revision:'another',reset:true,data:{skills:[]}});await flush();assert.doesNotMatch(cached.root.innerHTML,/cached-result/);
console.log('页面缓存：保留可见数据、携带版本、不重建未变 DOM、自动同步、换号清理通过');

const otherAccounts=boot({page:'other_accounts'});await flush();assert.equal(JSON.parse(otherAccounts.requests[0].options.body).arguments.refresh,true);otherAccounts.requests[0].resolve({context:'other-account',revision:'other-r1',reset:true,data:{view:'other_accounts',accounts:[]}});await flush();assert.doesNotMatch(otherAccounts.root.innerHTML,/正在更新用量/);await otherAccounts.visibility(true);await otherAccounts.visibility(false);assert.equal(JSON.parse(otherAccounts.requests.at(-1).options.body).arguments.refresh,true);assert.match(otherAccounts.root.innerHTML,/正在更新用量/);otherAccounts.requests.at(-1).resolve({context:'other-account',revision:'other-r1',unchanged:true});await flush();assert.doesNotMatch(otherAccounts.root.innerHTML,/正在更新用量/);
console.log('其他账户：进入与可见恢复显式刷新用量，缓存展示和刷新状态清理通过');

// 原生入口会主动推送首屏结果，即使页面自主 tools/call 仍在等待也必须显示。
const pushed=boot({embedded:true});await flush();await pushed.reply(pushed.messages[0],{});assert.match(pushed.root.innerHTML,/正在读取/);
await pushed.hostResult({structuredContent:{view:'agents',skills:[{id:'host-initial',name:'Host initial'}],_sync:{context:'test-account',revision:'initial-revision'}}});
assert.match(pushed.root.innerHTML,/Host initial/);assert.doesNotMatch(pushed.root.innerHTML,/正在读取/);assert.equal(pushed.timers.size,0);
await pushed.visibility(true);await pushed.visibility(false);const subsequent=pushed.messages.filter(x=>x.method==='tools/call').at(-1);assert.equal(subsequent.params.arguments.revision,'initial-revision');
await pushed.reply(subsequent,{structuredContent:{context:'test-account',revision:'initial-revision',unchanged:true}});assert.match(pushed.root.innerHTML,/Host initial/);
console.log('原生首屏：主动推送解除加载、取消重复请求、携带首屏版本增量同步通过');

// 原生 HTML 首屏立即可见；所有后续读取等待握手且只走宿主工具通道。
const bootstrap={context:'bootstrap-account',views:[{args:{view:'agents'},revision:'bootstrap-r1',data:{view:'agents',skills:[{id:'bootstrap-skill',name:'首屏技能',enabled:true}]}}]};
const native=boot({embedded:true,bootstrap});
assert.match(native.root.innerHTML,/首屏技能/);assert.doesNotMatch(native.root.innerHTML,/正在读取/);await flush();
assert.equal(native.requests.length,0);assert.equal(native.messages.filter(x=>x.method==='ui/initialize').length,1);assert(!native.messages.some(x=>x.method==='tools/call'));
await native.reply(native.messages[0],{});
const nativeCall=native.messages.find(x=>x.method==='tools/call');assert(nativeCall);assert.equal(nativeCall.params.arguments.revision,'bootstrap-r1');
assert(native.messages.findIndex(x=>x.method==='ui/notifications/initialized')<native.messages.indexOf(nativeCall));
await native.reply(nativeCall,{structuredContent:{context:'bootstrap-account',revision:'bootstrap-r1',unchanged:true}});
assert.match(native.root.innerHTML,/首屏技能/);assert.equal(native.timers.size,0);assert.equal(native.requests.length,0);
// 宿主失联时保留快照，手动重试重新握手；不尝试被原生沙箱阻止的 HTTP。
const handshake=boot({embedded:true,bootstrap});await flush();await handshake.expire();assert.match(handshake.root.innerHTML,/首屏技能/);assert.match(handshake.root.innerHTML,/超时/);
handshake.controls.get('retry').click();await flush();assert.equal(handshake.messages.filter(x=>x.method==='ui/initialize').length,2);
await handshake.reply(handshake.messages.at(-1),{});await handshake.reply(handshake.messages.at(-1),{structuredContent:{context:'bootstrap-account',revision:'bootstrap-r2',reset:true,data:{skills:[{id:'reconnected',name:'重新连接成功'}]}}});
assert.match(handshake.root.innerHTML,/重新连接成功/);assert.doesNotMatch(handshake.root.innerHTML,/id="retry"/);assert.equal(handshake.requests.length,0);
const cold=boot({embedded:true,bootstrap:{views:[],error:'首屏数据暂时不可用，请点击刷新读取'}});assert.match(cold.root.innerHTML,/首屏数据暂时不可用/);assert.doesNotMatch(cold.root.innerHTML,/正在读取/);
console.log('原生首屏：缓存即时显示、单次握手、宿主增量同步、失联手动恢复、无 HTTP 请求通过');

const boardFirst=boot({page:'board',bootstrap:{context:'board-account',views:[{args:{view:'board'},revision:'board-r1',data:{view:'board',selected_session_id:'session-1',tasks:[],sessions:[],projects:[],sections:[]}}]}});
await flush();assert.doesNotMatch(boardFirst.root.innerHTML,/读取中|正在读取/);assert.equal(JSON.parse(boardFirst.requests[0].options.body).arguments.revision,'board-r1');boardFirst.requests[0].resolve({context:'board-account',revision:'board-r1',unchanged:true});await flush();assert.doesNotMatch(boardFirst.root.innerHTML,/读取中|正在读取/);
console.log('执行首屏：默认会话与明确会话共享缓存，后台核对不显示读取中通过');

// 浏览器首次失败和缓存刷新失败都能手动恢复，不重复发起后台重试。
const network=boot();await flush();network.requests[0].reject(new TypeError('Failed to fetch'));await flush();
assert.match(network.root.innerHTML,/暂时无法连接本机工作台/);assert.doesNotMatch(network.root.innerHTML,/Failed to fetch|正在读取/);assert.equal(network.requests.length,1);
network.controls.get('retry').click();await flush();network.requests.at(-1).resolve(data('network-restored'));await flush();
assert.match(network.root.innerHTML,/network-restored/);assert.doesNotMatch(network.root.innerHTML,/暂时无法连接|id="retry"/);
await network.visibility(true);await network.visibility(false);network.requests.at(-1).reject(new TypeError('Failed to fetch'));await flush();
assert.match(network.root.innerHTML,/network-restored/);assert.match(network.root.innerHTML,/暂时无法连接本机工作台/);assert.equal(network.requests.length,3);
network.controls.get('retry').click();await flush();network.requests.at(-1).resolve(data('latest-network-data'));await flush();
assert.match(network.root.innerHTML,/latest-network-data/);assert.doesNotMatch(network.root.innerHTML,/暂时无法连接|id="retry"/);assert.equal(network.timers.size,0);
console.log('浏览器网络恢复：中文错误、缓存保留、手动重试更新、不自动轮询通过');

// 详情自动展开只读取一次；普通字段可见，Key 不进入 DOM，离页清理明文。
const fields=boot({page:'models',fieldPayload:{format:'json',fields:{type:'api-key',authentication_tested:false,endpoint:'https://example.invalid/v1/chat/completions',api_key:'synthetic-secret-do-not-display'}}});
await flush();fields.requests[0].resolve({context:'test-account',revision:'fields-r1',reset:true,data:{models:[]}});await flush();
const opening=fields.fields.detail('model','model-1');await flush();
fields.requests.at(-1).resolve({model:{id:'model-1',name:'Synthetic model'},credential:{id:'entry-1'}});await flush();
assert.equal(JSON.parse(fields.requests.at(-1).options.body).name,'credential_details');
fields.requests.at(-1).resolve({});await opening;
assert.match(fields.root.innerHTML,/api-key/);assert.match(fields.root.innerHTML,/>否</);
assert.doesNotMatch(fields.root.innerHTML,/synthetic-secret-do-not-display/);
assert.match(fields.root.innerHTML,/data-reveal="3"/);assert.doesNotMatch(fields.root.innerHTML,/data-reveal="0"/);
const reads=fields.requests.length;await fields.visibility(true);assert.equal(fields.fields.s.revealed.size,0);assert.doesNotMatch(fields.root.innerHTML,/api-key/);assert.equal(fields.requests.length,reads);
// 旧条目在后台解密完成，不应重新显示；失败保留手动重试。
const again=fields.fields.readSecret('fields');await flush();await fields.visibility(true);fields.requests.at(-1).resolve({});await again;assert.equal(fields.fields.s.revealed.size,0);
await fields.visibility(false);const failing=fields.fields.readSecret('fields');await flush();fields.requests.at(-1).reject(new TypeError('synthetic network failure'));await failing;assert.equal(fields.fields.s.fieldBusy,false);assert.equal(fields.fields.s.detail.fieldsState,'error');
console.log('配置默认展开：一次读取、普通字段直显、秘密遮挡、离页与过期清理、错误恢复通过');

// 默认汇总；单会话切回全部必须清除查询，不能沿用旧会话。
const allBoard=boot({page:'board'});await flush();
assert.equal(JSON.parse(allBoard.requests[0].options.body).arguments.session_id,undefined);
allBoard.requests[0].resolve({context:'board-account',revision:'all-r1',reset:true,data:{view:'board',selected_session_id:null,tasks:[],sessions:[{id:'one',title:'One'},{id:'two',title:'Two'}],projects:[],sections:[]}});await flush();
assert.match(allBoard.root.innerHTML,/>全部会话</);
allBoard.controls.get('session').change({target:{value:'two'}});await flush();
assert.equal(JSON.parse(allBoard.requests.at(-1).options.body).arguments.session_id,'two');
allBoard.requests.at(-1).resolve({context:'board-account',revision:'one-r1',reset:true,data:{view:'board',selected_session_id:'two',tasks:[],sessions:[{id:'one'},{id:'two'}],projects:[],sections:[]}});await flush();
allBoard.controls.get('session').change({target:{value:''}});await flush();
assert.equal(JSON.parse(allBoard.requests.at(-1).options.body).arguments.session_id,undefined);
allBoard.requests.at(-1).resolve({context:'board-account',revision:'all-r1',unchanged:true});await flush();
allBoard.controls.get('section').change({target:{value:'__none__'}});await flush();
const filteredArgs=JSON.parse(allBoard.requests.at(-1).options.body).arguments;
assert.equal(filteredArgs.section_id,'__none__');assert.equal(filteredArgs.session_id,undefined);
console.log('全部会话：默认汇总、单会话切回全部、分区查询清除会话通过');

// 配置中心按用户选择直显全部字段，仍只在详情临时解密，离页清空。
const plain=boot({page:'config',fieldPayload:{format:'json',fields:{service:'fixture-service',target:'fixture-target',kind:'fixture-kind',password:'synthetic-visible-secret',enabled:false,port:0,nested:{value:'<script>synthetic</script>'}}}});
await flush();plain.requests[0].resolve({context:'test-account',revision:'config-r1',reset:true,data:{view:'config',entries:[{id:'fixture-entry',label:'Fixture'}],folders:[]}});await flush();
assert.match(plain.root.innerHTML,/config-card/);assert.doesNotMatch(plain.root.innerHTML,/synthetic-visible-secret/);
const plainOpen=plain.fields.detail('config','fixture-entry');await flush();assert.equal(JSON.parse(plain.requests.at(-1).options.body).name,'credential_details');plain.requests.at(-1).resolve({});await plainOpen;
for(const value of ['fixture-service','fixture-target','fixture-kind','synthetic-visible-secret','false','0','&lt;script&gt;synthetic&lt;\/script&gt;'])assert(plain.root.innerHTML.includes(value));
assert.doesNotMatch(plain.root.innerHTML,/data-reveal|data-copy|••••|<script>synthetic/);
await plain.visibility(true);assert.equal(plain.fields.s.revealed.size,0);assert.doesNotMatch(plain.root.innerHTML,/synthetic-visible-secret/);
console.log('配置字段：直显、精确值、无查看复制按钮、转义及离页清理通过');

// 资源点击后先展示目录信息；异步失败、重试和旧响应均不能造成空白或串项。
const resource={id:'asset-a',name:'示例图标',kind:'icon',skill_name:'网页设计',relative_path:'assets/sample.svg',size:128,updated_at:1};
const resources=boot({page:'assets'});await flush();
resources.requests[0].resolve({context:'test-account',revision:'assets-r1',reset:true,data:{view:'assets',assets:[resource],truncated:false}});await flush();
const resourceOpen=resources.fields.detail('asset',resource.id);await flush();
assert.match(resources.root.innerHTML,/资源信息/);assert.match(resources.root.innerHTML,/assets\/sample.svg/);assert.match(resources.root.innerHTML,/正在读取资源详情/);
resources.requests.at(-1).reject(new TypeError('Failed to fetch'));await resourceOpen;
assert.match(resources.root.innerHTML,/资源信息/);assert.match(resources.root.innerHTML,/id="retry-asset-detail"/);
resources.controls.get('retry-asset-detail').click();await flush();
resources.requests.at(-1).resolve({asset:resource,preview:{kind:'text',text:'<svg>可查看的源码</svg>'}});await flush();
assert.match(resources.root.innerHTML,/&lt;svg&gt;可查看的源码/);assert.doesNotMatch(resources.root.innerHTML,/正在读取资源详情|retry-asset-detail/);
const firstResource=resources.fields.detail('asset',resource.id);await flush();const oldRequest=resources.requests.at(-1);
const secondResource=resources.fields.detail('asset','asset-b');await flush();
resources.requests.at(-1).resolve({asset:{...resource,id:'asset-b',name:'第二项资源'},preview:{kind:'metadata'}});await secondResource;
oldRequest.resolve({asset:resource,preview:{kind:'text',text:'过期详情'}});await firstResource;
assert.match(resources.root.innerHTML,/第二项资源/);assert.doesNotMatch(resources.root.innerHTML,/过期详情/);
console.log('资源详情：点击即时显示、失败保留信息、原位重试与过期响应隔离通过');
