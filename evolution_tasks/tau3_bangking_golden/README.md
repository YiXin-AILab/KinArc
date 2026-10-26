# Tau3 Golden Banking

使用题目所需文档直接优化 [Golden Banking Agent](../../application/banking_golden_agent)。
执行器、评分器、环境准备和排障方式复用 [Banking 任务说明](../tau3_banking/README.md)。

## 与普通 Banking 的区别

| 项目 | Golden Banking |
| --- | --- |
| 初始应用 | `application/banking_golden_agent` |
| Agent ID | `banking-golden-agent` |
| 检索模式 | `golden_retrieval` |
| 文档来源 | Tau2 为当前 Case 提供所需文档，运行时注入系统提示词 |
| 优化约束 | 任务目标禁止新增或启用 `KB_search`、`grep` |

`plugins/golden_prompt.py` 在系统提示词构建后注入官方文档，替换
`{{required_documents}}` 占位符。该插件不可由优化器修改，优化系统提示词时须保留
此占位符。文档与业务数据受工作区保护，具体范围以 [config.yaml](config.yaml) 为准。

本任务通过配置复用 `../tau3_banking/` 的执行器、评分器和数据集，
无需复制内部实现或另外安装 Tau2。并发、迭代次数和采样次数以本任务配置为准。

## 运行

完成 [环境准备](../tau3_banking/README.md#环境准备) 后，从仓库根目录执行：

```bash
uv run evolution run evolution_tasks/tau3_bangking_golden
uv run evolution status evolution_tasks/tau3_bangking_golden
```

确定 Candidate 后手动测试：

```bash
uv run evolution test evolution_tasks/tau3_bangking_golden <candidate-id>
```

只重评已有 Test Execution 时，在上述 `test` 命令后加 `--reevaluate`。
结果保存在本任务的 `.evolution/` 中，与普通 Banking 实验分开保存。

## 数据与评测范围

[数据集](../tau3_banking/datasets/) 与普通 Banking 共用同一份文件：
Train 34、Validation 23、Test 40。本目录不再保存数据集副本；修改共享数据会同时影响两个任务。
评分规则和多次采样聚合方式见 [数据集与评分](../tau3_banking/README.md#数据集与评分)。

Golden 模式主要评估给定所需文档后的业务处理能力，不能用来衡量自主检索能力。
数据存在历史暴露，不能当作全新盲测。部分题目的文档、参数或 DB 判定存在已知风险，
异常结果应结合执行轨迹和官方评分反馈复核；当前评测不会自动过滤这些题目或调整其分数。
