"""单一只读工作台 MCP 契约；客户端声明不能绕过服务端白名单。"""
from __future__ import annotations
from typing import Any

VERSION = "0.9.1"
UI_URIS = {"open_workbench":"ui://codex-workbench/v7/workbench.html"}
PAGES = {"workbench":("codex-workbench","open_workbench","工作台")}
MODULES=[
 {'id':'accounts','name':'Codex 账户','group':'账户与配置'},
 {'id':'other_accounts','name':'其他账户','group':'账户与配置'},
 {'id':'config','name':'配置中心','group':'账户与配置'},
 {'id':'agents','name':'技能助手','group':'能力与知识'},{'id':'assets','name':'资源中心','group':'能力与知识'},
 {'id':'knowledge','name':'知识中心','group':'能力与知识'},{'id':'models','name':'模型目录','group':'能力与知识'},
]
# 默认入口与导航第一项保持一致。
DEFAULT_VIEW = MODULES[0]["id"]
UI_VIEWS = frozenset(module["id"] for module in MODULES)

def text(maximum=300, nullable=False):
    """有界文本参数。"""
    return {"type":["string","null"] if nullable else "string","maxLength":maximum}

ID=text(255)

def definition(name,title,description,properties=None,required=None):
    """所有公开工具仅允许读取；秘密详情仅对 App 可见。"""
    result={"name":name,"title":title,"description":description,
        "inputSchema":{"type":"object","properties":properties or {},"required":required or [],"additionalProperties":False},
        "annotations":{"readOnlyHint":True,"destructiveHint":False,"idempotentHint":True,"openWorldHint":name in ('workbench_state','workbench_sync')}}
    if name in UI_URIS:
        result["_meta"] = {"ui": {"resourceUri": UI_URIS[name]},
                           "openai/ui": {"entrypoints": [{"type": "global"}]}}
    if name in ('credential_details','asset_detail','knowledge_detail','account_default'):result["_meta"]={"ui":{"visibility":["app"]}}
    if name=='account_default':result['annotations']['readOnlyHint']=False
    return result

PAGING={'page':{'type':'integer','minimum':1,'maximum':100000},'page_size':{'type':'integer','minimum':1,'maximum':50},'query':text(300),'kind':text(100),'provider':text(200),'tag':text(200),'folder':text(200)}

TOOLS=[
 definition('account_default','设置默认账户','设置已登记且身份核验通过的新会话默认账户；不改变已有会话或官方登录。',{'id':ID,'expected_default_id':text(255,True)},['id','expected_default_id']),
 definition('open_workbench','工作台','打开只读工作台，不创建、修改或执行业务对象。'),
 definition('workbench_state','读取工作台','按视图读取原生记录与已登记配置；其他账户视图会受控使用已绑定凭据查询 MiniMax 官方只读用量。',{'view':{'type':'string','enum':sorted(UI_VIEWS)},**PAGING}),
 definition('skill_detail','读取技能详情','只读展示官方已发现技能的能力信息。',{'id':ID},['id']),
 definition('model_list','读取模型目录','读取已经登记的模型与验证快照，不发起验证。'),
 definition('model_detail','读取模型详情','读取指定模型的公开配置与保险库条目引用。',{'id':ID},['id']),
 definition('credential_list','读取配置目录','仅展示非秘密目录与匹配当前修订的结构索引。'),
 definition('credential_details','读取单条配置','返回由请求浏览器临时私钥解密的单条信封，不返回明文。',{'id':ID,'public_key':text(5500),'request_id':text(128)},['id','public_key','request_id']),
]
TOOLS.extend([
 definition('workbench_sync','同步工作台变化','按修订返回条目差异；其他账户刷新时通过隔离消费者使用已绑定凭据查询 MiniMax 官方只读用量，失败保留快照，不返回密钥、不启动定时查询。',{'view':{'type':'string','enum':sorted(UI_VIEWS)},'scope':text(200),**PAGING,'revision':text(64),'refresh':{'type':'boolean'}},['view']),
 definition('module_list','读取工作台模块','读取用户可见模块目录，不公开内部成果、诊断或验收记录。'),
 definition('project_list','读取项目概览','读取原生项目、分区与会话关联，不新建或改写项目。'),
 definition('project_detail','读取项目详情','只读取已存在原生项目及关联会话。',{'id':ID},['id']),
 definition('asset_list','读取技能资源','读取已发现技能内的有界静态资源目录，不移动或复制资源。'),
 definition('asset_detail','查看技能资源','按不透明标识读取受限预览，不接受客户端路径。',{'id':ID},['id']),
 definition('knowledge_list','读取知识目录','读取选定既有范围内的已审核知识摘要。',{'scope':text(200),'query':text(300)}),
 definition('knowledge_detail','阅读知识','读取指定范围内已审核且有效的知识，不创建或修改知识。',{'scope':text(200),'key':text(200)},['scope','key']),
 definition('service_list','读取服务目录','从已登记模型或当前凭证结构生成只读引用，不解密、不重新登记服务。'),
 definition('service_detail','读取服务引用','读取服务来源和其关联配置引用。',{'id':ID},['id']),
 definition('connection_list','读取工具连接','读取 MCP 配置及本地已安装插件，不进行探测、登录或安装。'),
 definition('global_search','搜索工作台','搜索公开元数据与用户指定范围的知识摘要，不搜索秘密值。',{'query':text(200),'scope':text(200)},['query']),
])

# 输出声明与模块边界一致；动态来源字段仅在各读取适配器中投影。
_OUTPUT_KEYS={
 'account_default':['default_account_id','applies_to'],
 'workbench_sync':['context','revision','base_revision','unchanged'],
 'open_workbench':['view','read_only','status'],'workbench_state':['view','read_only','status'],
 'skill_detail':['skill'],
 'model_list':['models'],'model_detail':['model','credential'],'credential_list':['entries','folders','status'],
 'credential_details':['entry_id','revision','generation','request_id','public_key_sha256','wrapped_key','iv','ciphertext'],
 'module_list':['modules','read_only'],'project_list':['projects','sections','sessions'],'project_detail':['project','sessions'],
 'asset_list':['assets','truncated'],'asset_detail':['asset','preview'],'knowledge_list':['scopes','knowledge','selected_scope'],
 'knowledge_detail':['knowledge'],'service_list':['services'],'service_detail':['service'],
 'connection_list':['connections','source_errors'],'global_search':['results','source_errors'],
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
