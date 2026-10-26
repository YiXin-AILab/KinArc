# 数据来源与训练、验证、测试划分

本文说明当前代码的默认协议，以及此前办公和信息检索实验用过的不同数据版本。
数据量按 EvoAgentBench 固定源码版本
`948a17288782d5120778da16b4cf1cad9305d8b4` 核对；变更上游版本后应重新核对，不能沿用表中数字。

## 1. 仓库里有哪些数据

Git 中保存公共实现和数据来源说明；两份派生实验目录整体保留在本地，不随 Git 分发，
包括公开题目清单、划分摘要、配置与运行记录。公共模板只维护代码与主 README。
下列路径是补齐实验材料后的本地布局，不是 clone 后自带的数据：

```text
evolution_tasks/evoagentbench_knowledge_qwen_t46v24/datasets/ # 46 / 24 / 60
evolution_tasks/evoagentbench_research_qwen_t118v36/datasets/ # 118 / 36 / 65
<派生任务>/.evolution/                                      # 本地候选与成绩，不随 Git 分发
```

具体续跑命令、资源要求与发布边界见公共任务的
[办公 README](../../../evolution_tasks/evoagentbench_knowledge/README.md) 和
[检索 README](../../../evolution_tasks/evoagentbench_research/README.md)。

生成的 `train.json`、`validation.json`、`test.json` 包含 role、上游 revision、来源 SHA-256
和 `cases`；每个 case 只有 `id` 与公开 `prompt`。实际附件和工作目录由官方 adapter 在每次
trial 中准备，导出清单使用占位路径。单元测试中的题目数量或 ID 是 fixture，不能当作数据来源。

## 2. 实际使用的数据

