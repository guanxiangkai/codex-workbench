import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import {webcrypto} from 'node:crypto';
const primitives=fs.readFileSync(new URL('../ui/readonly-primitives.js',import.meta.url),'utf8');
const source=fs.readFileSync(new URL('../ui/readonly.js',import.meta.url),'utf8').replace('window.__workbenchReadonlyTest={','window.__fieldTest={detail,s,load,pageChange,updateProfile};window.__workbenchReadonlyTest={');
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
function boot({embedded=false,bootstrap=null,page='models',fieldPayload=null}={}){
 const events={},docEvents={},controls=new Map(),requests=[],messages=[],timers=new Map(),observers=[];let timerId=0;
 let html='',renders=0;const root={get innerHTML(){return html;},set innerHTML(value){html=value;renders++;},get renders(){return renders;}};
 const document={hidden:false,activeElement:null,addEventListener:(k,v)=>docEvents[k]=v,querySelectorAll:()=>[],getElementById(id){if(id==='app')return root;if(!root.innerHTML.includes(`id="${id}"`))return null;if(!controls.has(id))controls.set(id,{addEventListener(k,v){this[k]=v;},focus(){}});return controls.get(id);}};
 const window={addEventListener:(k,v)=>events[k]=v};const parent=embedded?{postMessage:m=>messages.push(m)}:window;window.parent=parent;
 const context={IntersectionObserver:class{constructor(callback){this.callback=callback;this.active=false;observers.push(this);}observe(){this.active=true;}disconnect(){this.active=false;}},window,parent,document,INITIAL_PAGE:page,WORKBENCH_MODULES:[{id:'accounts',name:'Codex 账户',group:'账户管理'},{id:'other_accounts',name:'平台账户',group:'账户管理'},{id:'models',name:'模型服务',group:'账户管理'}],READONLY_ICONS:{},ConfigurationCrypto:{async prepare(entry){return {arguments:{id:entry.id},privateKey:{}};},async decrypt(){return fieldPayload;}},WORKBENCH_BOOTSTRAP:bootstrap,URL,console,AbortController,DOMException,crypto:webcrypto,history:{replaceState(){}},setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),fetch:(url,options)=>new Promise((resolve,reject)=>{const r={url,options,resolve:body=>resolve({ok:true,json:async()=>body}),response:resolve,reject};requests.push(r);options.signal?.addEventListener('abort',()=>reject(options.signal.reason||new DOMException('Aborted','AbortError')),{once:true});})};
 vm.runInNewContext(primitives,context);vm.runInNewContext(source,context);
 return {fields:window.__fieldTest,root,requests,messages,timers,controls,async scrollModels(){observers.filter(x=>x.active).forEach(x=>x.callback([{isIntersecting:true}]));await flush();},async visibility(hidden){document.hidden=hidden;docEvents.visibilitychange();await flush();},async event(name,args={}){events[name]?.(args);await flush();},async hostResult(result){await this.event('message',{source:parent,data:{jsonrpc:'2.0',method:'ui/notifications/tool-result',params:result}});},async reply(message,result){await this.event('message',{source:parent,data:{jsonrpc:'2.0',id:message.id,result}});},async expire(){for(const [id,{fn}] of [...timers]){timers.delete(id);fn();}await flush();}};
}
const data=name=>({context:'test-account',revision:name,reset:true,unchanged:false,data:{models:[{id:name,name,provider:'fixture'}]}});
// 隐藏窗口不应丢弃公共目录读取，返回时复用尚在执行的请求。
const hidden=boot();await flush();assert.match(hidden.root.innerHTML,/正在读取/);
await hidden.visibility(true);hidden.requests[0].resolve(data('hidden-result'));await flush();
assert.match(hidden.root.innerHTML,/hidden-result/);assert.doesNotMatch(hidden.root.innerHTML,/正在读取/);
await hidden.visibility(false);assert.equal(hidden.requests.length,1);
await hidden.visibility(true);await hidden.visibility(false);assert.equal(hidden.requests.length,1);
assert.match(hidden.root.innerHTML,/hidden-result/);
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
// 冷入口只带页面标记，不能抢占正在读取的真实数据或写入空缓存。
await host.hostResult({structuredContent:{view:'models',_bootstrap_pending:true}});
assert.equal(host.fields.s.syncing,true);assert.equal(Object.keys(host.fields.s.data).length,0);
await host.visibility(true);await host.reply(call,data('host-result'));assert.match(host.root.innerHTML,/host-result/);
await host.visibility(false);assert.equal(host.messages.filter(x=>x.method==='tools/call').length,1);await host.expire();assert.doesNotMatch(host.root.innerHTML,/超时/);assert.doesNotMatch(host.root.innerHTML,/aria-busy="true"/);
console.log('UI 加载：隐藏/返回、页面恢复、超时重试、乱序响应、宿主桥接通过');
assert.equal(host.timers.size,0);assert.equal(stalled.timers.size,0);assert.equal(bodyStall.timers.size,0);

