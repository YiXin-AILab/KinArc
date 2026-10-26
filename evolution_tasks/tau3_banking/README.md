# Tau3 Banking

通过自进化优化 [Banking Agent](../../application/banking_agent)，使用 Tau2 Bench
`v1.0.1` 的 Banking Knowledge 任务执行对话并评分。训练集提供优化反馈，验证集用于
选择候选，测试集由用户手动运行。

普通 Banking 使用 `bm25_grep` 检索模式；直接提供题目所需文档的版本见
[Golden Banking](../tau3_bangking_golden/README.md)。自进化框架说明见
[Evolution README](../../evolution/README.md)。

## 目录结构

```text
tau3_banking/
├── config.yaml          # 优化目标、可修改范围和执行配置
├── datasets/            # train.json、validation.json、test.json
├── executor.py          # 执行 Candidate，生成对话与工具调用记录
├── evaluator.py         # 对已有执行记录评分
├── _internal/
│   ├── process.py       # 配置、进程间协议及子进程管理
│   ├── runner.py        # Tau2 执行、官方评分及 DB 差异反馈
│   ├── adapter.py       # Candidate HTTP/SSE 适配
│   └── tool_bridge.py   # Candidate 与 Tau2 的工具交接
└── tau2-bench/          # 本地安装的 Tau2 项目，不提交到 Git
```

每个 Case 使用独立的 Candidate 服务、Redis 前缀和临时应用副本。工具通过 Bridge
交给官方 Tau2 环境执行，用户与 Agent 操作同一份业务数据；Evaluator 对保存的
Simulation 评分，不重新执行 Candidate。

## 环境准备

以下命令均从仓库根目录执行，需要 Python 3.13+、uv 和 Docker。

安装本项目依赖，首次下载并安装 Tau2：

```bash
uv sync
git clone --branch v1.0.1 --single-branch --depth 1 \
  https://github.com/sierra-research/tau2-bench.git \
  evolution_tasks/tau3_banking/tau2-bench
uv sync --project evolution_tasks/tau3_banking/tau2-bench --extra knowledge
deploy/runtime/start.sh --profile redis
```

Tau2 的安装位置固定为当前任务目录下的 `tau2-bench/`，不再使用 `dependencies/` 层级，
执行前会检查版本和运行环境。
已有 checkout 时无需重复 clone。
下载固定使用 `v1.0.1` 标签和浅克隆，只获取该版本，不下载其他分支及完整历史。

若本地仍使用旧目录，可从仓库根目录迁移，然后重新同步虚拟环境：

```bash
mv evolution_tasks/tau3_banking/dependencies/tau2-bench evolution_tasks/tau3_banking/tau2-bench
uv sync --project evolution_tasks/tau3_banking/tau2-bench --extra knowledge
```

在仓库根目录 `.env` 中配置下列变量，或在启动前导出。CLI 加载 `.env` 时不覆盖已导出的值。

| 用途 | 环境变量 |
| --- | --- |
| Tau2 用户模拟器、自然语言断言裁判 | `TAU3_USER_LLM`、`TAU3_JUDGE_LLM` |
| 模拟器与裁判的连接 | `OPENAI_API_KEY`、`OPENAI_BASE_URL` |
| 被优化的 Banking Agent | `TEST_AGENT_LLM_MODEL`、`TEST_AGENT_LLM_API_KEY`、`TEST_AGENT_LLM_BASE_URL` |
| 优化器 Agent | `EVOLUTION_LLM_MODEL`、`EVOLUTION_LLM_API_KEY`、`EVOLUTION_LLM_BASE_URL` |
| Redis | `REDIS_HOST`、`REDIS_PORT`、`REDIS_USERNAME`、`REDIS_PASSWORD`、`REDIS_DATABASE` |

模型标识使用对应服务支持的 LiteLLM 格式，例如 `openai/<model-name>`。
其他变量按 [Runtime 配置](../../config/runtime.yaml) 的引用提供。
Banking 执行要求 `public_stream.enabled: true`，Message 和 State Manager 均使用 Redis。

## 运行

启动或继续普通 Banking 实验：

```bash
uv run evolution run evolution_tasks/tau3_banking
```

重复执行 `run` 会恢复当前任务已有实验，并按阶段复用已保存的结果。它不会重新导入
已建立 Seed 的 `initial_target`，也不会因修改数据集或实现而自动刷新已有评分。
需要独立实验时，可复制任务为 `tau3_banking_<name>`，不复制 `.evolution/`。

查看所有 Candidate 的训练、验证和测试评分：

```bash
uv run evolution status evolution_tasks/tau3_banking
```

确定 Candidate 后，手动执行测试集；将 `<candidate-id>` 替换为状态中显示的 ID：

```bash
uv run evolution test evolution_tasks/tau3_banking <candidate-id>
```

已有 Test Execution 时，可只重新评分：

```bash
uv run evolution test evolution_tasks/tau3_banking <candidate-id> --reevaluate
```

