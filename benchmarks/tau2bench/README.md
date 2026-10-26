# Tau2 Benchmark

## 用途

通过官方 Tau2 Runner 评测 KinArc Banking Agent，使用官方任务、用户模拟器、
业务工具和评分流程。该入口独立于 evolution，不启动优化器。

| 被测应用 | 检索配置 |
| --- | --- |
| `banking_agent`（默认） | 默认 `bm25_grep`，需与 Agent 工具配置匹配 |
| `banking_golden_agent` | `golden_retrieval` |

目前支持 macOS/Linux、`banking_knowledge` 域的文本半双工评测，
使用 Redis Message/State Manager 和本地 AssetStore。每个 case 独立运行，
同一 case 的用户与 Agent 共用官方业务 DB。

## 快速开始

需要 Python 3.13、uv、Git、可用的 Redis 和模型服务。以下命令在仓库根目录执行：

```bash
uv sync --frozen
cp benchmarks/tau2bench/.env.example benchmarks/tau2bench/.env
```

编辑 `benchmarks/tau2bench/.env`，填写被测 Agent、用户模拟器的模型名称、服务地址和凭证，
保留变量前的 `export`。在 `config/runtime.yaml` 中确认 Redis 连接和存储配置；
不使用 Trace 时设置 `trace.enabled: false`。

先跑两个任务验证配置：

```bash
bash benchmarks/tau2bench/run.sh \
  --num-tasks 2 --num-trials 1 --max-concurrency 2 --max-retries 0
```

脚本会自动克隆固定版本 Tau2 `v1.0.1`、应用接入补丁并安装其独立环境依赖。
已有 checkout 版本不符或补丁冲突时会停止，不覆盖已有修改。

脚本自动加载同目录的 `.env`，不自动加载仓库根目录的 `.env`，
也不读取 `local-config.json`。终端中已导出的变量会被继承；
同名变量若在 benchmark 的 `.env` 中重新赋值，以该文件为准。

## 常用配置

模型配置在 `benchmarks/tau2bench/.env` 中填写：

| 环境变量 | 用途 |
| --- | --- |
| `TEST_AGENT_LLM_MODEL` | 被测 Agent 模型，也可用 `--agent-llm` 覆盖 |
| `TEST_AGENT_LLM_BASE_URL` / `TEST_AGENT_LLM_API_KEY` | 默认 Banking 应用的模型服务与凭证 |
| `TAU3_USER_LLM` | 用户模拟器和 NL 裁判模型，也可用 `--user-llm` 覆盖 |
| `OPENAI_API_BASE` / `OPENAI_API_KEY` | 用户模拟器和 NL 裁判的模型服务与凭证 |

接入路径和超时同样通过环境变量配置，无需修改脚本：

| 环境变量 | 默认值 |
| --- | --- |
| `TAU2_AGENT_DIR` | `../../application/banking_agent` |
| `TAU2_RUNTIME_CONFIG` | `../../config/runtime.yaml` |
| `TAU2_RUNTIME_PYTHON` | 仓库根目录下的 `.venv/bin/python` |
| `TAU2_STARTUP_TIMEOUT_SECONDS` | 60 秒 |
| `TAU2_SHUTDOWN_TIMEOUT_SECONDS` | 20 秒 |
| `TAU2_HTTP_TIMEOUT_SECONDS` | 1200 秒 |

相对路径以 `benchmarks/tau2bench/` 为基准。超时必须是有限正数；
case 总预算由 `--timeout` 单独设置，脚本默认为 1200 秒。
用户模拟器和裁判的请求超时最多取 case 总预算。

脚本默认运行全量任务、8 并发、4 trials、最多重试 3 次。
追加原生 CLI 参数即可覆盖，例如：

```bash
bash benchmarks/tau2bench/run.sh --max-concurrency 4 --num-trials 1 --timeout 1200
```

Golden 示例：

```bash
TAU2_AGENT_DIR=../../application/banking_golden_agent \
  bash benchmarks/tau2bench/run.sh --retrieval-config golden_retrieval \
  --num-tasks 2 --num-trials 1 --max-concurrency 2 --max-retries 0
```

检索配置交给官方 Tau2 处理，需与所选 Agent 匹配。Golden 应用会读取接入层提供的
case policy；普通 Banking 应用沿用自身 prompt 和工具配置。

## 结果与评测口径

默认输出到 `benchmarks/tau2bench/results/tau3-banking-task-<时间戳>-<进程号>/`。
可用 `--save-to /absolute/path/to/results` 指定目录。

- `results.json` 和轨迹由官方 Runner 保存，可使用官方工具读取。
- `runtime-<task_id>-<simulation_id>-<attempt_uuid>.log` 保存各次尝试的 Runtime 日志。

原生 trial、seed、checkpoint/resume 和失败重试保留。
