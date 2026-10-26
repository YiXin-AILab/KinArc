# Evolution 自进化系统

Evolution 是面向智能体（Agent）的自进化闭环系统。它将代码、提示词与技能资产等任意文件
纳入可迭代版本管理，基于评测事实完成故障归因与代码修复，通过跨轮复盘持续沉淀改进依据；
在候选择优与自动回退机制保障下，持续收敛至业务目标。

## 系统架构

```text
┌──────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│       进化对象        │    │       进化配置        │     │        数据集         │
│   待优化的代码与资产    │    │   优化目标与运行策略    │    │  改进、验证与验收样例   │
└──────────────────────┘    └──────────────────────┘    └──────────────────────┘
            │                           │                           │
            ▼                           ▼                           ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                                    编排器                                     │
│                          调度迭代、回退与选优、断点恢复                           │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────┐                ┌──────────────────────────────┐
│       候选与工作区管理器        │──候选代码──────►│            执行器             │
│      管理工作区、版本与产物      │                │      运行候选，记录执行结果     │
└──────────────────────────────┘                └──────────────────────────────┘
                ▲                                               │
                │ 代码修改与日志                                  │ 执行事实
                │                                               ▼
┌──────────────────────────────┐                ┌──────────────────────────────┐
│            进化器             │                │            评分器             │
│      根据证据归因并实施修改      │                │      评判效果，输出评分反馈     │
└──────────────────────────────┘                └──────────────────────────────┘
                ▲                                               │
                │ 失败簇与问题库                                  │ Train 证据
                │                                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                                    聚类器                                     │
│                            聚合失败，积累跨轮问题                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 组件职责

| 组件 | 职责 |
| --- | --- |
| **进化对象** | 被优化的代码与资产，文件目录形式，例如 Agent 的 Prompt、Skill、Plugin 和业务代码。 |
| **进化配置** | 定义优化目标、允许修改的范围，以及轮次、重试和回退等运行策略。 |
| **数据集** | 提供业务样例与评测依据，Train 用于改进，Validation 用于选优，Test 用于独立验收。 |
| **编排器** | 串联执行、评分、诊断和改进，控制迭代与回退，选择并返回最佳候选。 |
| **候选与工作区管理器** | 创建可修改的工作区，校验并保存候选版本，管理版本关系、评测结果和日志。 |
| **执行器** | 在指定数据集上运行候选代码，记录执行结果、轨迹和资源消耗。 |
| **评分器** | 根据业务评测标准判断执行效果，输出逐题分数、总分和失败反馈。 |
| **聚类器** | 按失败签名聚合 Case，整理退步与通过 Case，并维护跨轮问题库。 |
| **进化器** | 结合 Train 评分反馈、执行轨迹和当前代码进行失败归因，形成修改假设并实施修改，记录修改依据与预期效果。 |

## 开始使用

先按[项目 README](../README.md)准备 Python、uv、Runtime 配置和所需服务，
再准备任务所依赖的数据、模型凭证及外部环境。命令在仓库根目录执行：

```bash
# 启动实验；已有实验会继续执行尚未完成的阶段
uv run evolution run evolution_tasks/task_name --runtime-config config/runtime.yaml

# 查看候选版本及 Train、Validation、Test 分数
uv run evolution status evolution_tasks/task_name

# 手动执行指定版本的 Test
uv run evolution test evolution_tasks/task_name <candidate-id>

# 复用已有 Test 执行结果，只重新评分
uv run evolution test evolution_tasks/task_name <candidate-id> --reevaluate
```

`task_name` 表示你准备的任务目录名称。仓库中的具体任务见
[evolution_tasks](../evolution_tasks)；Tau3 Banking 的环境准备见
[任务说明](../evolution_tasks/tau3_banking/README.md)。

`run` 在当前进程内启动优化器 Runtime；`test` 不加载优化器，
`status` 只读已保存的评分，不调用模型或补跑评测。
同一个实验目录只支持一个写入者，不要同时运行会修改该实验的命令。

## 任务组成

一个完整任务包含配置、执行器、评分器和三份数据集：

```text
evolution_tasks/task_name/
├── config.yaml
├── executor.py
├── evaluator.py
└── datasets/
    ├── train.json
    ├── validation.json
    └── test.json
```

三份数据集分别用于改进、选优和独立评测：

| 数据集 | 用途 |
| --- | --- |
| Train | 提供评分、执行轨迹和失败诊断，驱动优化器修改代码 |
| Validation | 验证候选效果，选择最终最佳版本 |
| Test | 用户手动评测指定版本，不参与优化输入或选优 |

最小配置示例：

```yaml
objective: 提高目标 Agent 在任务上的得分
initial_target: ../../application/example_agent
workspace:
  read_only: [data/, tests/]
  inaccessible: ["**/.*", "**/__pycache__"]
max_iterations: 5
max_case_concurrency: 1
settings: {}
```

`initial_target` 是初始代码目录；`settings` 传给任务的执行器和评分器。
数据集默认使用上面的三个 JSON 路径，也可以配置其他文件格式，由任务自行解释。
`datasets` 可用相对路径引用同级任务的数据文件；解析后的文件必须位于 Tasks 根目录内，
不接受绝对路径或越界符号链接。例如：

```yaml
datasets:
  train: ../tau3_banking/datasets/train.json
  validation: ../tau3_banking/datasets/validation.json
  test: ../tau3_banking/datasets/test.json
