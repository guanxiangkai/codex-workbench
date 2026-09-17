"""运行时 manifest 驱动的 MCP stdio 入口。"""
from __future__ import annotations
import json, os, select, sys, threading
from typing import Any
from .catalog import PAGES
from .runtime import RuntimeClient, MAX_MESSAGE, safe_error
from .ui_resources import ui_html, page_icons

def _validate(value: Any, schema: dict) -> None:
    kinds=schema.get("type"); kinds=kinds if isinstance(kinds,list) else [kinds]
    if value is None and "null" in kinds:return
    if "string" in kinds and isinstance(value,str):
        if len(value)>schema.get("maxLength",1_000_000):raise ValueError()
    elif "boolean" in kinds and type(value) is bool:pass
    elif "integer" in kinds and type(value) is int:
        if not schema.get("minimum",value)<=value<=schema.get("maximum",value):raise ValueError()
    elif "array" in kinds and isinstance(value,list):
        if len(value)>schema.get("maxItems",1000):raise ValueError()
        for item in value:_validate(item,schema["items"])
    elif "object" in kinds and isinstance(value,dict):
        props=schema.get("properties",{})
        if set(value)-set(props) or set(schema.get("required",[]))-set(value):raise ValueError()
        for key,item in value.items():_validate(item,props[key])
    else:raise ValueError()
    if "enum" in schema and value not in schema["enum"]:raise ValueError()

class McpSession:
    def __init__(self,client:RuntimeClient,page:str="workbench"):
        if page not in PAGES:raise ValueError("页面不存在")
        self.client,self.page=client,page;self.server_name,self.entry,self.title=PAGES[page]
        self.revision=None;self.manifest=None;self.initialized=False;self.ready=False;self.read_uris=set();self.subscribed_uris=set();self.changed=False
        self.resource_list_changed=False
    def _refresh(self):
        result=self.client.call("_runtime_manifest",{"page":self.page,"known_revision":self.revision})
        if not isinstance(result,dict) or not isinstance(result.get("revision"),str):raise ValueError("运行时界面清单不可用")
        if result.get("unchanged"):
            if self.manifest is None:raise ValueError("运行时界面清单不可用")
            return
        required={"revision","version","tools","page","entry","title","resource_uri","html","icons"}
        if not required<=set(result) or result["page"]!=self.page or result["entry"]!=self.entry or not isinstance(result["tools"],list) or not isinstance(result["html"],str) or not isinstance(result["resource_uri"],str):raise ValueError("运行时界面清单无效")
        names=[]
        for tool in result["tools"]:
            if not isinstance(tool,dict) or not isinstance(tool.get("name"),str) or not isinstance(tool.get("inputSchema"),dict):raise ValueError("运行时工具清单无效")
            names.append(tool["name"])
        if len(names)!=len(set(names)):raise ValueError("运行时工具清单无效")
        # 资源地址或标题变化会使宿主缓存的资源列表失效；内容修订不触发列表通知。
        self.resource_list_changed|=self.manifest is not None and any(result[key]!=self.manifest[key] for key in ("resource_uri","title"))
        self.changed|=self.manifest is not None and result["revision"]!=self.revision;self.manifest,self.revision=result,result["revision"]
    def notifications(self):
        if not self.ready or not (self.changed or self.resource_list_changed):return []
        self.changed=False;result=[{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}]
        if self.resource_list_changed:
            result.append({"jsonrpc":"2.0","method":"notifications/resources/list_changed"});self.resource_list_changed=False
        for uri in self.read_uris|self.subscribed_uris:result.append({"jsonrpc":"2.0","method":"notifications/resources/updated","params":{"uri":uri}})
        return result
    def _resource_allowed(self,uri):
        """只接受本页当前、历史或已批准的资源；订阅在退订后不再单独授权。"""
        return isinstance(uri, str) and uri == self.manifest["resource_uri"]
    def handle(self,message:dict):
        ident,method=message.get("id"),message.get("method");notification="id" not in message
        if message.get("jsonrpc")!="2.0" or not isinstance(method,str):return {"jsonrpc":"2.0","id":ident,"error":{"code":-32600,"message":"Invalid Request"}}
        if not notification and (isinstance(ident,bool) or not isinstance(ident,(str,int,type(None)))):
            return {"jsonrpc":"2.0","id":None,"error":{"code":-32600,"message":"Invalid Request id"}}
        params=message.get("params",{})
        if not isinstance(params,dict):return None if notification else {"jsonrpc":"2.0","id":ident,"error":{"code":-32602,"message":"params must be an object"}}
        try:
            if method=="initialize":
                if self.initialized:raise ValueError()
                self._refresh();self.initialized=True;protocol=params.get("protocolVersion") if params.get("protocolVersion") in {"2025-11-25","2025-06-18","2024-11-05"} else "2025-06-18"
                value={"protocolVersion":protocol,"capabilities":{"tools":{"listChanged":True},"resources":{"subscribe":True,"listChanged":True},"extensions":{"io.modelcontextprotocol/ui":{"mimeTypes":["text/html;profile=mcp-app"]}}},"serverInfo":{"name":self.server_name,"title":self.manifest["title"],"version":self.manifest["version"],"icons":self.manifest["icons"]},"instructions":"本地工作台。"}
            elif method=="notifications/initialized":self.ready=self.initialized;return None
            elif method=="ping":value={}
            elif notification:return None
            elif not self.ready:raise ValueError()
            elif method in {"tools/list","resources/list","resources/read","tools/call","resources/subscribe","resources/unsubscribe"}:
                self._refresh()
                if method=="tools/list":value={"tools":self.manifest["tools"]}
                elif method=="resources/list":value={"resources":[{"uri":self.manifest["resource_uri"],"name":self.entry,"title":self.manifest["title"],"mimeType":"text/html;profile=mcp-app"}]}
                elif method=="resources/read":
                    uri=params.get("uri")
                    if not self._resource_allowed(uri):raise ValueError("UI 资源不存在")
                    page=self.client.call("_runtime_page",{})
                    self.read_uris.add(uri);value={"contents":[{"uri":uri,"mimeType":"text/html;profile=mcp-app","text":page["html"],"_meta":{"ui":{"prefersBorder":False,"permissions":{"clipboardWrite":{}},"csp":page["csp"]}}}]}
                elif method=="resources/subscribe":
                    uri=params.get("uri")
                    if not self._resource_allowed(uri):raise ValueError("UI 资源不存在")
                    self.subscribed_uris.add(uri);value={}
                elif method=="resources/unsubscribe":self.subscribed_uris.discard(params.get("uri"));value={}
                else:
                    name,args=params.get("name"),params.get("arguments",{});tools={tool["name"]:tool for tool in self.manifest["tools"]}
                    try:
                        if name not in tools:raise ValueError("此入口不提供该工具")
                        _validate(args,tools[name]["inputSchema"]);data=self.client.call(name,args);value={"structuredContent":data if isinstance(data,dict) else {"items":data},"content":[{"type":"text","text":"工作台操作已完成。"}]}
                    except Exception as error:
                        safe=safe_error(error);value={"isError":True,"structuredContent":{"error":safe},"content":[{"type":"text","text":safe["message"]}]}
            elif method=="resources/templates/list":value={"resourceTemplates":[]}
            else:return {"jsonrpc":"2.0","id":ident,"error":{"code":-32601,"message":"Method not found"}}
            return None if notification else {"jsonrpc":"2.0","id":ident,"result":value}
        except (ValueError,TypeError,KeyError,RecursionError):return None if notification else {"jsonrpc":"2.0","id":ident,"error":{"code":-32602,"message":"Invalid parameters"}}
        except OSError:
            safe=safe_error(OSError())
            return None if notification else {"jsonrpc":"2.0","id":ident,"error":{"code":-32603,"message":safe["message"]}}

