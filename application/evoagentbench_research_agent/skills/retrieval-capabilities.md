# Research capability reference

This describes the enhanced, isolated EvoAgentBench research task environment.
It contains no case answers or graded examples. The application Agent alone does
not provision the corpus, services, model proxy or container dependencies; the
task must validate and supply them before execution.

## Available retrieval primitives

The public MCP service exposes the following primitives when configured by the
research task. By default, the Runtime injects only `search` as an Agent tool.
Other primitives require a registered plugin tool that calls the service; adding
their names to `enabled_tools` alone does not register them. Do not claim to have
called a tool that is absent from the current tool list.

| Primitive | Parameters | Capability |
| --- | --- | --- |
| search | query | Dense search, top 5 |
| dense_search | query, top_k=5 | Dense candidates, top_k 1..50 |
| bm25_search | query, top_k=5 | Same frozen corpus, SQLite FTS5 BM25 |
| hybrid_search | query, top_k=5 | Dense and BM25 reciprocal rank fusion |
| rerank | query, docids | Lexical BM25 reorder of up to 50 selected sources |
| get_document | docid, start_char=0, max_chars=12000, query=null | Full document pages or query-focused excerpts |
| web_search | query, top_k=5 | Public Bing results without login |
| web_fetch | url, start_char=0, max_chars=12000 | Public webpage text pages |

Preserve returned document IDs or URLs with the evidence. Follow `next_start`
when additional pages are needed. Dense queries do not implement Google search
operators; BM25 tokenizes ordinary words with OR. BM25 reranking is lexical, not
a neural cross-encoder. Read full-document evidence when snippets do not resolve
dates, ordering, relationships or multiple constraints.

## Candidate plugins and reusable changes

When the task enables isolated candidate plugins, a direct `plugins/*.py` module
can expose a synchronous `register(context)` function. Register typed synchronous
or asynchronous tools with `context.register_tool`; return `ToolOutput(content=...)`
from `agentsystem.tool.models`. Include each registered name in `enabled_tools`.
Helper modules without `register` do not create Agent tools.

A retrieval wrapper can use the installed MCP `ClientSession` and `sse_client`
with the task-provided `EAB_RESEARCH_URL`, initialize the session, then call a
primitive above. Bound the entire operation with a timeout, propagate tool errors,
and preserve source identities in structured or text results. Native context
providers and middleware are also available through the installed plugin API.

Follow the experiment's editable-path restrictions and fixed execution-model
contract. Do not access evaluators, answer keys, other experiments or evolution
credentials. Use installed packages; do not download dependencies during a trial.
Candidate assets and Runtime source are read-only during execution. Temporary
notes belong to the trial workspace or `eab_write_memory`.

Reusable improvements include query decomposition, alternate queries, source
aggregation, document paging, constraint checks, evidence selection and stopping
rules. Implement and enable any required tools; mentioning an unavailable
primitive in a prompt does not make it executable.

## Model access and budgets

Use the execution model configured for this experiment; it is not necessarily
Qwen. In the isolated runner, plugin model calls must use the provided
`EAB_BASE_URL`, `EAB_API_KEY` and `EAB_BASE_MODEL`. These authorize the trial's fixed
model through the recorded proxy. Do not use other providers or credentials.
The runner accounts for observed usage and revokes the scope when the trial ends.
Public web access goes through the MCP service, not direct external networking.

`max_output_tokens: null` means no explicit client output cap; the provider may
still impose limits. The Agent's 1200-second model-call timeout is distinct from
the task's cumulative per-case deadline. Recovery consumes the remaining task
budget rather than granting a new deadline. The task may recover execution or
regrade complete output in its original workspace within that budget; valid
grades are not resampled. Deadline exhaustion is recorded as a timeout zero, not
as a negative verdict from the grader.
