# EvoAgentBench 集成

本目录只支持办公（GDPVal）与信息检索（BrowseComp-Plus）。复用 Runtime 和
EvolutionEngine，提供数据准备、领域执行、官方基线对照与报告。

**这里不包含真实题目、附件、评分资料、检索语料或索引。**
数据来源、获取方式、训练/验证/测试划分和历史实验区别见 **[数据说明](docs/DATASETS.md)**。

## 许可证与第三方材料

本项目原创集成代码适用根目录的 [MIT License](../../LICENSE)。外部 EvoAgentBench
网站根目录采用 MIT，但本集成使用的 `benchmark/` 评测实现采用 **Apache-2.0**；
调用其 adapter 不改变双方代码的许可范围。

固定参考版本为 `948a17288782d5120778da16b4cf1cad9305d8b4`，对应的
[Apache 许可证](../../licenses/evoagentbench/LICENSE)和
[上游第三方声明](../../licenses/evoagentbench/THIRD_PARTY_NOTICES.md)随本仓库保留。
后者包含 BrowseComp-Plus、ClawWork 改编代码的 MIT 声明。再分发复制或修改的上游代码时，
应保留适用许可证与版权声明，注明修改，并按实际分发版本保留适用的 NOTICE 内容。

GDPval 题目与附件、BrowseComp-Plus 题目与语料、索引和模型权重分别适用其来源条款，
不能用本项目 MIT 或 EvoAgentBench 的 Apache-2.0 统一替代。
派生实验档案在 [KinArc-Evolution](https://huggingface.co/datasets/YiXin-AILab/KinArc-Evolution)
单独发布，按实际包含的材料维护发布声明。完整说明见根目录的
[第三方声明](../../THIRD_PARTY_NOTICES.md#evoagentbench)。

## 目录导航

| 目录 | 职责 |
| --- | --- |
| [data/](data/) | 数据模型、默认划分、自定义 ID 清单导出 |
| [domains/](domains/) | 官方适配器、领域设置、依赖冻结、容器连接、办公/检索反馈与答案收尾 |
| [runners/](runners/) | KinArc 和 Nanobot 执行器、Skill 装配、工具桥接、容器内命令监督 |
| [experiments/](experiments/) | 四组实验与 vanilla 入口、实验状态、资产冻结 |
| [reporting/](reporting/) | 单组/配对统计、四组报告、token 汇总 |
| [common/](common/) | 源码身份、子进程通信、trial 归档 |
| [configs/](configs/) | Runtime 与官方基线示例配置；不存真实凭据 |
| [docker/](docker/) | 办公工具镜像与执行工具测试镜像；不存题目或 grader |
| [docs/](docs/) | 数据说明及运行手册 |

任务工厂在 `evolution_tasks/evoagentbench_{knowledge,research}/`；
初始 Agent 和业务工具在 `application/evoagentbench_{knowledge,research}_agent/`。

## 从哪里开始

- [数据说明](docs/DATASETS.md)：有哪些数据、怎样取得、如何划分及防止泄漏。
- [基础运行手册](docs/USAGE.md)：环境准备、单任务进化、冻结后测试、配对报告。
- [领域与依赖](docs/DOMAINS.md)：官方生命周期、依赖冻结、异常恢复边界。
- [四组实验](docs/EXPERIMENTS.md)：base / seed / prompt / evolved 的统一对照流程。
- [原始训练集基线](docs/VANILLA.md)：完整官方 train 的初始模型对比。

从仓库根目录执行，例如：

```bash
uv run python -m benchmarks.evoagentbench.data.prepare_dataset --help
uv run python -m benchmarks.evoagentbench.experiments.experiment --help
uv run python -m benchmarks.evoagentbench.experiments.vanilla --help
uv run python -m benchmarks.evoagentbench.reporting.report --help
```

## 目录迁移说明

旧平铺模块已移入对应子包，旧命令路径不保留兼容入口。例如
`benchmarks.evoagentbench.prepare_dataset` 改为
`benchmarks.evoagentbench.data.prepare_dataset`。配置移至 `configs/`，镜像文件移至
`docker/`，专题文档移至 `docs/`；调用方和任务模板同步更新。

原有模块均仍服务于执行、恢复、基线或验证，因此没有删掉这些功能。
此次清理移除了旧位置的入口和重复导航，不改题目、模型、采样预算或评分逻辑。
文件移动会改变 `source_digest`：已冻结实验继续使用其原代码与配置；新代码使用新实验目录，
不要重写旧 receipt 或将新旧源码的缓存混用。
