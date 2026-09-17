import {readFileSync} from 'node:fs';
import {strict as assert} from 'node:assert';
import {webcrypto} from 'node:crypto';
import {runInNewContext} from 'node:vm';

const html=readFileSync(new URL('../ui/app.html',import.meta.url),'utf8');
assert.match(html,/\.task-board\{[^}]*overflow-x:auto/);
assert.match(html,/\.task-column-cards\{[^}]*overflow:auto/);
assert.match(html,/TASK_COLUMNS=\[\['backlog','待处理'\],\['ready','待执行'\],\['running','执行中'\],\['done','已完成'\],\['archived','已归档'\]\]/);
assert.doesNotMatch(html,/\['review','待审核'\]|\['failed','失败'\]|\['cancelled','已取消'\]/);
assert.match(html,/function taskGroup\(task\)\{return TASK_COLUMNS\.some\(\(\[state\]\)=>state===task\.state\)\?task\.state:null\}/);
assert.match(html,/source\?\.can_create_task!==false&&source\?\.native_status!==\'missing\'/);
assert.match(html,/session_native_status===\'missing\'/);
assert.match(html,/new-menu-options/);
console.log('Board scale and five-column contract passed');

const script=html.match(/<script>([\s\S]*)<\/script>/)[1];
function boot(page='agents',embedded=false,fetchImpl=async()=>({ok:true,json:async()=>({structuredContent:{}})}),navigation=null){
 const target={innerHTML:''};
 const document={activeElement:null,hidden:false,title:'',querySelector:s=>s==='#app'?target:null,querySelectorAll:()=>[],getElementById:()=>null,addEventListener(){},createElement:()=>({setAttribute(){},remove(){}}),body:{appendChild(){}}};
 const window={addEventListener(){},open:()=>({}),innerWidth:1440,innerHeight:900,...(navigation?{__workbenchNavigation:navigation}:{})},parent=embedded?{postMessage(){}}:window;
 runInNewContext(script.replace('__WORKBENCH_INITIAL_PAGE__',page),{window,parent,document,navigator:{clipboard:{writeText:async()=>{}}},TextEncoder,FormData:class {},crypto:webcrypto,setTimeout:()=>0,clearTimeout(){},btoa:v=>Buffer.from(v,'binary').toString('base64'),atob:v=>Buffer.from(v,'base64').toString('binary'),fetch:fetchImpl});
 return {target,api:window.__workbenchTest};
}

const {api}=boot('board');
const kinds=['backlog','ready','running','done','archived'];
const tasks=Array.from({length:5000},(_,i)=>({id:'t'+i,title:'任务 '+i,state:kinds[i%5],section_id:i%2?'s1':null,project_id:'p'+i%3,session_id:'c'+i%7,created_at:i+1}));
api.setState({tasks,sections:[],projects:[],sessions:[],sectionFilter:'',projectFilter:'',sessionFilter:'',query:''});
for(const sectionFilter of ['', '__none__','s1'])for(const projectFilter of ['','p1'])for(const sessionFilter of ['','c2']){
 api.setState({sectionFilter,projectFilter,sessionFilter});
 const filtered=api.filteredTasks(),counts=api.taskCounts(filtered);
 assert.equal(Object.values(counts).reduce((a,b)=>a+b,0),filtered.length);
 for(const key of kinds){const html=api.taskColumn(key,key,filtered,counts);assert.equal((html.match(/data-task="/g)||[]).length,counts[key])}
}
assert.equal(api.taskGroup({state:'unknown'}),null);
console.log('5000 synthetic tasks: five-column filters, counts and rendered-card counts agree');
