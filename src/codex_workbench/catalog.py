"""工作台 MCP 契约；客户端声明不能绕过服务端工具白名单。"""
from __future__ import annotations
from typing import Any

VERSION = "0.9.1"
UI_URIS = {"open_workbench":"ui://codex-workbench/v7/workbench.html"}
PAGES = {"workbench":("codex-workbench","open_workbench","工作台")}
MODULES=[
 {'id':'accounts','name':'Codex 账户','group':'账户与配置'},
 {'id':'other_accounts','name':'其他账户','group':'账户与配置'},
 {'id':'config','name':'配置中心','group':'账户与配置'},
 {'id':'agents','name':'技能助手','group':'能力与知识'},
 {'id':'knowledge','name':'知识中心','group':'能力与知识'},{'id':'models','name':'模型目录','group':'能力与知识'},
 {'id':'planning','name':'工作计划','group':'工作台'},
]
# 默认入口与导航第一项保持一致。
DEFAULT_VIEW = MODULES[0]["id"]
UI_VIEWS = frozenset(module["id"] for module in MODULES)

def text(maximum=300, nullable=False):
    """有界文本参数。"""
    return {"type":["string","null"] if nullable else "string","maxLength":maximum}

ID=text(255)

def definition(name,title,description,properties=None,required=None,*,read_only=True,idempotent=True):
    """声明公开工具的输入、写入语义和可见范围。"""
    result={"name":name,"title":title,"description":description,
        "inputSchema":{"type":"object","properties":properties or {},"required":required or [],"additionalProperties":False},
        "annotations":{"readOnlyHint":read_only,"destructiveHint":False,"idempotentHint":idempotent,"openWorldHint":name in ('workbench_state','workbench_sync')}}
    if name in UI_URIS:
        result["_meta"] = {"ui": {"resourceUri": UI_URIS[name]},
                           "openai/ui": {"entrypoints": [{"type": "global"}]}}
    if name in ('credential_details','knowledge_detail','account_create','account_login','account_status','account_default'):result["_meta"]={"ui":{"visibility":["app"]}}
    if name in ('account_create','account_login','account_status','account_default'):result['annotations']['readOnlyHint']=False
    if name in ('account_create','account_login'):result['annotations']['idempotentHint']=False
    return result

FILTERS={'query':text(300),'kind':text(100),'provider':text(200),'tag':text(200),'folder':text(200)}

