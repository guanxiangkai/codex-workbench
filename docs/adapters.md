# 本机适配器接口

## 非秘密目录

通过 `--resources-dir` 或 `WORKBENCH_RESOURCES_DIR` 指定目录。工作台只读其中的 `accounts/` 与 `models/`，不会把内容上传到 GitHub，也不提供这些目录的增删改接口。Codex 可直接维护 JSON。

为避免多人或多个自动化步骤改写同一大文件，可保留旧的 `accounts/catalog.json`、`models/catalog.json`，并将每条新增记录拆成独立文件；同一目录的两种格式会合并读取，重复 ID 会明确报错，绝不按文件名或读取顺序静默覆盖。

单模型分片 `models/<model-id>.json`：

```json
{"version": 1, "model": {"id": "example", "name": "示例模型", "service_name": "示例服务", "model_type": "reasoning", "base_url": "https://example.invalid/v1", "model": "example", "model_source": "explicit", "updated_at": "2026-09-21T00:00:00+08:00"}}
```

单账户分片 `accounts/<account-id>.json`：

```json
{"version": 1, "provider": {"id": "minimax", "name": "MiniMax"}, "account": {"id": "example", "label": "示例账户"}}
```

拆分可缩小同一文件的修改范围，但并不能代替写入侧的原子写、版本核对或锁。

现有聚合目录可先预检，再拆分并保留恢复副本：

```sh
PYTHONPATH=src python3 migrate_resource_catalogs.py --resources-dir <资源目录>
PYTHONPATH=src python3 migrate_resource_catalogs.py --resources-dir <资源目录> --apply --runtime-stopped
```

迁移只在目录中没有既有分片或 `legacy/catalog.json` 时执行。它先在隔离目录写入并验证拆分后的公开投影与旧目录完全一致，才落盘；应用阶段必须先停止工作台运行时，失败会恢复已移动的 catalog 并清理本次分片。成功后旧文件位于 `models/legacy/catalog.json` 和 `accounts/legacy/catalog.json`，运行时不读取该恢复副本。

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
