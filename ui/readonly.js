/* 只读工作台：视图状态、宿主桥接与逐字段解密，均不提供业务写入。 */
(()=>{'use strict';
/** 仅翻译展示名称，原始标识保留用于查找和工具调用。 */
const CHINESE_TITLES={
 __proto__:null,
 "Docker Linux DevOps": "容器与系统运维",
 "Documents": "文档编辑",
 "PDF": "便携文档处理",
 "Presentations": "演示文稿制作",
 "Security Threat Model": "安全威胁建模",
 "Seedance 2.0 Skill OS": "视频创作助手",
 "sites:sites-building": "网站搭建",
 "sites:sites-hosting": "网站托管",
 "Software Quality": "软件质量保障",
 "Spreadsheets": "电子表格处理",
 "Excel Live Control": "表格实时控制",
 "Template Creator": "模板创建",
 "Visualize": "交互可视化",
 "Image Gen": "图像生成",
 "OpenAI Docs": "OpenAI 官方文档",
 "Plugin Creator": "插件创建",
 "Review Agent": "代码审查助手",
 "Skill Creator": "技能创建",
 "Skill Installer": "技能安装",
 "BodoniModa-Italic[opsz,wght]": "波多尼衬线字体（可变斜体）",
 "BodoniModa[opsz,wght]": "波多尼衬线字体（可变正体）",
 "OFL": "开放字体许可证",
 "hero-cinematic": "电影风格主视觉",
 "hero-command-center": "创作指挥台主视觉",
 "hero-dark": "深色主视觉",
 "hero-global-filmmaker-mode": "全球导演模式主视觉",
 "hero-light": "浅色主视觉",
 "infographic-cdn-delivery-map": "内容分发网络交付图",
 "infographic-production-delivery": "制作与交付流程图",
 "infographic-professional-qc-stack": "专业质量检查体系图",
 "infographic-reference-role-map": "参考素材角色关系图",
 "infographic-skill-capabilities": "技能能力信息图",
 "masthead-outlines": "刊头轮廓素材",
 "skill-map-cinematic": "电影风格技能地图",
 "skill-map": "技能地图",
 "skill-os-infographic": "技能系统信息图",
 "file-document": "文档文件图标",
 "file-presentation": "演示文稿文件图标",
 "file-spreadsheet": "电子表格文件图标",
 "plugin-creator-small": "插件创建图标（小）",
 "plugin-creator": "插件创建",
 "imagegen-small": "图像生成图标（小）",
 "imagegen": "图像生成",
 "openai-small": "官方文档图标（小）",
 "openai": "官方文档图标",
 "skill-installer-small": "技能安装图标（小）",
 "skill-installer": "技能安装",
 "site-preview": "网站预览图",
 "skill-creator-small": "技能创建图标（小）",
 "skill-creator": "技能创建",
 "search": "搜索",
 "chevron-down": "向下展开",
 "chevron-up": "向上收起",
 "chevron-left": "向左展开",
 "chevron-right": "向右展开",
 "arrow-left": "向左箭头",
 "arrow-right": "向右箭头",
 "check": "勾选",
 "x": "关闭",
 "plus": "添加",
 "minus": "减少",
 "menu": "菜单",
 "settings": "设置",
 "sliders-horizontal": "水平调节",
 "user": "用户",
 "users": "用户组",
 "folder": "文件夹",
 "folder-open": "打开文件夹",
 "file": "文件",
 "file-text": "文本文件",
 "copy": "复制",
 "eye": "显示",
 "eye-off": "隐藏",
 "lock": "锁定",
 "shield": "安全盾牌",
 "database": "数据库",
 "server": "服务器",
 "cloud": "云端",
 "globe": "网络",
 "mail": "邮件",
 "bell": "通知",
 "calendar": "日历",
 "clock": "时钟",
 "circle-question-mark": "帮助",
 "info": "信息",
 "circle-alert": "警告",
 "circle-check": "完成",
 "circle-x": "取消",
 "circle-pause": "暂停",
 "circle-play": "播放",
 "archive": "归档",
 "inbox": "收件箱",
 "layout-dashboard": "仪表盘布局",
 "list-filter": "列表筛选",
 "ellipsis": "更多",
 "palette": "调色板",
 "panels-top-left": "页面面板",
 "book-open": "阅读",
 "sparkles": "灵感",
 "code": "代码",
 "download": "下载",
 "upload": "上传",
 "external-link": "外部链接",
 "image": "图片",
 "heart": "喜爱",
 "star": "收藏",
 "refresh-cw": "刷新",
 "InterVariable": "英特无衬线可变字体",
 "LICENSE": "使用许可证",
 "fonts": "字体样式",
 "design-brief": "设计简报模板",
 "review-record": "评审记录模板",
 "semantic-page": "语义页面模板",
 "foundation": "基础设计令牌",
 "visualize": "交互可视化",
 "06-docker-linux-devops": "容器与系统运维",
 "documents:documents": "文档编辑",
 "pdf:pdf": "便携文档处理",
 "presentations:Presentations": "演示文稿制作",
 "security-threat-model": "安全威胁建模",
 "seedance-20": "视频创作助手",
 "software-quality": "软件质量保障",
 "spreadsheets:Spreadsheets": "电子表格处理",
 "spreadsheets:excel-live-control": "表格实时控制",
 "template-creator:template-creator": "模板创建",
 "visualize:visualize": "交互可视化",
 "openai-docs": "OpenAI 官方文档",
 "review-agent": "代码审查助手"
};
/** 优先使用已命名的中文标题；未知名称保留标识和中文类别，避免不同条目显示同一占位名称。 */
function chineseTitle(value,kind='条目'){
 const name=String(value||'').trim();
 return CHINESE_TITLES[name]||(/[\u3400-\u9fff]/.test(name)?name:name?`${kind} · ${name}`:kind);
}
function skillTitle(x){return CHINESE_TITLES[x.name]||chineseTitle(x.display_name||x.name,'技能');}
function accountTitle(x){for(const value of [x.display_name,x.username,x.name])if(typeof value==='string'&&value.trim()&&!value.includes('@'))return value.trim();return '用户名未提供';}
function avatarMarkup(account){
 const uri=typeof account.avatar_data_uri==='string'?account.avatar_data_uri:'';
 return /^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$/.test(uri)
  ?`<span class="avatar"><img src="${E(uri)}" alt=""></span>`
  :`<span class="avatar">${I('user')}</span>`;
}
const PAGES=Object.fromEntries(WORKBENCH_MODULES.map(x=>[x.id,x.name]));
const GROUPS=[...new Set(WORKBENCH_MODULES.map(x=>x.group))].map(name=>({name,pages:WORKBENCH_MODULES.filter(x=>x.group===name).map(x=>x.id)}));
const NAV={accounts:'user',other_accounts:'user',agents:'sparkles',models:'brain',config:'shield',projects:'folder',knowledge:'book-open',services:'server',connections:'globe'};
const STATES={backlog:['待处理','orange','inbox'],ready:['待执行','blue','clock'],running:['执行中','purple','circle-play'],done:['已完成','green','circle-check'],archived:['已归档','muted','archive']};
const TYPES={image_generation:'图像生成',video_generation:'视频生成',reasoning:'推理',multimodal:'多模态',speech_to_text:'语音转文字',text_to_speech:'文字转语音',embedding:'嵌入',rerank:'重排序',unconfigured:'未配置'};
const RESULTS={completed:'成功',failed:'失败',interrupted:'已中断',cancelled:'已取消',inProgress:'执行中'};
const VALIDATION={verified:'验证通过',failed:'上次验证失败',pending:'未验证',validating:'已记录：验证中',paused:'已暂停'};
const KINDS={private_key:'私钥',certificate:'证书',api_key:'API Key',credential:'账户凭证',secret:'密钥',token:'Token',password:'密码',ssh:'SSH',other:'配置记录',unknown:'未识别结构'};
const LABELS={type:'类型',purpose:'用途',source:'来源',authentication_tested:'已验证认证',account:'账户',username:'用户名',password:'密码',key:'Key',api_key:'API Key',token:'Token',endpoint:'服务地址',base_url:'接口地址',host:'主机',ip:'IP 地址',port:'端口',database:'数据库',database_type:'数据库类型',database_index:'数据库索引',namespace:'命名空间',group:'配置组',bucket:'Bucket',region:'Region',access_key:'Access Key',secret_key:'Secret Key',private_key:'私钥',public_key:'公钥',description:'说明',remote_path:'路径',passphrase:'口令',certificate:'证书',kubeconfig:'Kubeconfig',extra_config:'扩展配置'};
const A=x=>Array.isArray(x)?x:[];const E=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const I=(name,cls='')=>`<span class="icon ${E(cls)}" aria-hidden="true">${READONLY_ICONS[name]||READONLY_ICONS['file-text']||''}</span>`;
const badge=(name,tone='muted')=>`<span class="badge ${E(tone)}">${E(name)}</span>`;
// 类型配色只表达类别；验证、登录和启用状态保留独立的语义色。
const CATEGORY={
 image_generation:['image','rose'],video_generation:['circle-play','rose'],reasoning:['brain','indigo'],multimodal:['sparkles','violet'],speech_to_text:['file-text','cyan'],text_to_speech:['bell','cyan'],embedding:['database','teal'],rerank:['list-filter','amber'],
 icon:['palette','violet'],image:['image','rose'],font:['book-open','indigo'],template:['panels-top-left','amber'],text:['file-text','teal'],tokens:['sliders-horizontal','cyan'],stylesheet:['code','indigo'],license:['shield','slate'],
 private_key:['key','violet'],certificate:['shield','teal'],api_key:['key','amber'],secret:['lock','amber'],token:['key','violet'],password:['lock','rose'],ssh:['terminal','indigo'],credential:['user','blue'],other:['settings','slate'],unknown:['circle-question-mark','slate']
};
Object.assign(CATEGORY,{postgresql:['database','blue'],database:['database','blue'],redis:['database','rose'],mongodb:['database','teal'],s3:['server','amber'],file_server:['server','amber'],github:['code','violet'],gitee:['code','rose'],gitlab:['code','amber'],gpg:['key','teal'],frontend_release:['code','blue'],backend_release:['server','indigo'],model_service:['brain','violet'],nacos:['settings','cyan'],dify:['sparkles','teal'],service:['server','slate']});
const category=k=>CATEGORY[k]||['file-text','slate'];
const typeBadge=(label,k)=>`<span class="badge type-badge" data-tone="${category(k)[1]}">${I(category(k)[0])}${E(label)}</span>`;
const symbol=(icon)=>`<span class="module-symbol">${I(icon)}</span>`;
/** 技能与知识共用主题图标；颜色只表示内容类别，不表示启用或审核状态。 */
const CARD_THEMES={
 engineering:['terminal','slate','工程规范'],architecture:['panels-top-left','indigo','架构设计'],
 backend:['server','blue','后端开发'],frontend:['code','teal','前端开发'],
 database:['database','cyan','数据管理'],operations:['settings','slate','系统运维'],
 models:['brain','violet','模型与推理'],agents:['sparkles','violet','智能应用'],
 workflow:['clock','amber','工作流程'],research:['search','blue','调研查询'],
 writing:['file-text','blue','文档编写'],contract:['list-filter','cyan','接口契约'],
 browser:['globe','cyan','浏览器与网站'],consistency:['refresh-cw','teal','数据一致性'],
 knowledge:['book-open','teal','知识管理'],security:['shield','rose','安全防护'],
 video:['circle-play','rose','视频创作'],quality:['circle-check','teal','质量验收'],
 spreadsheet:['columns-3','teal','电子表格'],liveSheet:['sliders-horizontal','cyan','实时表格'],
 template:['copy','amber','模板复用'],visualization:['layout-dashboard','indigo','可视化'],
 design:['palette','rose','视觉设计'],image:['image','rose','图像创作'],
 docs:['graduation-cap','blue','官方文档'],plugins:['plus','violet','插件扩展'],
 review:['eye','teal','代码审查'],skillCreate:['star','amber','技能创建'],
 skillInstall:['download','blue','技能安装'],account:['users','blue','账户管理'],
 credentials:['key','amber','身份凭据'],privacy:['lock','rose','隐私与加密'],
 storage:['cloud','cyan','同步与存储'],archive:['archive','slate','归档与恢复'],
 organization:['folder','indigo','资料组织']
};
const SKILL_THEMES={
 '01-personal-engineering-standard':'engineering','02-software-architect':'architecture',
 '03-java-spring-enterprise':'backend','04-vue3-enterprise':'frontend','05-database-engineer':'database',
 '06-docker-linux-devops':'operations','07-llm-engineer':'models','08-ai-application-engineer':'agents',
 '09-python-ai-service':'backend','10-workflow-automation':'workflow','11-research-agent':'research',
 '12-technical-writer':'writing','api-contract-check':'contract','backend-contract':'contract',
 'browser-acceptance':'browser','data-consistency-check':'consistency','frontend-state':'frontend',
 'manage-personal-knowledge':'knowledge','security-threat-model':'security','seedance-20':'video',
 'software-quality':'quality','web-design':'design','documents:documents':'writing','pdf:pdf':'writing',
 'presentations:presentations':'visualization','spreadsheets:spreadsheets':'spreadsheet',
 'spreadsheets:excel-live-control':'liveSheet','template-creator:template-creator':'template',
 'visualize:visualize':'visualization','imagegen':'image','openai-docs':'docs','plugin-creator':'plugins',
 'review-agent':'review','skill-creator':'skillCreate','skill-installer':'skillInstall',
 'sites:sites-building':'browser','sites:sites-hosting':'storage'
};
/** 优先使用标题判定知识主题，标签只在标题没有明确主题时补充。 */
const KNOWLEDGE_THEMES=[
 [/多账|账号切换|账户|account-switch/i,'account'],
 [/凭据|默认身份|credential|identity|key-vault/i,'credentials'],
 [/加密|秘密|私有|隐私|privacy|secret|encrypted/i,'privacy'],
 [/模型|推理|model-routing|inference/i,'models'],
 [/图标|配色|视觉|颜色|icon|visual|color/i,'design'],
 [/校验|验收|反馈|测试|validation|evaluation|feedback/i,'quality'],
 [/扫描|调研|研究|research/i,'research'],
 [/自我进化|self-evolution/i,'agents'],
 [/备份|恢复范围|归档|backup|archive/i,'archive'],
 [/文稿|文档|正式输出|markdown|document|artifact/i,'writing'],
 [/重装|云端|同步|icloud|reinstall|storage/i,'storage'],
 [/组织|分类|命名|公司|体系|organization|folder/i,'organization'],
 [/任务|流程|自动化|workflow|heartbeat|idempotency/i,'workflow'],
 [/知识|沉淀|knowledge/i,'knowledge']
];
function knowledgeTheme(item){
 for(const text of [String(item.title||''),A(item.tags).join(' ')]){
  const match=KNOWLEDGE_THEMES.find(([pattern])=>pattern.test(text));if(match)return match[1];
 }
 return 'knowledge';
}
/** 已登记技能按稳定名称匹配；新增技能用中文能力标题选择相同主题体系。 */
function skillTheme(item){
 const name=String(item.name||'').toLowerCase();
 if(Object.hasOwn(SKILL_THEMES,name))return SKILL_THEMES[name];
 return knowledgeTheme({title:skillTitle(item),tags:[]});
}
function cardSymbol(theme){
 const [icon,tone,label]=CARD_THEMES[theme]||CARD_THEMES.knowledge;
 return `<span class="module-symbol subject-symbol" data-tone="${tone}" title="${label}">${I(icon)}</span>`;
}

function tagBadge(label){const tones=['violet','blue','teal','rose','amber','cyan'];let hash=0;for(const ch of String(label))hash=(hash*31+ch.codePointAt(0))>>>0;return `<span class="badge tag-badge" data-tone="${tones[hash%tones.length]}">${E(label)}</span>`;}
const num=v=>typeof v==='number'&&Number.isFinite(v)?v:null;
const fmtDate=v=>v==null?'未提供':new Date(typeof v==='number'?v*1000:v).toLocaleString('zh-CN',{hour12:false});
const duration=v=>num(v)===null?'未提供':v<1000?`${Math.round(v)} 毫秒`:v<60000?`${Math.floor(v/1000)} 秒`:`${Math.floor(v/60000)} 分 ${Math.floor(v/1000)%60} 秒`;
function resetTime(v,now=Date.now()){if(num(v)===null)return '未提供';const d=v*1000-now;return d<=0?'等待重置更新':d>=86400000?`${Math.floor(d/86400000)} 天后重置`:d>=3600000?`${Math.floor(d/3600000)} 小时后重置`:`${Math.max(1,Math.ceil(d/60000))} 分钟后重置`;}
const nativeUrl=u=>typeof u==='string'&&/^codex:\/\/threads\/[a-zA-Z0-9_-]+$/.test(u)?u:null;
function canonicalKey(value){if(Array.isArray(value))return '['+value.map(canonicalKey).join(',')+']';if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+canonicalKey(value[key])).join(',')+'}';return JSON.stringify(value);}
function timestamp(value){if(typeof value==='number'&&Number.isFinite(value))return Math.abs(value)<100_000_000_000?value*1000:value;const parsed=typeof value==='string'?Date.parse(value):NaN;return Number.isFinite(parsed)?parsed:null;}
function timestampOf(record,fields){for(const field of fields){const value=timestamp(record?.[field]);if(value!==null)return value;}return null;}
function recordTimestamp(record){return timestampOf(record,['updated_at','created_at','observed_at','updatedAt','createdAt','observedAt','last_checked_at','lastCheckedAt']);}
/** 未提供时间的来源顺序固定；仅把有权威时间的任务填回原来的已知时间槽位。 */
function newestBy(items,getTimestamp){const result=A(items).slice(),known=[];result.forEach((item,index)=>{const time=getTimestamp(item);if(time!==null)known.push({item,index,time});});const slots=known.map(entry=>entry.index);known.sort((a,b)=>b.time-a.time||a.index-b.index);slots.forEach((slot,index)=>{result[slot]=known[index].item;});return result;}
function newestRecords(items){return newestBy(items,recordTimestamp);}
const findText=(items,q,fields)=>!q?A(items):A(items).filter(x=>fields.some(k=>[String(x[k]??''),CHINESE_TITLES[x[k]]||''].some(value=>value.toLocaleLowerCase().includes(q.toLocaleLowerCase()))));
function payloadRoot(p){if(p.format==='text')return {text:p.text};if(['workbench_credential_v1','workbench_credential'].includes(p.format)&&p.fields?.credential!==undefined)return p.fields.credential;return p.fields;}
function fieldPaths(value,path=[],result=[],depth=0){if(result.length>=256)return result;if(value!==null&&typeof value==='object'&&depth<8&&Object.keys(value).length){for(const key of Object.keys(value)){if(result.length>=256)break;fieldPaths(value[key],[...path,key],result,depth+1);}}else result.push({path,type:value===null?'null':Array.isArray(value)?'array':typeof value});return result;}
function fieldValue(root,path){let value=root;for(const key of path){if(value===null||typeof value!=='object'||!Object.hasOwn(value,key))throw Error('字段已变化，请重新打开详情');value=value[key];}return typeof value==='string'?value:JSON.stringify(value,null,2);}
function foldersIn(folders,id){const result=new Set(id?[id]:[]);for(let n=0;n<folders.length;n++){let changed=false;for(const f of folders)if(result.has(f.parent_id)&&!result.has(f.id)){result.add(f.id);changed=true;}if(!changed)break;}return result;}
const readResult=r=>{if(r?.isError||r?.error)throw Error(r?.structuredContent?.error?.message||r.error?.message||'读取失败，请重试');const v=r?.structuredContent??r;if(!v||typeof v!=='object')throw Error('读取结果格式无效');return v;};
/** 公共读取及宿主消息统一限制等待时间；取消时释放计时器和监听器。 */
async function boundedRead(run,signal){
 const controller=new AbortController();
 const cancel=()=>controller.abort(signal.reason||new DOMException('读取已取消','AbortError'));
 let timer,onAbort;
 if(signal?.aborted)cancel();else signal?.addEventListener('abort',cancel,{once:true});
 try{
  const interrupted=new Promise((_,reject)=>{
   onAbort=()=>reject(controller.signal.reason);
   if(controller.signal.aborted)onAbort();else controller.signal.addEventListener('abort',onAbort,{once:true});
  });
  timer=setTimeout(()=>controller.abort(Error('读取超时，请重新读取')),45000);
  return await Promise.race([interrupted,Promise.resolve().then(()=>{controller.signal.throwIfAborted();return run(controller.signal);})]);
 }finally{clearTimeout(timer);signal?.removeEventListener('abort',cancel);controller.signal.removeEventListener('abort',onAbort);}
}
/** 网络错误仅说明连接未完成；不把断网、浏览器拦截或服务暂不可用误报为数据丢失。 */
async function fetchView(url,options){
 try{return await fetch(url,options);}
 catch(error){
  if(options.signal?.aborted)throw options.signal.reason||error;
  if(error?.name==='TypeError')throw Error('暂时无法连接本机工作台，请点击“重试”；若持续失败，请重新打开工作台。');
  throw error;
 }
}
class Bridge{
 constructor(){this.embedded=window.parent!==window;this.pending=new Map();this.ready=null;window.addEventListener('message',e=>{if(e.source!==parent||!e.data||e.data.jsonrpc!=='2.0')return;const d=e.data;if(d.method==='ui/notifications/tool-result'){receiveHostResult(d.params);return;}if(d.id&&this.pending.has(d.id)){const p=this.pending.get(d.id);d.error?p.reject(Error('宿主未完成读取请求')):p.resolve(d.result);}});}
 rpc(method,params,signal){return boundedRead(active=>new Promise((resolve,reject)=>{
  const id=crypto.randomUUID();
  const finish=(callback,value)=>{this.pending.delete(id);active.removeEventListener('abort',cancel);callback(value);};
  const cancel=()=>finish(reject,active.reason);
  active.addEventListener('abort',cancel,{once:true});
  this.pending.set(id,{resolve:value=>finish(resolve,value),reject:error=>finish(reject,error)});
  try{parent.postMessage({jsonrpc:'2.0',id,method,params},'*');}catch(e){finish(reject,e);}
 }),signal);}
 /** 原生读取先完成宿主握手；首屏缓存可提前渲染，不能提前调用工具。 */
 async initialize(){
  if(!this.embedded)return;
  if(!this.ready)this.ready=this.rpc('ui/initialize',{appInfo:{name:'codex-workbench-ui',version:'0.9.1'},appCapabilities:{},protocolVersion:'2026-01-26'}).then(()=>{parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/initialized',params:{}},'*');}).catch(error=>{this.ready=null;throw error;});
  return this.ready;
 }
 async tool(name,args={},signal){
  if(this.embedded){await this.initialize();signal?.throwIfAborted();return readResult(await this.rpc('tools/call',{name,arguments:args},signal));}
  return boundedRead(async active=>{
   const response=await fetchView('/rpc',{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Request':'1'},body:JSON.stringify({name,arguments:args}),signal:active});
   if(!response.ok)throw Error('读取失败（HTTP '+response.status+'）');
   return readResult(await response.json());
  },signal);
 }
 async openDocumentation(url){if(!['https://developers.openai.com/api/reference/resources/images/methods/generate','https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create'].includes(url))throw Error('文档地址无效');if(this.embedded)return this.rpc('ui/open-link',{url});const a=document.createElement('a');a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.click();}
 async open(url){if(!nativeUrl(url))throw Error('原生会话地址无效');if(this.embedded)return this.rpc('ui/open-link',{url});const a=document.createElement('a');a.href=url;a.click();}
}
const s={page:Object.hasOwn(PAGES,INITIAL_PAGE)?INITIAL_PAGE:(WORKBENCH_MODULES[0]?.id||'agents'),data:{},query:'',section:'',project:'',session:'',folder:'',kind:'',modelProvider:'',configTag:'',accountProvider:'',configProvider:'',collapsedFolders:new Set(),loading:false,syncing:false,usageRefreshing:false,error:'',seq:0,detail:null,detailSeq:0,revealed:new Map(),fieldBusy:false,scope:'global',searchOpen:false,searchQuery:'',searchResults:[],searchBusy:false,searchSeq:0,searchError:''};
const root=document.getElementById('app');const bridge=new Bridge();let abort,snapshotPollTimer,snapshotPollAttempts=0;
// 保存状态独立于列表读取，切页和刷新不会重放写入。
s.accountSaving=null;
const viewCache=new Map();let cacheContext=null;
/** 只替换增量中的条目，保留未变化对象引用；版本不匹配时拒绝拼接。 */
function applySync(cached,response){
 if(response.unchanged){if(!cached||cached.revision!==response.revision)throw Error('缓存版本已失效');return cached;}
 if(response.reset){if(!response.data||typeof response.data!=='object')throw Error('同步数据无效');return {revision:response.revision,data:response.data};}
 if(!cached||cached.revision!==response.base_revision)throw Error('缓存版本已失效');
 const data={...cached.data},patch=response.patch;
 if(!patch||typeof patch!=='object')throw Error('同步数据无效');
 for(const key of A(patch.remove))delete data[key];
 for(const [key,value] of Object.entries(patch.set||{}))Object.defineProperty(data,key,{value,enumerable:true,writable:true,configurable:true});
 for(const [key,change] of Object.entries(patch.collections||{})){
  const values=new Map(A(data[key]).map(item=>[item[change.key],item]));
  for(const id of A(change.remove))values.delete(id);
  for(const item of A(change.upsert))values.set(item[change.key],item);
  const order=change.order??[...values.keys()];
  if(!Array.isArray(order)||order.some(id=>!values.has(id)))throw Error('同步集合无效');
  Object.defineProperty(data,key,{value:order.map(id=>values.get(id)),enumerable:true,writable:true,configurable:true});
 }
 return {revision:response.revision,data};
}
function rememberView(key,value){
 viewCache.delete(key);viewCache.set(key,value);
 while(viewCache.size>32||[...viewCache.values()].reduce((size,x)=>size+JSON.stringify(x.data).length,0)>4*1024*1024)viewCache.delete(viewCache.keys().next().value);
}
function installBootstrap(){
 const bootstrap=typeof WORKBENCH_BOOTSTRAP==='object'?WORKBENCH_BOOTSTRAP:null;if(!bootstrap||!Array.isArray(bootstrap.views))return false;
 if(typeof bootstrap.context==='string'&&bootstrap.context)cacheContext=bootstrap.context;
 let current=null;
 for(const item of bootstrap.views){
  if(!item||typeof item!=='object'||!item.args||typeof item.args!=='object'||typeof item.revision!=='string'||!item.revision||!item.data||typeof item.data!=='object')continue;
  const args={...item.args},data={...item.data,view:item.data.view||args.view};if(!Object.hasOwn(PAGES,args.view)||args.view!==data.view)continue;
  const record={revision:item.revision,data,args};rememberView(canonicalKey(args),record);
  const pageMatches=args.view===s.page;
  if(pageMatches&&(!current||canonicalKey(args)===canonicalKey({view:s.page})))current=record;
 }
 if(!current){if(typeof bootstrap.error==='string'&&bootstrap.error)s.error=bootstrap.error;return typeof bootstrap.error==='string'&&!!bootstrap.error;}
 s.data=current.data;s.loading=false;s.syncing=false;s.error=typeof bootstrap.error==='string'?bootstrap.error:'';if(s.page==='knowledge')s.scope=s.data.selected_scope||current.args?.scope||s.scope;return true;
}

/** 原生入口的首屏结果由宿主推送；不要忽略已有数据继续等待重复查询。 */
function receiveHostResult(result){
 if(!root||!bridge.embedded||s.detail||Object.keys(s.data).length)return;
 try{
  const value=readResult(result);
  if(value.view!==s.page)return;
  const {_sync,...data}=value;
  if(!Object.hasOwn(PAGES,data.view))return;
  s.seq++;abort?.abort();clearSecrets();s.data=data;s.loading=false;s.syncing=false;s.error='';
  if(_sync&&typeof _sync.revision==='string'&&typeof _sync.context==='string'){
   if(cacheContext!==null&&cacheContext!==_sync.context)viewCache.clear();
   cacheContext=_sync.context;
   const next={revision:_sync.revision,data},args={view:data.view};
   rememberView(canonicalKey(args),next);
  }
  render();
 }catch(e){s.seq++;abort?.abort();s.loading=false;s.syncing=false;s.error=e.message;render();}
}
function clearSecrets(){s.detailSeq++;s.revealed.clear();s.fieldBusy=false;if(s.detail){s.detail.fields=[];s.detail.fieldsState='idle';s.detail.loading=false;}}
function message(value){const toast=document.getElementById('toast');if(toast)toast.textContent=value;}
const NATIVE_COLORS={blue:'#3566bc',green:'#087565',orange:'#b85a18',yellow:'#a87810',purple:'#7957bc',pink:'#bd448b',red:'#ba4545',gray:'#626b78',grey:'#626b78',default:'#20242c'};
const NATIVE_SYMBOLS={customize:'sliders-horizontal',logs:'file-text',book:'book-open','currency-dollar':'badge-dollar-sign',health:'heart',plant:'leaf'};
function color(v){return /^#[0-9a-f]{6}$/i.test(v||'')?v:Object.hasOwn(NATIVE_COLORS,v)?NATIVE_COLORS[v]:NATIVE_COLORS.default;}
/** 从原生外观投影图标；SVG 只进入独立图片上下文，不注入页面 DOM。 */
function entityIcon(entity,fallback='folder'){
 const icon=entity?.icon,value=icon?.value;let content=I(fallback);
 if(icon?.kind==='emoji'&&typeof value==='string'&&value.length<=64)content=`<span class="icon emoji" aria-hidden="true">${E(value)}</span>`;
 else if(icon?.kind==='symbol'&&typeof value==='string'){
  const symbol=Object.hasOwn(NATIVE_SYMBOLS,value)?NATIVE_SYMBOLS[value]:value;
  if(Object.hasOwn(READONLY_ICONS,symbol))content=I(symbol);
 }else if(icon?.kind==='svg'&&typeof value==='string'&&value.length<=65536&&/^\s*<svg[\s>]/i.test(value)){
  content=`<img class="icon" alt="" src="data:image/svg+xml,${E(encodeURIComponent(value))}">`;
 }
 return `<span class="entity-icon" style="color:${color(entity?.color)}">${content}</span>`;
}
function entityTag(entity,withIcon=true){return `<span class="project-tag" style="color:${color(entity.color)}">${withIcon?entityIcon(entity):''}${E(entity.name)}</span>`;}

function options(items,value,empty){return `<option value="">${E(empty)}</option>`+A(items).map(x=>`<option value="${E(x.id)}" ${x.id===value?'selected':''}>${E(x.name||x.title||x.id)}</option>`).join('');}
function select(id,label,content){return `<label class="select"><span class="sr-only">${E(label)}</span><select id="${id}">${content}</select>${I('chevron-down')}</label>`;}
function search(){return `<label class="search">${I('search')}<span class="sr-only">搜索${PAGES[s.page]}</span><input id="query" placeholder="搜索${PAGES[s.page]}" value="${E(s.query)}" autocomplete="off"></label>`;}
function empty(text='暂无数据'){return `<div class="empty">${I('inbox')}<p>${E(text)}</p></div>`;}
function entryTitle(e){return chineseTitle(e.label||e.name||e.id,'配置');}
function projectMeta(t){const p=A(s.data.projects).find(x=>x.id===t.project_id),sec=A(s.data.sections).find(x=>x.id===t.section_id);return [sec,p].filter(Boolean).map(entityTag).join('');}
function sessionsFiltered(){return A(s.data.sessions).filter(x=>(!s.section||(s.section==='__none__'?!x.section_id:x.section_id===s.section))&&(!s.project||x.project_id===s.project));}
function orderedAccounts(items){const sorted=newestRecords(items);return [...sorted.filter(a=>a.is_current),...sorted.filter(a=>!a.is_current)];}
function accountView(){
 const items=orderedAccounts(findText(s.data.accounts,s.query,['display_name','username','name','email']));
 return `<div class="toolbar">${search()}</div><div class="account-grid">${items.map(a=>{
  const percent=num(a.remaining_percent);
  const status={ready:'',not_logged_in:'未登录',identity_mismatch:'身份待确认',unavailable:'暂未读取'}[a.login_status]||'未读取';
  return `<article class="card account" data-tone="${a.is_current?'green':a.is_default?'yellow':'blue'}"><div class="identity">${avatarMarkup(a)}<div class="grow"><h2 id="account-title-${E(a.id)}" tabindex="-1" title="${E(accountTitle(a))}">${E(accountTitle(a))}</h2><p>${E(a.email||'邮箱未提供')}</p></div>${badge(({pro:'Pro',plus:'Plus',free:'Free',team:'Team',business:'Business'})[a.plan]||a.plan||'套餐未提供','green')}</div><div class="account-tags">${a.is_current&&a.login_status==='ready'?`<span class="badge type-badge" data-tone="green">${I('user')}主账户</span>`:a.login_status==='ready'?'':badge(status,'muted')}${a.is_default?`<span class="badge type-badge" data-tone="yellow">${I('star')}默认</span>`:''}</div><span class="muted">额度剩余</span><strong class="quota" aria-label="剩余额度百分比">${percent===null?'—':Math.round(percent)+'%'}</strong><div class="progress" role="img" aria-label="${percent===null?'额度未提供':'剩余 '+percent+'%'}"><span style="width:${percent===null?0:Math.max(0,Math.min(100,percent))}%"></span></div><div class="metrics"><div><span>重置时间</span><strong title="${E(fmtDate(a.resets_at))}">${resetTime(a.resets_at)}</strong></div><div><span>重置卡</span><strong>${num(a.reset_cards)===null?'未提供':a.reset_cards+' 张'}</strong></div></div><div class="account-footer"><small>更新于 ${E(fmtDate(a.profile_observed_at||a.observed_at))}</small>${defaultAccountButton(a)}</div></article>`;
 }).join('')||empty('未读取到 Codex 账户')}</div>`;
}
function skillsView(){const items=newestRecords(findText(s.data.skills,s.query,['name','display_name','description','short_description']));return `<div class="toolbar">${search()}<span class="count">${items.length} 项技能</span></div><div class="skill-grid">${items.map(x=>`<article class="card skill" data-tone="${CARD_THEMES[skillTheme(x)][1]}"><div class="row">${cardSymbol(skillTheme(x))}${badge(x.enabled?'可用':'已禁用',x.enabled?'green':'muted')}</div><h2 title="${E(x.display_name||x.name)}">${E(skillTitle(x))}</h2><p>${E(x.short_description||x.description)}</p><div class="row"><span class="badge module-badge">${E({user:'个人技能',repo:'项目技能',system:'系统技能',admin:'组织技能'}[x.scope]||x.scope||'Codex Skills')}</span><button class="soft" data-skill="${E(x.id)}">查看详情</button></div></article>`).join('')||empty('没有匹配的技能')}</div>`;}
const NETWORKS={internal:['内网','server','cyan'],external:['外网','globe','blue']};
function networkBadge(value){const info=NETWORKS[value];return info?`<span class="badge type-badge" data-tone="${info[2]}">${I(info[1])}${info[0]}</span>`:badge('网络待标注');}
function modelProviders(models){const providers=new Map();for(const model of A(models)){const id=model.provider_id,name=model.provider_name;if(typeof id==='string'&&id&&typeof name==='string'&&name&&!providers.has(id))providers.set(id,{id,name});}return [...providers.values()];}
function compactModelSearch(value){return String(value??'').toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu,'');}
function modelMatches(model,query){if(!query)return true;const needle=compactModelSearch(query),values=['name','model','base_url','provider_id','provider_name'].map(key=>compactModelSearch(model[key]));if(values.includes('minimax'))values.push('minmax');return values.some(value=>value.includes(needle));}
function modelsView(){const providers=s.data.facets?.providers||modelProviders(s.data.models);let items=newestRecords(A(s.data.models).filter(x=>modelMatches(x,s.query)));if(s.kind)items=items.filter(x=>x.model_type===s.kind);if(s.modelProvider)items=items.filter(x=>x.provider_id===s.modelProvider);return `<div class="toolbar">${search()}${select('kind','模型能力',options(Object.entries(TYPES).map(([id,name])=>({id,name})),s.kind,'全部能力'))}${providers.length?select('model-provider','供应商',options(providers,s.modelProvider,'全部供应商')):''}</div><div class="table-scroll"><table><thead><tr><th>模型与接口</th><th>供应商</th><th>能力</th><th>已记录状态</th><th><span class="sr-only">详情</span></th></tr></thead><tbody>${items.map(x=>`<tr><td><div class="identity"><span class="module-symbol" data-tone="${category(x.model_type)[1]}">${I(category(x.model_type)[0])}</span><div><strong title="${E(x.name)}">${E(chineseTitle(x.name,'模型'))}</strong>${x.network_scope==='external'?'':networkBadge(x.network_scope)}${x.configuration_status==='missing_credential'?badge('待配置 API Key','orange'):''}<p>${E(x.base_url||'地址不可公开展示')}</p></div></div></td><td>${x.provider_name?badge(x.provider_name,'blue'):badge('未登记','muted')}</td><td>${typeBadge(TYPES[x.model_type]||x.model_type,x.model_type)}</td><td>${badge(VALIDATION[x.validation_status]||'未提供',x.validation_status==='verified'?'green':x.validation_status==='failed'?'red':x.validation_status==='pending'?'orange':'muted')}</td><td><button class="soft" data-model="${E(x.id)}">查看详情</button></td></tr>`).join('')}</tbody></table>${items.length?'':empty('暂无匹配的已登记模型')}</div>`;}
function folderPath(folders,id){const byId=new Map(A(folders).map(f=>[f.id,f])),path=[],seen=new Set();while(id&&byId.has(id)&&!seen.has(id)){seen.add(id);const f=byId.get(id);path.unshift(f.name);id=f.parent_id;}return path.join(' / ');}
/** 平台由已登记账户派生，空平台不会产生筛选项。 */
function accountProviders(accounts){const providers=new Map();for(const a of A(accounts)){if(!providers.has(a.provider_id))providers.set(a.provider_id,{id:a.provider_id,name:a.provider_name,count:0});providers.get(a.provider_id).count++;}return [...providers.values()];}
function accountUsage(usage){
 if(!usage)return {title:'用量',value:'未提供',percent:null,summary:'尚未记录用量'};
 const used=num(usage.used),limit=num(usage.limit),remaining=num(usage.remaining),unit=usage.unit||'';
 const rest=remaining??(limit!==null&&used!==null?Math.max(0,limit-used):null);
 const percent=limit!==null&&limit>0&&rest!==null?Math.max(0,Math.min(100,rest/limit*100)):null;
 const format=n=>['CNY','USD'].includes(unit)?n.toLocaleString('zh-CN',{style:'currency',currency:unit,minimumFractionDigits:2,maximumFractionDigits:4}):n.toLocaleString('zh-CN',{maximumFractionDigits:4})+(unit==='%'?'%':' '+unit);
 return {title:percent!==null?'额度剩余':rest!==null?(['CNY','USD','人民币','元','美元'].includes(unit)?'账户余额':'剩余用量'):used!==null?'已用量':'总额度',
 value:percent!==null?Math.round(percent)+'%':format(rest??used??limit),percent,
 summary:[used!==null?'已用 '+format(used):'',limit!==null?'总额 '+format(limit):'',rest!==null?'剩余 '+format(rest):''].filter(Boolean).join(' · ')};
}
function accountSource(value){return ({catalog_registration:'配置登记',registered:'配置登记',manual_audit:'人工核验',manual:'人工登记',provider_console:'平台控制台',provider_api:'平台接口'})[value]||value||'未提供';}
function accountTime(value,note){return value?fmtDate(value):note||'未提供';}
function usageProgress(usage){if(usage.percent===null)return '';const remaining=Math.max(0,Math.min(100,usage.percent));return `<div class="progress" role="progressbar" aria-label="剩余额度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${remaining}"><span style="width:${remaining}%"></span></div>`;}
function usageWindow(window,notes){const usage=accountUsage(window.usage);return `<section class="other-usage usage-window"><strong>${E(window.label)}</strong><span class="muted">${usage.title}</span><b>${E(usage.value)}</b>${usageProgress(usage)}<p class="usage-summary">${E(usage.summary||notes.usage||'尚未记录用量')}</p><small>重置时间：${E(accountTime(window.resets_at,notes.resets_at))}</small></section>`;}
function otherAccountCard(a){
 const notes=a.provider_id==='minimax'?{...a.field_notes,usage:''}:a.field_notes||{};
 const usage=accountUsage(a.usage),hasUsageState=typeof a.is_used==='boolean',status=a.is_used===true?'使用中':a.is_used===false?'未使用':'状态未核验',apiAuth=a.api_auth?.status==='accepted'?badge('API 已验证','green'):a.api_auth?.status==='rejected'?badge('API 验证失败','red'):'';
 const tone=a.provider_id==='zhipu'?'green':a.provider_id==='deepseek'?'blue':'purple';
 const profileTitle=accountTitle(a),title=profileTitle==='用户名未提供'?(typeof a.label==='string'&&!a.label.includes('@')?a.label:'用户名未提供'):profileTitle;
 const observedAt=a.profile_observed_at||a.api_auth?.observed_at||a.observed_at;
 return `<article class="card account other-account" data-tone="${tone}" aria-label="${E(a.provider_name+' · '+title)}">
 <div class="identity">${avatarMarkup(a)}<div class="grow"><h2>${E(title)}</h2><p>${E(a.provider_name)}</p></div>${badge(a.provider_name,tone)}</div>
 <div class="account-tags">${apiAuth}${hasUsageState?badge(status,a.is_used===true?'green':'muted'):a.api_auth?'':badge(status,'muted')}${observedAt?`<span class="muted account-observation" title="${E(accountSource(a.api_auth?.source||a.status_source))}">${a.profile_observed_at?'资料更新于':'检查于'} ${E(fmtDate(observedAt))}</span>`:''}</div>
 ${A(a.usage_windows).length?`<div class="usage-windows">${A(a.usage_windows).map(window=>usageWindow(window,notes)).join('')}</div>${notes.usage?`<p class="usage-summary">${E(notes.usage)}</p>`:''}`:`<div class="other-usage"><span class="muted">${usage.title}</span><strong class="quota ${usage.value==='未提供'?'unknown-quota':''}">${E(usage.value)}</strong>${usageProgress(usage)}<p class="usage-summary">${E(usage.summary||notes.usage||'')}</p></div>`}
 ${a.usage_refresh?.state==='failed'?`<p class="error" role="alert">${E(a.usage_refresh.message||'用量更新失败')}</p>`:''}<dl class="other-account-times">${A(a.usage_windows).length?'':field('重置时间',accountTime(a.resets_at,a.field_notes?.resets_at))}${a.expires_at?field('到期时间',fmtDate(a.expires_at)):''}${a.last_used_at?field('最近使用',fmtDate(a.last_used_at)):''}${field('更新于',fmtDate(a.profile_observed_at||a.updated_at||a.usage?.observed_at||a.observed_at))}</dl>
 <footer class="other-account-footer">${a.vault_id?`<button class="soft" id="other-config-${E(a.id)}" data-config="${E(a.vault_id)}">查看配置 ${I('chevron-right')}</button>`:'<span class="muted">未关联配置</span>'}</footer></article>`;
}
function otherAccountsView(data=s.data,query=s.query,selected=s.accountProvider){
 const accounts=A(data.accounts),providers=data.facets?.providers||accountProviders(accounts),provider=providers.some(p=>p.id===selected)?selected:'';
 const items=findText(accounts,query,['label','provider_name']).filter(a=>!provider||a.provider_id===provider);
 return `<div class="toolbar other-account-toolbar">${search()}<button id="refresh-usage" class="soft" ${s.usageRefreshing?'disabled':''}>刷新用量</button><span class="count" role="status">${items.length} 个账户</span>${s.usageRefreshing?'<span class="muted" role="status">正在更新用量…</span>':''}</div>${providers.length?`<div class="provider-filters" role="group" aria-label="账户平台"><button id="provider-all" data-provider="" aria-pressed="${!provider}">全部平台 <span>${providers.reduce((n,p)=>n+p.count,0)||accounts.length}</span></button>${providers.map(p=>`<button id="provider-${E(p.id)}" data-provider="${E(p.id)}" aria-pressed="${provider===p.id}">${E(p.name)} <span>${p.count}</span></button>`).join('')}</div>`:''}<div class="account-grid other-account-grid">${items.map(otherAccountCard).join('')||empty(accounts.length?'没有匹配的账户':'暂无其他账户，配置账户后会显示对应平台')}</div>`;
}

function configType(x){return x.service_type||x.kind||'unknown';}
function configTypeLabel(x){return x.service_type_label||KINDS[x.kind]||'通用配置';}
function filteredConfigurations(data,folder,kind,tag,query){const descendants=foldersIn(A(data.folders),folder),q=String(query||'').toLocaleLowerCase();return newestRecords(A(data.entries).filter(x=>(!folder||descendants.has(x.folder_id))&&(!kind||configType(x)===kind)&&(!tag||A(x.tags).includes(tag))&&(!q||[entryTitle(x),x.label,x.name,x.id,configTypeLabel(x),folderPath(data.folders,x.folder_id),...A(x.tags)].join(' ').toLocaleLowerCase().includes(q))));}
function folderTree(){const all=A(s.data.folders),byId=new Map(all.map(f=>[f.id,f])),seen=new Set(),childrenById=new Map(),counts=new Map();
 for(const f of all){const siblings=childrenById.get(f.parent_id)||[];siblings.push(f);childrenById.set(f.parent_id,siblings);}
 for(const entry of A(s.data.entries)){let id=entry.folder_id;const visited=new Set();while(id&&byId.has(id)&&!visited.has(id)){visited.add(id);counts.set(id,(counts.get(id)||0)+1);id=byId.get(id).parent_id;}}
 function branch(f,depth){if(seen.has(f.id))return '';seen.add(f.id);const children=A(childrenById.get(f.id)).filter(x=>!seen.has(x.id)),closed=s.collapsedFolders.has(f.id);const count=s.data.facets?.folder_counts?.[f.id]??counts.get(f.id)??0;
 const row=`<div class="directory-row" style="--depth:${Math.min(depth,12)};--folder-color:${color(f.color)}">${children.length?`<button class="directory-toggle" id="folder-toggle-${E(f.id)}" data-folder-toggle="${E(f.id)}" aria-label="${closed?'展开':'收起'} ${E(f.name)}" aria-expanded="${!closed}">${I(closed?'chevron-right':'chevron-down')}</button>`:'<span class="directory-spacer"></span>'}<button class="folder ${s.folder===f.id?'active':''}" id="folder-${E(f.id)}" data-folder="${E(f.id)}" aria-pressed="${s.folder===f.id}"><span>${E(f.name)}</span><small>${count}</small></button></div>`;
 const nested=children.map(child=>branch(child,depth+1)).join('');return row+(closed?'':nested);}
 let html=all.filter(f=>!f.parent_id||!byId.has(f.parent_id)).map(f=>branch(f,0)).join('');for(const f of all)if(!seen.has(f.id))html+=branch(f,0);return html;
}
function configView(){const entries=A(s.data.entries),items=filteredConfigurations(s.data,s.folder,s.kind,s.configTag,s.query).filter(x=>!s.configProvider||A(x.account_provider_ids).includes(s.configProvider)),kinds=s.data.facets?.kinds||[...new Map(entries.map(x=>[configType(x),{id:configType(x),name:configTypeLabel(x)}])).values()],tags=s.data.facets?.tags||[...new Set(entries.flatMap(x=>A(x.tags)))].sort().map(id=>({id,name:id})),path=folderPath(s.data.folders,s.folder);
 return `<div class="config-layout"><aside class="directories" aria-label="配置目录"><h2>目录</h2>${folderTree()||'<p class="muted directory-empty">暂无目录</p>'}</aside><section class="config-main" aria-label="配置列表"><div class="config-list-heading"><h2>${E(path||'全部配置')}</h2><span class="badge muted" role="status">${items.length}</span>${s.folder?'<button class="config-clear" id="clear-folder">全部配置</button>':''}</div><div class="toolbar config-toolbar"><label class="search">${I('search')}<span class="sr-only">搜索配置</span><input id="query" value="${E(s.query)}" placeholder="搜索名称、目录、标签"></label>${select('kind','配置类型',options(kinds,s.kind,'全部类型'))}${select('config-tag','配置标签',options(tags,s.configTag,'全部标签'))}${A(s.data.other_account_providers).length?select('config-provider','账户平台',options(s.data.other_account_providers,s.configProvider,'全部账户平台')):''}</div><div class="config-grid">${items.map(x=>`<article class="config-card" data-tone="${category(configType(x))[1]}"><div class="config-card-top">${typeBadge(configTypeLabel(x),configType(x))}${x.mapping_status==='stale'?badge('索引待更新','orange'):''}</div><h3 title="${E(x.label||x.name||x.id)}">${E(entryTitle(x))}</h3>${x.folder_id?`<p class="config-path">${E(folderPath(s.data.folders,x.folder_id))}</p>`:''}<div class="config-tags">${A(x.tags).map(tagBadge).join('')}${A(x.account_provider_ids).map(id=>badge(A(s.data.other_account_providers).find(p=>p.id===id)?.name||id,'blue')).join('')}</div><footer><button class="config-open" id="config-entry-${E(x.id)}" data-config="${E(x.id)}" aria-label="查看配置：${E(entryTitle(x))}">查看配置 ${I('chevron-right')}</button>${x.updated_at?`<time class="muted">${E(fmtDate(x.updated_at))}</time>`:''}</footer></article>`).join('')||empty('当前筛选下暂无配置')}</div></section></div>`;
}
const field=(label,value)=>`<div class="field"><dt>${E(label)}</dt><dd>${E(value??'未提供')}</dd></div>`;
function detailView(){const d=s.detail;let content='';if(d.kind==='skill'){const x=d.value.skill;content=`<div class="detail-grid"><section class="card"><h2>能力说明</h2><p class="long-text">${E(x.description)}</p></section><section class="card"><dl>${field('名称',skillTitle(x))+field('原始名称',x.name)}${field('来源',x.plugin_id||'Codex Skills')}${field('状态',x.enabled?'可用':'已禁用')}</dl></section></div>`;}
else if(d.kind==='model'){const x=d.value.model;content=`<div class="detail-grid"><section class="card"><h2>接口信息</h2><dl>${x.model?field('模型',x.model):''}${field('接口地址',x.base_url)}${x.provider_name?field('供应商',x.provider_name):''}${x.configuration_status==='missing_credential'?field('API Key','待配置 API Key'):''}${x.network_scope==='external'?'':`<dt>网络范围</dt><dd>${networkBadge(x.network_scope)}</dd>`}${field('能力',TYPES[x.model_type]||x.model_type)}</dl></section><section class="card"><h2>已记录验证结果</h2><dl>${field('状态',VALIDATION[x.validation_status]||'未提供')}${field('验证时间',fmtDate(x.last_checked_at))}</dl></section></div>${secretPanel(d)}${apiFields(d)}`;}
else if(d.kind==='project'){const x=d.value.project;content=`<div class="detail-grid"><section class="card"><h2>关联会话</h2>${A(d.value.sessions).map(t=>`<div class="row"><span>${E(t.title)}</span><button data-project-session="${E(t.id)}" class="soft">查看执行记录</button></div>`).join('')||empty('项目暂无关联会话')}</section><section class="card"><dl>${field('项目',x.name)}${field('来源','Codex 原生项目')}${field('工作目录',x.cwd)}</dl></section></div>`;}
else if(d.kind==='knowledge'){const x=d.value.knowledge;content=`<article class="card knowledge-article"><div>${badge('已审核','green')}${badge(x.scope_key)}</div><dl class="knowledge-meta">${field('来源标识',x.source_native_id)}${field('审核时间',fmtDate(x.updated_at))}</dl><p class="long-text">${E(x.content||x.summary||'正文未提供')}</p></article>`;}
else {const x=d.entry;content=`<div class="detail-grid"><section class="card"><h2>条目信息</h2><dl>${field('记录类型',KINDS[x.kind]||x.kind)}${field('更新时间',fmtDate(x.updated_at))}${field('结构状态',x.mapping_status==='stale'?'索引待更新':x.structure_status==='indexed'?'已索引':'未索引')}</dl></section><section class="card"><h2>标签</h2>${A(x.tags).map(tagBadge).join(' ')||'<span class="muted">无标签</span>'}</section></div>${secretPanel(d)}`;}
return `<div class="detail-head"><button id="back">${I('arrow-left')}返回${PAGES[s.page]}</button></div><div class="heading"><h1${d.kind==='project'?` style="color:${color(d.value.project.color)}"`:''}>${d.kind==='project'?entityIcon(d.value.project):''}${E(d.title)}</h1></div>${content}`;}
function apiFields(d){const c=d.value?.api_contract;if(!c)return d.kind==='model'?'<section class="card api-contract"><h2>入参与出参</h2><p class="muted">尚未登记接口字段</p></section>':'';const table=(title,rows)=>`<section class="card api-contract"><h2>${title}</h2><div class="table-scroll"><table><thead><tr><th>字段</th><th>类型</th><th>条件</th><th>说明</th></tr></thead><tbody>${A(rows).map(r=>`<tr><td><code>${E(r[0])}</code></td><td>${E(r[1])}</td><td>${E(r[2]||'—')}</td><td>${E(r[3])}</td></tr>`).join('')}</tbody></table></div></section>`;return `<section class="api-reference"><div class="row"><h2>${E(c.name)}</h2>${badge('协议参考','orange')}</div><p class="muted">${E(c.note)}</p><button class="soft" id="protocol-source">${I('external-link')}官方字段说明</button></section>${table('入参',c.inputs)}${table('出参',c.outputs)}`;}
function automaticField(f,value){
 if(f.path.length!==1)return false;
 const k=f.path[0];
 if(['endpoint','base_url','host','ip'].includes(k)){
  if(typeof value!=='string'||value.length>2048)return false;
  if(k==='host'||k==='ip')return !/[\s@/?#]/.test(value);
  try{const u=new URL(value);return ['http:','https:'].includes(u.protocol)&&!u.username&&!u.password&&!u.search&&!u.hash;}catch{return false;}
 }
 return ['type','purpose','source','authentication_tested','port','database_type','region'].includes(k)&&(['boolean','number'].includes(typeof value)||typeof value==='string'&&value.length<=512);
}
// 字段颜色只表达含义，不把未知字段或密码错误标成故障状态。
function configurationFieldTone(field){
 const names=A(field.path).map(x=>String(x).toLowerCase());
 if(names.some(x=>/password|passwd|secret|token|private_key|passphrase|api_key|access_key|credential/.test(x)||x==='key'))return 'rose';
 if(names.some(x=>['target','endpoint','base_url','host','ip','port','url','uri','remote_path','connection_uri','brokers','endpoints'].includes(x)))return 'blue';
 if(names.some(x=>['account','username','user','owner','email'].includes(x)))return 'violet';
 if(names.some(x=>['service','kind','type','database','database_type','database_index','namespace','group','bucket','region','environment'].includes(x)))return 'teal';
 return 'slate';
}
function secretPanel(d){const entry=d.entry||d.value?.credential;if(!entry)return '';return `<section class="card secret-panel ${d.kind==='config'?'config-field-panel':''}"><div class="row"><h2>配置字段</h2><button class="soft" id="fields" ${s.fieldBusy?'disabled':''}>${s.fieldBusy?'读取中':'重新读取字段'}</button></div>${d.fieldsState==='error'?'<p class="muted">字段读取失败，请重新读取。</p>':!d.fields?.length?`<p class="muted" role="status">${s.fieldBusy?'正在读取配置…':d.fieldsState==='ready'?'暂无配置字段':'配置已清理，点击重新读取字段'}</p>`:''}<div class="configuration-fields">${A(d.fields).map((f,i)=>`<div class="secret-field"${d.kind==='config'?` data-tone="${configurationFieldTone(f)}"`: ''}><div><strong>${E(f.path.map(k=>LABELS[k]||k).join(' / ')||'内容')}</strong><pre>${s.revealed.has(i)?E(s.revealed.get(i)):'••••••••'}</pre></div>${d.kind==='config'||f.autoVisible?'':`<div class="actions"><button data-reveal="${i}" ${s.fieldBusy?'disabled':''}>${s.revealed.has(i)?'隐藏':'查看'}</button><button data-copy="${i}" ${s.fieldBusy?'disabled':''}>复制</button></div>`}</div>`).join('')}</div></section>`;}

function navigation(){const group=GROUPS.find(g=>g.pages.includes(s.page));return `<header class="top"><span class="brand">${I('layout-dashboard')}工作台</span><nav aria-label="工作台分组">${GROUPS.map(g=>`<button data-group="${E(g.name)}" aria-current="${g===group?'true':'false'}">${E(g.name)}</button>`).join('')}</nav><button id="open-search" aria-label="搜索工作台">${I('search')}</button></header><nav class="subnav" aria-label="功能模块">${group.pages.map(p=>`<button data-page="${p}" aria-current="${p===s.page?'page':'false'}">${I(NAV[p])}${PAGES[p]}</button>`).join('')}</nav>`;}
function projectView(){let items=newestRecords(findText(s.data.projects,s.query,['name']));if(s.section)items=items.filter(x=>s.section==='__none__'?!x.section_id:x.section_id===s.section);return `<div class="toolbar">${search()}${select('project-section','分区',options(s.data.sections,s.section,'全部分区')+`<option value="__none__" ${s.section==='__none__'?'selected':''}>无分区</option>`)}</div><div class="skill-grid">${items.map(x=>`<article class="card skill project-card" style="--project-color:${color(x.color)}"><div class="row">${entityIcon(x)}${A(s.data.sections).find(sec=>sec.id===x.section_id)?entityTag(A(s.data.sections).find(sec=>sec.id===x.section_id),false):badge('无分区')}</div><h2 style="color:${color(x.color)}">${E(x.name)}</h2><p>${x.session_count??0} 个关联会话</p><button class="soft" data-project="${E(x.id)}">查看关联会话</button></article>`).join('')||empty('当前范围没有项目')}</div>`;}
function knowledgeView(){const items=A(s.data.knowledge);return `<div class="toolbar">${select('knowledge-scope','知识范围',A(s.data.scopes).map(x=>`<option value="${E(x.id)}" ${x.id===s.scope?'selected':''}>${E(x.name)}</option>`).join(''))}${search()}<button id="knowledge-query" class="soft">查询</button></div><div class="knowledge-list">${items.map((x,i)=>`<article class="card knowledge-card" data-tone="${CARD_THEMES[knowledgeTheme(x)][1]}"><div class="row">${cardSymbol(knowledgeTheme(x))}<div class="grow"><h2 title="${E(x.title)}">${E(chineseTitle(x.title,'知识'))}</h2><p class="muted">${E(x.summary)}</p><span>${A(x.tags).map(tagBadge).join(' ')}</span></div>${badge('已审核','green')}<button class="soft" data-knowledge="${i}">阅读详情</button></div></article>`).join('')||empty('当前知识范围暂无匹配内容')}</div>${s.data.limit_reached?'<p class="muted">已达到单次结果上限，可通过查询缩小范围。</p>':''}`;}
function servicesView(){let items=newestRecords(findText(s.data.services,s.query,['name','kind','endpoint']));if(s.kind)items=items.filter(x=>x.reference_kind===s.kind);return `<div class="toolbar">${search()}${select('kind','来源',options([{id:'model',name:'模型目录'},{id:'credential',name:'密钥保险库'}],s.kind,'全部来源'))}</div><div class="knowledge-list">${items.map(x=>`<article class="card"><div class="row">${symbol(x.reference_kind==='model'?'cloud':'server')}<div class="grow"><h2 title="${E(x.name)}">${E(chineseTitle(x.name,'服务'))}</h2><p class="muted">${x.source==='model_configuration'?'模型服务 · 密钥保存在保险库':'连接配置 · 密钥保险库'}</p>${A(x.tags).map(tagBadge).join(' ')}</div><span class="badge module-badge">${E(x.kind)}</span>${A(x.models).map(m=>`<button class="model-link" data-tone="${category(m.model_type)[1]}" data-model="${E(m.id)}">${I(category(m.model_type)[0])}${E(m.name)}</button>`).join('')}<button class="soft" data-service="${E(x.id)}">查看关联配置</button></div></article>`).join('')||empty('暂无可识别的服务配置引用')}</div>`;}
function connectionsView(){let items=newestRecords(findText(s.data.connections,s.query,['name','kind']));if(s.kind)items=items.filter(x=>x.kind===s.kind);return `<div class="toolbar">${search()}${select('kind','连接类型',options([{id:'mcp',name:'MCP'},{id:'plugin',name:'已安装插件'}],s.kind,'全部连接类型'))}</div>${A(s.data.source_errors).map(e=>`<p class="error">${E(e)}</p>`).join('')}<div class="knowledge-list">${items.map(x=>`<article class="card"><div class="row">${I(x.kind==='mcp'?'globe':'sliders-horizontal','purple')}<div class="grow"><h2 title="${E(x.name)}">${E(chineseTitle(x.name,'服务'))}</h2><p class="muted">${x.kind==='mcp'?'本机 MCP 配置':'已安装插件'}${x.transport?' · '+E(x.transport):''}${x.version?' · '+E(x.version):''}</p></div>${badge(x.enabled?'配置已启用':'配置已停用',x.enabled?'green':'muted')}${badge('可用性未核验')}</div></article>`).join('')||empty('没有已登记的工具连接')}</div>`;}
function searchDialog(){if(!s.searchOpen)return '';return `<dialog id="global-search"><div class="row"><h2>搜索工作台</h2><button id="close-search">关闭</button></div><form id="global-search-form" class="toolbar"><label class="search">${I('search')}<span class="sr-only">搜索公开信息</span><input id="global-query" value="${E(s.searchQuery)}" placeholder="技能、资源、模型或配置名称" maxlength="200"></label><button type="submit" class="soft" ${s.searchBusy?'disabled':''}>${s.searchBusy?'查询中':'搜索'}</button></form>${s.searchError?`<p class="error">${E(s.searchError)}</p>`:''}<div class="search-results">${s.searchResults.map((x,i)=>`<button class="search-hit" data-search-hit="${i}"><span>${badge(PAGES[x.module]||x.module)}</span><strong title="${E(x.title)}">${E(chineseTitle(x.title,PAGES[x.module]||'条目'))}</strong><small>${E(x.summary)}</small></button>`).join('')}</div></dialog>`;}
async function globalSearch(){const seq=++s.searchSeq;s.searchBusy=true;s.searchError='';render();try{const result=await bridge.tool('global_search',{query:s.searchQuery,scope:s.scope});if(seq!==s.searchSeq||!s.searchOpen)return;s.searchResults=A(result.results);s.searchError=A(result.source_errors).length?'部分来源暂时不可用':s.searchResults.length?'':'没有匹配的公开信息';}catch(e){if(seq===s.searchSeq)s.searchError=e.message;}finally{if(seq===s.searchSeq){s.searchBusy=false;render();}}}
let listSearchTimer;
function snapshotStatus(){const snapshot=s.data.status?.snapshot;if(!snapshot||snapshot.state==='ready')return '';const updated=snapshot.updated_at?` · 更新于 ${E(fmtDate(snapshot.updated_at))}`:'';const messages={ready:'本地快照已就绪',pending:'正在准备本地快照',stale:'正在显示上次本地快照',error:snapshot.updated_at?'本地快照更新失败，正在显示上次数据':'本地快照读取失败'};return `<p class="snapshot-status muted" role="status">${E(messages[snapshot.state]||'本地快照状态未知')}${updated}${snapshot.refreshing?' · 后台更新中':''}</p>`;}
function pendingSnapshot(){const snapshot=s.data.status?.snapshot;return !snapshot?.updated_at&&['pending','error'].includes(snapshot?.state);}
function clearSnapshotPoll(){clearTimeout(snapshotPollTimer);snapshotPollTimer=null;snapshotPollAttempts=0;}
function scheduleSnapshotPoll(page){const snapshot=s.data.status?.snapshot;if(!snapshot||(!snapshot.refreshing&&snapshot.state!=='pending')||document.hidden||s.detail||s.page!==page||snapshotPollAttempts>=30)return;clearTimeout(snapshotPollTimer);snapshotPollTimer=setTimeout(()=>{if(!document.hidden&&s.page===page&&!s.detail){snapshotPollAttempts++;load({poll:true});}},2000);}
function render(){
 const focused=document.activeElement?.id,selection=document.activeElement?.selectionStart;
 const scrolls=[...document.querySelectorAll('.directories')].map(x=>[x.scrollTop,x.scrollLeft]);
 const pageDetail=Boolean(s.detail);
 root.innerHTML=`${navigation()}<main data-module="${s.page}" class="" aria-busy="${s.loading}">${pageDetail?detailView():`<div class="heading"><h1>${symbol(NAV[s.page])}${PAGES[s.page]}</h1></div>`}${s.error?`<div class="error" role="alert">${E(s.error.replaceAll('请点击刷新读取','请重试'))} <button id="retry">重试</button></div>`:''}${!pageDetail?snapshotStatus():''}${!pageDetail?(s.loading&&!Object.keys(s.data).length?'<div class="loading" role="status">正在读取…</div>':pendingSnapshot()?`<div class="${s.data.status?.snapshot?.state==='error'?'error':'loading'}" role="status">${s.data.status?.snapshot?.state==='error'?'暂时无法读取可用数据，请重试。':'正在准备可用数据…'}</div>`:({accounts:accountView,other_accounts:otherAccountsView,agents:skillsView,models:modelsView,config:configView,projects:projectView,knowledge:knowledgeView,services:servicesView,connections:connectionsView}[s.page])()):''}</main>${searchDialog()}<div id="toast" role="status" aria-live="polite"></div>`;
 bind();document.querySelectorAll('.directories').forEach((x,i)=>{if(scrolls[i]){x.scrollTop=scrolls[i][0];x.scrollLeft=scrolls[i][1];}});
 const modal=document.getElementById('global-search');if(modal&&!modal.open){modal.showModal();document.getElementById('global-query')?.focus();}
 if(focused&&focused!=='query')document.getElementById(focused)?.focus({preventScroll:true});
 if(focused==='query'){const q=document.getElementById('query');q?.focus();try{q?.setSelectionRange(selection,selection);}catch{}}
}

/** 默认按钮保留明确文案，不能选择未核验账户或重复提交。 */
function defaultAccountButton(account){
 if(account.is_default)return '';
 const busy=s.accountSaving===account.id,disabled=account.is_default||account.login_status!=='ready'||s.accountSaving!==null;
 return `<button id="account-default-${E(account.id)}" class="soft" data-account-default="${E(account.id)}" aria-label="${E(accountTitle(account))}：${account.is_default?'默认账户':'设为默认账户'}" ${disabled?'disabled':''} ${busy?'aria-busy="true"':''} title="${account.login_status==='ready'?'用于新会话，已有会话保持原账户':'账户未登录或身份尚未确认'}">${I('star')}${busy?'设置中…':account.is_default?'默认账户':'设为默认'}</button>`;
}
/** 仅在服务端确认保存后更新标记；错误保持原选择，过期读取不得覆盖结果。 */
async function setDefaultAccount(id){
 const account=A(s.data.accounts).find(a=>a.id===id);
 if(s.page!=='accounts'||s.accountSaving!==null||!account||account.is_default||account.login_status!=='ready')return;
 const expected=A(s.data.accounts).find(a=>a.is_default)?.id??null;
 s.accountSaving=id;s.error='';s.seq++;abort?.abort();s.syncing=false;s.loading=false;render();
 let saved=false;
 try{
  const result=await bridge.tool('account_default',{id,expected_default_id:expected});
  if(result.default_account_id!==id)throw Error('默认账户保存结果无法确认，请刷新后查看');
  viewCache.delete(canonicalKey({view:'accounts'}));
  if(s.page==='accounts')s.data={...s.data,accounts:A(s.data.accounts).map(a=>({...a,is_default:a.id===result.default_account_id}))};
  saved=true;
 }catch(error){
  if(s.page==='accounts')s.error=error.message;
 }finally{
  s.accountSaving=null;
  if(s.page==='accounts'){
   if(!A(s.data.accounts).length){load({refresh:true});return;}
   render();document.getElementById((saved?'account-title-':'account-default-')+id)?.focus({preventScroll:true});
   if(saved)message('默认账户已更新，新会话将使用此账户');
  }
 }
}
async function load({cursor,session,refresh=false,poll=false}={}){
 if(s.page==='accounts'&&s.accountSaving!==null)return;
 clearTimeout(snapshotPollTimer);snapshotPollTimer=null;if(!poll)snapshotPollAttempts=0;
 const seq=++s.seq;abort?.abort();abort=new AbortController();clearSecrets();s.error='';s.syncing=true;
 const page=s.page,usageRefresh=page==='other_accounts'&&refresh,shouldRefresh=refresh,args={view:page};
 if(['other_accounts','models','config'].includes(page))Object.assign(args,{query:s.query,kind:s.kind,provider:page==='models'?s.modelProvider:page==='config'?s.configProvider:s.accountProvider,tag:s.configTag,folder:s.folder});

 if(page==='knowledge'){args.scope=s.scope;args.query=s.query;}
 const key=canonicalKey(args);let cached=viewCache.get(key),changed=false;
 const previous=s.data;
 // 回到页面立即使用公开缓存；缓存只保留在内存，不包含展开过的秘密详情。
 s.loading=!cached;s.usageRefreshing=usageRefresh;
 if(cached){s.data=cached.data;changed=previous!==s.data;}
 if(!cached||changed||usageRefresh)render();
 try{
  let response=await bridge.tool('workbench_sync',{...args,...(cached?{revision:cached.revision}:{}),refresh:shouldRefresh},abort.signal);
  if(seq!==s.seq||page!==s.page)return;
  if(cacheContext!==null&&cacheContext!==response.context){viewCache.clear();cached=null;s.data={};s.detail=null;clearSecrets();}
  cacheContext=response.context;
  if(!cached&&!response.reset){response=await bridge.tool('workbench_sync',{...args,refresh:shouldRefresh},abort.signal);if(seq!==s.seq||page!==s.page)return;}
  const next=applySync(cached,response);rememberView(key,next);
  let data=next.data;
  const shouldRender=!response.unchanged||changed||!cached||usageRefresh;
  s.data=data;
  if(page==='knowledge')s.scope=data.selected_scope||s.scope;
  if(page==='other_accounts'&&!(data.facets?.providers||accountProviders(data.accounts)).some(p=>p.id===s.accountProvider))s.accountProvider='';
  if(page==='config'&&!A(data.other_account_providers).some(p=>p.id===s.configProvider))s.configProvider='';
  s.loading=false;s.syncing=false;s.usageRefreshing=false;
  if(shouldRender)render();scheduleSnapshotPoll(page);
 }catch(e){
  if(seq!==s.seq||e.name==='AbortError')return;
  s.error=e.message;s.loading=false;s.syncing=false;s.usageRefreshing=false;
  // 临时网络失败保留已读取内容并明确报错；失效许可才清空账户缓存。
  if(e.clearCache){viewCache.clear();s.data={};cacheContext=null;}else{s.data=cached?.data||previous;}
  clearSecrets();render();
 }finally{if(seq===s.seq){s.loading=false;s.syncing=false;s.usageRefreshing=false;}}
}
function pageChange(p){clearTimeout(listSearchTimer);clearSnapshotPoll();if(!Object.hasOwn(PAGES,p)||p===s.page)return;s.seq++;if(!bridge.embedded)history.replaceState(null,'','/'+p);s.page=p;s.modelProvider='';s.detail=null;s.data={};s.query='';s.section='';s.project='';s.folder='';s.kind='';s.configTag='';s.accountProvider='';s.configProvider='';s.session='';s.scope='global';s.searchOpen=false;clearSecrets();load();}
async function detail(kind,id,detailScope){clearSecrets();const epoch=s.detailSeq;s.error='';try{let value,entry,title;if(kind==='skill'){value=await bridge.tool('skill_detail',{id});title=skillTitle(value.skill);}else if(kind==='model'){value=await bridge.tool('model_detail',{id});entry=value.credential;title=chineseTitle(value.model.name,'模型');}else if(kind==='project'){value=await bridge.tool('project_detail',{id});title=value.project.name;}else if(kind==='knowledge'){value=await bridge.tool('knowledge_detail',{scope:detailScope||s.scope,key:id});title=chineseTitle(value.knowledge.title,'知识');}else{entry=A(s.data.entries).find(x=>x.id===id);if(!entry){const catalog=await bridge.tool('credential_list');entry=A(catalog.entries).find(x=>x.id===id);}if(!entry)throw Error('配置条目已变化');value={};title=entryTitle(entry);if(entry.model_id){value=await bridge.tool('model_detail',{id:entry.model_id});kind='model';entry=value.credential;}}if(epoch!==s.detailSeq)return;s.detail={kind,value,entry,title,fields:[],trigger:id};render();document.getElementById('back')?.focus();if(entry&&!document.hidden)await readSecret('fields');}catch(e){if(epoch===s.detailSeq){s.error=e.message;render();}}}
async function readSecret(action,index){const d=s.detail,entry=d?.entry||d?.value?.credential;if(!entry||s.fieldBusy)return;const epoch=s.detailSeq;s.fieldBusy=true;s.error='';if(action==='fields'){s.revealed.clear();d.fields=[];d.fieldsState='loading';}render();let request,payload;try{request=await ConfigurationCrypto.prepare(entry);const envelope=await bridge.tool('credential_details',request.arguments);payload=await ConfigurationCrypto.decrypt(request,envelope);if(epoch!==s.detailSeq||d!==s.detail||document.hidden)return;const data=payloadRoot(payload);if(action==='fields'){d.fields=fieldPaths(data);s.revealed.clear();for(const [i,f] of d.fields.entries()){const raw=f.path.reduce((v,k)=>v?.[k],data);f.autoVisible=d.kind==='config'||automaticField(f,raw);if(f.autoVisible)s.revealed.set(i,d.kind!=='config'&&typeof raw==='boolean'?(raw?'是':'否'):fieldValue(data,f.path));}d.fieldsState='ready';}else{const f=d.fields[index];if(!f)throw Error('字段不存在');const value=fieldValue(data,f.path);if(action==='copy'){if(!navigator.clipboard?.writeText)throw Error('当前环境不支持复制');await navigator.clipboard.writeText(value);if(epoch===s.detailSeq)message('已复制');}else s.revealed.set(index,value);}}catch(e){if(epoch===s.detailSeq){s.error=e.message;if(action==='fields')d.fieldsState='error';}}finally{payload=null;if(request)request.privateKey=null;if(epoch===s.detailSeq){s.fieldBusy=false;render();if(action==='copy'&&!s.error)message('已复制');}}}
function bind(){
 document.getElementById('refresh-usage')?.addEventListener('click',()=>load({refresh:true}));
 document.querySelectorAll('[data-account-default]').forEach(button=>button.onclick=()=>setDefaultAccount(button.dataset.accountDefault));
 document.getElementById('model-provider')?.addEventListener('change',e=>{s.modelProvider=e.target.value;load();});
 document.getElementById('protocol-source')?.addEventListener('click',()=>{const url=s.detail?.value?.api_contract?.source_url;if(typeof url==='string'&&url.startsWith('https://developers.openai.com/api/reference/'))bridge.openDocumentation(url).catch(e=>message(e.message));});
document.querySelectorAll('[data-group]').forEach(b=>b.onclick=()=>{const g=GROUPS.find(x=>x.name===b.dataset.group);if(g&&!g.pages.includes(s.page))pageChange(g.pages[0]);});
document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>pageChange(b.dataset.page));
document.getElementById('retry')?.addEventListener('click',()=>{s.detail=null;load({refresh:true});});
document.getElementById('query')?.addEventListener('input',e=>{s.query=e.target.value;clearTimeout(listSearchTimer);listSearchTimer=setTimeout(render,150);});
for(const kind of ['section','project'])document.getElementById(kind)?.addEventListener('change',e=>{s[kind]=e.target.value;if(kind==='section')s.project='';s.session='';load({session:''});});
document.getElementById('session')?.addEventListener('change',e=>{s.session=e.target.value;load({session:e.target.value});});
document.querySelectorAll('[data-provider]').forEach(b=>b.onclick=()=>{s.accountProvider=b.dataset.provider;render();});
document.getElementById('config-provider')?.addEventListener('change',e=>{s.configProvider=e.target.value;render();});
document.getElementById('config-tag')?.addEventListener('change',e=>{s.configTag=e.target.value;render();});
document.querySelectorAll('[data-folder-toggle]').forEach(b=>b.onclick=()=>{const id=b.dataset.folderToggle;s.collapsedFolders.has(id)?s.collapsedFolders.delete(id):s.collapsedFolders.add(id);render();});
document.getElementById('kind')?.addEventListener('change',e=>{s.kind=e.target.value;render();});
document.querySelectorAll('[data-folder]').forEach(b=>b.onclick=()=>{s.folder=b.dataset.folder;render();});
document.getElementById('clear-folder')?.addEventListener('click',()=>{s.folder='';render();});
for(const kind of ['skill','model','config','project'])document.querySelectorAll('[data-'+kind+']').forEach(b=>b.onclick=()=>detail(kind,b.dataset[kind]));
document.getElementById('project-section')?.addEventListener('change',e=>{s.section=e.target.value;render();});
document.getElementById('knowledge-scope')?.addEventListener('change',e=>{s.scope=e.target.value;s.query='';load();});
document.getElementById('knowledge-query')?.addEventListener('click',()=>load());
document.querySelectorAll('[data-knowledge]').forEach(b=>b.onclick=()=>{const item=s.data.knowledge[Number(b.dataset.knowledge)];detail('knowledge',item.knowledge_key,item.scope_key);});
document.querySelectorAll('[data-service]').forEach(b=>b.onclick=async()=>{try{const r=await bridge.tool('service_detail',{id:b.dataset.service});await detail(r.service.credential_id?'config':r.service.reference_kind==='model'?'model':'config',r.service.credential_id||r.service.reference_id);}catch(e){s.error=e.message;render();}});
document.getElementById('open-search')?.addEventListener('click',()=>{clearSecrets();s.searchOpen=true;render();});const closeSearch=()=>{s.searchSeq++;s.searchOpen=false;s.searchBusy=false;s.searchResults=[];s.searchQuery='';render();document.getElementById('open-search')?.focus();};
document.getElementById('close-search')?.addEventListener('click',closeSearch);document.getElementById('global-search')?.addEventListener('cancel',e=>{e.preventDefault();closeSearch();});document.getElementById('global-search')?.addEventListener('click',e=>{if(e.target.id==='global-search'){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeSearch();}});document.getElementById('global-query')?.addEventListener('input',e=>s.searchQuery=e.target.value);document.getElementById('global-search-form')?.addEventListener('submit',e=>{e.preventDefault();globalSearch();});
document.querySelectorAll('[data-search-hit]').forEach(b=>b.onclick=async()=>{const h=s.searchResults[Number(b.dataset.searchHit)];if(!h||!Object.hasOwn(PAGES,h.module))return;if(h.entity_type==='module'){s.searchSeq++;pageChange(h.module);return;}s.searchSeq++;s.searchOpen=false;s.searchResults=[];s.page=h.module;if(!bridge.embedded)history.replaceState(null,'','/'+h.module);s.detail=null;s.data={};s.query='';s.kind='';s.configTag='';s.accountProvider='';s.configProvider='';s.section='';s.project='';s.folder='';s.session='';s.scope=h.scope||'global';clearSecrets();const pageEpoch=s.seq+1;await load();if(s.seq!==pageEpoch||s.page!==h.module)return;if(h.module==='other_accounts'){s.query=h.title;render();return;}detail({projects:'project',agents:'skill',models:'model',config:'config',knowledge:'knowledge'}[h.module],h.id,h.scope);});
document.getElementById('back')?.addEventListener('click',()=>{const trigger=s.detail?.trigger;clearSecrets();s.detail=null;s.error='';render();(document.getElementById('config-entry-'+trigger)||[...document.querySelectorAll('[data-config]')].find(b=>b.dataset.config===trigger))?.focus({preventScroll:true});});
document.getElementById('open-native')?.addEventListener('click',()=>bridge.open(s.detail.value.native_url).catch(e=>{s.error=e.message;render();}));document.getElementById('fields')?.addEventListener('click',()=>readSecret('fields'));document.querySelectorAll('[data-reveal]').forEach(b=>b.onclick=()=>{const n=Number(b.dataset.reveal);if(s.revealed.has(n)){s.revealed.delete(n);render();}else readSecret('view',n);});document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>readSecret('copy',Number(b.dataset.copy)));}
window.__workbenchReadonlyTest={accountProviders,accountUsage,otherAccountCard,otherAccountsView,modelProviders,compactModelSearch,modelMatches,modelsView,CARD_THEMES,SKILL_THEMES,skillTheme,knowledgeTheme,cardSymbol,chineseTitle,skillTitle,accountTitle,avatarMarkup,accountView,skillsView,entryTitle,folderPath,filteredConfigurations,configType,configTypeLabel,orderedAccounts,automaticField,nativeUrl,duration,resetTime,findText,payloadRoot,fieldPaths,fieldValue,foldersIn,readResult,color,entityIcon,entityTag,applySync,canonicalKey,timestamp,recordTimestamp,newestRecords,snapshotStatus,pendingSnapshot};
// 隐藏时只清理秘密，公共目录读取继续收尾；避免取消后永远停在忙状态。
window.addEventListener('pagehide',()=>{clearSnapshotPoll();s.seq++;abort?.abort();s.loading=false;s.syncing=false;clearSecrets();render();});
window.addEventListener('pageshow',event=>{if(event.persisted&&!s.detail&&!s.syncing)load();});
document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(snapshotPollTimer);snapshotPollTimer=null;const sensitive=!!s.detail;clearSecrets();if(sensitive)render();}else scheduleSnapshotPoll(s.page);});
const bootstrapped=installBootstrap();
if(root&&bootstrapped){render();if(Object.keys(s.data).length)load();}
if(root)bridge.initialize().then(()=>{if(!bootstrapped&&!s.syncing&&!Object.keys(s.data).length)load();}).catch(e=>{if(!bootstrapped){s.error=e.message;render();}});
})();
