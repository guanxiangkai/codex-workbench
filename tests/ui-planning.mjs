import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

const source=readFileSync(new URL('../ui/planning.js',import.meta.url),'utf8');
const window={confirm:()=>true},document={};
class CalendarDate extends Date { constructor(...args){super(...(args.length?args:['2026-09-27T12:00:00']));} }
class TestReader { readAsDataURL(){this.result='data:text/plain;base64,QQ==';this.onload();} }
runInNewContext(source,{window,document,Date:CalendarDate,Set,Error,Array,Object,String,RegExp,console,FileReader:TestReader,globalThis:{},setTimeout,clearTimeout});

const data={projects:[{id:'work',name:'工作台项目'},{id:'notes',name:'个人知识库'}],accounts:[{id:'codex',name:'Codex'}],tasks:[
 {id:'range',project_id:'work',title:'跨月交付',status:'pending',start_date:'2026-09-29',due_date:'2026-10-02'},
 {id:'start',project_id:'work',title:'仅开始日',status:'running',start_date:'2026-09-10',due_date:null},
 {id:'due',project_id:'work',title:'仅截止日',status:'completed',start_date:null,due_date:'2026-09-11'},
 {id:'undated',project_id:'work',title:'未排期工作',status:'failed',start_date:null,due_date:null},
 {id:'other',project_id:'notes',title:'不应出现在项目过滤中',status:'pending',start_date:'2026-09-10',due_date:null}
]};
const ui=window.WorkbenchPlanning.create({tool:async()=>({item:{}}),escape:x=>String(x).replace(/</g,'&lt;'),icon:n=>'<i>'+n+'</i>'});
ui.state.month='2026-09';
let html=ui.render('planning',data);
assert.match(html,/工作计划/);
assert.match(html,/跨月交付/);
assert.match(html,/↳ 跨月交付/,'跨日的后续日期应标记延续');
assert.match(html,/仅开始日/);
assert.match(html,/仅截止日/,'只有截止日期的工作项在截止日显示标题');
assert.match(html,/未排期工作/);
assert.doesNotMatch(html,/资料库/,'项目入口不显示独立资料库');

ui.state.project='work';
html=ui.render('planning',data);
assert.doesNotMatch(html,/不应出现在项目过滤中/,'项目过滤隔离工作项');
assert.equal(ui.state.month,'2026-09','切换项目不能改变已查看月份');
console.log('planning UI: month grid handles cross-month, single endpoint, undated, and project filtering');

ui.state.detail={id:'range',title:'跨月交付',project_id:'work',status:'running',version:3,start_date:'2026-09-29',due_date:'2026-10-02',note_status:'conflict',native_thread_id:'native-1',assets:[{id:'asset-1',name:'交付.png',mime:'image/png',version:2}],knowledge_refs:[{scope:'global',key:'k-1',title:'交付规范'}],runs:[{state:'failed',result:'已有输出',error:'已有错误',thread_id:'native-run'}]};
ui.state.view='detail';
const detail=ui.render('planning',data);
assert.match(detail,/<dt>Obsidian<\/dt><dd>保留手工笔记<\/dd>/);
assert.match(detail,/归档输出/);
assert.match(detail,/打开会话/);
assert.match(detail,/概览.*对话.*运行.*关联文件/);
ui.state.detailTab='runs';
assert.match(ui.render('planning',data),/已有输出/);
assert.match(ui.render('planning',data),/已有错误/);
ui.state.detailTab='files';
const files=ui.render('planning',data);
assert.match(files,/v2/,'附件版本数字正常显示');
assert.match(files,/关联知识/);
assert.match(files,/预览/);
console.log('planning UI: detail retains knowledge, Obsidian, output, run, native-session, and attachment actions');

ui.state.view='knowledge';
ui.state.knowledge=[{knowledge_key:'k-1',scope_key:'global',title:'交付规范',summary:'交付前检查'}];
const chooser=ui.render('planning',data);
assert.match(chooser,/data-planning-knowledge-scope/);
assert.match(chooser,/data-planning-knowledge-query/);
assert.match(chooser,/data-scope="global" data-key="k-1"/);
assert.doesNotMatch(chooser,/name="scope"|name="key"/,'知识关联只展示可搜索的选择器，不暴露 scope/key 技术字段');
console.log('planning UI: knowledge linking uses searchable approved-knowledge choices');