const cached=boot();await flush();cached.requests[0].resolve(data('cached-result'));await flush();const rendered=cached.root.renders;
await cached.visibility(true);await cached.visibility(false);assert.match(cached.root.innerHTML,/cached-result/);assert.doesNotMatch(cached.root.innerHTML,/正在读取/);
assert.equal(cached.requests.length,1);cached.fields.load();await flush();const request=JSON.parse(cached.requests[1].options.body);assert.equal(request.name,'workbench_sync');assert.equal(request.arguments.revision,'cached-result');
cached.requests[1].resolve({context:'test-account',revision:'cached-result',unchanged:true});await flush();assert.equal(cached.root.renders,rendered);
cached.fields.load();await flush();assert.equal(JSON.parse(cached.requests.at(-1).options.body).arguments.refresh,false);
cached.requests.at(-1).resolve({context:'another-account',revision:'another',reset:true,data:{models:[]}});await flush();assert.doesNotMatch(cached.root.innerHTML,/cached-result/);
console.log('页面缓存：保留可见数据、携带版本、不重建未变 DOM、自动同步、换号清理通过');

const otherAccounts=boot({page:'other_accounts'});await flush();
assert.equal(JSON.parse(otherAccounts.requests[0].options.body).arguments.refresh,false);
otherAccounts.requests[0].resolve({context:'other-account',revision:'other-r1',reset:true,data:{view:'other_accounts',accounts:[]}});await flush();
await otherAccounts.visibility(true);await otherAccounts.visibility(false);assert.equal(otherAccounts.requests.length,1);otherAccounts.fields.load();await flush();
assert.equal(JSON.parse(otherAccounts.requests.at(-1).options.body).arguments.refresh,false);
otherAccounts.requests.at(-1).resolve({context:'other-account',revision:'other-r1',unchanged:true});await flush();
otherAccounts.controls.get('refresh-usage').click();await flush();
assert.equal(JSON.parse(otherAccounts.requests.at(-1).options.body).arguments.refresh,true);
assert.match(otherAccounts.root.innerHTML,/刷新中\.\.\./);
otherAccounts.requests.at(-1).resolve({context:'other-account',revision:'other-r1',unchanged:true});await flush();
assert.doesNotMatch(otherAccounts.root.innerHTML,/刷新中\.\.\./);
console.log('其他账户：页面恢复使用缓存，用户刷新用量及忙状态清理通过');
for(const page of ['accounts','other_accounts']){
 const board=boot({page});await flush();
 const result=(revision,refreshing,name)=>({context:'usage-test',revision,reset:true,data:{view:page,accounts:[{id:'one',display_name:name}],status:{snapshot:{state:'ready',refreshing}}}});
 board.requests[0].resolve(result('old',false,'旧账户'));await flush();
 board.controls.get('refresh-usage').click();await flush();
 board.requests.at(-1).resolve(result('pending',true,'中间数据'));await flush();
 assert.match(board.root.innerHTML,/旧账户/);assert.doesNotMatch(board.root.innerHTML,/中间数据/);assert.match(board.root.innerHTML,/刷新中\.\.\./);
 for(const [id,timer] of [...board.timers])if(timer.ms===1000){board.timers.delete(id);timer.fn();}await flush();
 assert.equal(JSON.parse(board.requests.at(-1).options.body).arguments.refresh,false);
 board.requests.at(-1).resolve(result('saved',false,'新账户'));await flush();
 assert.match(board.root.innerHTML,/新账户/);assert.doesNotMatch(board.root.innerHTML,/刷新中\.\.\./);
}
console.log('账户用量：采集中保留旧数据，完成后统一替换，两个账户页通过');


