import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
const window={addEventListener(){}};window.parent=window;
const context={window,parent:window,INITIAL_PAGE:'accounts',READONLY_ICONS:JSON.parse(fs.readFileSync(new URL('../ui/assets/readonly-icons.json',import.meta.url),'utf8')),WORKBENCH_MODULES:[{id:'accounts',name:'Codex 账户',group:'账户与配置'}],document:{getElementById(){return null;},addEventListener(){}},console,Map,Set,URL};
vm.createContext(context);vm.runInContext(fs.readFileSync(new URL('../ui/readonly.js',import.meta.url),'utf8'),context);
const h=window.__workbenchReadonlyTest;
assert.equal(h.nativeUrl('javascript:alert(1)'),null);assert.equal(h.nativeUrl('codex://threads/id?token=secret'),null);assert.equal(h.nativeUrl('codex://threads/abc-123'),'codex://threads/abc-123');
assert.equal(h.duration(null),'未提供');assert.equal(h.duration(0),'0 毫秒');assert.equal(h.duration(61234),'1 分 1 秒');
assert.equal(h.resetTime(null,0),'未提供');assert.equal(h.resetTime(0,0),'等待重置更新');assert.equal(h.resetTime(90061,0),'1 天后重置');assert.equal(h.resetTime(7200,0),'2 小时后重置');assert.equal(h.resetTime(60,0),'1 分钟后重置');
assert.equal(h.findText([{title:'修复 A'},{title:'检查 B'}],'修复',['title']).length,1);
assert.equal(h.accountTitle({email:'name@example.com'}),'用户名未提供');
assert.equal(h.accountTitle({display_name:'Jacob',email:'name@example.com'}),'Jacob');
assert.match(h.avatarMarkup({avatar_data_uri:'data:image/png;base64,AA=='}),/<img /);
assert.doesNotMatch(h.avatarMarkup({avatar_data_uri:'data:image/svg+xml;base64,PHN2Zz4='}),/<img /);
assert.doesNotMatch(h.avatarMarkup({avatar_data_uri:'https://example.invalid/avatar.png'}),/<img /);
assert.match(h.otherAccountCard({id:'p',label:'Provider label',display_name:'Jacob',provider_name:'Provider',field_notes:{}}),/>Jacob</);
const minimax={id:'minimax-text',name:'MiniMax 文本',model:'MiniMax-Text-01',base_url:'https://api.minimax.invalid/v1',provider_id:'minimax',provider_name:'MiniMax',model_type:'reasoning',validation_status:'pending'};
const legacy={id:'legacy',name:'未登记供应商模型',base_url:'https://legacy.invalid/v1',model_type:'embedding',validation_status:'pending'};
assert.deepEqual(JSON.parse(JSON.stringify(h.modelProviders([minimax,legacy]))),[{id:'minimax',name:'MiniMax'}]);
for(const query of ['MiniMax','minimax','minmax','mini max'])assert(h.modelMatches(minimax,query),query+' must find MiniMax');
assert(h.modelMatches(legacy,'未登记'));
assert(!h.modelMatches(legacy,'minimax'));
console.log('模型目录：供应商元数据、MiniMax 常见拼写和未登记供应商显示通过');
const root={a:{password:'first'},b:{password:'second'},zero:0,flag:false,blank:null};const paths=h.fieldPaths(root);
assert.equal(paths.length,5);assert.equal(h.fieldValue(root,['a','password']),'first');assert.equal(h.fieldValue(root,['b','password']),'second');assert.equal(h.fieldValue(root,['zero']),'0');assert.equal(h.fieldValue(root,['flag']),'false');assert.equal(h.fieldValue(root,['blank']),'null');assert.throws(()=>h.fieldValue(root,['missing']));
assert.equal(JSON.stringify(paths).includes('first'),false);assert.equal(JSON.stringify(paths).includes('second'),false);
assert.equal(h.payloadRoot({format:'workbench_credential_v1',fields:{credential:root}}),root);assert.equal(h.payloadRoot({format:'json',fields:root}),root);assert.equal(h.payloadRoot({format:'text',text:'synthetic'}).text,'synthetic');
const folders=[{id:'a',parent_id:null},{id:'b',parent_id:'a'},{id:'c',parent_id:'b'}];assert.equal(h.foldersIn(folders,'a').size,3);
assert.equal(h.readResult({structuredContent:{ok:true}}).ok,true);assert.throws(()=>h.readResult({isError:true,structuredContent:{error:{message:'expected'}}}),/expected/);
const script=fs.readFileSync(new URL('../ui/readonly.js',import.meta.url),'utf8');assert(!/setInterval\s*\(/.test(script));assert(!/\bprompt\s*\(/.test(script));assert(!/localStorage|sessionStorage/.test(script));assert(script.includes("'X-Workbench-Request':'1'"));
console.log('只读 UI：状态、空值、时间、字段精确定位、目录、响应解包及边界检查通过');

const icons=context.READONLY_ICONS;
for(const [native,asset] of Object.entries({terminal:'terminal',globe:'globe',customize:'sliders-horizontal',palette:'palette',logs:'file-text',book:'book-open','graduation-cap':'graduation-cap',brain:'brain','currency-dollar':'badge-dollar-sign',health:'heart',plant:'leaf'})){
 const html=h.entityIcon({icon:{kind:'symbol',value:native},color:'pink'});
 assert(html.includes(icons[asset]),native+' must use its configured symbol');assert(html.includes(h.color('pink')));
}
assert.notEqual(h.color('yellow'),h.color('blue'));assert.equal(h.color('#12AbEf'),'#12AbEf');assert.equal(h.color('red;position:fixed'),h.color(null));
assert(h.entityIcon({icon:{kind:'emoji',value:'🧭'}}).includes('🧭'));
assert(!h.entityIcon({icon:{kind:'emoji',value:'<img src=x onerror=alert(1)>'}}).includes('<img'));
assert(h.entityIcon({icon:{kind:'symbol',value:'unknown-symbol'}}).includes(icons.folder));
const svg=h.entityIcon({icon:{kind:'svg',value:'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'}});
assert(svg.includes('src="data:image/svg+xml,'));assert(!svg.includes('<script>'));assert(!svg.includes('<svg'));
const tag=h.entityTag({name:'<project>',icon:{kind:'symbol',value:'terminal'},color:'purple'});assert(tag.includes('&lt;project&gt;'));assert(tag.includes(h.color('purple')));assert(tag.includes(icons.terminal));
console.log('项目外观：原生符号映射、颜色、Emoji、SVG 图片隔离、未知图标和转义通过');

const keep={id:'a',name:'A'},replace={id:'b',name:'B'};
const base={revision:'r1',data:{skills:[keep,replace],total:2,obsolete:true}};
const patched=h.applySync(base,{revision:'r2',base_revision:'r1',reset:false,patch:{set:{total:2},remove:['obsolete'],collections:{skills:{key:'id',upsert:[{id:'c',name:'C'}],remove:['b'],order:['c','a']}}}});
assert.equal(patched.data.skills[1],keep);assert.equal(patched.data.skills[0].name,'C');assert.equal(base.data.skills[1],replace);assert(!Object.hasOwn(patched.data,'obsolete'));
assert.equal(h.applySync(base,{revision:'r1',unchanged:true}),base);
assert.throws(()=>h.applySync(base,{revision:'r2',base_revision:'stale',patch:{}}),/缓存版本/);
assert.throws(()=>h.applySync(null,{revision:'r1',unchanged:true}),/缓存版本/);
console.log('增量合并：新增、修改、删除、重排、对象复用、版本拒绝通过');

assert.equal(h.canonicalKey({b:1,a:{z:2,y:3}}),h.canonicalKey({a:{y:3,z:2},b:1}));
console.log('首屏缓存：规范缓存键、未知时间稳定和已知时间倒序通过');
// 自动显示采用精确白名单；嵌套项、未知项、带认证信息的 URL 仍遮挡。
assert(h.automaticField({path:['type']},'api-key'));
assert(h.automaticField({path:['authentication_tested']},false));
assert(h.automaticField({path:['endpoint']},'https://example.invalid/v1/chat/completions'));
for(const [path,value] of [[['api_key'],'synthetic'],[['password'],'synthetic'],[['extra','type'],'synthetic'],[['endpoint'],'https://example.invalid/?key=synthetic'],[['endpoint'],'https://synthetic@example.invalid'],[['host'],'user@host']])assert(!h.automaticField({path},value));

// 主账户优先于默认账户与更新时间，输入数组不被重排。
const accountOrder=[{id:'new',observed_at:30,is_default:true},{id:'main',observed_at:10,is_current:true},{id:'old',observed_at:20}];
assert.equal(h.orderedAccounts(accountOrder).map(x=>x.id).join(','),'main,new,old');assert.equal(accountOrder[0].id,'new');
const configData={folders:[{id:'p',name:'生产环境'},{id:'s',name:'数据服务器',parent_id:'p'},{id:'d',name:'开发环境'}],entries:[{id:'a',label:'业务数据库',folder_id:'s',service_type:'postgresql',service_type_label:'PostgreSQL',tags:['核心'],updated_at:10},{id:'b',label:'缓存',folder_id:'d',service_type:'redis',service_type_label:'Redis',tags:['内网'],updated_at:20}]};
assert.equal(h.folderPath(configData.folders,'s'),'生产环境 / 数据服务器');
assert.equal(h.filteredConfigurations(configData,'p','','','').length,1);
assert.equal(h.filteredConfigurations(configData,'','','','数据服务器')[0].id,'a');
assert.equal(h.filteredConfigurations(configData,'','redis','核心','').length,0);
assert.equal(h.filteredConfigurations(configData,'','redis','内网','缓存')[0].id,'b');
assert.equal(h.folderPath([{id:'x',name:'X',parent_id:'y'},{id:'y',name:'Y',parent_id:'x'}],'x'),'Y / X');
console.log('配置中心：目录后代、循环保护、类型与标签交集、目录搜索通过');
