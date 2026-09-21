import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const window={};const context={window};vm.createContext(context);
vm.runInContext(fs.readFileSync(new URL('../ui/readonly-primitives.js',import.meta.url),'utf8'),context);
const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const components=window.createWorkbenchReadonlyPrimitives({escape,icon:name=>`<i>${name}</i>`});
assert.match(components.options([{id:'a',name:'<A>'}],'a','全部'),/value="a" selected/);
assert.match(components.select('kind','类别',components.options([], '', '全部')),/<label class="select">/);
assert.match(components.select('kind" data-test="x','类别',''),/id="kind&quot; data-test=&quot;x"/);
assert.equal(components.listCount(null,'项'),'<span class="count" role="status">0 项</span>');
assert.match(components.search('<页面>', 'x&y'),/搜索&lt;页面&gt;/);
assert.match(components.empty('<无数据>'),/&lt;无数据&gt;/);
assert.equal(components.toolbar('A','','B'),'<div class="toolbar">AB</div>');
assert.throws(()=>window.createWorkbenchReadonlyPrimitives({escape}),/依赖无效/);
console.log('Readonly primitives are context-injected, escaped, and reusable');
