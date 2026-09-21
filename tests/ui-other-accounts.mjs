import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
const window={addEventListener(){}};window.parent=window;
const context={window,parent:window,INITIAL_PAGE:'other_accounts',READONLY_ICONS:JSON.parse(fs.readFileSync(new URL('../ui/assets/readonly-icons.json',import.meta.url),'utf8')),WORKBENCH_MODULES:[{id:'other_accounts',name:'其他账户',group:'账户与配置'}],document:{getElementById(){return null;},addEventListener(){}},console,Map,Set,URL};
vm.createContext(context);vm.runInContext(fs.readFileSync(new URL('../ui/readonly-primitives.js',import.meta.url),'utf8'),context);vm.runInContext(fs.readFileSync(new URL('../ui/readonly.js',import.meta.url),'utf8'),context);
const h=window.__workbenchReadonlyTest;
const account={id:'sample',label:'开发账户',provider_id:'zhipu',provider_name:'智谱',usage:null,is_used:null};
assert.equal(h.accountUsage(null).value,'未提供');
assert.equal(h.accountUsage({used:0,limit:100,remaining:100,unit:'次'}).value,'100%');
assert.equal(h.accountUsage({used:100,limit:100,remaining:0,unit:'次'}).value,'0%');
assert.equal(h.accountUsage({remaining:0,unit:'CNY'}).value,'¥0.00');
assert.equal(h.accountUsage({remaining:128.5,unit:'CNY'}).title,'账户余额');
assert.equal(h.accountUsage({used:42,unit:'tokens'}).title,'已用量');
assert.equal(h.accountUsage({limit:100,unit:'次'}).title,'总额度');
assert.equal(h.accountUsage({used:0,limit:0,remaining:0,unit:'次'}).percent,null);
const accounts=[account,{...account,id:'second',label:'接口账户',provider_id:'deepseek',provider_name:'DeepSeek',is_used:false}];
assert.equal(h.accountProviders(accounts).length,2);
let html=h.otherAccountsView({accounts},'','zhipu');assert(html.includes('开发账户'));assert(!html.includes('接口账户'));assert(html.includes('DeepSeek'));assert(html.includes('aria-pressed="true"'));
html=h.otherAccountsView({accounts},'不存在','');assert(html.includes('没有匹配的账户'));assert(!html.includes('暂无其他账户'));
html=h.otherAccountsView({accounts:[],providers:[{id:'unused',name:'不应显示'}]},'','');assert(!html.includes('不应显示'));assert(!html.includes('全部平台'));assert(html.includes('暂无其他账户'));
html=h.otherAccountsView({accounts},'','removed');assert(html.includes('开发账户'));assert(html.includes('接口账户'));
html=h.otherAccountCard({...account,label:'<script>bad()</script>',vault_id:'2.account.ref',is_used:true,observed_at:'2026-09-16T09:00:00+08:00',status_source:'provider_console'});
assert(html.includes('&lt;script&gt;'));assert(!html.includes('<script>'));assert(html.includes('data-config="2.account.ref"'));assert(html.includes('使用中'));assert(html.includes('检查于'));assert(!html.includes('<summary>数据来源</summary>'));assert(!html.includes('主账户'));assert(!html.includes('默认'));assert(!html.includes('重置卡'));
html=h.otherAccountCard({...account,label:'旧标签',display_name:'资料昵称',profile_observed_at:'2026-09-16T09:00:00+08:00'});
assert(html.includes('<h2>资料昵称</h2>'));assert(!html.includes('旧标签'));assert(html.includes('资料更新于'));
assert(h.otherAccountCard(account).includes('状态未核验'));assert(h.otherAccountCard(accounts[1]).includes('未使用'));
assert(!h.otherAccountCard({...account,is_used:undefined,api_auth:{status:'accepted',source:'provider_api',observed_at:'2026-09-16T09:00:00+08:00'}}).includes('状态未核验'));
const verified={...account,provider_id:'minimax',provider_name:'MiniMax',is_used:null,api_auth:{status:'accepted',source:'provider_api',observed_at:'2026-09-16T09:00:00+08:00'},usage_windows:[{id:'five-hour',label:'5 小时',usage:{used:20,limit:100,remaining:80,unit:'%',observed_at:'2026-09-16T09:00:00+08:00',source:'provider_api'},resets_at:'2026-09-16T14:00:00+08:00'},{id:'weekly',label:'每周',usage:{used:0,limit:100,remaining:100,unit:'%',observed_at:'2026-09-16T09:00:00+08:00',source:'provider_api'},resets_at:null}],field_notes:{usage:'视频套餐额度尚未核验',resets_at:'接口未返回',expires_at:'接口未返回到期时间',last_used_at:'接口未返回最近使用时间'}};
html=h.otherAccountCard({...verified,usage_refresh:{state:'failed',message:'<更新失败>'}});assert(html.includes('API 已验证'));assert(html.includes('检查于'));assert(!html.includes('状态未核验'));assert(html.includes('5 小时'));assert(html.includes('每周'));assert(!html.includes('视频套餐额度尚未核验'));assert(!html.includes('接口未返回到期时间'));assert(!html.includes('接口未返回最近使用时间'));assert(html.includes('&lt;更新失败&gt;'));assert(!html.includes('<更新失败>'));assert.equal((html.match(/<dt>重置时间<\/dt>/g)||[]).length,0);
console.log('其他账户 UI：分类隐藏、筛选、未知与零值、用量单位、状态来源、配置关联、转义通过');

// Quota bars and the headline both show remaining quota.
for(const [used,remaining] of [[0,100],[7,93],[100,0]]){
 const usage={used,limit:100,remaining,unit:'%'};
 for(const card of [{...account,usage},{...account,usage_windows:[{id:'quota',label:'5 小时',usage}]}]){
  const markup=h.otherAccountCard(card);
  assert(markup.includes(`width:${remaining}%`));
  assert(markup.includes(`aria-valuenow="${remaining}"`));
  assert(markup.includes(`已用 ${used}%`));
  assert(markup.includes(`>${remaining}%<`));
 }
}
assert(!h.otherAccountCard(account).includes('role="progressbar"'));
assert(!h.otherAccountCard({...account,usage:{used:0,remaining:0,limit:0,unit:'%'}}).includes('role="progressbar"'));

assert(!html.includes('<dt>到期时间</dt>'));assert(!html.includes('<dt>最近使用</dt>'));
const dated=h.otherAccountCard({...verified,expires_at:'2030-01-01T00:00:00Z',last_used_at:'2026-01-01T00:00:00Z'});assert(dated.includes('<dt>到期时间</dt>'));assert(dated.includes('<dt>最近使用</dt>'));

assert.notEqual(h.providerStyle("minimax"),h.providerStyle("kimi"));
const scoped={skills:[{id:"one",name:"Alpha",scope:"user",enabled:true},{id:"two",name:"Beta",scope:"system",enabled:true}]};
const filtered=h.skillsView(scoped,"","user");assert(filtered.includes("Alpha"));assert(!filtered.includes(">Beta<"));assert(filtered.includes("1 项技能"));assert(filtered.includes("技能类型"));assert(h.skillsView(scoped,"Beta","user").includes("0 项技能"));
