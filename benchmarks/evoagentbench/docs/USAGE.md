# EvoAgentBench：办公、信息检索与四组自进化实验

本实现复用现有 EvolutionEngine、
CandidateRepository、AgentsystemImprover 和 Runtime，不另建进化循环。

四组实验入口、运行步骤、trace 与恢复见 **[EXPERIMENTS.md](EXPERIMENTS.md)**。
当前支持 **knowledge_work（办公 / GDPVal）** 和
**information_retrieval（信息检索）**，配置与依赖见 **[DOMAINS.md](DOMAINS.md)**。
完整官方 train 的初始模型对比见 **[VANILLA.md](VANILLA.md)**。
支持这些入口不代表真实模型实验已完成。允许进化 `prompt/`、`skills/`、`memory/` 和
`tools.json` 中的工具描述；执行模型配置固定。
原生 Skill 通过候选根目录的 `skills.yaml` 启用，复用 Runtime 的 Skill catalog
和 `skill_view`；普通 UTF-8 经验文件仍可通过 `eab_read` 读取。
任意 Python 插件、外部 memory 服务、harness 代码和权重进化未接入本协议。
因此结果表示这一受控工具配置下的自进化收益，不能声称覆盖所有 harness 能力。

办公任务通过固定容器工具操作交付物；信息检索任务使用官方 SSE MCP 搜索工具。
这不允许进化工具实现、运行任意宿主插件或改变权限。

## 原生 Skill 自进化

进化 Agent 可以新增 `skills/<name>/SKILL.md`（含 `name`、`description` frontmatter）
及 `references/` 等资源，并修改候选根目录的 `skills.yaml`：

```yaml
skill_roots: [skills]
enabled_skills: [office-delivery-check]
```

`skills.yaml` 仅接受上述两项，覆盖固定 Agent 配置中的同名字段；不存在时沿用原配置。
启用后 worker 自动选择内置 `skill_view`，Runtime 原生目录展示、触发描述、分页读取
均生效。无需把 Skill 目录或读取接口复制到系统提示词，也不允许修改冻结的模型配置。
所有根目录必须位于候选的 `skills/` 下；读取仅允许已启用 Skill 的已校验资源，
禁止绝对路径、越界和软链接。脚本资源只可读取，不会自动在宿主执行。

Skill 清单和文件随候选一起发布、回退、冻结；C（只移植 prompt）保留 seed 的
Skill 清单和文件，D（完整进化）使用最佳候选的清单与文件。`config/` 的冻结约束
保持不变。未启用 Skill 的旧候选不增加工具；启用时新增的 `skill_view` 是学习资产
访问能力，业务工具集合保持固定，报告应披露这一差异。

trace 的 `enabled_skills` 记录实际启用项，`effective_agent_config_sha256` 记录应用
Skill 清单后的配置；`agent_config_sha256` 仍标识冻结配置用于同模型对照，候选哈希
覆盖清单与文件。实际读取见原生 `skill_view` 工具调用。

旧实验目录的 seed 已冻结，不会自动获得新的清单；请用当前代码新建实验，勿覆盖旧
receipt 或把新增能力后的采样混进正在进行的旧实验。

## 可选容器执行工具

办公工具位于 `application/evoagentbench_knowledge_agent/plugins/`，信息检索 SSE
工具位于 `application/evoagentbench_research_agent/plugins/`，由领域 worker 注入；没有改写 Runtime
核心，也没有默认向所有 Agent 开放。办公使用 `docker/Dockerfile.office` 提供文档、
表格、演示文稿和图片处理依赖：

```bash
docker build -t ea-bench-office:local \
  -f benchmarks/evoagentbench/docker/Dockerfile.office .
```

`docker/Dockerfile.execution` 保留为公共执行工具的测试镜像。办公工具包括
`run_command/read_file/write_file/list_files/replace_text/read_image`；命令和
文件操作在任务容器内执行并检查路径，命令有有限超时和输出上限。
隐藏评分资料和服务凭据不注入模型环境，任务结束或超时后清理所属容器。
信息检索只装配受限的 `search(query)`，不提供 shell。

