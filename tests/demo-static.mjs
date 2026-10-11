import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import vm from 'node:vm';

const output=resolve(process.argv[2]||'outputs/static-demo');
const index=readFileSync(join(output,'index.html'),'utf8');
const assets=join(output,'assets');
const app=readFileSync(join(assets,'app.js'),'utf8');
const fixtures=readFileSync(join(assets,'fixtures.js'),'utf8');

assert.deepEqual(readdirSync(output).sort(),['assets','index.html']);
assert.deepEqual(readdirSync(assets).sort(),['app.css','app.js','fixtures.js']);
assert.match(index,/静态演示数据/);
assert.match(index,/connect-src 'none'/);
assert.match(index,/style-src 'self' 'unsafe-inline'/);
assert.doesNotMatch(index,/<script(?![^>]*\bsrc=)/);
for(const forbidden of ['fetch(','/rpc','postMessage','credential_details','ConfigurationCrypto','account_create','account_login','account_status','account_default','data-reveal','data-copy','id="fields"'])assert.ok(!app.includes(forbidden),`static app contains ${forbidden}`);

const window={addEventListener(){}};
window.parent=window;
const context={
  window,document:{getElementById(){return null;},addEventListener(){},querySelectorAll(){return[];},hidden:false},
  console,AbortController,setTimeout,clearTimeout,URL,Map,Set,Promise,JSON,Object,Array,String,Number,Boolean,Math,Date,RegExp,Error
};
context.globalThis=context;
vm.createContext(context);
vm.runInContext(fixtures,context,{filename:'fixtures.js'});
const data=context.DEMO_FIXTURES;
assert.equal(data.views.accounts.accounts.length,1);
assert.equal(data.views.other_accounts.accounts.length,1);
assert.equal(data.views.models.models.length,1);
assert.deepEqual(Array.from(data.modules,item=>item.id),['accounts','other_accounts','models']);
assert.doesNotMatch(JSON.stringify(data.modules),/config|connections|knowledge|agents/);
assert.ok(data.modules.every(item=>typeof item.name==='string'&&typeof item.group==='string'));
assert.ok(JSON.stringify(data).includes('demo.invalid'));
assert.ok(!JSON.stringify(data).match(/vault|secret|token|password|api[_-]?key/i));
vm.runInContext(app,context,{filename:'app.js'});
const modelDetail=await context.WORKBENCH_ADAPTER.call('model_detail',{id:'demo-model'});
assert.equal(modelDetail.model.id,'demo-model');
await assert.rejects(context.WORKBENCH_ADAPTER.call('credential_list'),/不支持凭据/);
await assert.rejects(context.WORKBENCH_ADAPTER.call('credential_details',{}),/不支持凭据/);

for(const page of data.modules.map(item=>item.id)){
  let html='';
  const root={set innerHTML(value){html=value;},get innerHTML(){return html;}};
  const testWindow={addEventListener(){}};
  testWindow.parent=testWindow;
  const renderContext={
    window:testWindow,
    document:{getElementById(id){return id==='app'?root:null;},addEventListener(){},querySelectorAll(){return[];},hidden:false},
    console,AbortController,setTimeout,clearTimeout,URL,Map,Set,Promise,JSON,Object,Array,String,Number,Boolean,Math,Date,RegExp,Error
  };
  renderContext.globalThis=renderContext;
  vm.createContext(renderContext);
  vm.runInContext(fixtures,renderContext,{filename:'fixtures.js'});
  vm.runInContext(app.replace("const INITIAL_PAGE='accounts';",`const INITIAL_PAGE='${page}';`),renderContext,{filename:`${page}.js`});
  await Promise.resolve();
  await Promise.resolve();
  assert.match(html,new RegExp(`data-module="${page}"`));
  assert.ok(!html.includes('undefined'),`${page} rendered an undefined navigation value`);
}
console.log('Static demo contract passed');