| 场景 | 原始来源 | EvoAgentBench 固定版本选用范围 | 还需要的资源 |
| --- | --- | --- | --- |
| 办公 `knowledge_work` | [OpenAI GDPVal](https://huggingface.co/datasets/openai/gdpval) | 官方 train 105 + test 60，共 165 题；ID 为完整 UUID | GDPVal 题目行、每题原始参考附件、职业评分 meta-prompts；文档转换依赖 |
| 信息检索 `information_retrieval` | [BrowseComp-Plus](https://github.com/texttron/BrowseComp-Plus)，数据仓库 `Tevatron/browsecomp-plus` | 官方 train 154 + test 65，共 219 题 | 解密题目 JSONL、本地语料/FAISS 索引、embedding 模型和 SSE MCP 搜索服务 |

这些数字是 **EvoAgentBench split 的选用规模**，不是 GDPVal 或 BrowseComp-Plus 全量数据集规模。
检索 split 还有 `clusters` 元数据；当前默认准备脚本仅使用顶层 `train`、`test`，
不会把 cluster 中重复出现的 ID 再当成新题，也不做按 cluster 分层抽样。

权威划分文件：

- [knowledge_work.json](https://github.com/EverMind-AI/EvoAgentBench/blob/948a17288782d5120778da16b4cf1cad9305d8b4/benchmark/data/splits/knowledge_work.json)
  — 原始文件 SHA-256：`343e6c5ad12baf102e30e44fa2e1a2e58ac6a93533023d8bf4f7bc7aebe8524a`。
- [information_retrieval.json](https://github.com/EverMind-AI/EvoAgentBench/blob/948a17288782d5120778da16b4cf1cad9305d8b4/benchmark/data/splits/information_retrieval.json)
  — 原始文件 SHA-256：`71e9d2d5bb3ae5d8c49823698e5c32cca60dade4a88e7f8fcf80d8198dd9c794`。

以上摘要针对原文件字节，不是重新序列化后的 JSON。源码 revision 不等于外部数据 revision：
GDPVal HF 缓存、附件、检索语料/索引仍须独立固定与校验。

## 3. 当前代码怎样划分

实现：[prepare_dataset.py](../data/prepare_dataset.py)。默认 `preset=formal`、
`validation_count=36`、`seed=42`。

| 协议 | 场景 | train | validation | test | test 来源 |
| --- | --- | ---: | ---: | ---: | --- |
| 官方原始 split | 办公 | 105 | 无 | 60 | 官方 test |
| 本地 formal 默认 | 办公 | 69 | 36 | 60 | 官方 test，保持不变 |
| 官方原始 split | 检索 | 154 | 无 | 65 | 官方 test |
| 本地 formal 默认 | 检索 | 118 | 36 | 65 | 官方 test，保持不变 |
| 本地 pilot 默认 | 两个场景分别独立抽取 | 20 | 8 | 12 | 本地 formal 的 train，**不是官方 test** |

formal 的具体步骤：

1. 检查官方 train/test 非空、各自 ID 唯一且互不重叠。
2. 将官方 train ID 排序，用 `random.Random(seed).shuffle` 打乱，取前 `validation_count` 个作为 validation。
3. 剩余官方 train 作为训练集；输出 train/validation 时保持官方原列表中的相对顺序。
4. 官方 test 的成员及顺序不变。脚本不按成绩删题，不进行附件修补或额外来源补充。

pilot 在上述 formal 的 train 中再次排序、使用同一 seed 打乱，依次取 20/8/12 题；
不触碰 formal validation 或官方 test，剩余训练题不用。pilot 用于跑通流程，不能作为正式泛化结果。

`train` 给进化器提供改进证据；`validation` 用于选择候选；`test` 用于冻结后的最终比较，
不能查看 test 后再据此调参选优。官方 train 全集的单独评测也不是独立测试，因为其中可能包含
训练和选优时已使用的题目。

准备时同时写出 `datasets/split_manifest.json`，记录 preset、seed、validation_count、来源
摘要、实际角色 ID 列表和 test_source；**复现以实际 manifest 为准**，不要只凭文件名或数量推断。
三个输出文件任一已存在时拒绝覆盖，请使用新任务目录。

自定义划分入口 [domain_prepare.py](../data/domain_prepare.py) 接受 `--ids roles.json`，
要求 train/validation/test 都是非空、不重复、两两不交叉的 ID 列表；但它**不证明 ID 属于官方
train/test**，也不自动写上述 split_manifest。调用者必须保存并审计原始 roles.json。
此入口支持定制子集，不能据“导出成功”宣称符合官方划分。

## 4. 如何准备与冻结

先按 [环境准备](USAGE.md#准备环境) 获取固定上游源码及独立上游 Python 环境；
本地配置使用领域 YAML 的私有副本。将相对数据路径改为正确绝对路径，避免复制 YAML 后
相对路径解析到错误位置。密钥通过环境变量注入。

### 办公

按固定版本的[办公数据准备说明](https://github.com/EverMind-AI/EvoAgentBench/blob/948a17288782d5120778da16b4cf1cad9305d8b4/benchmark/src/domains/knowledge_work/README.md)
准备 `openai/gdpval` HF 缓存、split、参考附件及 meta-prompts。上游文档给出的
meta-prompts 数据 revision 为 `3ac46d860f2f89ff4000f03c9936b618d10570ad`。

当前桥接层强制 GDPVal **离线读取**，所以必须在准备阶段下载完所选题目的附件，
不能依赖上游默认的“首跑下载”。私有 `knowledge_work.yaml` 配置 `reference_dir`、
`meta_prompts_dir`、`split_file`，并设置 `EAB_HF_CACHE_DIR`。冻结缓存、原始附件及
meta-prompts 的完整目录与文件摘要；缺附件应作为准备失败处理，不应悄悄删题。

### 信息检索

按固定版本的[检索数据准备说明](https://github.com/EverMind-AI/EvoAgentBench/blob/948a17288782d5120778da16b4cf1cad9305d8b4/benchmark/src/domains/information_retrieval/README.md)
运行上游 `benchmark/src/utils/browsecomp-plus-tools/setup_data.py`（在上游 `benchmark/`
目录执行 `python src/utils/browsecomp-plus-tools/setup_data.py`），取得解密 JSONL 和索引。
核对 `dataset_file`、`split_file`、`mcp_server.index_path`、`model_name` 的实际路径和版本。
更换 embedding 模型/索引会改变检索条件，须重新冻结，不能混入同一实验。

搜索服务由外部单独管理，本地桥接要求 `mcp_server.auto_start: false`。
冻结题目、语料、索引和模型文件；文件摘要并不能证明服务实际加载了这些文件，
依赖清单中的外部服务仍标为 `unverified`，需另存服务启动配置与加载记录。

### 本地公开清单

两种领域分别设置 `EAB_UPSTREAM_REPO`、`EAB_UPSTREAM_PYTHON`、`EAB_DOMAIN_CONFIG`、
`EAB_DEPENDENCY_MANIFEST`；办公还要设置 `EAB_HF_CACHE_DIR`。依赖冻结命令见
[DOMAINS.md](DOMAINS.md)。这些低层命令只适用于已另行建立普通领域 config/settings 的全新任务目录，
不适用于仅维护实现的公共模板或 portable 历史派生目录。环境与数据准备好后运行：

```bash
# 使用办公领域的环境变量
uv run python -m benchmarks.evoagentbench.data.prepare_dataset \
  NEW_KNOWLEDGE_TASK_DIR --preset formal --validation-count 36 --seed 42

# 切换为信息检索领域的配置和依赖清单后运行
uv run python -m benchmarks.evoagentbench.data.prepare_dataset \
  NEW_RESEARCH_TASK_DIR --preset formal --validation-count 36 --seed 42
```

若使用四组实验入口，由 `experiments.experiment prepare RUN_DIR` 统一准备，按
[实验手册](EXPERIMENTS.md) 操作，不要混用两套输出。数据获取须遵守各上游的数据条款；
不要把原始附件、私有评分资料或解密题目提交进代码仓库。

## 5. 历史实验不是当前默认数据

| 历史批次 | 实际数据口径 | 与当前默认协议的区别 |
| --- | --- | --- |
| 办公 `office-filtered-access-20261008`、2026-10-09 office-v2 | train 46、validation 24；当时 test 暂缓，空 `cases` 只是占位 | 从原训练/验证数据筛选，并缓存补充公开参考资料；不是新增 70 道独立官方题，也不是默认 69/36/60；验证有历史暴露 |
| 2026-10-09 retrieval-capabilities-r2 | train 118、validation 36 | 当时审计记录确认此规模；具体 ID、测试是否执行和来源版本以该批冻结文件为准，不凭数量认定等同当前默认协议 |
| 后续办公原始集合评测 | 原始官方 train 105 或 test 60 分别独立执行 | 不再使用 46/24 的筛选集合；全量 train 105 仍不能充当未见测试集 |
| R2 超时题 9 题、再 2 题复测 | 从原始 train 105 的超时记录选题，另开批次 | 是选择性复测，不覆盖原 105 题结果，也不是完整 105 题独立复测 |

办公定制数据的来源版本记录为 `d4d6e06da479ff92df1bf042b32e1ccb3f8cfa13`；office-v2
部署又使用修改后的读取器/评分源码，并非当前固定官方版本的原样运行，不能直接跨协议比较能力提升。
补充资料来源记录在该批 `case_resources.json`（URL、获取时间、版本、文件摘要）和
`source-input-provenance.json`；运行清单与补充文件不在本 Git 仓库，默认准备脚本也不会重建它们。

历史依据是工作区报告归档中的 `2026-10-09-office-v2/provenance.json`、
`2026-10-08-office-fresh/prepare_fresh.py`、
`2026-10-09-retrieval-capabilities-r2/run-stop-audit.json` 以及对应批次的冻结清单。
这些归档位于仓库外，clone 代码不会获得；复现历史批次必须另外取得原始角色 ID、补充资料、
源码/依赖版本和哈希证据，不能仅凭本说明的数量重新随机抽题。

## 6. 数据隔离与结果解释

Agent 仅接收公开题目及获准输入；标准答案、私有评分细则和完整 verifier 证据留在评分侧，
不放入可进化资产。依赖清单冻结文件成员、内容哈希、镜像 ID；协议摘要还绑定公开案例清单、
本地与上游源码和解析后的配置。公开来源的补充资料也会改变输入，须记录并按新协议评测。

不同集合分数不能直接相减当作进化收益；同集合对照也要核对模型、工具、评分器和时限一致性。
环境故障、读取失败、时限计零与评分器明确否定应分别说明，不能用重试挑最好成绩或覆盖旧失败记录。
