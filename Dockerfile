FROM python:3.13-slim
WORKDIR /agentsystem
ENV LANG=C.UTF-8

# 只安装锁定依赖；源码从工作目录加载，不安装包含 evolution 命令的项目包。
COPY pyproject.toml uv.lock README.md ./
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY licenses/ licenses/
RUN pip install --no-cache-dir uv==0.11.24 \
    && uv sync --frozen --no-dev --no-install-project

# 复制服务、评测与自进化模块，以及任务入口依赖的源码和资产。
COPY alembic.ini ./
COPY agentsystem/ agentsystem/
COPY server/ server/
COPY evolution/ evolution/
COPY evaluation/ evaluation/
COPY evolution_tasks/ evolution_tasks/
COPY eval_tasks/ eval_tasks/
COPY benchmarks/ benchmarks/
COPY application/ application/
COPY skills/ skills/
COPY knowledge/ knowledge/
COPY config/ config/
COPY deploy/runtime/runtime-sql.yaml deploy/runtime/runtime-sql.yaml
COPY deploy/runtime/runtime-s3.yaml deploy/runtime/runtime-s3.yaml

EXPOSE 8000

CMD [".venv/bin/python", "-m", "server.entrypoint", "--runtime-config", "config/runtime.yaml", "--applications-root", "application", "--project-root", ".", "--consumer-id", "docker-worker", "--host", "0.0.0.0", "--port", "8000"]
