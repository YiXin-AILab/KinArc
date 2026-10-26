# 汽车售后客服 Agent（auto_after_sales）

汽车售后服务中心智能客服「售后管家」，7×24 在线，覆盖五类意图：

| 意图 | 处理方式 |
| --- | --- |
| 日常咨询（价格/工时费/服务时长） | `kb_search` 检索 `pricing` 知识库后作答 |
| 服务类（预约/找顾问/投诉） | `create_appointment` / `forward_to_advisor` / `file_complaint` |
| 技术类 FAQ | `kb_search` 检索 `tech_faq` 知识库 |
| 事故救急 | 第一时间 `escalate_emergency` 通知值班救援专员 |
| 定期招揽 | `generate_reminders` 生成草稿 → 人工 `review_reminder` 审核发送 |

本示例不对应任何真实汽车品牌。S1～S6 为虚拟车型；客户与顾问电话均为
111 开头的 11 位演示号码，价格与服务政策仅用于演示。

知识库检索默认 BM25 + 相关性裁判（relevance_gate），开箱即可演示；切换
embedding/向量检索见下文「检索管线切换」。

## 目录结构

```text
config/agent.yaml      # Agent 配置：工具白名单、knowledge_bases 引用、LLM
prompt/system.md       # 人设与意图路由、事故救急硬规则
plugins/service_tools.py   # 业务工具（签名推导式注册）
services/              # 客户档案、通知通道、提醒生成三个服务
data/
  customers.json       # 客户档案（mock）
  advisors.json        # 顾问档案（adv-999 为应急救援值班）
  appointments.jsonl   # 运行时生成：预约登记
  notifications.jsonl  # 运行时生成：通知落地（mock 通道）
  reminders.json       # 运行时生成：提醒草稿与审核状态
tests/                 # 应用层 pytest（services 行为测试）
```

三个知识库已升级为项目级平台资源，位于仓库根 `knowledge/{pricing,tech_faq,service_flow}/`
（各含 `kb.yaml` 清单与 `documents.json` 文档），agent.yaml 通过
`knowledge_bases` 按名引用；应用级可在 `application/auto_after_sales/knowledge/`
放同名目录覆盖全局库。

## 环境变量

`config/agent.yaml` 与 `knowledge/*/kb.yaml` 引用以下变量，全部必须在 `.env` 中真实存在：

| 变量 | 用途 |
| --- | --- |
| `AUTO_LLM_MODEL` / `AUTO_LLM_API_KEY` / `AUTO_LLM_BASE_URL` | 主 LLM，同时作为三个知识库 relevance_gate 裁判模型 |

可选（切换向量检索与 rerank 时）：

| 变量 | 用途 |
| --- | --- |
| `JINA_API_KEY` | Jina embedding/rerank API Key |
| `AUTO_KB_EMBEDDING_MODEL` / `AUTO_KB_RERANK_MODEL` | 自定义模型名（也可直接写死在 YAML） |

## 数据 Schema 约定（接真实数据时保持）

`knowledge/<库名>/documents.json` 统一为数组，元素：

```json
{
  "doc_id": "pricing-001",
  "title": "文档标题",
  "category": "分类",
  "content": "正文（检索与回答依据）",
  "updated_at": "YYYY-MM-DD"
}
```

`doc_id` 规范为 `<库名前缀>-<三位序号>`，库内唯一。

`data/customers.json` 元素：

```json
{
  "customer_id": "cust-001",
  "name": "王建国",
  "phone": "11100000001",
  "vin": "17 位车架号",
  "model": "S4",
  "purchase_date": "YYYY-MM-DD",
  "last_service_date": "YYYY-MM-DD",
  "last_service_mileage": 38600,
  "insurance_expiry": "YYYY-MM-DD",
  "birthday": "YYYY-MM-DD",
  "preferred_advisor": "adv-002",
  "tags": ["金卡会员"]
}
```

`data/advisors.json` 元素：`{advisor_id, name, specialty, shift, phone}`，
应急救援值班专员额外标记 `"emergency_oncall": true`（**必须恰好配置一名**，
否则事故升级工具会报错）。

`data/reminders.json`（运行时生成）元素：

```json
{
  "reminder_id": "rem-cust-001-maintenance-20260930",
  "customer_id": "cust-001",
  "type": "maintenance | birthday | insurance",
  "title": "...",
  "content": "...",
  "reason": "生成依据",
  "status": "pending | sent | dismissed",
  "created_at": "YYYY-MM-DD"
}
```

`data/notifications.jsonl`（运行时生成）每行一条通知，`type` 为
`advisor_forward` / `emergency_escalation` / `reminder_send`。接真实通道时
替换 `services/notify_service.py` 的落地实现即可，工具层不变。

## 招揽提醒规则

`generate_reminders` 扫描客户档案，命中以下规则生成 `pending` 草稿：

- **保养**：按「半年或 1 万公里先到为准」折算为上次保养日期 +183 天，
  到期前 14 天内（含已超期）触发；
- **生日**：生日前 7 天内触发，文案含生日月工时 8 折权益；
- **续保**：车险到期前 30 天内触发。

**频控与幂等**：同一 `customer_id + type` 在 30 天内已有任何状态的提醒记录
（含已发送、已驳回）则跳过；因此定时任务每天重复跑也不会产生重复 pending
草稿。草稿必须由人工（或运营 Agent）调用 `review_reminder` 审核，`send`
动作才会经通知通道触达客户。

