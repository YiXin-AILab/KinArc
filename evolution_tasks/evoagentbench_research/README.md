# EvoAgentBench 信息检索公共任务

本目录统一维护场景执行器、评分适配器、`_internal/` 和管理脚本 `scripts/`。
Runtime/Evolution 只使用主项目的 `agentsystem/`、`evolution/`，没有第二份副本。
具体实验在本地同级目录 `evoagentbench_research_qwen_t118v36/`（整体不随 Git 分发），配置通过
`../evoagentbench_research/executor.py` 与 `../evoagentbench_research/evaluator.py` 引用本目录。
公共模板不保存实验配置、数据、清单、历史或运行状态；不要直接对本目录执行进化。

## 实验目录及数据

| 派生任务内的位置 | 内容 |
| --- | --- |
| `config.yaml`、`configs/` | 实际实验与裁判配置、第三方包版本；连接参数使用环境变量 |
| `datasets/` | train / validation / test 公开题目及 split_manifest；本实验题数 118 / 36 / 65 |
| `.evolution/candidates/` | 包含历史起点 seed 的最早 20 个候选、代码、父链、正式评分、changelog 和 rollback |
| `resources/` | 原题、附件、私有评分数据，或检索语料/索引/embedding 模型 |
| `dependencies/upstream/` | 固定 revision 的第三方 EvoAgentBench 基准/定制评分器，保留许可证；不含 Runtime/Evolution |
| `dependencies/images/`、`dependencies/wheels/upstream/` | 固定镜像与不能从普通包源获得的第三方安装材料 |
| `manifests/` | 资源清单、当前候选文件摘要与转换前后摘要；不记录来源机器完整路径 |
| `.local/` | 本实验的服务凭据、锁、安装产物、工作区、累计时限和执行回执；不公开提交 |

候选按 ID 时间顺序截取；20 个目录含 seed 与 rollback，不等于新增 20 次进化。
历史任务的 `config.yaml.initial_target` 直接指向 manifest 中 seed 候选的
`.evolution/candidates/CANDIDATE_ID/target`，不再保存重复的 `initial_target/`。
续跑恢复已有候选，不会随 application 的修改而改变历史起点。

新建实验时，应在新任务的 `config.yaml` 中设置
`initial_target: ../../application/evoagentbench_research_agent`，并配置该实验的
数据集及公共执行器/评分器入口。通用 Evolution 首次初始化会把 application
保存为 seed，之后从候选继续进化；不要清空历史任务的候选来重新开始。
下面的 `assemble` / `restore-check` / `run` 管理入口用于导入和续跑已有历史，
不是创建全新实验的入口；新实验使用主项目的 [Evolution CLI](../../evolution/README.md)。

活动问题库仅由保留候选的训练结果重建，不使用验证/测试反馈。
历史评测使用当前 `parent_comparison`，已经适配的文件不重复转换。
旧回退记录中的哈希保持原样，由迁移清单关联到已验证的当前文件摘要，
不保留原评测/原 changelog 副本。当前协议从候选结果恢复最佳状态；
本批没有需要迁移的旧迭代计数或 incumbent 结构化状态。

原始数据来自 BrowseComp-Plus（`Tevatron/browsecomp-plus`）。官方 train 154 题
以 seed 42 划分为训练 118、验证 36；官方 test 65 题保持原成员与顺序。
语料、FAISS/词法索引、embedding 模型和私有评分记录须补齐，答案不放进公开题目。
检索容器只读挂载当前主项目必要代码，不挂载任务根、历史或裁判数据。
`.local/container-python/` 是按当前 `uv.lock` 安装的产物，不是另一套源码。

实际 ID、顺序与摘要以 `datasets/split_manifest.json` 为准。原始 split 源于
EvoAgentBench revision `948a17288782d5120778da16b4cf1cad9305d8b4`；评分代码版本
另见 `manifests/resources.json`。这些数据存在历史暴露，不宣称全新盲测。
定制读取器/评分器不是全官方原版；公共协议与默认数据划分见
[DATASETS.md](../../benchmarks/evoagentbench/docs/DATASETS.md)。

## 检查与续跑

从项目根目录（包含 `pyproject.toml`）执行，要求 Python 3.13、uv；实际执行需
Linux amd64 Docker、可获取精确第三方包版本的包源以及模型服务。
先补齐清单中的资源和第三方源码/镜像；管理入口不会自动下载缺失资源或跳过校验。