def serve_stdio(client:RuntimeClient,page:str="workbench"):
    session=McpSession(client,page);lock=threading.Lock()
    def emit(value):
        with lock:sys.stdout.write(json.dumps(value,ensure_ascii=False)+"\n");sys.stdout.flush()
    fd=sys.stdin.buffer.fileno();buffer=bytearray();dropping=False;dropped=0;watch_key=None
    def parse_line(line):
        try:
            if len(line)>MAX_MESSAGE:raise ValueError()
            message=json.loads(line)
            if not isinstance(message,dict):raise ValueError()
            return session.handle(message)
        except (ValueError,UnicodeDecodeError,RecursionError,OSError):
            return {"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Parse error"}}
    while True:
        ready,_,_=select.select([fd],[],[],2)
        if not ready:
            try:
                current_key=client.manifest_watch_key() if hasattr(client,"manifest_watch_key") else object()
                if session.ready and current_key!=watch_key:
                    session._refresh();watch_key=current_key
            except Exception:pass
            for notice in session.notifications():emit(notice)
            continue
        chunk=os.read(fd,65536)
        if not chunk:break
        if dropping:
            dropped+=len(chunk);newline=chunk.find(b"\n")
            if dropped>32*1024*1024:break
            if newline<0:continue
            emit({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Parse error"}})
            dropping=False;dropped=0;buffer.extend(chunk[newline+1:])
        else:buffer.extend(chunk)
        while not dropping:
            newline=buffer.find(b"\n")
            if newline<0:
                if len(buffer)>MAX_MESSAGE:
                    dropping=True;dropped=len(buffer);buffer.clear()
                break
            line=bytes(buffer[:newline]);del buffer[:newline+1]
            response=parse_line(line)
            if response is not None:emit(response)
        for notice in session.notifications():emit(notice)

__all__=["McpSession","serve_stdio","ui_html","page_icons"]