B/C/D 必须使用相同的领域配置、镜像和固定工具 schema。
Langfuse 复用既有工具 spans，需在实际 Runtime 配置中开启 trace。
领域所需数据、镜像、评分服务和依赖冻结见 [DOMAINS.md](DOMAINS.md)。

## 文件与职责

完整目录导航及迁移说明见 [入口文档](../README.md)，数据来源和划分见 [DATASETS.md](DATASETS.md)。所有 shell 示例从仓库根目录（`runtime/`）执行。

- `evolution_tasks/evoagentbench_knowledge/`、`evolution_tasks/evoagentbench_research/`：公共 Executor/Evaluator 工厂及管理脚本，具体实验配置和数据在同级派生目录。
- `application/evoagentbench_knowledge_agent/`、`application/evoagentbench_research_agent/`：可进化的初始 Agent 资产。
- `domains/domain_adapter.py`、`domains/domain_worker.py`：完整案例执行与官方环境/评分生命周期。
- `runners/runtime_worker.py`：每 trial 独立 Runtime、Redis namespace、会话、scratch 和领域工具装配。
- `domains/domain_environment.py`：连接官方办公任务容器并监督工具超时。
- `runners/tools.py`：只读冻结技能/记忆；写入仅限当前 trial scratch。
- `data/prepare_dataset.py`、`data/domain_prepare.py`：导出题目 ID 和公开 prompt，不导出隐藏答案。
- `reporting/report.py`、`reporting/experiment_report.py`：配对收益、pass@k、pass^k、成本、任务 bootstrap 区间。

## 准备环境

Runtime 使用本仓库锁定的 Python 3.13+ 环境；官方评测独立使用 Python 3.12。
从仓库根目录运行：

```bash
uv sync --frozen
git clone https://github.com/EverMind-AI/EvoAgentBench.git /path/to/EvoAgentBench
git -C /path/to/EvoAgentBench checkout 948a17288782d5120778da16b4cf1cad9305d8b4
uv venv --python 3.12 /path/to/eab-python
uv pip install --python /path/to/eab-python/bin/python nanobot-ai==0.1.4.post3
```

替换上述示例绝对路径；不要把私钥写进仓库。环境变量：

```bash
export EAB_UPSTREAM_REPO=/path/to/EvoAgentBench
export EAB_UPSTREAM_PYTHON=/path/to/eab-python/bin/python
export EAB_DOMAIN_CONFIG=/path/to/domain.yaml
export EAB_DEPENDENCY_MANIFEST=/path/to/deps.json
```

按 [DOMAINS.md](DOMAINS.md) 安装所选领域的官方依赖并配置数据与服务。
办公另需 `EAB_HF_CACHE_DIR` 指向已冻结的离线 HF 缓存。
另需配置被测模型 `EAB_MODEL`、`EAB_API_KEY`、`EAB_BASE_URL`，
以及已有进化器使用的 `EVOLUTION_LLM_MODEL`、`EVOLUTION_LLM_API_KEY`、
`EVOLUTION_LLM_BASE_URL`。这些模型版本和参数须在正式实验前固定。
准备数据的命令从 shell 环境读取配置；Evolution CLI 也支持仓库根目录的 `.env`。

编辑 `benchmarks/evoagentbench/configs/runtime.example.yaml` 的测试 Redis 连接。
Message/State 必须选择 Redis，当前不支持 SQL 隔离。为该实验使用专用 Redis；
代码给每 trial 分配 UUID namespace，不执行 FLUSHDB/FLUSHALL。
当前会保留 Redis 运行证据，长实验后由运维按实验 namespace 归档清理。

正式运行前冻结依赖清单与 Docker image ID；不要在同一实验中重建同名镜像。
办公容器禁用网络，官方评分器在私有上游进程中执行。办公数据、参考文件和 rubric
必须提前部署；信息检索需自行运行加载冻结语料与索引的 SSE MCP 服务。
执行/评分阶段不安装包。

## 已有实验续跑与数据准备

公共模板不再包含实验配置或数据。已有两份实验的资源补齐、检查、安装与显式续跑命令见
[办公任务](../../../evolution_tasks/evoagentbench_knowledge/README.md) 和
[检索任务](../../../evolution_tasks/evoagentbench_research/README.md)。
不要对公共模板执行 `evolution run` 或重新生成数据覆盖派生历史。