```bash
uv sync --frozen
export PYTHONPATH="$PWD"
TEMPLATE=evolution_tasks/evoagentbench_research
TASK=evolution_tasks/evoagentbench_research_qwen_t118v36
uv run python "$TEMPLATE/scripts/manage.py" check "$TASK"
# 即使尚未安装第三方环境，也可只核验已有历史；禁止模型和补评
uv run python "$TEMPLATE/scripts/manage.py" restore-check "$TASK"
# 显式安装依赖、载入镜像，不是离线检查的一部分
uv run python "$TEMPLATE/scripts/manage.py" install "$TASK"
```

`check` 检查资源文件是否齐全，不要求外部资源的字节数和 SHA-256 与历史清单一致；
缺失文件会逐项列出并阻止后续执行。候选谱系及历史记录仍按迁移摘要核验。
实际运行时重新记录当前资源摘要，用于区分执行环境，不改写历史资源清单或已有评分。
`restore-check` 只验证当前代码可读取并恢复历史，不证明模型/容器真实续跑可用。
未传 `--additional-iterations` 或设为 0 的 `run` 同样只做离线恢复，
发现需要新执行/补评的未完成状态会报错，不能借零预算暗中调用模型。

配置执行模型 `EAB_MODEL`、`EAB_API_KEY`、`EAB_BASE_URL`；
裁判 `EAB_JUDGE_MODEL`、`EAB_JUDGE_API_KEY`、`EAB_JUDGE_BASE_URL`；
进化模型 `EVOLUTION_LLM_MODEL`、`EVOLUTION_LLM_API_KEY`、`EVOLUTION_LLM_BASE_URL`。
凭据只由调用者环境提供；不提交 `.env` 或 `.local`。

终端一保持服务运行（同样先验证资源和安装环境）：

```bash
uv run python "$TEMPLATE/scripts/services.py" "$TASK"
```

终端二配置相同环境后，按需显式执行：

```bash
uv run python "$TEMPLATE/scripts/manage.py" smoke "$TASK" --case-id CASE_ID
uv run python "$TEMPLATE/scripts/manage.py" run "$TASK" --additional-iterations 1
# 只有明确需要测试时才调用；结果写入选定候选 test/，不重测 train/validation
uv run evolution test "$TASK" CANDIDATE_ID
```

新增预算仅作用于本次调用，不写回 `config.yaml`；`no_change` 计预算，rollback 不计。
每题跨恢复累计 1200 秒，有效低分不重采样，完整执行优先原 workspace 补评。
`smoke` 在独立验收目录记录结果，正式续跑使用本实验锁及工作区。
服务端口和 Redis 凭据由每个实验独立分配/保存，不跨实验复用。
`DOCKER_HOST` 指定候选 daemon；Redis 可通过 `EAB_SERVICE_DOCKER_HOST` 单独指定。
重启时保留 `.local/`，不要删除它来重置累计时限；Ctrl-C 只关闭当前实验拥有的服务。

## 装配其他历史

`export_resources.py` 只导出数据、第三方代码和安装材料，不能导出主项目副本。
在同一个 Tasks 父目录下执行：

```bash
uv run python "$TEMPLATE/scripts/manage.py" assemble \
  --history SOURCE_TASK --resources RESOURCE_BUNDLE \
  --destination evolution_tasks/NEW_EXPERIMENT
```

输入必须含配置、datasets、manifest 和 candidates。装配只复制实验材料并保留最早
20 个候选；直接引用保留的 seed，不读取或生成重复起点目录。
入口使用相对路径引用本公共模板，不复制实现、不导入原运行状态、不创建
旧配置/原问题库/原 JSON/changelog 备份，也不修改输入源归档。

## 发布范围与验证边界

派生实验目录整体不纳入版本控制，包括配置、公开数据集、摘要清单、资源、
第三方源码/镜像、候选历史和 `.local/`。**仅 clone 不足以恢复本实验**：
需另行提供完整派生实验材料，并按其中的清单补齐资源。
保留的原始候选证据没有为发布而改写，可能含来源主机路径、请求或输出细节；
它们、未跟踪的本地文件以及 Git 历史不属于已清理的公开配置范围，发布前需单独审查。
本次不重写 Git 历史，也不将忽略规则视为数据脱敏或再分发授权。

当前代码恢复历史不保证与旧冻结实现逐字节/逐分数一致。代码摘要进入执行协议；
原成绩仍是历史观测。未进行真实模型续跑、外部服务安装或重新评测，不能宣称验证通过。
