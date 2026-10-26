# Golden Banking Agent

独立于 `application/banking_agent/` 的应用，Agent ID 为
`banking-golden-agent`。使用官方 golden policy，业务工具仍通过 Tau2 Bridge 执行。
配置排除 `KB_search` 和 `grep`，不需要模型先检索知识。

## 原生 Tau2 评测

按 [Tau2 接入说明](../../benchmarks/tau2bench/README.md) 准备模型环境，
在 run.sh 中设置 Agent 目录、Runtime 路径和超时，无需另维护 JSON。
从仓库根目录执行：

```bash
TAU2_AGENT_DIR=../../application/banking_golden_agent \
  bash benchmarks/tau2bench/run.sh --retrieval-config golden_retrieval --max-concurrency 8
```

CLI 适配器名称仍是 `agentsystem_banking`，应用由 agent_dir 选择。
模型配置来自显式环境变量，不在本目录存放凭证。

## 知识注入

`plugins/golden_prompt.py` 注册 `golden-system-prompt` Middleware，
在 `after_system_prompt_build` 读取 `TAU2_DOMAIN_POLICY_FILE`，提取当前 case
的文档，原样替换已构建提示词中的 `{{required_documents}}`，返回 UPDATE。
默认构建的工具规则和 Skill 目录等分区保留；插件不额外转义文档，也不启用 Skill。
最终 SYSTEM 包含框架分区，不再与官方 LLMAgent.system_prompt 逐字一致。
注册处保留 BEFORE point 的切换注释：将 AFTER 换为 BEFORE 即跳过默认构建，
直接使用填入文档后的应用 Prompt。两种模式共用同一处理函数，插件不导入或调用 Tau2。
`prompt/system.md` 保存 v1.0.1 官方完整系统模板，包括外层 instructions/policy、
公共组件和文档使用说明。固定指令以该文件为准，policy 文件仅提供 case 文档。

评测接入为每个 attempt 写独立的临时 policy 文件，在子进程启动前提供路径；
插件注册不读取文件，因此无 case 的启动预检可正常完成。
路径缺失、文件不可读、文档区块格式错误、模板缺少占位符或 5 秒超时均使初始化失败。
在评测之外使用此应用时，也需由调用方显式提供同格式的 policy 文件。

每个 session 的 SYSTEM 消息仅在首次成功初始化时保存，后续轮次不重复注入。
case 结束时由接入层清理文件；不同 trial 和异常重试不复用文件、session 或 Redis prefix。
工具、prompt 和中间件的选择由本应用配置决定，评测接入不自动替换它们。