TOOLS=[
 definition('account_create','添加 Codex 账户','准备临时官方登录目录；成功核验后才登记账户，不自动设为默认。',{'name':text(160)},['name']),
 definition('account_login','登录 Codex 账户','打开官方 OpenAI 登录页面，登录完成后自动核验账户状态。',{'id':ID},['id']),
 definition('account_status','核验 Codex 账户','读取官方登录状态和额度，并登记已确认的账户主体。',{'id':ID},['id']),
 definition('account_default','设置默认账户','设置已登记且身份核验通过的默认账户；所有会话下一次请求使用该账户。',{'id':ID,'expected_default_id':text(255,True)},['id','expected_default_id']),
 definition('open_workbench','工作台','打开只读工作台，不创建、修改或执行业务对象。'),
 definition('workbench_state','读取工作台','按视图读取后台更新的公开 JSON 快照，不在页面请求中访问来源。',{'view':{'type':'string','enum':sorted(UI_VIEWS)},**FILTERS}),
 definition('skill_detail','读取技能详情','只读展示官方已发现技能的能力信息。',{'id':ID},['id']),
 definition('model_list','读取模型目录','读取已经登记的模型与验证快照，不发起验证。'),
 definition('model_detail','读取模型详情','读取指定模型的公开配置与保险库条目引用。',{'id':ID},['id']),
 definition('credential_list','读取配置目录','仅展示非秘密目录与匹配当前修订的结构索引。'),
 definition('credential_details','读取单条配置','返回由请求浏览器临时私钥解密的单条信封，不返回明文。',{'id':ID,'public_key':text(5500),'request_id':text(128)},['id','public_key','request_id']),
]
TOOLS.extend([
 definition('workbench_sync','同步工作台变化','按修订返回后台快照差异；刷新仅排入后台采集，失败保留最后成功数据，不返回密钥。',{'view':{'type':'string','enum':sorted(UI_VIEWS)},'scope':text(200),**FILTERS,'revision':text(64),'refresh':{'type':'boolean'}},['view']),
 definition('module_list','读取工作台模块','读取用户可见模块目录，不公开内部成果、诊断或验收记录。'),
 definition('project_list','读取项目概览','读取原生项目、分区与会话关联，不新建或改写项目。'),
 definition('project_detail','读取项目详情','只读取已存在原生项目及关联会话。',{'id':ID},['id']),
 definition('knowledge_list','读取知识目录','读取选定既有范围内的已审核知识摘要。',{'scope':text(200),'query':text(300)}),
 definition('knowledge_detail','阅读知识','读取指定范围内已审核且有效的知识，不创建或修改知识。',{'scope':text(200),'key':text(200)},['scope','key']),
 definition('service_list','读取服务目录','从已登记模型或当前凭证结构生成只读引用，不解密、不重新登记服务。'),
 definition('service_detail','读取服务引用','读取服务来源和其关联配置引用。',{'id':ID},['id']),
 definition('connection_list','读取工具连接','读取 MCP 配置及本地已安装插件，不进行探测、登录或安装。'),
 definition('global_search','搜索工作台','搜索公开元数据与用户指定范围的知识摘要，不搜索秘密值。',{'query':text(200),'scope':text(200)},['query']),
 definition('planning_create','创建计划任务','在本机计划库创建任务；不会提交、推送或创建远端任务。',{
     'title':text(300),'prompt':text(12000),'project_id':text(255,True),'project_name':text(160,True),
     'section_id':text(255,True),'section_name':text(80,True),'period':text(80,True),
     'start_date':{'type':['string','null'],'pattern':'^\\d{4}-\\d{2}-\\d{2}$'},
     'due_date':{'type':['string','null'],'pattern':'^\\d{4}-\\d{2}-\\d{2}$'},
     'asset_ids':{'type':'array','items':ID,'maxItems':40},'execution_account_id':text(255,True),
     'model':text(120,True),'effort':{'type':['string','null'],'enum':['none','minimal','low','medium','high','xhigh','max','ultra',None]},
     'sandbox':{'type':'string','enum':['read-only','workspace-write']}},['title'],read_only=False,idempotent=False),
 definition('planning_update','更新计划任务','用版本号比较并更新本机任务。',{
     'task_id':ID,'expected_version':{'type':'integer','minimum':1},'patch':{'type':'object','properties':{
         'title':text(300),'prompt':text(12000),'period':text(80,True),
         'start_date':{'type':['string','null'],'pattern':'^\\d{4}-\\d{2}-\\d{2}$'},
         'due_date':{'type':['string','null'],'pattern':'^\\d{4}-\\d{2}-\\d{2}$'},
         'execution_account_id':text(255,True),
         'model':text(120,True),'effort':{'type':['string','null'],'enum':['none','minimal','low','medium','high','xhigh','max','ultra',None]},
         'sandbox':{'type':'string','enum':['read-only','workspace-write']}},'additionalProperties':False}},['task_id','expected_version','patch'],read_only=False,idempotent=False),
 definition('planning_start','启动计划任务','用版本号确认后启动本机任务；message_id 仅执行保存的问答消息。',{'task_id':ID,'expected_version':{'type':'integer','minimum':1},'message_id':text(255,True)},['task_id','expected_version'],read_only=False,idempotent=False),
 definition('planning_stop','停止计划任务','停止本机正在运行的任务。',{'task_id':ID},['task_id'],read_only=False,idempotent=False),
 definition('planning_archive','归档计划任务','归档已结束的本机任务；重试不会再次执行 Codex。',{'task_id':ID},['task_id'],read_only=False,idempotent=True),
 definition('planning_delete','删除计划任务','用版本号确认后软删除本机任务，保留资料和执行历史。',{'task_id':ID,'expected_version':{'type':'integer','minimum':1}},['task_id','expected_version'],read_only=False,idempotent=False),
 definition('planning_followup','记录任务追问','只记录追问，不自动启动或执行任务。',{'task_id':ID,'expected_version':{'type':'integer','minimum':1},'prompt':text(12000)},['task_id','expected_version','prompt'],read_only=False,idempotent=False),
 definition('planning_knowledge_link','关联已审核知识','将用户明确选取的已审核知识条目关联到本机任务。',{'task_id':ID,'scope':text(200),'key':text(200)},['task_id','scope','key'],read_only=False,idempotent=False),
 definition('planning_export','导出计划笔记','生成本机 Obsidian Markdown 投影；可包含任务显式关联的已审核知识，不改写权威知识。',{'task_id':ID,'include_knowledge':{'type':'boolean'}},['task_id'],read_only=False,idempotent=False),
 definition('planning_detail','读取计划任务','读取本机任务及其运行和追问记录。',{'task_id':ID},['task_id']),
 definition('planning_draft','生成计划草案','使用已验证的本机模型目录生成草案；失败时返回手工草案。',{'text':text(12000)},['text'],read_only=False,idempotent=False),
 definition('planning_intake','识别文字任务录入','把文字拆为待确认的新任务、补充或问答草案；不保存或执行。',{'text':text(12000),'project_id':text(255,True),'task_id':text(255,True),'start_date':{'type':['string','null'],'pattern':'^\\d{4}-\\d{2}-\\d{2}$'},'due_date':{'type':['string','null'],'pattern':'^\\d{4}-\\d{2}-\\d{2}$'},'period':text(80,True)},['text'],read_only=False,idempotent=False),
 definition('planning_intake_save','保存文字任务录入','原子保存经确认的工作项，不会启动 Codex。',{'operation_id':text(255),'items':{'type':'array','minItems':1,'maxItems':12,'items':{'type':'object','properties':{'intent':{'type':'string','enum':['create_task','supplement','question']},'target_task_id':text(255,True),'expected_version':{'type':['integer','null'],'minimum':1},'title':text(300),'prompt':text(12000),'project_id':text(255,True),'project_name':text(160,True),'section_id':text(255,True),'section_name':text(80,True),'start_date':{'type':['string','null'],'pattern':'^\\d{4}-\\d{2}-\\d{2}$'},'due_date':{'type':['string','null'],'pattern':'^\\d{4}-\\d{2}-\\d{2}$'},'period':text(80,True),'tags':{'type':'array','items':text(80),'maxItems':6},'execution_account_id':text(255,True),'model':text(120,True),'effort':{'type':['string','null'],'enum':['none','minimal','low','medium','high','xhigh','max','ultra',None]},'sandbox':{'type':['string','null'],'enum':['read-only','workspace-write',None]}},'required':['intent','title','prompt'],'additionalProperties':False}}},['operation_id','items'],read_only=False,idempotent=True),
 definition('library_upload','上传资料','将不超过 256 KiB 的 Base64 资料写入本机资料库。',{'name':text(255),'content_base64':text(349528),'mime':text(150,True),'task_id':text(255,True)},['name','content_base64'],read_only=False,idempotent=False),
 definition('library_upload_begin','开始分块上传','为最大 32 MiB 的本机资料创建分块上传。',{'name':text(255),'mime':text(150,True),'task_id':text(255,True),'size':{'type':'integer','minimum':0,'maximum':33554432}},['name','size'],read_only=False,idempotent=False),
 definition('library_upload_chunk','上传资料分块','写入单个 Base64 分块；偏移必须与当前上传位置一致。',{'upload_id':ID,'offset':{'type':'integer','minimum':0},'content_base64':text(349528)},['upload_id','offset','content_base64'],read_only=False,idempotent=False),
 definition('library_upload_commit','完成分块上传','校验总大小后将已上传资料写入本机资料库。',{'upload_id':ID},['upload_id'],read_only=False,idempotent=False),
 definition('library_link','关联资料','把已存在资料关联到本机任务。',{'asset_id':ID,'task_id':ID},['asset_id','task_id'],read_only=False,idempotent=False),
 definition('library_list','读取资料列表','读取本机资料元数据，不返回资料内容。',{'task_id':text(255,True),'query':text(300),'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':500}}),
 definition('library_content','读取资料分块','读取本机资料的 Base64 分块。',{'asset_id':ID,'offset':{'type':'integer','minimum':0},'length':{'type':'integer','minimum':1,'maximum':262144}},['asset_id']),
])

