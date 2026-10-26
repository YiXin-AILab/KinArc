# EvoAgentBench office tools

This application package registers nothing by default. The domain worker supplies an
`ExecutionEnvironment`, call `execution_tools(environment)`, pass the definitions
to `AgentRuntimeSpec.external_tools`, and explicitly enable the same names in
their AgentConfig. Runtime ToolExecutor and trace projection remain unchanged.

Tools: `run_command`, `read_file`, `write_file`, `list_files`, `replace_text`.
Search and unified patches use `rg` / `git apply` through `run_command`.
Python is an executable in the environment, not an extra Runtime interpreter.
`replace_text` requires exactly one match. File helpers require Python 3 in the
environment, resolve symlinks there, and accept only workspace-relative paths.
Text previews contain at most 8000 characters, with an explicit truncation flag;
UTF-8 boundaries are preserved. Encoded file-operation requests are limited to
100000 bytes to stay below the process argument limit.

The default workspace is `/workspace`; another implementation can bind this pack
to an existing task environment. Shell access gives access to the environment's
whole filesystem: relative file restrictions are not a shell sandbox. Isolation
is the environment provider's responsibility.

`DockerEnvironment` owns a fresh non-root container with no network, host mounts,
provider secrets or grader data. Writable `/workspace` and `/tmp` are tmpfs;
resource limits are explicit. The image must already exist. The owner calls
`start()` and always `close()` in finally. Commands are serialized, synchronous,
limited to 300s through the tools; output is capped at 64000 bytes per stream.
Timeout/cancellation closes the whole environment so descendants cannot continue.
A timed-out trial cannot reuse that environment; subsequent tool calls report
`environment_closed`. Interactive sessions and background job handles are not
implemented. No auto-registration, package installation or network enablement.

EAB adds a parent-process cleanup fallback for hard worker termination. Other
applications must similarly supervise cleanup if they kill the owning process.
Tool calls are traced by the existing Runtime/Langfuse integration when enabled.

Office software belongs to task images. Official task setup, grading, hidden
tests, domain-specific MCP endpoints and allowed tools belong to adapters, not
this package. The existing stdio MCP handler is unchanged; SSE search tools live
in `application/evoagentbench_research_agent/plugins/sse.py`.

Workers import these fixed implementations from the application source tree,
never from an evolved candidate copy. Both EAB templates make `plugins/`
inaccessible to the improver, and experiment source identities include both
application packages. Moving or changing this code invalidates old protocol
identities; use a new experiment directory.