## 检索管线切换（BM25 → 向量 + rerank）

默认三个知识库均为 `strategy: bm25`，无需 embedding API Key。有
`JINA_API_KEY` 或自部署 embedding 端点后，把对应 `knowledge/<库名>/kb.yaml`
的 `retrieval` 改为：

```yaml
name: pricing
description: 保养/维修价格、工时费标准、套餐、质保与服务时长
retrieval:
  strategy: hybrid            # 或 vector
  top_k: 8
  embedding:
    model: jina_ai/jina-embeddings-v3
    api_key: ${JINA_API_KEY}
    timeout_seconds: 30
rerank:
  model: jina_ai/jina-reranker-v2
  api_key: ${JINA_API_KEY}
  top_n: 4
relevance_gate:
  model: ${AUTO_LLM_MODEL}
  api_key: ${AUTO_LLM_API_KEY}
  base_url: ${AUTO_LLM_BASE_URL}
  min_relevance: partial
```

也可以只对本 Agent 做引用级覆盖（不改库默认），在 agent.yaml 的
`knowledge_bases` 条目上声明 `retrieval` / `relevance_gate` 即可。

`vector`/`hybrid` 策略必须配置 `embedding`；`relevance_gate` 建议保留，
知识库无相关内容时工具会明确返回「无相关内容」，提示词约束 Agent 礼貌拒答
而不是编造。

## 定时招揽设置

### 当前用户的跨对话记忆

Runtime 配置启用 Redis Memory；本 Agent 仅开放 `user_agent` 范围，使用
`memory_read` / `memory_add` / `memory_update` 延续同一
`app_id + user_id + agent_id` 的服务，不跨用户或跨 Agent 共享。
新对话自动获取初始记忆；已有会话处理预约和提醒前显式读取最新内容。

系统提示词规定保存称呼、联系时段偏好、已核对的客户关联、已确认预约、
待确认改期与提醒任务 ID；正式安排与待确认需求分开记录，不复制完整电话、
VIN、生日或聊天记录。记忆不是身份认证或预约数据库，工具结果与用户明确确认
的事实优先。用户可要求更正、忘记或停止提醒。

### 主动预约提醒

预约确认后，Agent 在后台使用已有 `schedule_task_*` 工具安排一次性提醒，
默认前一个自然日10:00（Asia/Shanghai），仅在09:00–18:00或用户更窄的联系时段内触达。
默认时刻已过时，选择至少五分钟后且早于到店的合适时刻；没有合适时刻则不创建。

任务名为“预约提醒/<预约号>”，类型 `one_time`、会话模式 `fixed`。
先查询再创建；已确认的改期或联系偏好变化后更新原任务，停止和恢复也复用原任务。
通过 `memory_*` 保存必要预约事实及任务关联，不主动向客户介绍后台管理过程。

业务规则由系统提示词约束，复用 Runtime 的记忆和调度能力，没有预约专用工具、
中间件或固定回复模板。客户查询工具不返回完整电话、VIN 和生日，记忆最小化由提示词约束，
没有额外的写入过滤器。本示例仍不提供正式预约查询、修改或取消工具，需交由顾问处理；
仅在用户明确确认顾问已办妥后同步记忆和提醒。

前端保持原实现，调试卡片保留，不增加空闲会话轮询；空闲页面可能需要刷新才能看到后台提醒。

### 定期招揽草稿

运行时需要 `config/runtime.yaml` 开启 `scheduler.enabled: true`（已默认开启）。
在对话中对 Agent 说「每天早上 9 点生成客户关怀提醒草稿」，Agent 会调用
`schedule_task_create` 创建 cron 任务（如 `0 9 * * *`），任务触发时自动以
固定会话执行 `generate_reminders`。也可用 `schedule_task_list` /
`schedule_task_delete` 管理。

## 运行与测试

```bash
# 应用层测试
uv run pytest application/auto_after_sales/tests

# 启动 Runtime（发现 application/ 下全部 Agent）
uv run python -m server.entrypoint
```

对话接口：`POST /v1/sessions/{session_id}/messages`，请求体：

```json
{
  "app_id": "demo",
  "user_id": "u1",
  "agent_id": "auto-after-sales",
  "message_id": "msg-001",
  "role": "user",
  "content": [{"type": "text", "text": "汽车 S4 A 保多少钱？"}],
  "mode": "queue"
}
```

事件流 `GET /v1/sessions/{id}/events`（SSE），历史消息
`GET /v1/sessions/{id}/messages?app_id=...&user_id=...&agent_id=...`。

## 演示入口（web 前端）

前端（`cd web && npm run dev`，默认 10061 代理到 10060）演示入口：

| 入口 | 路径 | 内容 |
| --- | --- | --- |
| 对话演示 | `/chat?agent=auto-after-sales` | 通用对话页锁定本 Agent |
| 提醒审核 | 对话内指令 | 让 Agent 调 `list_reminders` 查看草稿、`review_reminder` 确认发送 / 忽略 |
| 知识库管理 | `/platform/knowledge` | 知识库文档的查看/新增/替换/删除，保存后下次检索即生效 |

评测见 `docs/evaluation-system.md`，前端评测页位于 `/platform/evaluation`；
知识库平台（检索测试、生成评测集）见 `docs/knowledge-platform.md`。演示操作手册见
`docs/demo-runbook.md`。
