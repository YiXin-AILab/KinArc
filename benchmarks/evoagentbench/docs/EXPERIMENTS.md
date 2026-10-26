# 四组自进化实验运行手册

实现位于 `ea-bench` 分支。以下命令已经接入 CLI；本文不包含真实模型成绩。
四组为 `base`（A）、`yx`（B）、`prompt`（C）、`evolved`（D）。
C 只移植 D 选中版本的 system prompt；D 带完整选中资产。
训练和选择复用 EvolutionEngine；模型配置、工具实现与 schema 固定。
当前可进化范围是 prompt、文件式 skills/memory、工具描述，不能修改权重或 Runtime 代码。

## 1. 已实现的模块

| 内容 | 代码模块 | 行为 |
| --- | --- | --- |
| 四组命令与恢复 | `experiments/experiment.py`、`experiments/experiment_state.py` | init/prepare/preflight/smoke/evolve/freeze/evaluate/report/status；同目录写操作互斥 |
| C/D 冻结 | `experiments/arms.py` | B 取 seed，D 取 validation winner，C 只换 prompt；记录缺失资产引用，不修补 |
| 官方基线 | `runners/official_baseline*.py`、`runners/official_tool_transport.py` | Nanobot 0.1.4.post3、独立 HOME、完整 usage、相同模型与 endpoint 校验 |
| 办公/信息检索 | `domains/domain_adapter.py`、`domains/domain_worker.py`、`domains/domain_environment.py` | 官方 load/setup/build_prompt/verify/cleanup 生命周期 |
| 数据与环境冻结 | `data/prepare_dataset.py`、`data/domain_prepare.py`、`domains/domain_dependencies.py` | pilot/formal 清单、依赖文件摘要、镜像 ID、配置摘要 |
| 应用工具包 | `application/evoagentbench_knowledge_agent/plugins/`、`application/evoagentbench_research_agent/plugins/` | 办公执行/文件/图片与信息检索 SSE 查询；按领域注入 |
| 逐 trial 证据 | `common/trace_store.py`、`common/process.py` | 原子归档、失败诊断；成功 trial 按完整身份恢复 |
| 四列表与差值 | `reporting/experiment_report.py` | pass@1..4/pass^1..4、配对任务 bootstrap、token/时长、逐题成绩与学习成本 |

工具包是可选应用装配，未默认加入所有 Runtime Agent。官方基线保留 Nanobot
的控制流程和原生工具 schema，但文件和命令 dispatch 进入公开任务容器，防止
读取宿主评分材料；记录为 `official-nanobot-with-isolated-tools-v1`。
它是带隔离适配的官方固定 harness，不等于未经修改的官方默认环境。
Nanobot 原生没有 GDPVal `read_image`；A/B 的能力差异必须随报告保留。

## 2. 首跑选择与依赖

办公与信息检索公共模板分别维护在 `evolution_tasks/evoagentbench_knowledge`、
`evolution_tasks/evoagentbench_research`。它们仅包含实现，历史续跑请使用各自 README
中的派生任务管理命令。本文是另行配置四组对照实验的低层协议，不用于导入这两份历史。
两个领域的数据、镜像、评分凭证和依赖清单见 [DOMAINS.md](DOMAINS.md)。
GDPVal 需要支持图片的执行模型才能使用图片工具，评分模型还需单独配置。

