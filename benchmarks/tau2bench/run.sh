#!/usr/bin/env bash
# 运行所有 Banking 任务，最多同时处理八个相互隔离的 case。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"

# 环境变量已导出时，本地配置文件可省略。
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  source "$SCRIPT_DIR/.env"
fi

# TAU2 安装准备开始
TAU2_DIR="$SCRIPT_DIR/tau2-bench"
TAU2_TAG=v1.0.1
TAU2_PATCH="$SCRIPT_DIR/patches/native-banking.patch"

if [[ ! -e "$TAU2_DIR" ]]; then
  git clone --branch "$TAU2_TAG" --depth 1 \
    https://github.com/sierra-research/tau2-bench.git "$TAU2_DIR"
fi

if [[ ! -e "$TAU2_DIR/.git" ]] || \
   [[ "$(git -C "$TAU2_DIR" describe --tags --exact-match --match "$TAU2_TAG" HEAD 2>/dev/null)" != "$TAU2_TAG" ]]; then
  printf 'Tau2 仓库不是预期的 v1.0.1 标签，请检查：%s\n' "$TAU2_DIR" >&2
  exit 1
fi

if git -C "$TAU2_DIR" apply --reverse --check "$TAU2_PATCH" 2>/dev/null; then
  printf 'Tau2 补丁已应用，跳过。\n'
else
  if ! git -C "$TAU2_DIR" apply --check "$TAU2_PATCH"; then
    printf 'Tau2 补丁与当前文件冲突，请检查：%s\n' "$TAU2_DIR" >&2
    exit 1
  fi
  git -C "$TAU2_DIR" apply "$TAU2_PATCH"
fi

uv sync --project "$TAU2_DIR" --frozen --extra knowledge
# TAU2 安装准备结束

# 用户追加的原生 CLI 参数会覆盖以下默认值。
AGENT_DIR="${TAU2_AGENT_DIR:-../../application/banking_agent}"
RUNTIME_CONFIG="${TAU2_RUNTIME_CONFIG:-../../config/runtime.yaml}"
RUNTIME_PYTHON="${TAU2_RUNTIME_PYTHON:-$REPO_ROOT/.venv/bin/python}"
# 超时默认值由 IntegrationConfig 定义；已导出的 TAU2_* 变量直接传入。

run_args=(--domain banking_knowledge --agent agentsystem_banking)
if [[ -n "${TEST_AGENT_LLM_MODEL:-}" ]]; then
  run_args+=(--agent-llm "$TEST_AGENT_LLM_MODEL")
fi
if [[ -n "${TAU3_USER_LLM:-}" ]]; then
  run_args+=(--user-llm "$TAU3_USER_LLM")
fi

exec env -u TAU2_AGENTSYSTEM_CONFIG \
  PYTHONPATH="$REPO_ROOT" \
  PYTHON_DOTENV_DISABLED=1 \
  LITELLM_LOCAL_MODEL_COST_MAP=True \
  TAU2_AGENT_DIR="$AGENT_DIR" \
  TAU2_RUNTIME_CONFIG="$RUNTIME_CONFIG" \
  TAU2_RUNTIME_PYTHON="$RUNTIME_PYTHON" \
  uv run --project "$SCRIPT_DIR/tau2-bench" --no-sync \
  tau2 run \
  "${run_args[@]}" \
  --user-llm-args '{"temperature": 0, "max_tokens": 32768}' \
  --retrieval-config bm25_grep \
  --num-trials 4 \
  --max-concurrency 8 \
  --max-retries 3 \
  --timeout 1200 \
  --save-to "$SCRIPT_DIR/results/tau3-banking-task-$(date +%Y%m%d-%H%M%S)-$$" \
  "$@"
