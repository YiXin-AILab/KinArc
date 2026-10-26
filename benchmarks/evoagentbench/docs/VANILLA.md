# 办公与信息检索 vanilla 对比

本次固定协议：官方全部 train，办公 105、信息检索 154，共 259 题。
每题 Nanobot base 与初始 YX-Agent 各一次，共 518 次。没有进化、验证集选择或 test 评测。
被测模型 `openai/Qwen3.8-27B`；办公及检索的评分器单独使用 Pro。
原榜单与此运行的数据角色、模型、评分器和工具配置可能不同，不能把分数差归因为单一因素。

## 实施顺序

1. 新建被 Git 忽略的 `.env.qwen38.local`，只切换当前运行进程，保留共享 `.env`。
2. 为两个领域各复制初始 task、Agent 和 runtime 配置；不引用上次进化产物。
3. 准备官方数据、容器、参考文件、rubric 和 MCP 索引，冻结依赖清单。
4. 使用 `vanilla prepare` 导出完整官方 train 并绑定模型及数据指纹。
5. 每领域用 `run --limit 1` 验证两条真实执行与评分链路。它是正式样本中的第一题，后续复用。
6. `run` 继续剩余题；`report` 生成 `summary.json`，最终再制作两领域对比报告。

## 运行

先用现有 `experiment init --preset formal` 初始化目录，然后配置该领域实际路径和
runtime Redis/trace。初始化仅复制资产，不运行学习；本流程随后只使用 vanilla 命令。

```bash
python -m benchmarks.evoagentbench.experiments.vanilla prepare RUN_DIR --env-file .env.qwen38.local
python -m benchmarks.evoagentbench.experiments.vanilla run RUN_DIR --env-file .env.qwen38.local --limit 1
python -m benchmarks.evoagentbench.experiments.vanilla run RUN_DIR --env-file .env.qwen38.local --concurrency 2
python -m benchmarks.evoagentbench.experiments.vanilla report RUN_DIR --env-file .env.qwen38.local
```

单题基础设施异常会写入 `results/{base,yx}/序号/error.json`，继续其他题。
重复运行仅重试未完成项；已成功评分的题，包括得零分的题，都不重采样。
领域执行与评分在任务环境生命周期内完成，
基础设施失败可能需要重新创建整个任务环境。这些异常不计作独立 trial，也不冒充模型失败。
部分运行只显示 observed_mean，全部完成才发布 pass_at_1。

每题保存 execution、evaluation、receipt，模型与工具执行证据保存在本地记录中。
`eab.experiment` 区分领域运行目录，`eab.purpose=vanilla_train`、`eab.arm` 和 case ID 区分实验。
每题只跑一次，因此不报告 pass^2/3/4 或稳定性结论。

`--concurrency N` 是单个领域运行进程内 base 与 YX **合计**的任务上限，默认 2。
一个槽位包括模型、工具和评分全过程；不会给两组各分配 N 个槽位。
多个领域进程的并发需要相加，例如办公 1、检索 1，共最多 2 个任务。
每次调度的并发数和题目列表写入 `dispatches/`。可在一次命令结束后改变 N 续跑。
同一领域目录有进程锁，不要同时启动第二个 run 来提高并发。
中断时停止派发新题，等待已有有限超时任务退出和容器清理；不会立刻杀掉正在进行的任务。
控制该入口并发需要使用此参数，仅修改 task 的 max_case_concurrency 不生效。

## 真实部署门槛

适配代码存在不代表数据/服务已部署。办公需要 office 镜像和
离线 HF 数据、参考文件、评分 rubric；检索需要可用的 corpus/index/embedding/MCP 服务。
模型与评分器密钥、磁盘容量或依赖缺失时，不应把领域标为已启动或已完成。