先按 [USAGE.md](USAGE.md#准备环境) 安装固定官方源码、独立 Python 3.12
环境及所选领域的数据、工具与评分依赖。官方 Python 环境还必须安装：

```bash
uv pip install --python /path/to/eab-python/bin/python nanobot-ai==0.1.4.post3
```

准备环境变量，值由实验负责人提供，不写入报告或版本库：

| 变量 | 含义 |
| --- | --- |
| `EAB_UPSTREAM_REPO` / `EAB_UPSTREAM_PYTHON` | 固定官方源码与其 Python 解释器 |
| `EAB_MODEL` / `EAB_BASE_MODEL` | 同一真实模型；YX 用 LiteLLM ID，base 用 Nanobot/custom 可解析 ID |
| `EAB_API_KEY` / `EAB_BASE_URL` | A/B/C/D 共用服务凭据与 endpoint |
| `EVOLUTION_LLM_MODEL` / `EVOLUTION_LLM_API_KEY` / `EVOLUTION_LLM_BASE_URL` | 进化器模型配置 |
| `EAB_DOMAIN_CONFIG` / `EAB_DEPENDENCY_MANIFEST` | 所选领域的私有官方配置副本与依赖清单 |
| `EAB_HF_CACHE_DIR` | GDPVal 已下载并冻结的 HF 缓存，评测强制离线 |

CLI 从当前工作目录 `.env` 读取，已有 shell 环境优先。初始化后编辑实验自己的
`runtime.yaml`，配置专用测试 Redis。不要修改共享生产配置。正式冻结包括解析后的
执行/进化模型配置、进化器资产、数据、源码和评分依赖；敏感配置仅保存摘要。

## 3. 从零跑一版

先另行准备带普通领域 settings/config.yaml 和初始目标的 `NEW_CONFIGURED_TASK_DIR`；
公共代码目录不能直接用作四组初始化模板。从仓库 `runtime/` 目录执行。所有示例中的 `knowledge-pilot-v1` 都指同一个实验目录。

```bash
uv run python -m benchmarks.evoagentbench.experiments.experiment init \
  evolution_tasks/eab_runs/knowledge-pilot-v1 \
  --task-template NEW_CONFIGURED_TASK_DIR \
  --baseline-config benchmarks/evoagentbench/configs/official_baseline.example.yaml \
  --preset pilot
```

现在检查新目录内的 `runtime.yaml`、`initial_target/config/agent.yaml`、
`baseline.yaml`、`task/config.yaml`。A/B 的温度、输出上限、上下文窗口必须一致。
初始化沿用模板中的 Runtime trace 配置，不强制启用或注入 OTLP 配置。
不再采集新增的实验阶段 spans、worker timeline 和进化器会话归档。

```bash
uv run python -m benchmarks.evoagentbench.experiments.experiment prepare evolution_tasks/eab_runs/knowledge-pilot-v1
uv run python -m benchmarks.evoagentbench.experiments.experiment preflight evolution_tasks/eab_runs/knowledge-pilot-v1
uv run python -m benchmarks.evoagentbench.experiments.experiment smoke evolution_tasks/eab_runs/knowledge-pilot-v1
```

prepare 只导出公开题目；preflight 检查真实 Redis、镜像、版本、缓存及配置，
不请求模型。smoke 开始真实模型调用，运行前两道训练题，每题 A/B 各一次，
结果写 `smoke/`。检查答案、评分、实际 token、容器清理。
preflight 成功本身不能证明模型服务可用，也不能替代 smoke。

```bash
uv run python -m benchmarks.evoagentbench.experiments.experiment evolve evolution_tasks/eab_runs/knowledge-pilot-v1
uv run python -m benchmarks.evoagentbench.experiments.experiment freeze evolution_tasks/eab_runs/knowledge-pilot-v1
uv run python -m benchmarks.evoagentbench.experiments.experiment evaluate evolution_tasks/eab_runs/knowledge-pilot-v1
uv run python -m benchmarks.evoagentbench.experiments.experiment report evolution_tasks/eab_runs/knowledge-pilot-v1
```

`evolve` 只用训练证据改进，validation 选 best；没有提升时 D 可以等于 B。
`freeze` 生成三份不可变目标及摘要，C 的依赖缺失写入 `arms/manifest.json`。
`evaluate` 不调用进化器；四组缺一组或缺试次，report 拒绝输出完整比较。
可用 `evaluate ... --arms yx,prompt,evolved` 分批执行；完整报告仍需 A。
首版按组执行，未实现任务分块随机交错；服务负载漂移是需要记录的解释限制。

pilot 为 20 train / 8 validation / 12 holdout，全部来自正式 train 的内部子集；
train/validation/holdout 每题每组各 1 次，每次 run 最多 2 轮改进。
正式另建 `--preset formal` 目录，每题每组各 1 次；
初始化保留模板的搜索策略，当前 EAB 模板为 20 轮、启用回退、探索深度 3、
回退日志来源 best。详见 [搜索预算与回退](USAGE.md#搜索预算与回退)。
这些默认值不追溯修改已有实验；重复 run 前应核对剩余预算。
两个领域均由官方 train 划出 36 validation、保留各自官方 test。
pilot holdout 只能用于工程验证，不能当作官方 test 成绩。

## 4. 中断与执行证据

```bash
uv run python -m benchmarks.evoagentbench.experiments.experiment status evolution_tasks/eab_runs/knowledge-pilot-v1
```

status 可在实验运行时执行，输出并保存 `trace-index.json`，包括所有 A/B/C/D
试次的身份、状态、session 和本地证据路径。原有 Runtime trace 由其配置控制，
不再生成实验专用的 Langfuse 会话链接。

| 产物 | 查看什么 |
| --- | --- |
| `evidence/trials/` | YX 与环境域 baseline 逐次运行、失败诊断与恢复证据 |
| `evidence/outputs/`、`evidence/media/` | GDPVal 交付物及实际查看的图片 |
| `task/.evolution/candidates/` | 每个候选资产、changelog、train/validation 执行与评分 |
| `results/{base,yx,prompt,evolved}/` | 冻结测试 execution、evaluation、完整性 receipt |
| `report.html` / `report.json` | 四列表、配对差值、逐任务分数与学习成本 |

本地归档负责实验身份、评分和失败证据。公开消息视图可能截断大工具输出，
不能当作 provider 完整原文。Redis 保留运行态，但不替代归档。

环境故障修复后重跑同一个命令；相同身份的成功 trial 会复用，失败尝试保留。
已完成组由 receipt 校验后跳过。更改模型、源码、数据、镜像或冻结资产会拒绝续跑，
需要新实验目录；不能复用旧成绩。基础设施失败使批次不完整，不静默按零分混入。
模型正常结束但答案错误则由官方评分计失败。不要删除失败记录以挑选高分。

## 5. 解释与验收边界

主要对比 D−B；C−B 是 prompt 单独迁移，D−C 含资产相互作用，A−B 含原生工具和
harness 差异。统计区间按任务抽样，不能代表不同进化随机种子的波动。
当前统一温度/输出上限/上下文与试次数，不承诺相同整题 token 消耗。
结果包含执行 tokens/总时长/P50/P95、完成情况和学习 tokens；未知货币成本为 null，
verifier/judge 的完整费用仍需供应商账单，不用零值掩盖缺失。

本地自动化验证使用确定性模型与受控服务，不构成真实 benchmark 成绩。
完整官方 GDPVal/信息检索语料端到端运行还依赖部署的数据、镜像、模型和评分服务。
外部 SSE 服务实际加载的索引无法由本地文件 hash 证明，清单明确标为 unverified；
正式运行应在服务节点核对其加载的冻结语料/index，并记录部署版本。

## 观测边界

原有 Runtime Agent/Turn/LLM/Tool tracing 保留，是否导出由 Runtime 配置控制。
实验阶段观测、跨进程 trace 关联、context snapshot、工具资产访问 spans、
worker timeline 和进化器会话归档已移除。进化器用量标为未采集（null），不计为零。
执行结果、模型用量、官方判定、失败诊断、恢复回执及协议哈希仍保留。
源码身份变化后必须使用新实验目录，不能以新代码续跑旧协议绑定的实验。

### Fixed Nanobot CLI configuration entry point

The pinned `nanobot-ai==0.1.4.post3` CLI does not accept EvoAgentBench's
`--workspace` / `--config` flags. `official_baseline_worker.prepare_cli_arguments`
validates the upstream task workspace and installs its generated configuration at
`$HOME/.nanobot/config.json` inside the existing per-trial temporary HOME (0600).
It then invokes the original CLI with message/session/format flags. Model,
provider, tool/MCP settings and workspace/session paths are preserved; neither
the Nanobot version nor its AgentLoop is changed. This is part of our baseline
launch adaptation, not an unmodified upstream invocation.

Regression checks cover configuration preservation, private file permissions,
prompt argument handling, and rejection of mismatched workspaces. Real baseline
smoke evidence is written separately from earlier failed attempts.

The usage recorder covers both LiteLLM and Nanobot's direct `custom` OpenAI
provider. The latter bypasses LiteLLM, so its per-instance SDK completion call is
recorded separately, preserving real request timing and returned token usage.
Missing usage still fails the experiment check; it is never replaced with zero.

### Recoverable official-base command timeouts

`runners/command_supervisor.py` now runs inside the public task container. The default
command deadline stays at 60 seconds. A Linux child subreaper supervises the shell
and adopts orphaned/detached descendants. On command completion or timeout it
kills and reaps remaining descendants before returning; timeout is a tool result
that the Agent can react to, with the workspace preserved. Command stdin is closed
unless supplied by the command itself (e.g. heredoc/redirection), preventing a
script reading stdin from hanging on the Docker connection.

The host gives the supervisor an additional 10 seconds for cleanup/transport.
Supervisor failure or an outer deadline still invalidates the entire container.
This is a change to the isolated baseline tool transport, not to Nanobot's loop,
model, official grader, or the task timeout. Previous failed smoke records remain
separate; new results must not be mixed into their runs.

### Official-base local evidence

The baseline retains its redacted local task/response, model calls, tool calls
and token usage records for result auditing. These records are no longer exported
as experiment-specific spans. Existing historical records remain unchanged.