```

配置字段、共享任务入口和执行策略的详细说明见
[任务配置与运行](../README.md#本地-agent-自进化)。

`max_iterations` 限制每次 `run` 的优化尝试轮数。没有产生代码变化的尝试也消耗轮次，
恢复已有评测阶段和发布回退候选不消耗轮次。`max_case_concurrency` 控制单批 Case
执行并发度，默认串行；评分和优化轮次按顺序进行。

## 优化流程

1. **初始化或恢复。** 首次运行从初始代码创建 Seed；继续运行时补齐已有候选必要的
   执行和评分阶段，复用完整结果。
2. **确定修改起点。** 通常从最新候选继续探索。最新候选与 Validation 最佳候选可以
   不同；达到回退条件时，先恢复最佳版本，再继续修改。
3. **分析并修改。** 为修改起点创建独立 Workspace，将目标、权限范围、Train 评分、
   轨迹和诊断交给优化器。跨轮问题库保留尚待处理的问题。
4. **发布候选。** 优化器及其后台执行停止后，校验受限文件未变化。只有目标代码存在
   净变化才发布候选；代码和 `changelog.md` 一起原子发布。
5. **评测与选优。** 先执行 Train；达到或超过当前 Validation 最佳版本的 Train 分数，
   才执行 Validation。Validation 严格提高才更新最佳版本，同分保留较早版本。
6. **继续迭代。** 回填评测日志后进入下一轮，直到轮次预算耗尽或遇到停止条件。

自动回退默认开启：连续 3 个正式探索候选未刷新 Validation 最佳，且还有下一轮预算时，
恢复最佳版本。如果最佳版本不在当前代码祖先链上，也会触发恢复。可通过
`rollback_enabled` 和 `exploration_patience` 调整策略。

回退候选复制最佳版本的代码及 Train/Validation 证据，不重新评测、不复制 Test。
`rollback_changelog_source` 可选择继承最佳版本（`best`，默认）或最新版本（`latest`）
的日志。回退限制偏离最佳版本的探索长度，不保证后续分数提高。

## 产物与恢复

实验产物保存在任务目录的 `.evolution/` 下：

```text
.evolution/
├── manifest.json
├── problem_library.json
└── candidates/<candidate-id>/
    ├── target/
    ├── changelog.md
    ├── parent
    ├── target.sha256
    ├── train/{execution,evaluation}.json
    ├── validation/{execution,evaluation}.json
    ├── test/{execution,evaluation}.json
    └── rollback.json
```

Validation 产物在执行该阶段后生成，Test 产物仅在手动评测后生成，
`rollback.json` 仅属于回退候选。`parent` 记录直接父版本；
`changelog.md` 记录修改分析及平台回填的评测结论。

普通候选按完整阶段恢复：执行和评分结果均缺失时执行后评分；仅缺评分时复用执行结果，
只补评分；两者完整时直接复用。评分存在但执行结果缺失，或产物损坏、不一致时，
停止运行。回退候选要求复制的证据完整，不通过重新执行修复。
恢复不包含未保存的 Case 进度、优化器会话或未发布 Workspace。

临时 Workspace 位于项目根目录的 `.evolution/` 下。优化器的文件工具受
`workspace.read_only` 和 `workspace.inaccessible` 限制，发布时再次校验受限内容。
这些限制不构成操作系统沙箱；任务入口仍是本地受信 Python 代码。

## 扩展接口

核心流程通过三个独立协议连接具体实现：

| 接口 | 职责 |
| --- | --- |
| [Executor](executor.py) | 在指定数据集上运行目标代码，返回完整、有序的执行结果 |
| [Evaluator](evaluator.py) | 根据执行结果和数据集评分，返回对应 Case 的分数与反馈 |
| [Improver](improver.py) | 根据改进请求修改 Workspace 和日志，结束前停止本次后台执行 |

任务入口分别提供 `create_executor(task_dir, settings)` 和
`create_evaluator(task_dir, settings)`。执行器负责遵守并发、重试和取消约定，
返回前回收全部执行资源；可以使用 [execution.py](execution.py) 中的共享 Case 调度器。

[AgentSystemImprover](agentsystem_improver.py) 是 Improver 的 Runtime 实现，
将改进请求交给内置优化器 Agent，并负责会话终态和后台执行的停止确认。
业务任务无需自行实现优化器。

## 代码组织

```text
evolution/
├── cli.py                   # 命令入口与依赖装配
├── task.py                  # 任务配置与加载
├── engine.py                # 优化循环、阶段恢复、选优与回退
├── candidates.py            # 候选快照、发布、产物校验及日志
├── execution.py             # Case 调度与结果比较
├── models.py                # 执行、评分与优化结果模型
├── diagnosis.py             # 失败诊断、问题聚类和跨轮问题库
├── workspace.py             # 工作区路径可见性与写权限规则
├── executor.py              # 执行协议
├── evaluator.py             # 评分协议
├── improver.py              # 改进协议
├── agentsystem_improver.py   # 优化器与 KinArc Runtime 的连接
└── agent/                   # 优化器 Agent 资产
    ├── config/agent.yaml    # Agent 配置
    ├── prompt/system.md     # 系统提示词
    ├── skills/              # 内置 Skill 与参考资料
    └── plugins/             # 文件、证据工具与插件装配
```

阅读主流程可从 `EvolutionEngine.run()` 开始，再查看候选存储和三个扩展接口。
`agent/` 专门放优化器资产，任务实现放在 [evolution_tasks/](../evolution_tasks/)。

## 开发验证

从仓库根目录运行：

```bash
uv run pytest tests/unit/evolution tests/e2e/test_evolution.py
uv run pytest tests/integration/evolution
uv run ruff check evolution tests/unit/evolution
uv run ruff format --check evolution tests/unit/evolution
uv run mypy
git diff --check
```

集成测试使用项目测试 Redis，配置见 [tests/redis_config.py](../tests/redis_config.py)。
确定性测试验证编排行为；真实模型的优化效果需要通过实际任务实验评估。