// 原生入口会主动推送首屏结果，即使页面自主 tools/call 仍在等待也必须显示。
const pushed=boot({embedded:true});await flush();await pushed.reply(pushed.messages[0],{});assert.match(pushed.root.innerHTML,/正在读取/);
await pushed.hostResult({structuredContent:{view:'models',models:[{id:'host-initial',name:'Host initial'}],_sync:{context:'test-account',revision:'initial-revision'}}});
assert.match(pushed.root.innerHTML,/Host initial/);assert.doesNotMatch(pushed.root.innerHTML,/正在读取/);assert.equal(pushed.timers.size,0);
await pushed.visibility(true);await pushed.visibility(false);pushed.fields.load();await flush();const subsequent=pushed.messages.filter(x=>x.method==='tools/call').at(-1);assert.equal(subsequent.params.arguments.view,'models');
await pushed.reply(subsequent,{structuredContent:{context:'test-account',revision:'initial-revision',unchanged:true}});assert.match(pushed.root.innerHTML,/Host initial/);
console.log('原生首屏：主动推送解除加载、取消重复请求、携带首屏版本增量同步通过');

// 原生 HTML 首屏立即可见；所有后续读取等待握手且只走宿主工具通道。
const bootstrap={context:'bootstrap-account',views:[{args:{view:'models'},revision:'bootstrap-r1',data:{view:'models',models:[{id:'bootstrap-model',name:'首屏模型'}]}}]};
const native=boot({embedded:true,bootstrap});
assert.match(native.root.innerHTML,/首屏模型/);assert.doesNotMatch(native.root.innerHTML,/正在读取/);await flush();
assert.equal(native.requests.length,0);assert.equal(native.messages.filter(x=>x.method==='ui/initialize').length,1);assert(!native.messages.some(x=>x.method==='tools/call'));
await native.reply(native.messages[0],{});
const nativeCall=native.messages.find(x=>x.method==='tools/call');assert(nativeCall);assert.equal(nativeCall.params.arguments.view,'models');
const initialized=native.messages.find(x=>x.method==='ui/notifications/initialized');assert(initialized);assert.equal(initialized.jsonrpc,'2.0');assert.equal(initialized.method,'ui/notifications/initialized');assert.equal(Object.keys(initialized.params).length,0);assert(native.messages.findIndex(x=>x.method==='ui/notifications/initialized')<native.messages.indexOf(nativeCall));
await native.reply(nativeCall,{structuredContent:{context:'bootstrap-account',revision:'bootstrap-r1',unchanged:true}});
assert.match(native.root.innerHTML,/首屏模型/);assert.equal(native.timers.size,1);assert.equal(native.requests.length,0);
// 宿主失联时保留快照，手动重试重新握手；不尝试被原生沙箱阻止的 HTTP。
const handshake=boot({embedded:true,bootstrap});await flush();await handshake.expire();assert.match(handshake.root.innerHTML,/首屏模型/);assert.match(handshake.root.innerHTML,/超时/);
handshake.controls.get('retry').click();await flush();assert.equal(handshake.messages.filter(x=>x.method==='ui/initialize').length,2);
await handshake.reply(handshake.messages.at(-1),{});await handshake.reply(handshake.messages.at(-1),{structuredContent:{context:'bootstrap-account',revision:'bootstrap-r2',reset:true,data:{models:[{id:'reconnected',name:'重新连接成功'}]}}});
assert.match(handshake.root.innerHTML,/重新连接成功/);assert.doesNotMatch(handshake.root.innerHTML,/id="retry"/);assert.equal(handshake.requests.length,0);
const cold=boot({embedded:true,bootstrap:{views:[],error:'首屏数据暂时不可用，请点击刷新读取'}});assert.match(cold.root.innerHTML,/首屏数据暂时不可用/);assert.doesNotMatch(cold.root.innerHTML,/正在读取/);
console.log('原生首屏：缓存即时显示、单次握手、宿主增量同步、失联手动恢复、无 HTTP 请求通过');



