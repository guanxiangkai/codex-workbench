# Codex 工作台静态演示

本演示复用工作台页面，展示独立生成的虚构账户、模型、技能、配置和知识。页面不会连接工作台服务、模型 API、Codex MCP 或真实账户；搜索、筛选和详情在浏览器内完成。演示中的额度、置信度、状态和时间不代表实际服务情况。

## 构建

在演示分支仓库根目录运行：

```sh
python3 demo/build.py --output outputs/static-demo
node tests/demo-static.mjs
```

无需安装后端依赖。`outputs/static-demo` 是待上传的完整静态目录，请只发布构建产物。不要上传 Git 仓库、本机工作台目录、资源目录、缓存或账户文件。

## 手动部署到阿里云 OSS

1. 将构建目录内的 `index.html` 和全部静态资源按原目录结构上传到目标 Bucket 根目录。
2. 按 OSS 控制台指引配置匿名读取站点对象和静态网站托管，默认首页设为 `index.html`。演示使用页内导航，不依赖后端路由或 `/rpc`。
3. 在 OSS 中绑定 `codex.slimjoy.cn`，按控制台给出的记录配置域名解析，并配置 HTTPS 证书。
4. 打开自己的域名，确认页顶显示演示标识、六个页面均可浏览。浏览器网络面板中仅应有站点静态资源请求；不应出现真实账户、模型 API 或 `/rpc` 请求。

HTML 中附带限制连接的 Content Security Policy；如额外配置响应头，应保留其限制，并允许本站脚本、样式和页面使用的数据图标。

官方操作说明：[静态网站托管](https://www.alibabacloud.com/help/en/oss/user-guide/hosting-static-websites)、[自定义域名](https://www.alibabacloud.com/help/en/oss/user-guide/access-buckets-via-custom-domain-names)。本任务未创建或修改 OSS、DNS、证书及云端权限。

## 演示边界

登录、添加真实账户、切换默认账户、真实额度刷新、解密和敏感字段读取均不可用。演示不会保存输入到远端，也不需要填写 API Key、密码或其他凭据。

此包已完成静态数据和隔离契约验证。当前 Codex 浏览器连接被权限校验阻止，尚未完成实际浏览器视觉验收；上传后的域名、HTTPS 与 OSS 响应由部署者验收。
