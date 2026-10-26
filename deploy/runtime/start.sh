#!/bin/sh
# 按需启动本地中间件；SQL 模式执行 migration，S3 模式初始化 bucket。

set -eu

deploy_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$deploy_dir/../.." && pwd)
compose_file="$deploy_dir/docker-compose.yml"
deploy_environment_file="$deploy_dir/.env"
if [ ! -f "$deploy_environment_file" ]; then
    deploy_environment_file="$deploy_dir/.env.example"
fi
project_environment_file="$project_dir/.env"
if [ ! -f "$project_environment_file" ]; then
    project_environment_file="$project_dir/.env.example"
fi
compose_local() {
    docker compose --env-file "$project_environment_file" \
        --env-file "$deploy_environment_file" -f "$compose_file" "$@"
}

use_redis=false
use_sql=false
use_s3=false
while [ "$#" -gt 0 ]; do
    if [ "$#" -lt 2 ] || [ "$1" != "--profile" ]; then
        echo "用法: deploy/runtime/start.sh [--profile redis] [--profile sql] [--profile s3]" >&2
        exit 2
    fi
    case "$2" in
        redis) use_redis=true ;;
        sql) use_sql=true ;;
        s3) use_s3=true ;;
        *)
            echo "用法: deploy/runtime/start.sh [--profile redis] [--profile sql] [--profile s3]" >&2
            exit 2
            ;;
    esac
    shift 2
done

if [ "$use_redis" = true ]; then
    compose_local --profile redis up -d --wait redis
fi

if [ "$use_sql" = true ]; then
    compose_local --profile sql up -d --wait mysql
    compose_local --profile sql run --build --rm mysql-migrate
fi

if [ "$use_s3" = true ]; then
    compose_local --profile s3 up -d --wait minio
    compose_local --profile s3 run --rm minio-init
fi

if [ "$use_redis" = false ] && [ "$use_sql" = false ] && [ "$use_s3" = false ]; then
    echo "未选择中间件。用法: deploy/runtime/start.sh [--profile redis] [--profile sql] [--profile s3]"
fi
