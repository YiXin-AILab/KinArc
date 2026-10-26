# Prompt

Prompt 定义基础职责、任务策略和输出要求，由 Runtime 组合为 SYSTEM 消息。

## 加载格式

`.md` / `.txt` 直接读取非空文本；其他后缀按包含 `prompt` 字段的 PromptDocument YAML 加载。
普通 YAML 和 Prompt 文本支持 `${UPPER_CASE_NAME}` 环境变量插值，缺失变量报错；
插值结果会成为模型上下文，认证信息应保留在连接配置中。
CLI 默认读取项目 `.env`，不覆盖已有进程环境；程序化 API 不隐式读取 `.env`。

基础 Prompt 保留原文写入 SYSTEM 的 agent_prompt 分区；
工具 schema 随模型请求单独传递。

## 配置与内容接口

| 位置 | 字段 / 内容 | 支持范围 |
| --- | --- | --- |
| AgentConfig | prompt | 必填非空路径，标准加载器限制在应用根内 |
| .md / .txt 文件 | 整个文本 | UTF-8、插值后非空；去除首尾空白 |
| Prompt YAML 文件 | prompt | 唯一字段、非空字符串；未知字段拒绝 |
| Tool 注册 | rules | 工具自己的短规则，由 Runtime 汇总 |
| Skill | name、description、正文 | SYSTEM 展示名称、描述和 linked_files，正文按需通过 skill_view 读取 |

环境变量插值作用于 YAML 的字符串值和 Prompt 文本。

## 示例

文本文件 prompt/system.md：

```text
协助用户查询当前业务信息并完成授权范围内的操作。
缺少工具必需参数时先澄清；查询已有结果足以回答时停止重复检索。
仅依据工具返回的事实回答，明确区分已完成操作与建议的下一步。
```

选择 YAML 时，文件例如 prompt/system.yaml：

```yaml
prompt: |
  协助用户查询当前业务信息。
  使用当前 Agent 已启用的工具完成必要查询。
```

同时将 AgentConfig.prompt 改为该 YAML 的实际路径。
支持系统分区按顺序为 agent_prompt、tool_rules、skill_catalog、sub_agents、
user_memory、agent_memory、safety、model_guidance；空可选分区省略。
默认 SystemPromptBuilder 的 sub_agents、safety 和 model_guidance 返回空内容。
配置可用 MemoryService 后，user_memory 和 agent_memory 分别原样渲染当前用户的
user_global、user_agent 首次快照；未提供服务、未启用作用域或无记忆时为空。
分区由 SystemPromptBuilder 的对应方法生成。

## 内容扩展

基础 Prompt 定义通用策略，Tool.rules 定义工具约束，Skill 提供可复用流程，
Tool 或 ContextProvider 提供动态事实。完整替换 SYSTEM 可注册
before_system_prompt_build 或 after_system_prompt_build Middleware 并返回 update；
具体 payload、跳过默认分区的语义和失败边界见 [Middleware](middlewares.md)。
保留默认构建时，Memory 由宿主服务与 AgentConfig.memory 控制，见 [配置](agent-config.md#内置能力配置)。

## 验证要求

以成功和失败场景检查提前停止、过度澄清、漏调工具、越权及规则冲突。
重新加载后用新 Session 验证；历史 SYSTEM 不随文件更新。
