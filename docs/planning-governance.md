# 任务规则治理

这次能力沿用任务详情、现有交付 API 和本机 SQLite；不增加页面、云端数据库或新的调度服务。规则文件仍由其原入口负责加载，治理候选不会自动写入 AGENTS.md 或 Skill。

## 能力与完成标准

| 能力 | 实际行为 | 边界 |
| --- | --- | --- |
| 稳定验收条件 | ID 贯穿任务卡、运行快照、证据及条件追踪；修改 A2 只使 A2 的条件证据失效 | 目标、范围、保留项、输入资料及运行变化仍影响相关交付；移除条件保留历史 |
| 规则清单 | 每次运行记录任务约束输入与发现的固定规则文件、UTF-8 SHA-256、字节数、来源 | discovered 不等于 loaded，injected 不等于遵循；不宣称发现全部动态 Skills |
| 冻结行为评测 | 候选生成前冻结规则文本及案例，分别执行基线、候选并本地核对断言 | 只证明给定规则文本对案例的结果，不证明 Skill 隐式触发、权限或生产行为 |
| 失败归因 | 共同根因聚合，显式独立性标识去重；用户明确纠正可单例提出候选 | 来源独立性仍是报告值，不能把会话数量当成已验证因果关系 |
| 未知调用恢复 | operation key、服务端操作编号、状态引用及证据留痕；完成后还需对应实际响应才能采纳 | 只记录外部核对结果，不自动访问任意状态 URL；unknown 不盲重发；prepared 可取消 |
| 规则候选 | 绑定任务权威范围、基线清单哈希、冻结规则哈希、反馈及统一差异 | 评测通过也不执行规则文件修改，不自动升级为已审核知识 |
| 指令预算 | UTF-8 字节/4 估算、分词器已知性、机械重复与相反文句候选、受保护约束 | 不是精确 Token 计量或语义冲突证明，不以预算自动删除安全约束 |
| 结果指标 | 已报告的完成、返工、介入、回归、耗时、用量，保留已知/未知数量与比例 | 无记录不等于零；模型调用成功不等于用户验收 |

## 使用入口

沿用 `planning_delivery(task_id, expected_version, action, payload)`。所有修改先检查任务范围和当前版本；过去日期与运行中的限制沿用任务契约。`planning_detail` 返回 `delivery.conditions` 与 `governance`，界面复用交付详情中的折叠组件。

1. `governance_capture`：仅采集固定入口的规则元数据，不读取任意客户端路径，也不外发文件。规则文件末级软链接、无法读取或超过 64 KB 会留下不可用原因。
2. `governance_freeze`：提供 `rule_id`（默认 `task-card`）及 `cases`。案例为 `{id,input,expected:{required:[],forbidden:[]}}`，最多 12 条；必须有实际断言。服务端冻结规则原文，拒绝重复 ID、空断言和互相矛盾的字面断言。冻结后不能替换。需要新基线时使用新的评测任务。
3. `governance_feedback`：提供 `source_kind`（run/user_correction/review/evaluation）、`root_cause`、`independence_key`、`summary`，可选 `source_ref` 和 `outcome`。同一故障重试沿用根因与独立性键。
4. `governance_propose`：提供 `proposal:{rule_id,replacement_text,rationale}`、`feedback_ids`。明确用户纠正可加 `user_correction:true`，且证据中必须有对应纠正。Scope 由任务决定，客户端不能跨任务指定范围；至少两个报告为独立的证据，或明确纠正。
5. `governance_preview`：提供 `candidate_id`，返回明确外发内容和 `packet_hash`。普通详情不包含冻结规则原文、案例输入或预期断言；预览也不包含预期断言。完整 AGENTS 文件候选只允许本地审查，不允许外发评测。
6. 审核当前材料可外发后，`governance_evaluate` 提供 `candidate_id` 和相同的 `approved_packet_hash`。仅使用已登记推理模型端点。匹配的是准确材料包，而非永久数据授权。基线/候选比较均为独立无工具模型调用；没有可用路由则明确失败。

评测前持久化调用尝试；相同候选、套件和基线重复请求复用结果，中断状态不盲重发。评测器异常、任务并发变化或来源变化均不能被标记为可采用；最终写入前在共享锁内再次核对原版本和指纹。通过的结果仍是指定套件的历史测量，没有自动应用入口。

`cancel_handoff` 只取消未发送材料包；`reconcile_handoff` 记录已查询的服务端操作编号、状态引用、阶段和证据；`recover_handoff` 要求之前已确认完成并提供相符操作编号的实际结构化响应。关闭页面或断开连接不代表远端取消，单独声称“完成”不等于拿到响应。

## 验证

直接测试位于 `test_planning_delivery.py`、`test_planning_coordination.py`、`test_planning_governance.py`、`test_planning_governance_integration.py` 和 `ui-planning.mjs`。集成测试使用隔离目录和合成模型返回，覆盖不外发规则文件/隐藏断言、包授权、重复调用复用及并发任务版本变化。

真实 GLM 已参与公开、脱敏失败用例设计；这属于独立辅助审查，不等于真实模型对全部行为套件通过。浏览器实际验收与代码/DOM 测试分别报告。
