# Environment-backed official domains

The task factories `evolution_tasks/evoagentbench_knowledge` and
`evolution_tasks/evoagentbench_research` implement the existing
Evolution Executor/Evaluator protocols. No Runtime core protocol is changed.

Set `EAB_UPSTREAM_REPO`, `EAB_UPSTREAM_PYTHON` and `EAB_DOMAIN_CONFIG` explicitly.
The latter points to a copy of the corresponding official domain YAML with real
data paths. Install the pinned official domain requirements in the upstream
interpreter, independently of the Runtime interpreter. The worker checks the
official Git revision before importing its adapters.
GDPVal additionally needs `EAB_HF_CACHE_DIR` and every domain needs
`EAB_DEPENDENCY_MANIFEST`. GDP dataset loading is forced offline against this
explicit cache; freeze the cache, reference directory and rubric meta-prompts
directory after provisioning them. References must already exist locally before
evaluation. A missing cache or reference is an environment error.

Export a public dataset with:

```sh
uv run python -m benchmarks.evoagentbench.data.domain_prepare TASK_DIR --ids roles.json
```

`roles.json` must map `train`, `validation`, and `test` to disjoint nonempty lists
of official IDs (GDPVal uses full official task UUIDs). Derive
validation from official train before supplying this manifest. The exporter does
not itself prove official split membership. It refuses existing output files.
Only case IDs and official public prompts are exported. Setup-specific workspace
paths are placeholders during export and are rebuilt by the official
adapter for every real trial.

The private upstream process owns `load_tasks`, `initialize`, `pre_task_trials`,
`setup`, `build_prompt`, `verify`, `cleanup`, `post_task_trials`, and `finalize`.
Artifacts are verified before the task environment is removed. Only the score
and scoring method leave the grader. Missing evaluator credentials and evaluator
errors fail the execution rather than produce a zero. Research preserves the
official exact-match fallback when no LLM judge is configured; the scoring method
is retained. Configure the official judge for comparable judge-based results.
Pass scores use the official `reward > adapter.pass_threshold()` condition
(GDPVal defaults to strictly greater than 0.6). Raw rubric rewards remain alongside
binary per-trial scores in evaluation traces.

Research discovers actual schemas from the official SSE `/mcp` endpoint with the
standard MCP SDK. The Runtime tool pack only allows `search(query)` by default,
even when the server publishes other tools. It provides no shell. Operate the
official corpus/index server before running trials; the official auto-start path
reads its source-tree YAML and escapes process-group cleanup. Therefore this bridge
requires `auto_start: false` and explicit external server management.

GDPVal runs official setup and rubric verification. Build the office image using
`docker build -f benchmarks/evoagentbench/docker/Dockerfile.office -t ea-bench-office:local .`.
Only the official reference/output workspace is mounted into the network-disabled
office container. Its Python libraries and LibreOffice support text extraction,
document/spreadsheet/presentation generation and conversion through the bounded
command tool. `read_image` transfers images up to 4 MiB through the existing asset
service and injects a model-visible ImageBlock before the next turn, without
changing ToolOutput or core Runtime protocols. Render office/PDF pages with
LibreOffice or pdftoppm before calling read_image. The exact viewed bytes are also
archived under `artifact_dir/media` for replay after the temporary asset store is
removed. Official rubric verification also requires evaluator credentials and
its separate conversion dependencies. No paid model is called by the unit or
local SSE contract tests.

`runner: official_baseline` and `official_agent_config` select the separately
pinned official Nanobot baseline instead of Runtime. Missing/partial usage is
rejected. Baseline tools follow the official adapter and may differ from the
Runtime tools; account for this in experiment interpretation. In particular,
the pinned Nanobot baseline has text-only file tools and no native read_image.

`artifact_dir` enables atomic shared TrialStore archives, failure facts and exact
identity resume. Generated deliverables are archived under
`artifact_dir/outputs`; hidden verifier details and reference files are excluded.
The identity hashes local adapter source, actual upstream source, dataset and
resolved configuration (including environment-derived values, hashed only).
Artifact destinations do not affect protocol identity. `grading_identity` also
excludes runner/baseline configuration for comparison across experiment arms.

Freeze real dependencies with:

```sh
uv run python -m benchmarks.evoagentbench.domains.domain_dependencies deps.json \
  --path /absolute/corpus-or-hf-cache --path /absolute/task-data \
  --image ea-bench-office:local \
  --external-service http://localhost:9101/mcp
uv run python -m benchmarks.evoagentbench.domains.domain_dependencies deps.json --verify
```

Set `settings.dependency_manifest` to this file. Files have SHA-256 and byte counts;
directories have exact membership checks; image tags are checked against actual
IDs. GDP execution images are additionally pinned to an ID during settings
resolution. Frozen file hashes cannot attest what a running external SSE server
actually loaded: its `external_services` entry remains explicitly `unverified`.
Provision and configure that service against the frozen corpus/index separately.
Local contract tests cover actual localhost SSE and image bytes through asset,
middleware and provider media mapping. Complete GDPVal/corpus runs still
require provisioned datasets, images and evaluator services.
# 批次异常与恢复

领域执行器启用 `run_cases(continue_on_error=True)`：单题普通异常不会取消其他
正在执行的题，也不会阻止尚未执行的题派发；异常在本批结束后抛出，阻止用不完整
数据选优。已成功的试次继续写入 TrialStore，恢复时复用。显式外部取消仍停止派发
并通知运行中的执行者退出。通用调度器默认行为不变，其他调用方须显式启用此策略。

该策略不增加重试次数，EAB 的 `max_retries=0` 保持不变。重启前先审计失败试次：
能从冻结产物补评的不得重新调用 Agent；确需重新执行的，应保留中断记录并按实验
授权范围执行。基础设施错误不计为题目零分，缺少结果时不发布完整成绩。