`status` 只读取保存的评分，不调用模型。`test` 不启动优化器，但执行 Case 和官方评分
仍需相应模型与运行环境。

## 配置

具体取值以 [config.yaml](config.yaml) 为准。通用配置含义见
[Evolution README](../../evolution/README.md)。

| 配置 | 作用 |
| --- | --- |
| `objective`、`initial_target` | 优化目标与 Seed 的来源应用 |
| `workspace.read_only`、`workspace.inaccessible` | 优化器只读和不可访问的路径 |
| `max_iterations` | 每次运行允许尝试的优化轮数 |
| `max_case_concurrency` | 同时执行的 Case 数量 |
| `max_retries`、`retry_delay_seconds` | Case 重试次数与间隔 |
| `rollback_enabled`、`exploration_patience`、`rollback_changelog_source` | 探索与回退策略 |
| `improver_timeout_seconds` | 单次优化请求的时间上限 |

Banking 专属配置位于 `settings`：

| 配置 | 作用 |
| --- | --- |
| `runtime_config` | Candidate 服务使用的 Runtime 配置，路径相对任务目录 |
| `user_llm`、`judge_llm` | 用户模拟器与裁判模型 |
| `agent_id`、`retrieval_config` | 应用中的 Agent 标识与 Tau2 检索模式 |
| `startup_timeout_seconds` | 服务启动与环境预检的等待上限 |
| `case_timeout_seconds` | 单个 Case 的执行预算 |
| `shutdown_timeout_seconds` | 子进程清理的等待上限 |
| `validation_num_trials` | 每个验证 Case 的独立采样次数 |
| `min_valid_ratio` | 批次触发定向补跑的有效结果比例阈值 |
| `max_reshoot_rounds` | 定向补跑轮数上限，设为 0 关闭 |

重试发生在 Case 调度过程中；定向补跑发生在整批执行之后。有效比例按
`result == "completed"` 计算，不代表答题正确率。比例不足时，仅补跑用户模拟器故障及
符合错误码条件的 `agent_error`；`result == "timeout"` 不进入定向补跑。
多 Trial 验证不因某一次失败而对整个采样组合重新执行。

## 数据集与评分

| 数据集 | Case 数 | 用途 |
| --- | ---: | --- |
| Train | 34 | 为优化器提供问题和改进反馈 |
| Validation | 23 | 比较候选、选择最佳版本 |
| Test | 40 | 候选确定后手动评估 |

三份数据均位于 [datasets/](datasets/)，各数据集的 Case ID 清单见
[数据集 README](datasets/README.md)。数据存在历史暴露，不能作为全新盲测；
部分题目的文档、参数或 DB 判定存在已知风险，异常结果应结合证据复核。

三个集合共用官方 Tau2 评分规则。多 Trial Case 的分数取各次 reward 的均值；整个
数据集的总分取 Case 分数的均值。多 Trial 的反馈和展示轨迹取最低分的一次，平分时
取最早的一次，反馈用 `representative_trial` 标识该次采样。

`feedback` 按需包含动作匹配（`action_checks`）和数据库差异（`db_checks`）。DB 差异
描述预期与实际终态的不同，不直接证明推理失败的原因；没有差异证据也不代表 DB 匹配。
完整执行记录与评分保存在结果文件中，优化器按需读取 Case 证据。

## 结果与排障

任务结果保存在 `evolution_tasks/tau3_banking/.evolution/`：

```text
.evolution/
├── manifest.json
└── candidates/<candidate-id>/
    ├── target/               # Candidate 应用快照
    ├── changelog.md
    ├── train/               # execution.json、evaluation.json
    ├── validation/          # execution.json、evaluation.json
    └── test/                # 手动运行后生成
```

运行日志位置由 Runtime 的 `logging` 配置决定。Case 临时应用和 Candidate 日志位于
仓库根目录 `.evolution/` 的 `.tau3-banking-case-*`、`.tau3-candidate-*` 目录；
失败信息会给出保留的诊断路径。Runner 失败时会保留系统临时目录中的 `tau3-runner-*`
请求与日志。正常完成后清理临时资源。

| 现象 | 检查项 |
| --- | --- |
| Tau2 预检失败 | 固定安装目录、版本、`.venv` 及 `knowledge` 依赖 |
| Redis 或 SSE 预检失败 | Redis 连接、Message/State Manager 类型及 `public_stream.enabled` |
| Candidate 启动失败 | 错误中的诊断目录、Agent ID、应用配置及必需环境变量 |
| 执行超时或模型错误 | Case 错误码、模型服务和时间预算；区分模拟器、Candidate 与 Runner |
| 修改后仍显示旧评分 | 是否恢复了已有实验；`test --reevaluate` 只重评已有 Test 执行记录 |

本地单元测试不需要真实模型：

```bash
uv run pytest tests/unit/evolution_tasks/tau3_banking
```