TASK_CARD={'type':'object','properties':{
 'goal':text(10000),'original':text(100000),
 **{key:{'type':'array','items':text(4000),'maxItems':100} for key in ('scope','preserve','facts','assumptions')},
 'acceptance':{'type':'array','minItems':1,'maxItems':100,'items':{'type':'object','properties':{'id':text(160),'text':text(4000)},'required':['id','text'],'additionalProperties':False}}},
 'required':['goal','original','scope','preserve','facts','assumptions','acceptance'],'additionalProperties':False}
for _tool in TOOLS:
    _properties=_tool['inputSchema']['properties']
    if _tool['name']=='planning_create':_properties.update(task_card=TASK_CARD,original_text=text(100000))
    if _tool['name']=='planning_update':_properties['patch']['properties'].update(task_card=TASK_CARD,original_text=text(100000))
    if _tool['name']=='planning_intake_save':_properties['items']['items']['properties'].update(task_card=TASK_CARD,original_text=text(100000))
TOOLS.extend([
 definition('planning_delivery','更新任务交付','在当前任务版本下更新任务卡、批注、证据或人工验收；不自动执行。',
  {'task_id':ID,'expected_version':{'type':'integer','minimum':1},
   'action':{'type':'string','enum':['save_card','create_anchor','add_annotation','record_evidence','accept_evidence','checkpoint','prepare_rework','review_delivery','diff_artifacts']},
   'payload':{'type':'object','maxProperties':30}},['task_id','expected_version','action','payload'],read_only=False,idempotent=False),
 definition('planning_context','检索任务上下文','仅检索本任务显式关联的资料和知识。semantic=true 使用已登记外部模型；默认本地关键词。',
  {'task_id':ID,'query':text(2000),'semantic':{'type':'boolean'}},['task_id']),
 definition('planning_knowledge_candidates','提取知识候选','从有效交付证据提取待审核候选，不直接写入已审核知识。',
  {'task_id':ID,'delivery':{'type':'object'},'scope':text(200),'source':{'type':'object'},'relation_type':text(80,True),'direction':text(80,True)},
  ['task_id','delivery','scope','source'],read_only=False,idempotent=False),
 definition('planning_output_filter','整理工具输出','保留控制字段、错误和证据引用；可显式关闭并读取原文。',
  {'value':{},'enabled':{'type':'boolean'}},['value']),
 definition('planning_output_source','回取工具原文','读取一小时内的本机工具输出原文，可按 JSON Pointer 读取省略片段。',
  {'source_id':{'type':'string','pattern':'^[0-9a-f]{64}$'},'pointer':text(2000)},['source_id']),
])
# 输出声明与模块边界一致；动态来源字段仅在各读取适配器中投影。
_OUTPUT_KEYS={
 'account_create':['account'], 'account_login':['login'], 'account_status':['account','login'],
 'account_default':['default_account_id','applies_to'],
 'workbench_sync':['context','revision','base_revision','unchanged'],
 'open_workbench':['view','read_only','status'],'workbench_state':['view','read_only','status'],
 'skill_detail':['skill'],
 'model_list':['models'],'model_detail':['model','credential'],'credential_list':['entries','folders','status'],
 'credential_details':['entry_id','revision','generation','request_id','public_key_sha256','wrapped_key','iv','ciphertext'],
 'module_list':['modules','read_only'],'project_list':['projects','sections','sessions'],'project_detail':['project','sessions'],
 'knowledge_list':['scopes','knowledge','selected_scope'],
 'knowledge_detail':['knowledge'],'service_list':['services'],'service_detail':['service'],
 'connection_list':['connections','source_errors'],'global_search':['results','source_errors'],
 'planning_create':['item'],'planning_update':['item'],'planning_start':['item'],'planning_stop':['item'],'planning_archive':['item'],
 'planning_delete':['item'],'planning_followup':['item'],'planning_knowledge_link':['item'],'planning_export':['item'],
 'planning_detail':['item'],'planning_draft':['draft'],'planning_intake':['draft'],'planning_intake_save':['items','tasks'],
 'planning_delivery':['item'],'planning_context':['item'],'planning_knowledge_candidates':['items'],'planning_output_filter':['item'],'planning_output_source':['item'],
 'library_upload':['item'],'library_upload_begin':['item'],'library_upload_chunk':['item'],'library_upload_commit':['item'],
 'library_link':['item'],'library_list':['item'],'library_content':['item'],
}
for _tool in TOOLS:
    _tool['outputSchema']={'type':'object','required':_OUTPUT_KEYS[_tool['name']],'additionalProperties':True}