const nativeEvents=new Map();
let nativeUrl='';
const nativeRoot={addEventListener:(kind,fn)=>nativeEvents.set(kind,fn),removeEventListener:()=>{},querySelector:()=>null};
const nativeUI=window.WorkbenchPlanning.create({escape:String,tool:async()=>({item:{}}),openNative:async url=>{nativeUrl=url;}});
nativeUI.bind(nativeRoot);
nativeEvents.get('click')({target:{closest:()=>({dataset:{planningAction:'open-native',threadId:'native/run'}})},preventDefault(){}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(nativeUrl,'codex://threads/native%2Frun');
console.log('planning UI: native-session action uses the workbench Codex URL');

const events=new Map(),calls=[];
const node=(name,value)=>({name,value});
const task={querySelectorAll:()=>[node('title','保存任务'),node('prompt','说明'),node('start_date','2026-09-29'),node('due_date','2026-09-30')]};
const nodeList=items=>({length:items.length,item:n=>items[n],[Symbol.iterator]:function*(){yield*items}});
const form={matches:q=>q==='[data-planning-form]',querySelectorAll:q=>q==='[data-draft-item]'?nodeList([]):nodeList([node('project_id','work'),node('project_name',''),node('source','拆分说明')])};
const root={addEventListener:(kind,fn)=>events.set(kind,fn),removeEventListener:(kind,fn)=>{if(events.get(kind)===fn)events.delete(kind)},querySelector:q=>q==='[data-planning-form="draft"]'?form:null};
const interactive=window.WorkbenchPlanning.create({tool:async(name,args)=>{calls.push([name,args]);if(name==='planning_intake')return {draft:{warning:'请确认日期',items:[{intent:'create_task',title:'模型任务',prompt:'模型说明',start_date:'2026-09-29',due_date:'2026-09-30'}]}};return {item:{}};},escape:String,invalidate:()=>{}});
interactive.render('draft',data);
interactive.bind(root);interactive.bind(root);
const emit=name=>events.get('click')({target:{closest:()=>({dataset:{planningAction:name}})},preventDefault(){}});
emit('generate');
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(interactive.state.draft.items[0].title,'模型任务','planning_intake must unwrap draft');
assert.equal(interactive.state.draft.source,'拆分说明','模型来源元数据不能覆盖用户原始项目说明');
assert.match(interactive.render('planning',data),/拆分说明<\/textarea><\/label>/,'项目说明标签在按钮和工作项之前闭合');
assert.match(interactive.render('planning',data),/模型任务/,'host refresh keeps active draft');
assert.equal(calls.filter(([name])=>name==='planning_intake').length,1,'重复绑定不得重复请求');
console.log('planning UI: intake draft keeps the source and does not duplicate bindings');

const uploadEvents=new Map(),uploadCalls=[];
let uploadRedraws=0;
const uploadRoot={addEventListener:(kind,fn)=>uploadEvents.set(kind,fn),removeEventListener:()=>{},querySelector:()=>null};
const uploadUI=window.WorkbenchPlanning.create({escape:String,invalidate:()=>{uploadRedraws++;},tool:async(name,args)=>{uploadCalls.push([name,args]);if(name==='planning_detail')return {item:{id:'work-1',status:'pending'}};return {item:{}};}});
uploadUI.state.view='detail';uploadUI.state.detail={id:'work-1',status:'pending'};
uploadUI.bind(uploadRoot);
uploadEvents.get('change')({target:{id:'planning-file-input',files:[{name:'evidence.txt',type:'text/plain',size:1}]}});
await new Promise(resolve=>setTimeout(resolve,0));
const uploaded=uploadCalls.find(([name])=>name==='library_upload');
assert.equal(uploaded[1].name,'evidence.txt');
assert.equal(uploaded[1].mime,'text/plain');
assert.equal(uploaded[1].content_base64,'QQ==');
assert.equal(uploaded[1].task_id,'work-1');
assert.equal(uploadRedraws,1,'上传后立即刷新工作项附件');
console.log('planning UI: attachment upload uses the documented Base64 contract and detail association');

const knowledgeEvents=new Map();
const knowledgeRoot={addEventListener:(kind,fn)=>knowledgeEvents.set(kind,fn),removeEventListener:()=>{},querySelector:()=>null};
const knowledgeUI=window.WorkbenchPlanning.create({tool:async(name)=>name==='knowledge_list'?{scopes:[{id:'global',name:'全局'},{id:'project-scope',name:'工作台项目'}],knowledge:[]}:{item:{}}});
knowledgeUI.state.detail={id:'work-1',status:'pending'};
knowledgeUI.state.view='detail';
knowledgeUI.bind(knowledgeRoot);
knowledgeEvents.get('click')({target:{closest:()=>({dataset:{planningAction:'knowledge'}})},preventDefault(){}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.match(knowledgeUI.render('planning',data),/value="project-scope"/,'知识范围来自真实知识接口，而非不含 scopes 的计划快照');
knowledgeUI.leave();
console.log('planning UI: project instructions, attachment redraw, and knowledge scope loading retain their contracts');

const editEvents=new Map();
let refreshed=0;
const editFields=[node('title','排期后的工作项'),node('prompt','保留说明'),node('start_date','2026-10-01'),node('due_date','2026-10-03')];
const editForm={querySelectorAll:()=>nodeList(editFields)};
const editRoot={addEventListener:(kind,fn)=>editEvents.set(kind,fn),removeEventListener:()=>{},querySelector:q=>q==='[data-planning-form="edit"]'?editForm:null};
const editCalls=[];
const editUI=window.WorkbenchPlanning.create({refresh:async()=>{refreshed++;},tool:async(name,args)=>{editCalls.push([name,args]);return {item:{id:'work-1',status:'pending',version:2,...args.patch}};}});
editUI.state.detail={id:'work-1',status:'pending',version:1,title:'编辑前'};
editUI.state.edit={...editUI.state.detail};editUI.state.view='edit';editUI.bind(editRoot);
const editMarkup=editUI.render('planning',data);assert.match(editMarkup,/执行时自动使用当前默认账户/);assert.doesNotMatch(editMarkup,/name="execution_account_id"/);
const editAction=name=>editEvents.get('click')({target:{closest:()=>({dataset:{planningAction:name}})},preventDefault(){}});
editAction('save-edit');await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(editUI.state.detail.start_date,'2026-10-01');
assert.equal(Object.hasOwn(editCalls[0][1].patch,'execution_account_id'),false,'编辑任务不再固定执行账户');
assert.equal(editUI.state.edit,null,'保存后退出编辑态，不阻止后续执行状态刷新');
editAction('back');await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(refreshed,1,'返回月历重取工作项，立即显示新的日期与标题');
editUI.leave();
console.log('planning UI: edit dates return to an updated calendar and clear edit state');

// Historical occurrences are read-only even while their multi-day task is active.
const historyEvents=new Map(),historyCalls=[];
const span={id:'ongoing',title:'跨日工作',project_id:'work',status:'pending',version:1,start_date:'2026-09-25',due_date:'2026-09-30',assets:[{id:'a',name:'existing.png',mime:'image/png'}]};
const historyRoot={addEventListener:(kind,fn)=>historyEvents.set(kind,fn),removeEventListener:()=>{},querySelector:()=>null};
const historyUI=window.WorkbenchPlanning.create({tool:async(name,args)=>{historyCalls.push([name,args]);return {item:span};}});
historyUI.state.month='2026-09';historyUI.bind(historyRoot);
const historyAction=async(name,extra={})=>{historyEvents.get('click')({target:{closest:()=>({dataset:{planningAction:name,...extra}})},preventDefault(){}});await new Promise(resolve=>setTimeout(resolve,0));};
const calendar=historyUI.render('planning',data);
const day=key=>calendar.match(new RegExp('<section class="planning-day[^>]+aria-label="'+key+'[^]*?<\\/section>'))?.[0]||'';
assert.match(day('2026-09-25'),/planning-tone-4 [^"<]*past/);
assert.doesNotMatch(day('2026-09-25'),/quick-new/);
assert.match(day('2026-09-26'),/planning-tone-5/);
assert.match(day('2026-09-27'),/planning-tone-5 [^"<]*today/);
assert.match(day('2026-09-27'),/quick-new/);
assert.equal(new Set([...calendar.matchAll(/planning-tone-(\d)/g)].map(x=>x[1])).size,6);
await historyAction('detail',{taskId:span.id,date:'2026-09-25'});
let historical=historyUI.render('planning',data);
assert.match(historical,/2026-09-25 的历史安排，仅供查看/);
assert.doesNotMatch(historical,/data-planning-action="(?:edit|start|stop|archive|delete|export|pick-upload|knowledge|followup)"/);
historyUI.state.detailTab='files';
historical=historyUI.render('planning',data);
assert.match(historical,/data-planning-action="preview"/);
assert.match(historical,/data-planning-action="download-asset"/);
await historyAction('edit');await historyAction('start');
assert.equal(historyUI.state.view,'detail');
assert.deepEqual(historyCalls.map(x=>x[0]),['planning_detail'],'只读入口不能通过事件调用绕过');
await historyAction('detail',{taskId:span.id,date:'2026-09-27'});
assert.match(historyUI.render('planning',data),/data-planning-action="edit"/);
historyUI.state.detail={...span,due_date:'2026-09-26'};historyUI.state.occurrence='';
assert.doesNotMatch(historyUI.render('planning',data),/data-planning-action="edit"/,'从列表进入已过期任务同样只读');
historyUI.home();
await historyAction('dimension',{dimension:'projects'});
assert.match(historyUI.render('planning',data),/项目跟踪/);
assert.match(historyUI.render('planning',data),/任务看板/);
await historyAction('project-board',{projectId:'notes'});
assert.equal(historyUI.state.dimension,'projects');assert.equal(historyUI.state.boardProject,'notes');
assert.match(historyUI.render('planning',data),/按状态分列的项目任务/);
await historyAction('dimension',{dimension:'tasks'});
assert.match(historyUI.render('planning',data),/跨月交付/,'统计默认展示所有项目，不沿用日历过滤');
historyUI.home();assert.equal(historyUI.state.dimension,'calendar');assert.equal(historyUI.state.view,'planning');
historyUI.leave();
console.log('planning UI: six weekday hues, historical read-only actions, and three project dimensions');

assert.match(calendar,/<div class="planning-weekdays"><span class="planning-tone-5">周日<\/span>/);
assert.match(calendar,/<div class="planning-days"><section[^>]+aria-label="2026-08-30/,'九月日历从周日开始');
assert.match(calendar,/<select data-planning-project/);
assert.doesNotMatch(calendar,/周一至周五各一色|灰色覆盖的过去日期/);

const dashboardData={projects:[{id:'alpha',name:'产品升级'},{id:'beta',name:'资料整理'},{id:'empty',name:'新建空项目',created_at:'2026-09-20T09:00:00'},{id:'quiet',name:'无活动旧项目',created_at:'2026-01-01T09:00:00'}],accounts:[{id:'codex',name:'测试执行账户'}],tasks:[
 {id:'a',project_id:'alpha',title:'跨月完成',status:'completed',due_date:'2026-09-10',run_history:[{state:'review',created_at:'2026-08-31T09:00:00',finished_at:'2026-09-11T12:00:00'}]},
 {id:'b',project_id:'alpha',title:'执行中的任务',status:'running',start_date:'2026-09-26',due_date:'2026-09-30',run_history:[{state:'running',created_at:'2026-09-26T09:00:00'}]},
 {id:'c',project_id:'alpha',title:'重试任务',status:'failed',due_date:'2026-09-12',run_history:[{state:'failed',created_at:'2026-09-08T09:00:00',finished_at:'2026-09-08T10:00:00'},{state:'review',created_at:'2026-09-09T09:00:00',finished_at:'2026-09-10T10:00:00'},{state:'failed',created_at:'2026-09-12T09:00:00',finished_at:'2026-09-12T10:00:00'}]},
 {id:'d',project_id:'beta',title:'已排期未分配',status:'pending',start_date:'2026-09-28'},
 {id:'e',project_id:'beta',title:'已分配未排期',status:'pending',execution_account_id:'codex',created_at:'2026-09-05T09:00:00'},
 {id:'f',project_id:'beta',title:'上月已完成',status:'completed',due_date:'2026-08-31',run_history:[{state:'review',created_at:'2026-08-30T09:00:00',finished_at:'2026-08-31T12:00:00'}]},
 {id:'g',project_id:'beta',title:'未来错误运行记录',status:'completed',due_date:'2026-10-01',run_history:[{state:'review',created_at:'2026-10-01T09:00:00',finished_at:'2026-10-01T10:00:00'}]},
 {id:'h',project_id:'beta',title:'缺少真实时间',status:'completed',due_date:'2026-09-18',run_history:[{state:'review',created_at:'invalid',finished_at:null}]}
]};
const dashboardEvents=new Map();const searchInput={value:''};
const dashboardRoot={addEventListener:(kind,fn)=>dashboardEvents.set(kind,fn),removeEventListener:()=>{},querySelector:q=>q==='[data-planning-query]'?searchInput:null};
const dashboard=window.WorkbenchPlanning.create({tool:async(name,args)=>({item:dashboardData.tasks.find(t=>t.id===args.task_id)})});
const renderDashboard=()=>dashboard.render('planning',dashboardData);
dashboard.bind(dashboardRoot);
const dashboardAction=async(action,dataset={})=>{dashboardEvents.get('click')({target:{closest:()=>({dataset:{planningAction:action,...dataset}})},preventDefault(){}});await new Promise(resolve=>setTimeout(resolve,0));};
const metric=(html,id)=>Number(html.match(new RegExp('data-metric="'+id+'"[^>]*><span>[^<]*<\/span><strong>([0-9]+)<\/strong>'))?.[1]);
dashboard.state.month='2026-09';dashboard.state.statsDate='2026-09-27';dashboard.state.dimension='projects';dashboard.state.project='alpha';
let dashboardHtml=renderDashboard();
assert.match(dashboardHtml,/type="search" data-planning-query/);
assert.doesNotMatch(dashboardHtml,/data-planning-project|拆分项目工作|data-planning-action="new"|project-calendar/);
for(const name of ['产品升级','资料整理','新建空项目','无活动旧项目'])assert.match(dashboardHtml,new RegExp(name),'项目跟踪不继承日历过滤');
assert.match(dashboardHtml,/planning-project-card planning-color-/);
assert.doesNotMatch(dashboardHtml,/data-task-id="a"/,'项目任务在子页面展开');
dashboard.state.query='执行中的';dashboardHtml=renderDashboard();
assert.match(dashboardHtml,/产品升级/);assert.doesNotMatch(dashboardHtml,/资料整理/);
assert.match(dashboardHtml,/完成 1 \/ 3/,'搜索不改变项目整体进度');
await dashboardAction('project-board',{projectId:'beta'});
let boardHtml=renderDashboard();
for(const column of ['pending','running','completed','attention'])assert.match(boardHtml,new RegExp('planning-kanban-column" data-status="'+column+'"'));
const column=id=>boardHtml.match(new RegExp('<section class="planning-kanban-column" data-status="'+id+'"[^]*?<\/section>'))?.[0]||'';
assert.match(column('pending'),/已排期未分配/);assert.match(column('pending'),/已分配未排期/);
assert.match(boardHtml,/执行时自动使用当前默认账户/);assert.doesNotMatch(boardHtml,/未分配：尚未选择执行账户/);
const taskColor=boardHtml.match(/planning-kanban-task (planning-color-[0-7])" data-task-id="d"/)[1];
await dashboardAction('detail',{taskId:'d'});assert.equal(dashboard.state.view,'detail');
await dashboardAction('back');assert.equal(dashboard.state.boardProject,'beta');
assert.ok(renderDashboard().includes(taskColor+'" data-task-id="d"'),'返回看板后颜色稳定');
await dashboardAction('projects-back');assert.equal(dashboard.state.query,'执行中的');
searchInput.value='已分配未排期';
dashboardEvents.get('submit')({target:{matches:q=>q==='[data-planning-search-form]'},preventDefault(){}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.match(renderDashboard(),/资料整理/);assert.doesNotMatch(renderDashboard(),/产品升级/);
console.log('planning UI: Sunday-first calendar, project cards, board navigation and assignment columns');

await dashboardAction('dimension',{dimension:'tasks'});
let stats=renderDashboard();
for(const [key,n] of Object.entries({projects:3,involved:6,planned:5,executed:2,completed:2,running:1,pending:2,attention:1}))assert.equal(metric(stats,key),n,key);
assert.match(stats,/多项目对比/);assert.match(stats,/当前项目状态/);
assert.match(stats,/planning-trend-line planning-series-executed/);
assert.match(stats,/计划 5 项，实际执行 2 项，成功完成 2 项/);
assert.match(stats,/<th scope="row">11日<\/th><td>0<\/td><td>0<\/td><td>1<\/td>/,'跨期启动仍按真实日期完成');
assert.match(stats,/<th scope="row">28日<\/th><td>1<\/td><td>—<\/td><td>—<\/td>/,'未来实际值不伪造');
assert.match(stats,/1 条运行记录缺少时间/);
assert.doesNotMatch(stats,/无活动旧项目<\/strong>/);
await dashboardAction('stat-metric',{metric:'completed'});
let details=renderDashboard().split('id="planning-stat-detail"')[1].split('</section>')[0];
assert.match(details,/跨月完成/);assert.match(details,/重试任务/);assert.doesNotMatch(details,/缺少真实时间|未来错误运行记录/,'最近重试失败不会抹掉之前成功，缺失或未来时间不算完成');
await dashboardAction('stat-metric',{metric:'pending'});
details=renderDashboard().split('id="planning-stat-detail"')[1].split('</section>')[0];
assert.match(details,/已分配未排期/);assert.match(details,/已排期未分配/);
dashboard.state.statsProject='alpha';stats=renderDashboard();assert.equal(metric(stats,'executed'),2);assert.equal(metric(stats,'planned'),3);assert.equal(metric(stats,'projects'),1);
dashboard.state.statsProject='';dashboard.state.statsDate='2026-09-11';
await dashboardAction('stats-scale',{scale:'day'});stats=renderDashboard();
assert.equal(metric(stats,'executed'),0);assert.equal(metric(stats,'completed'),1,'本期完成数允许大于本期启动数');
assert.match(stats,/<th scope="row">12:00<\/th><td>0<\/td><td>1<\/td>/);
assert.doesNotMatch(stats,/planning-trend-line planning-series-planned/,'日期级计划不得虚构小时');
await dashboardAction('stats-scale',{scale:'quarter'});stats=renderDashboard();assert.match(stats,/2026 年 第 3 季度/);assert.match(stats,/<th scope="row">7月/);
await dashboardAction('stats-next');assert.equal(dashboard.state.statsDate,'2026-10-01');
await dashboardAction('stats-next');assert.equal(dashboard.state.statsDate,'2027-01-01');
await dashboardAction('stats-scale',{scale:'year'});await dashboardAction('stats-prev');assert.equal(dashboard.state.statsDate,'2026-01-01');
assert.match(renderDashboard(),/<th scope="row">12月/);assert.equal(metric(renderDashboard(),'completed'),3,'年度也排除未来完成时间');
dashboard.state.statsDate='2024-02-29';await dashboardAction('stats-scale',{scale:'month'});
stats=renderDashboard();assert.match(stats,/<th scope="row">29日/);assert.doesNotMatch(stats,/<th scope="row">30日/);
await dashboardAction('stats-next');assert.equal(dashboard.state.statsDate,'2024-03-01');
await dashboardAction('stats-prev');assert.equal(dashboard.state.statsDate,'2024-02-01');
await dashboardAction('stats-today');assert.equal(dashboard.state.statsDate,'2026-09-27');
dashboard.leave();
console.log('planning UI: period metrics, all-run deduplication, truthful cross-period completion and day/month/quarter/year navigation');

// Intake keeps confirmation separate from execution and uses the backend's idempotent save contract.
const intakeEvents=new Map(),intakeCalls=[];
const intakeRoot={addEventListener:(kind,fn)=>intakeEvents.set(kind,fn),removeEventListener:()=>{},querySelector:()=>null};
const intakeUI=window.WorkbenchPlanning.create({refresh:async()=>{},tool:async(name,args)=>{intakeCalls.push([name,args]);return name==='planning_intake_save'?{items:[{intent:'question',task_id:'work-1',message_id:'message-1'}],tasks:[]}:{item:{id:'work-1',version:2,status:'pending'}};}});
intakeUI.state.view='draft';intakeUI.state.draft={items:[{intent:'create_task',title:'确认范围',prompt:'请说明验收范围',project_id:'__new__',project_name:'新项目',section_id:'__new__',section_name:'新分区'}]};
const newProjectDraft=intakeUI.render('planning',data);
assert.match(newProjectDraft,/name="project_name"/);
assert.match(newProjectDraft,/name="section_name"/);
intakeUI.bind(intakeRoot);
intakeEvents.get('click')({target:{closest:()=>({dataset:{planningAction:'save-draft'}})},preventDefault(){}});
await new Promise(resolve=>setTimeout(resolve,0));
const savedIntake=intakeCalls.find(([name])=>name==='planning_intake_save');
assert.ok(savedIntake[1].operation_id.startsWith('intake-'));
assert.equal(JSON.stringify(savedIntake[1].items),JSON.stringify([{intent:'create_task',title:'确认范围',prompt:'请说明验收范围',project_name:'新项目',section_name:'新分区'}]));
assert.equal(intakeCalls.some(([name])=>name==='planning_start'),false,'保存问答不得隐式执行');
intakeUI.state.view='detail';intakeUI.state.detail={id:'work-1',version:2,status:'pending'};
intakeEvents.get('click')({target:{closest:()=>({dataset:{planningAction:'start',taskId:'work-1',messageId:'message-1'}})},preventDefault(){}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(JSON.stringify(intakeCalls.find(([name,args])=>name==='planning_start'&&args.message_id)[1]),JSON.stringify({task_id:'work-1',expected_version:2,message_id:'message-1'}));
intakeUI.leave();
console.log('planning UI: intake save remains non-executing and question runs carry message_id');

// Delivery stays a separate user acceptance record and file review starts from an asset.
const deliveryUI=window.WorkbenchPlanning.create({tool:async()=>({item:{}}),escape:String});
deliveryUI.state.view='detail';
deliveryUI.state.detail={id:'delivery-1',title:'验收任务',version:4,status:'pending',assets:[{id:'asset-1',name:'需求.pdf'}],delivery:{card:{card:{goal:'交付',scope:['当前任务'],preserve:['证据'],acceptance:[{id:'a-1',text:'可人工验收'}],facts:[],assumptions:[],original:'原文'}},evidence:[{id:'e-1',criterion_id:'a-1',executor_state:'completed',validation_state:'passed',user_acceptance:'pending'}]}};
deliveryUI.state.detailTab='delivery';
assert.match(deliveryUI.render('planning',data),/交付验收/);
assert.match(deliveryUI.render('planning',data),/执行.*验证.*用户验收/);
assert.match(deliveryUI.render('planning',data),/user_acceptance|用户验收/,'验收有独立字段与展示');
deliveryUI.state.detailTab='files';
assert.match(deliveryUI.render('planning',data),/当前任务来源检索/);
assert.match(deliveryUI.render('planning',data),/语义检索（GLM）会发送当前任务已关联的文本/);
console.log('planning UI: delivery acceptance is explicit and task-scoped source retrieval is disclosed');

// Evidence keeps the user-facing asset selector but sends the server its task-scoped structured anchor.
const evidenceEvents=new Map(),evidenceCalls=[];
const evidenceForm={querySelectorAll:()=>nodeList([node('criterion_id','a-1'),node('source','asset:asset-evidence'),node('executor_state','completed'),node('validation_state','passed'),node('check_name','人工核对'),node('result','结果一致'),node('summary','已核验附件')])};
const evidenceRoot={addEventListener:(kind,fn)=>evidenceEvents.set(kind,fn),removeEventListener:()=>{},querySelector:q=>q==='[data-planning-form="evidence"]'?evidenceForm:null};
const evidenceUI=window.WorkbenchPlanning.create({tool:async(name,args)=>{evidenceCalls.push([name,args]);return {item:{id:'evidence-task',version:8,status:'pending',delivery:{evidence:[]}}};}});
evidenceUI.state.view='detail';evidenceUI.state.detail={id:'evidence-task',version:7,status:'pending',assets:[{id:'asset-evidence',name:'synthetic-A1.txt',sha256:'sha256-a1'}],runs:[],delivery:{card:{card:{goal:'目标',scope:[],preserve:[],acceptance:[{id:'a-1',text:'条件'}],facts:[],assumptions:[]}}}};
evidenceUI.bind(evidenceRoot);
evidenceEvents.get('click')({target:{closest:()=>({dataset:{planningAction:'record-evidence'}})},preventDefault(){}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(JSON.stringify(evidenceCalls[0]),JSON.stringify(['planning_delivery',{task_id:'evidence-task',expected_version:7,action:'record_evidence',payload:{criterion_id:'a-1',executor_state:'completed',validation_state:'passed',source_ref:{kind:'asset',id:'asset-evidence',sha256:'sha256-a1'},check_name:'人工核对',result:'结果一致',summary:'已核验附件'}}]));
evidenceUI.leave();
console.log('planning UI: evidence submissions preserve task-scoped structured asset anchors');

// Delivery follow-ups stay deliberate: differences and supervised suggestions are visible,
// rework is saved from an annotation, and knowledge extraction remains an unreviewed candidate.
deliveryUI.state.detailTab='delivery';
deliveryUI.state.detail.knowledge_refs=[{scope:'project:delivery',title:'已关联范围'}];
deliveryUI.state.detail.delivery.annotations=[{id:'annotation-1',change_text:'保留证据并补充说明'}];
deliveryUI.state.detail.delivery.artifact_diff={changed:true,line_count:2,diff:'- 旧文本\n+ 新文本'};
deliveryUI.state.detail.delivery.review={decision:'revise',suggestions:['补充验证'],starts_runner:false};
deliveryUI.state.detail.delivery.metrics={rework_count:1,first_round_acceptance:'rejected',correction_acceptance:'pending',runtime_ms:null,stopped:false,failed:true};
deliveryUI.state.knowledgeCandidates=[{scope:'project:delivery',title:'旧会话候选',content:'不可替代详情记录',status:'candidate'}];
deliveryUI.state.detail.delivery.knowledge_candidates=[{id:'candidate-1',scope:'project:delivery',title:'候选',content:'待审核内容',status:'candidate',reviewed:false,local_only:true,applicability:{status:'unknown',reason:'pending_confirmation'},source:{kind:'asset',id:'asset-1'},card_revision:4,card_sha256:'card-hash',verified_at:'2026-09-29T10:00:00Z'}];
const deliveryHtml=deliveryUI.render('planning',data);
assert.match(deliveryHtml,/交付指标/);
assert.match(deliveryHtml,/返工次数[\s\S]*1/);
assert.match(deliveryHtml,/首轮验收[\s\S]*未接受/);
assert.match(deliveryHtml,/修正后验收[\s\S]*待确认/);
assert.match(deliveryHtml,/运行时长[\s\S]*未知/);
assert.match(deliveryHtml,/已停止[\s\S]*否/);
assert.match(deliveryHtml,/失败[\s\S]*是/);
assert.match(deliveryHtml,/附件版本差异/);
assert.match(deliveryHtml,/文本差异/);
assert.match(deliveryHtml,/保存返工说明/);
assert.match(deliveryHtml,/不会启动执行/);
assert.match(deliveryHtml,/已验收来源的知识候选/);
assert.match(deliveryHtml,/候选仅供查看，尚未审核或写入知识库/);
assert.match(deliveryHtml,/待审核内容/);
assert.match(deliveryHtml,/本地待审核/);
assert.match(deliveryHtml,/适用性待确认/);
assert.match(deliveryHtml,/来源与版本/);
assert.match(deliveryHtml,/card-hash/);
assert.doesNotMatch(deliveryHtml,/旧会话候选/,'重开详情必须使用持久化候选，而非旧会话状态');
console.log('planning UI: delivery rework, diff, review, and knowledge candidates remain explicit');

const actionEvents=new Map(),deliveryActions=[];
const actionRoot={addEventListener:(kind,fn)=>actionEvents.set(kind,fn),removeEventListener:()=>{},querySelector:()=>null};
const actionUI=window.WorkbenchPlanning.create({tool:async(name,args)=>{deliveryActions.push([name,args]);return {item:{id:'delivery-2',version:5,status:'pending',delivery:{annotations:[]}}};}});
actionUI.state.view='detail';
actionUI.state.detail={id:'delivery-2',version:4,status:'pending',delivery:{annotations:[{id:'annotation-2'}]}};
actionUI.bind(actionRoot);
actionEvents.get('click')({target:{closest:()=>({dataset:{planningAction:'prepare-rework',annotationId:'annotation-2'}})},preventDefault(){}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(JSON.stringify(deliveryActions[0]),JSON.stringify(['planning_delivery',{task_id:'delivery-2',expected_version:4,action:'prepare_rework',payload:{annotation_id:'annotation-2'}}]));
actionUI.leave();
console.log('planning UI: annotation rework dispatch is explicit and non-running');

// Freshness, proposal decisions, ownership and model quality stay in the existing detail.
const coopCalls=[],coopEvents=new Map();
const coopTask={id:'coop',title:'协作任务',version:3,status:'pending',delivery:{evidence:[{id:'stale-e',criterion_id:'ready',user_acceptance:'pending',freshness:{valid:false}}],knowledge_candidates:[{title:'旧知识候选',content:'示例',source_validity:{valid:false}}]},coordination:{ownership:{owner:'',paths:[],resources:[],dependencies:[]},readiness:{blockers:[]},handoffs:[{id:'packet',state:'received',stale:false,response:{suggestions:['<unsafe>请检查结果']},decisions:[],packet_sha256:'abc'}],model_calls:[{call_id:4,model_id:'verified-model',success:1,elapsed_ms:12,usage:null,cost:null,quality:null,adopted:null}]}};
const coopUI=window.WorkbenchPlanning.create({tool:async(name,args)=>{coopCalls.push([name,args]);return {item:coopTask};}});
coopUI.state.detail=coopTask;coopUI.state.detailTab='delivery';coopUI.state.view='detail';
const coopRoot={querySelector:()=>null,querySelectorAll:()=>[],addEventListener:(kind,fn)=>coopEvents.set(kind,fn),removeEventListener:()=>{}};
coopUI.bind(coopRoot);let coopMarkup=coopUI.render('planning',data);
assert.match(coopMarkup,/来源已变化，需重新核验/);
assert.doesNotMatch(coopMarkup,/data-evidence-id="stale-e"/);
assert.match(coopMarkup,/来源已失效，需重新核验/);
assert.match(coopMarkup,/&lt;unsafe&gt;/);
assert.match(coopMarkup,/Token 未知/);
assert.match(coopMarkup,/并行分工与依赖/);
coopUI.state.coordinationDrafts['advice-packet-0']={decision:'rejected',reason:'依据不足'};
coopEvents.get('click')({target:{closest:()=>({dataset:{planningAction:'advice-decision',formKey:'advice-packet-0',packetId:'packet',suggestionIndex:'0'}})},preventDefault(){}});
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(coopCalls[0][1].action,'advice_decision');
assert.equal(coopCalls[0][1].payload.reason,'依据不足');
assert.equal(coopCalls.length,1,'采纳决定不得启动执行器或再调用模型');
coopTask.coordination.handoffs[0].stale=true;
coopMarkup=coopUI.render('planning',data);assert.doesNotMatch(coopMarkup,/data-planning-action="advice-decision"/);
coopTask.coordination.handoffs[0].state='unknown';coopMarkup=coopUI.render('planning',data);assert.match(coopMarkup,/系统不会自动重复发送/);
coopUI.leave();
console.log('planning UI: stale evidence blocks acceptance, decisions do not execute, missing usage stays unknown');

// Delivery conditions retain stable IDs across a reordered task-card draft; duplicate IDs stop before a write.
const conditionEvents=new Map(),conditionCalls=[];
let conditionInput='第二项\ncriterion-new: 新条件\n第一项';
const conditionForm={querySelectorAll:()=>nodeList([node('goal','保留目标'),node('scope',''),node('preserve',''),node('acceptance',conditionInput),node('facts',''),node('assumptions','')])};
const conditionRoot={addEventListener:(kind,fn)=>conditionEvents.set(kind,fn),removeEventListener:()=>{},querySelector:q=>q==='[data-planning-form="delivery-card"]'?conditionForm:null};
const conditionUI=window.WorkbenchPlanning.create({tool:async(name,args)=>{conditionCalls.push([name,args]);return {item:{id:'conditions',version:2,status:'pending',delivery:{card:{card:args.payload?.card||conditionUI.state.detail.delivery.card.card}}}};}});
conditionUI.state.view='detail';conditionUI.state.detail={id:'conditions',version:1,status:'pending',delivery:{card:{card:{goal:'保留目标',scope:[],preserve:[],acceptance:[{id:'a-1',text:'第一项'},{id:'a-2',text:'第二项'}],facts:[],assumptions:[]}}}};
conditionUI.bind(conditionRoot);
const conditionAction=name=>conditionEvents.get('click')({target:{closest:()=>({dataset:{planningAction:name}})},preventDefault(){}});
conditionAction('save-card');await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(JSON.stringify(conditionCalls[0][1].payload.card.acceptance),JSON.stringify([{id:'a-2',text:'第二项'},{id:'criterion-new',text:'新条件'},{id:'a-1',text:'第一项'}]));
conditionInput='duplicate: 一\nduplicate: 二';conditionAction('save-card');await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(conditionCalls.length,1,'重复验收条件 ID 在发起写入前被拒绝');
assert.match(conditionUI.state.error,/验收条件标识重复/);
conditionUI.state.detailTab='delivery';
assert.doesNotThrow(()=>conditionUI.render('planning',data),'无效验收草稿重绘不能抛出异常');
assert.match(conditionUI.render('planning',data),/duplicate: 一/,'无效验收草稿必须原样回显');
conditionAction('governance-capture');await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(JSON.stringify(conditionCalls[1]),JSON.stringify(['planning_delivery',{task_id:'conditions',expected_version:2,action:'governance_capture',payload:{}}]));
conditionUI.leave();

// The existing delivery detail fold presents stale conditions, governance state, and recovery evidence without a new page.
const governedUI=window.WorkbenchPlanning.create({tool:async()=>({item:{}})});
governedUI.state.view='detail';governedUI.state.detailTab='delivery';governedUI.state.detail={id:'governed',title:'治理任务',version:1,status:'pending',delivery:{card:{card:{goal:'目标',scope:[],preserve:[],acceptance:[{id:'a-1',text:'条件'}],facts:[],assumptions:[]}},conditions:[{id:'a-1',text:'条件',current:false,reason:'condition_changed',evidence:[{source_ref:'run:1',freshness:{reason:'condition_changed'}}]}],evidence:[]},coordination:{handoffs:[{id:'packet-1',state:'unknown',packet_sha256:'packet',provider_operation_id:'op-1',side_effect_state:'completed',reconciliation_source:'provider-status',status_query_ref:'provider-status:op-1',status_evidence:{state:'completed'},last_event_id:'event-1',decisions:[]}],model_calls:[]},governance:{measurement_boundary:'规则文本行为评测；不代表 Skill 隐式触发、工具权限或生产验收',manifests:[{id:'manifest-1',created_at:'2026-09-29T10:00:00Z',manifest:{rules:[{id:'project:rules',source:'authorised-collector',content_hash:'hash-rules',load_state:'loaded'}]}}],suite:{baseline_manifest_hash:'baseline-1'},candidates:[{id:'candidate-1',state:'proposed',budget:{estimated_tokens:64},diff:'- old\n+ new'}],evaluations:[],metrics:{observations:2,evaluations:1,completed:{true:1,false:0,known:1,unknown:1,true_ratio:1},rework:{true:0,false:0,known:0,unknown:2,true_ratio:null},user_intervention:{true:0,false:0,known:0,unknown:2,true_ratio:null},regression:{true:0,false:0,known:0,unknown:2,true_ratio:null}}}};
const governedMarkup=governedUI.render('planning',data);
for(const label of ['验收条件追踪','已过期','condition_changed','规则治理与评测','检查规则清单','规则加载状态','project:rules','已加载','authorised-collector','hash-rules','估算 Token（UTF-8 字节/4）','真占比 100%','规则文本行为评测','候选差异','- old','操作 ID','副作用状态','对账来源','状态查询引用','状态证据'])assert.match(governedMarkup,new RegExp(label));
console.log('planning UI: stable delivery conditions, governance checks, and reconciliation evidence stay in the existing detail folds');
