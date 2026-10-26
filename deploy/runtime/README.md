# KinArc Runtime 中间件启动

本目录按需启动 Redis、MySQL、MinIO，不启动 Agent 后端。
Langfuse 单独部署，后端与 Demo 前端分别启动。

Compose 项目名为 `kinarc`，迁移镜像为 `kinarc-runtime`，持久化卷为
`kinarc_redis_data`、`kinarc_mysql_data` 和 `kinarc_minio_data`。
这些是新部署资源；旧 `agentsystem_*` 数据卷保留，切换名称不会自动搬迁其中的数据。

## 快速开始

```bash
cp .env.example .env
cp deploy/runtime/.env.example deploy/runtime/.env
deploy/runtime/start.sh --profile redis
```

在根目录 `.env` 和 Runtime YAML 中配置中间件连接及 Agent 模型凭证，再启动后端：

```bash
uv sync && uv run agentsystem
```

## 按需启用

以下选项可任意组合；不指定选项时不启动任何服务：

| 选项 | 启动的本地服务 |
| --- | --- |
| `--profile redis` | Redis |
| `--profile sql` | MySQL，并执行迁移 |
| `--profile s3` | MinIO，并初始化 bucket |

使用已有服务时，不指定对应选项，在根目录 `.env` 和 Runtime YAML 中配置连接信息。

## 停止

```bash
docker compose --env-file .env --env-file deploy/runtime/.env \
  -f deploy/runtime/docker-compose.yml --profile redis --profile sql --profile s3 down
```

`down` 保留数据卷。
