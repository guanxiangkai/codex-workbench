import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.WORKBENCH_PLAYWRIGHT_MODULE||'playwright');
const root=process.env.WORKBENCH_TEST_ROOT||process.cwd();
const window={addEventListener(){}};window.parent=window;
const context={window,parent:window,INITIAL_PAGE:'other_accounts',READONLY_ICONS:JSON.parse(fs.readFileSync(root+'/ui/assets/readonly-icons.json','utf8')),WORKBENCH_MODULES:[{id:'other_accounts',name:'其他账户',group:'账户与配置'}],document:{getElementById(){return null;},addEventListener(){}},console,Map,Set,URL};
vm.createContext(context);vm.runInContext(fs.readFileSync(root+'/ui/readonly.js','utf8'),context);
const h=window.__workbenchReadonlyTest;
const account={id:'synthetic',label:'示例账户',provider_id:'minimax',provider_name:'MiniMax',usage_windows:[77,19,0,100].map((used,index)=>({id:'sample-'+index,label:['通用额度 · 5 小时','通用额度 · 本周','未使用示例','已用完示例'][index],usage:{used,remaining:100-used,limit:100,unit:'%'}}))};
const browser=await chromium.launch({headless:true,timeout:20000,...(process.env.WORKBENCH_BROWSER_EXECUTABLE?{executablePath:process.env.WORKBENCH_BROWSER_EXECUTABLE}:{})});
try{
 const page=await browser.newPage({viewport:{width:1000,height:950}});
 const card=h.otherAccountCard(account);
 const current=fs.readFileSync(root+'/ui/readonly.css','utf8');
 const measure=()=>page.locator('.progress').evaluateAll(bars=>bars.map(bar=>{const fill=bar.firstElementChild;return {remaining:Number(bar.getAttribute('aria-valuenow')),ratio:Math.round(fill.getBoundingClientRect().width/bar.getBoundingClientRect().width*100),height:fill.getBoundingClientRect().height,color:getComputedStyle(fill).backgroundColor,track:getComputedStyle(bar).backgroundColor};}));
 const set=css=>page.setContent('<!doctype html><html><meta charset="utf-8"><style>'+css+'</style><body><main data-module="other_accounts">'+card+'</main></body></html>');

 await set(current);
 for(const width of [1000,375]){
  await page.setViewportSize({width,height:950});const bars=await measure();assert.deepEqual(bars.map(x=>x.ratio),[23,81,100,0]);assert.deepEqual(bars.map(x=>x.remaining),[23,81,100,0]);
  for(const bar of bars){assert(bar.height>0);assert.notEqual(bar.color,'rgba(0, 0, 0, 0)');assert.notEqual(bar.color,bar.track);}
  console.log(JSON.stringify({viewport:width,bars}));
 }
 if(process.env.WORKBENCH_UI_SCREENSHOT){await page.setViewportSize({width:1000,height:950});await page.screenshot({path:process.env.WORKBENCH_UI_SCREENSHOT,fullPage:true});}
 // An unknown future card tone must still have a visible progress fill.
 await page.locator('.other-account').evaluate(card=>card.setAttribute('data-tone','unregistered'));
 assert((await measure()).every(bar=>bar.color!=='rgba(0, 0, 0, 0)'&&bar.color!==bar.track));
 console.log('Quota widths, visible fills, zero/full states, narrow layout and unknown-tone fallback passed.');
}finally{await browser.close();}