// 浏览器首次失败和缓存刷新失败都能手动恢复，不重复发起后台重试。
const network=boot();await flush();network.requests[0].reject(new TypeError('Failed to fetch'));await flush();
assert.match(network.root.innerHTML,/暂时无法连接本机工作台/);assert.doesNotMatch(network.root.innerHTML,/Failed to fetch|正在读取/);assert.equal(network.requests.length,1);
network.controls.get('retry').click();await flush();network.requests.at(-1).resolve(data('network-restored'));await flush();
assert.match(network.root.innerHTML,/network-restored/);assert.doesNotMatch(network.root.innerHTML,/暂时无法连接|id="retry"/);
network.fields.load();await flush();network.requests.at(-1).reject(new TypeError('Failed to fetch'));await flush();
assert.match(network.root.innerHTML,/network-restored/);assert.match(network.root.innerHTML,/暂时无法连接本机工作台/);assert.equal(network.requests.length,3);
network.controls.get('retry').click();await flush();network.requests.at(-1).resolve(data('latest-network-data'));await flush();
assert.match(network.root.innerHTML,/latest-network-data/);assert.doesNotMatch(network.root.innerHTML,/暂时无法连接|id="retry"/);assert.equal(network.timers.size,0);
console.log('浏览器网络恢复：中文错误、缓存保留、手动重试更新、不自动轮询通过');

// 模型详情只读取公开元数据，不提供配置读取、解密或复制入口。
const fields=boot({page:'models'});await flush();fields.requests[0].resolve({context:'test-account',revision:'fields-r1',reset:true,data:{models:[]}});await flush();
const opening=fields.fields.detail('model','model-1');await flush();
fields.requests.at(-1).resolve({model:{id:'model-1',name:'Synthetic model'}});await opening;
assert.equal(fields.requests.length,2);assert.match(fields.root.innerHTML,/Synthetic model/);
assert.doesNotMatch(fields.root.innerHTML,/配置字段|data-reveal|data-copy/);
console.log('模型详情：仅公开元数据，不读取凭据通过');

// 退役页面不能再被导航或渲染为可用页面。
const retired=boot({page:'config'});await flush();
assert.doesNotMatch(retired.root.innerHTML,/配置中心|config-card/);
console.log('退役页面：配置中心不再出现在工作台导航或视图中通过');

// Only an actual page change requests its target; aborted old responses cannot replace it.
const navigation=boot();await flush();
navigation.fields.pageChange('models');await flush();assert.equal(navigation.requests.length,1);
navigation.fields.pageChange('accounts');await flush();assert.equal(navigation.requests.length,2);
assert(navigation.requests[0].options.signal.aborted);
assert.equal(JSON.parse(navigation.requests[1].options.body).arguments.view,'accounts');
navigation.requests[1].resolve({context:'test-account',revision:'accounts-r1',reset:true,data:{accounts:[]}});
navigation.requests[0].resolve(data('obsolete-page'));await flush();
assert.equal(navigation.fields.s.page,'accounts');assert.doesNotMatch(navigation.root.innerHTML,/obsolete-page/);
await navigation.visibility(true);await navigation.visibility(false);assert.equal(navigation.requests.length,2);
console.log('按页加载：只请求目标页、同页点击和窗口聚焦不请求、旧响应隔离通过');

