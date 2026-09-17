# 本机适配器接口

## 非秘密目录

通过 `--resources-dir` 或 `WORKBENCH_RESOURCES_DIR` 指定目录。工作台读取其中的 `accounts/catalog.json` 与 `models/catalog.json`，但不会把内容上传到 GitHub。

最小其他账户目录：

```json
{
  "version": 1,
  "providers": [
    {
      "id": "minimax",
      "name": "MiniMax",
      "accounts": [{"id": "example", "label": "示例账户"}]
    }
  ]
}
```

这是未绑定凭据的示例，不会发起远端用量查询。用户可另行在自己的目录中配置 `vault_id` 与 `usage_credential_id`，二者都只接受保险库条目引用，不能填写 Key 本身。模型供应商字段及能力枚举由 `model_catalog.py` 和 `model_registry.py` 校验。

## 保险库

默认调用用户自行安装的 `~/.codex/scripts/key-vault/key-vault.sh`。公开仓库没有该工具、实际条目或密钥。测试通过构造函数注入合成适配器。

读取接口包括：

- `status`：只输出准备状态。
- `list`：只输出非秘密条目清单，包含条目引用和修订号。
- `exec-stdin <entry-reference> <consumer> <arguments...>`：直接把秘密载荷送入固定消费者 stdin；不得向日志输出。

MiniMax 消费者支持原始 Key 字符串及含 `api_key` 字段的 JSON。公开父进程只接收经过白名单转换的用量与固定错误码。配置详情消费者使用浏览器临时公钥，将结果以 RSA-OAEP / AES-GCM 信封返回。

## 知识工具

默认调用用户自行安装的 `~/.codex/skills/manage-personal-knowledge/scripts/personal-knowledge`，支持 `scopes`、`catalog`、`search`、`lookup` 等只读操作。仓库不带知识内容。调用方可注入符合 `KnowledgeCatalog` 契约的替代实现。

用于缓存失效检测的目录可通过 `WORKBENCH_KNOWLEDGE_DIR` 和 `WORKBENCH_VAULT_DIR` 指定，默认位于本机工作台数据目录。检测只读取文件元数据，不读取秘密内容。

## 测试素材

`src/codex_workbench/fixtures/probe.wav` 由仓库脚本生成纯正弦测试音，不包含人声或录音。它只适合检查容器与传输链路，不证明真实语音识别准确率。其他媒体夹具是代码生成的像素和静音帧。
