"""MCP 与已安装插件的配置清单；不探测、不登录，也不泄露启动环境。"""
import json
import subprocess


class ConnectionCatalog:
    def __init__(self,codex,native,run=None):
        self.codex,self.native,self.run=codex,native,run or subprocess.run

    def listing(self):
        """分别报告配置与已安装事实，不把 enabled 当作在线或可调用。"""
        errors=[];items=[]
        try:
            r=self.run([self.codex,'mcp','list','--json'],text=True,capture_output=True,timeout=15)
            if r.returncode or len(r.stdout)>2*1024*1024:raise ValueError()
            source=json.loads(r.stdout)
            if not isinstance(source,list):raise ValueError()
            for item in source[:200]:
                if not isinstance(item,dict) or not isinstance(item.get('name'),str):continue
                transport=item.get('transport') or {}
                items.append({'id':'mcp:'+item['name'],'name':item['name'],'kind':'mcp','enabled':item.get('enabled') is True,
                              'transport':transport.get('type'),'auth_status':item.get('auth_status'),'source':'codex_configuration',
                              'availability':'unverified'})
        except (OSError,ValueError,subprocess.TimeoutExpired):errors.append('MCP 配置暂时不可读取')
        try:
            items.extend(self.native.plugins())
        except (OSError,ValueError):errors.append('已安装插件目录暂时不可读取')
        return {'connections':items,'source_errors':errors}