// ui/message 只表示宿主已受理；官网导入完成之前持续读本地快照，不重复触发来源刷新。
const captured=boot({embedded:true,page:'other_accounts'});await flush();
await captured.reply(captured.messages[0],{});
const captureData=(revision,name,observed)=>({context:'capture-test',revision,reset:true,data:{view:'other_accounts',accounts:[{id:'one',provider_id:'minimax',provider_name:'MiniMax',usage_credential_id:'key-ref',display_name:name,profile_observed_at:observed}],status:{snapshot:{state:'ready',refreshing:false}}}});
await captured.reply(captured.messages.find(x=>x.method==='tools/call'),{structuredContent:captureData('old','旧名称','before')});
const updating=captured.fields.updateProfile('other_accounts','one');await flush();
const captureGrant=captured.messages.filter(x=>x.method==='tools/call').at(-1);
assert.equal(captureGrant.params.name,'account_profile_capture');
assert(!captured.messages.some(x=>x.method==='ui/message'));
await captured.reply(captureGrant,{structuredContent:{capture_id:'test-capture-grant',expires_in_seconds:600}});await flush();
const captureRequest=captured.messages.find(x=>x.method==='ui/message');assert(captureRequest);
assert.match(captureRequest.params.content[0].text,/test-capture-grant/);
await captured.reply(captureRequest,{});await updating;
assert.match(captured.root.innerHTML,/旧名称/);
async function runCapturePoll(result){
 const timer=[...captured.timers].find(([,timer])=>timer.ms===5000);assert(timer);
 captured.timers.delete(timer[0]);timer[1].fn();await flush();
 const call=captured.messages.filter(x=>x.method==='tools/call').at(-1);
 assert.equal(call.params.name,'workbench_sync');assert.equal(call.params.arguments.refresh,false);
 await captured.reply(call,{structuredContent:result});
}
await runCapturePoll(captureData('waiting','旧名称','before'));
assert(captured.fields.s.profileCapture);
await runCapturePoll(captureData('new','官网新名称','after'));
assert.match(captured.root.innerHTML,/官网新名称/);assert.equal(captured.fields.s.profileCapture,null);
assert(![...captured.timers.values()].some(timer=>timer.ms===5000));
assert.equal(captured.messages.filter(x=>x.method==='ui/message').length,1);
console.log('官网资料更新：宿主受理后只轮询本地快照，导入完成自动显示并结束轮询');

// 搜索旧昵称时，改名可以让账户离开当前列表，但不能让采集状态永远等待。
const filteredCapture=boot({embedded:true,page:'other_accounts'});await flush();
await filteredCapture.reply(filteredCapture.messages[0],{});
await filteredCapture.reply(filteredCapture.messages.find(x=>x.method==='tools/call'),{structuredContent:captureData('filtered-old','旧名称','before')});
filteredCapture.fields.s.query='旧名称';filteredCapture.fields.s.accountProvider='minimax';
const filteredUpdating=filteredCapture.fields.updateProfile('other_accounts','one');await flush();
await filteredCapture.reply(filteredCapture.messages.filter(x=>x.method==='tools/call').at(-1),{structuredContent:{capture_id:'filtered-grant',expires_in_seconds:600}});await flush();
await filteredCapture.reply(filteredCapture.messages.find(x=>x.method==='ui/message'),{});await filteredUpdating;
const filteredTimer=[...filteredCapture.timers].find(([,timer])=>timer.ms===5000);assert(filteredTimer);
filteredCapture.timers.delete(filteredTimer[0]);filteredTimer[1].fn();await flush();
const filteredCall=filteredCapture.messages.filter(x=>x.method==='tools/call').at(-1);
assert.deepEqual(Object.keys(filteredCall.params.arguments).sort(),['refresh','view']);
assert.equal(filteredCall.params.arguments.refresh,false);
await filteredCapture.reply(filteredCall,{structuredContent:captureData('filtered-new','官网新名称','after')});
assert.equal(filteredCapture.fields.s.profileCapture,null);
assert.equal(filteredCapture.fields.s.query,'旧名称');
assert.equal(filteredCapture.fields.s.accountProvider,'minimax');
assert.doesNotMatch(filteredCapture.root.innerHTML,/<h2[^>]*>官网新名称/);
assert.equal(filteredCapture.fields.s.data.accounts[0].display_name,'官网新名称');
assert(![...filteredCapture.timers.values()].some(timer=>timer.ms===5000));
console.log('官网资料更新：搜索与供应商筛选不影响完成判定，仍保留用户的筛选条件');