BY_NAME={tool['name']:tool for tool in TOOLS}

def tools_for_page(page):
    """只有单一工作台入口提供固定只读工具。"""
    if page not in PAGES:raise ValueError('页面不存在')
    return list(TOOLS)

def validate(name: str, arguments: Any) -> dict:
    """执行最小 JSON Schema 类型与边界校验，避免客户端声明成为唯一门禁。"""
    if name not in BY_NAME or not isinstance(arguments, dict):
        raise ValueError("工具或参数无效")
    schema = BY_NAME[name]["inputSchema"]
    if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
        raise ValueError("请求包含未知字段或缺少必填字段")
    for key, value in arguments.items():
        _value(value, schema["properties"][key])
    return dict(arguments)


def _value(value, schema):
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    if value is None and "null" in types:
        return
    if "string" in types and isinstance(value, str):
        if len(value) > schema.get("maxLength", 1000000):
            raise ValueError("字段文本过长")
    elif "boolean" in types and type(value) is bool:
        pass
    elif "integer" in types and type(value) is int:
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            raise ValueError("数值超出范围")
    elif "array" in types and isinstance(value, list):
        if len(value) > schema.get("maxItems", 1000):
            raise ValueError("列表过长")
        for item in value:
            _value(item, schema["items"])
    elif "object" in types and isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
            raise ValueError("对象包含未知字段或缺少必填字段")
        for key, child in value.items():
            _value(child, properties[key])
    else:
        raise ValueError("参数类型无效")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("参数选项无效")
