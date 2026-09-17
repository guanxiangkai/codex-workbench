/** 自动刷新与嵌入页面升级的合成协议检查，不连接真实账户。 */
import {readFileSync} from 'node:fs';
import {strict as assert} from 'node:assert';
import {runInNewContext} from 'node:vm';
import {webcrypto} from 'node:crypto';
const raw=readFileSync(new URL('../ui/app.html',import.meta.url),'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
const first='a'.repeat(64),second='b'.repeat(64),uri='ui://codex-workbench/v4/accounts.html';
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function harness(embedded=false){
 let revision=first,reads=0,initializes=0,reloads=0,written='',nextTimer=0;
 const timers=new Map(),listeners=new Map(),events=new Map(),app={innerHTML:''};
 const snapshot=()=>({accounts:[{id:'current',name:'Codex',isCurrent:true,login:{status:'ready'},usage:{plan:'pro',limits:{codex:{primary:{windowKind:'week',remainingPercent:70-reads}}}}}],current_account_id:'current',runtime:{ui_revision:revision}});
 const document={activeElement:null,hidden:false,title:'',querySelector:s=>s==='#app'?app:null,querySelectorAll:()=>[],getElementById:()=>null,addEventListener:(key,fn)=>events.set(key,fn),createElement:()=>({setAttribute(){},remove(){}}),body:{appendChild(){}},open(){},write(value){written=value;reloads++},close(){}};
 const window={innerWidth:1000,innerHeight:900,addEventListener:(key,fn)=>listeners.set(key,fn),removeEventListener:(key,fn)=>{if(listeners.get(key)===fn)listeners.delete(key)},location:{reload(){reloads++}},open:()=>({})};
 const reply=(id,result)=>queueMicrotask(()=>listeners.get('message')?.({source:parent,data:{jsonrpc:'2.0',id,result}}));
 const parent=embedded?{postMessage(message){if(!message.id)return;if(message.method==='ui/initialize'){initializes++;reply(message.id,{hostCapabilities:{}})}else if(message.method==='tools/call'){reads++;reply(message.id,{structuredContent:snapshot()})}else if(message.method==='resources/read'){assert.equal(message.params.uri,uri);reply(message.id,{contents:[{uri,text:'<html>codex-workbench-ui updated</html>'}]})}else assert.fail(message.method)}}:window;
 const context={window,parent,document,TextEncoder,FormData:class{},crypto:webcrypto,setTimeout:(fn,delay)=>{const id=++nextTimer;timers.set(id,{fn,delay});return id},clearTimeout:id=>timers.delete(id),btoa:v=>Buffer.from(v,'binary').toString('base64'),atob:v=>Buffer.from(v,'base64').toString('binary'),fetch:async(url)=>{if(url!=='/rpc')return {ok:true,text:async()=>'<html>codex-workbench-ui updated</html>'};reads++;return {ok:true,json:async()=>({structuredContent:snapshot()})}}};
 function boot(hash=first){runInNewContext(raw.replace('__WORKBENCH_INITIAL_PAGE__','accounts').replace('__WORKBENCH_RESOURCE_URI__',uri).replace('__WORKBENCH_UI_REVISION__',hash),context);return window.__workbenchTest}
 return {boot,document,window,timers,events,setRevision:value=>revision=value,stats:()=>({reads,initializes,reloads,written})};
}
const h=harness(),api=h.boot();await settle();
const draft={type:'credential',title:'保留未保存输入'};api.setState({selected:draft,accountDetail:{account:{id:'current'},usage:{plan:'old'}}});h.document.activeElement={matches:()=>true,closest:()=>null};
const before=h.stats().reads;await api.load({background:true});assert.equal(h.stats().reads,before+1);assert.doesNotMatch(api.accountCard(api.currentAccount()),/old/);
api.setState({providerModels:[{validation_status:'pending'}]});assert.equal(api.refreshInterval(),3000);api.setState({providerModels:[{validation_status:'validating'}]});assert.equal(api.refreshInterval(),3000);api.setState({providerModels:[{validation_status:'verified'}]});assert.equal(api.refreshInterval(),15000);api.setState({providerModels:[{validation_status:'failed'}]});assert.equal(api.refreshInterval(),15000);
const hiddenReads=h.stats().reads;h.document.hidden=true;h.events.get('visibilitychange')();await settle();assert.equal(h.stats().reads,hiddenReads);h.document.hidden=false;h.events.get('visibilitychange')();await settle();assert.equal(h.stats().reads,hiddenReads+1);
h.setRevision(second);await api.load({background:true});assert.equal(h.stats().reloads,0);api.setState({selected:null});h.document.activeElement=null;await api.load({background:true});assert.equal(h.stats().reloads,1);
const embedded=harness(true),embeddedApi=embedded.boot();await settle();await settle();assert.equal(embedded.stats().initializes,1);embedded.setRevision(second);await embeddedApi.load({background:true});assert.equal(embedded.stats().reloads,1);assert.match(embedded.stats().written,/updated/);embedded.boot(second);await settle();assert.equal(embedded.stats().initializes,1);const count=embedded.stats().reads;await embeddedApi.load({background:true});assert.equal(embedded.stats().reads,count);
console.log('Background polling, cache replacement, deferred upgrades and scoped embedded resource reload passed');
