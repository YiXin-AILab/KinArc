# KinArc 本地部署入口

- 仓库根目录 `Dockerfile`：构建 Agent Runtime 后端、数据库迁移、评测和自进化模块，
  包含 `evolution_tasks/`、`eval_tasks/`、任务依赖的 `benchmarks/` 源码，以及应用、
  全局 Skill 和知识库资产，以及根目录许可证声明和 `licenses/` 中的第三方原文；不包含 Web 前端。
- `runtime/`：按需启动专用 Redis、MySQL 或 MinIO，不启动 Agent 后端。
- `redis-cluster/`：显式启用、无持久化的 Redis Cluster 合约环境。
- `langfuse/`：独立的 Langfuse 本地依赖栈，不与 Runtime 共用服务或数据卷。

构建后端镜像：

```bash
docker build -t kinarc-runtime .
```

镜像包含构建上下文中的任务配置、执行器、评分器和数据集；`.dockerignore` 排除
`.evolution/`、评测 Run 结果、虚拟环境和本地克隆的 `tau2-bench/` 等运行产物。
额外数据集、外部 benchmark 环境、模型凭证和服务仍需按任务说明准备，打包任务目录
不代表这些外部依赖已安装。运行结果需要保留时，将相应任务目录挂载到持久卷。