低层 `prepare_dataset` 仅用于另行建立了普通领域 settings/config.yaml 的全新实验目录，
不能直接用于 `settings.portable` 的历史装配目录：

```bash
uv run python -m benchmarks.evoagentbench.data.prepare_dataset NEW_TASK_DIR --validation-count 36 --seed 42
```

默认从官方 train 派生训练/验证，保持官方 test；目标数据已存在时拒绝覆盖。
这与办公历史 46/24 的筛选补充协议不同，不能混用或据此替换已有历史数据。

## 进化与冻结测试

派生任务默认新增预算 0，正式续跑通过公共 `manage.py run TASK_DIR --additional-iterations N`
显式指定。以下为通用引擎行为说明。

沿用本地现有引擎：每次改进只读取训练证据，train 不低于当前 validation 最佳版本的
train 分数才进入 validation，最终按 validation 选择 best。新增预算由本次调用显式指定；候选目录数不是预算。新建 pilot/formal 实验的 train、validation、test
均为每题每组 1 次，`max_retries` 必须为 0。已有实验保留其冻结配置，
不得在中途混用采样次数。单次采样只报告 pass@1 / pass^1，不能据此计算
pass@2–4 或 pass^2–4；进化轮数和任务并发与每题采样次数是不同配置。
记录 CLI 返回的 `seed_candidate_id` 和 `best_candidate_id`。

### 搜索预算与回退

派生实验使用以下通用引擎参数，无需独立的 EAB 回退实现：

```yaml
max_iterations: 0
rollback_enabled: true
exploration_patience: 3
rollback_changelog_source: best
```

`max_iterations` 是**本次 run 调用**最多的改进轮数，不包含 seed；无改动的提案也占一轮，
回退节点另计在候选记录中。因此它不是“恰好生成 20 个有效候选”，也不是每题运行 20 次。
重复调用 run 会增加预算，续跑监督器必须计算剩余轮数，不能每次重新给 20。

`rollback_enabled` 允许从探索分支回到 validation 最佳候选；并非每次退步立即回退。
`exploration_patience: 3` 按真实 parent 链统计相对最佳目标的探索深度，达到 3 后在下一次
改进前回退；最佳锚点不在该分支祖先链中也会触发回退。它不是连续三轮无提升就停止实验。
恢复节点复用最佳目标及已有评测证据，不重新采样。`rollback_changelog_source: best`
从最佳候选继承 changelog 并追加回退记录；`latest` 则保留最新探索分支的 changelog。
旧分支和失败证据仍保留，选择 `best` 不会删除历史。

办公 objective 面向**进化器**，不是直接注入执行 Agent 的 system prompt。新版强调基于
训练证据做局部改进、尽早形成交付物、限定检查与修复、真实来源和明确 Skill 触发条件。
它不会给容器开网、修复模型服务空输出或保证提高分数；效果仍需固定条件下的训练/验证
及最终冻结测试确认，不能根据已查看的 test 继续调参。

模板变更只影响后续初始化；已有实验复制出的任务配置、结果和预算不会自动更新。
部署时核对引擎源码版本：历史远端版本曾采用不同训练门槛，不能把换引擎视作只改 YAML。
新增 20 轮会增加调用量和验证集选优风险；保留独立最终 test，不借增加候选数量声称泛化提升。

先固定 best，再分别执行以下命令，将 CANDIDATE_ID 替换为 seed 与 best 的实际 ID：

```bash
uv run evolution test evolution_tasks/evoagentbench_knowledge_qwen_t46v24 CANDIDATE_ID
```

`test` 不加载或调用 Improver，也不参与优化或选优。该命令不接受
`--runtime-config`；被测 Agent 的 Runtime 配置由任务 `config.yaml` 中的
`settings.runtime_config` 指定，相对路径以任务目录为基准。

只有执行和评分均成功时，才替换该候选已有的 Test 执行与评分结果。
EAB Executor 会复用身份匹配的成功 trial 归档，重复执行 `test` 不保证重新采样。
如果只需对已有执行记录重新评分，使用：