// 平台账户不再提供独立配置或凭据入口。
const refs=boot({page:'other_accounts'});await flush();refs.requests[0].resolve({context:'refs',revision:'1',reset:true,data:{accounts:[]}});await flush();
assert.doesNotMatch(refs.root.innerHTML,/配置引用|查看配置|data-config/);
for(const token of ['credential_list','credential_details','ConfigurationCrypto','readSecret'])assert(!source.includes(token));
console.log('配置解耦：无目录、解密或操作入口通过');

// 滚动连续加载保留已有行；重复交叉观察不会发出并发请求，失败允许重试同一页。
const modelBatch=(page,ids,{pages=3,total=45,context='scroll-test'}={})=>({context,revision:'batch-'+page+'-'+ids.join('-'),reset:true,data:{models:ids.map(id=>({id,name:'Model '+id})),pagination:{page,pages,total,page_size:20}}});
const scrolling=boot();await flush();
scrolling.requests[0].resolve(modelBatch(1,Array.from({length:20},(_,i)=>String(i))));await flush();
assert.doesNotMatch(scrolling.root.innerHTML,/模型 · Model|id="model-(previous|next)"/);
assert.match(scrolling.root.innerHTML,/每次加载条数/);
await scrolling.scrollModels();await scrolling.scrollModels();
assert.equal(scrolling.requests.length,2);
assert.equal(JSON.parse(scrolling.requests[1].options.body).arguments.page,2);
assert.equal(scrolling.fields.s.data.models.length,20);
scrolling.requests[1].resolve(modelBatch(2,['0',...Array.from({length:19},(_,i)=>String(i+20))]));await flush();
assert.equal(scrolling.fields.s.data.models.length,39);
assert.equal(new Set(scrolling.fields.s.data.models.map(x=>x.id)).size,39);
await scrolling.scrollModels();scrolling.requests[2].reject(Error('临时网络失败'));await flush();
assert.equal(scrolling.fields.s.data.models.length,39);assert.match(scrolling.root.innerHTML,/重试加载/);
await scrolling.scrollModels();assert.equal(scrolling.requests.length,3);
scrolling.controls.get('model-load-retry').click();await flush();
assert.equal(JSON.parse(scrolling.requests[3].options.body).arguments.page,3);
scrolling.requests[3].resolve(modelBatch(3,['40','41','42','43','44']));await flush();
await scrolling.scrollModels();assert.equal(scrolling.requests.length,4);assert.match(scrolling.root.innerHTML,/已显示全部模型/);
// 筛选变化取消旧续页，过期响应不得混入新列表；条数选择器从第一页重新加载。
const scrollFilter=boot();await flush();scrollFilter.requests[0].resolve(modelBatch(1,['old']));await flush();
await scrollFilter.scrollModels();
scrollFilter.controls.get('model-status').change({target:{value:'available'}});await flush();
assert(scrollFilter.requests[1].options.signal.aborted);
assert.equal(JSON.parse(scrollFilter.requests[2].options.body).arguments.page,1);
scrollFilter.requests[2].resolve(modelBatch(1,['filtered'],{pages:1,total:1}));
scrollFilter.requests[1].resolve(modelBatch(2,['obsolete']));await flush();
assert.equal(scrollFilter.fields.s.data.models.map(x=>x.id).join(','),'filtered');
scrollFilter.controls.get('model-page-size').change({target:{value:'50'}});await flush();
assert.equal(JSON.parse(scrollFilter.requests[3].options.body).arguments.page_size,50);
assert.equal(JSON.parse(scrollFilter.requests[3].options.body).arguments.page,1);
scrollFilter.requests[3].resolve(modelBatch(1,['resized'],{pages:1,total:1}));await flush();
assert.equal(scrollFilter.fields.s.data.models.map(x=>x.id).join(','),'resized');
// 账户环境变化不能拼接到原列表。
const scrollContext=boot();await flush();scrollContext.requests[0].resolve(modelBatch(1,['original']));await flush();
await scrollContext.scrollModels();scrollContext.requests[1].resolve(modelBatch(2,['foreign'],{context:'another-context'}));await flush();
assert.deepEqual(Object.keys(scrollContext.fields.s.data),[]);assert.match(scrollContext.root.innerHTML,/账户环境已变化/);
console.log('模型滚动加载：追加去重、请求互斥、失败重试、筛选隔离、条数选择及环境切换通过');