```bash
uv run evolution test evolution_tasks/evoagentbench_knowledge_qwen_t46v24 CANDIDATE_ID --reevaluate
```

`--reevaluate` 要求已有 Test `execution.json`，不调用 Executor，只更新评分结果。
不要查看 test 后继续调参选版本。需要多次独立实验时新建独立实验目录，
保留每次完整产物，不挑选更高分的一次。

```bash
uv run python -m benchmarks.evoagentbench.reporting.report \
  evolution_tasks/evoagentbench_knowledge_qwen_t46v24/.evolution/candidates/SEED_ID/test/evaluation.json \
  evolution_tasks/evoagentbench_knowledge_qwen_t46v24/.evolution/candidates/BEST_ID/test/evaluation.json
```

报告给出任务平均 pass@1、pass@1..4、pass^1..4、收益/退化案例数、
配对 task bootstrap 95% 区间，以及执行 token、耗时。
这些成本仅包含被测 Agent 的评测执行，不包含进化模型、训练或 verifier 成本。
95% 区间描述固定学习产物下的任务采样不确定性，不代表多次进化随机种子的方差。

执行轨迹记录 target 摘要、协议摘要、模型配置摘要。
协议摘要包括数据及依赖清单、固定官方源码、预算和 Runtime 配置。
模型响应失败会按失败 trial 记分；子进程超时、环境异常会使该批无法形成完整成绩，
不会丢弃异常任务后给出一个虚高的“完整成绩”。四组入口配置逐 trial 归档后，
修复环境并重跑同一命令会复用匹配的成功试次，保留之前失败记录。
公开 Runtime 消息视图可能截断大型工具内容，不等价于 provider 原始全量日志。

信息检索试次成功结束但没有公开正文时，最多执行一次独立的答案收尾请求：只使用公开
题目和本试次已有 search 证据，不注入 Skill、工具或私有思考。Qwen 收尾关闭 thinking，
要求 explanation、exact_answer、confidence 的 JSON，再转换为官方答案格式。收尾与
初始执行共用完整试次 deadline，token 和耗时合并；完整请求、响应、校验结果及证据截断
数量保存在 `tracelog.finalization`。无效或仍为空的答案保留零分，供应商失败保留执行异常，
不剔题、不重复采样。该协议替代旧的同会话追加收尾指令；应用于独立的新实验，不能混入
已经冻结并开始评分的旧运行。非空答案仍须由官方 verifier 判断正确性。

Runtime 执行已完成但 usage 缺失或不完整时，仍导出产物并继续评分。
`tracelog.token_accounting` 保留统计质量；报告中的精确 token 成本为 `null`，
已返回的计数通过 `reported_tokens` 单列，不能将占位零值解释为零成本。

## 验证与限制

单元测试涵盖重复采样、官方评分调用次数、数据覆盖、统计公式、
配对身份、路径/符号链接隔离、scratch 独立及子进程回收。
真实 Redis 集成测试使用确定性的 LiteLLM provider，不调用付费模型：

```bash
uv run pytest tests/unit/benchmarks/test_evoagentbench_domains.py tests/unit/benchmarks/test_four_arm_experiment.py tests/unit/benchmarks/test_eab_vanilla.py
TEST_REDIS_URL=redis://127.0.0.1:6379/15 uv run pytest tests/integration/test_evoagentbench_runtime.py
```

正式上线前还需在目标节点完成官方数据与领域环境准备、
官方评分链路 smoke，以及真实模型少量训练题试跑。
不要把 fixture/模拟模型通过当成真实 benchmark 成绩。

官方来源：[EvoAgentBench](https://github.com/EverMind-AI/EvoAgentBench/tree/948a17288782d5120778da16b4cf1cad9305d8b4/benchmark)、
[办公领域](https://github.com/EverMind-AI/EvoAgentBench/tree/948a17288782d5120778da16b4cf1cad9305d8b4/benchmark/src/domains/knowledge_work)、
[信息检索领域](https://github.com/EverMind-AI/EvoAgentBench/tree/948a17288782d5120778da16b4cf1cad9305d8b4/benchmark/src/domains/information_retrieval)。
