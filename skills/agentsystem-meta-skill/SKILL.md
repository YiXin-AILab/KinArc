---
name: agentsystem-meta-skill
description: 在创建、配置、扩展或诊断 AgentSystem Application，以及核对应用配置、内置能力、插件接口和资产加载行为时使用。
metadata:
  version: "1.2"
  verified-at: "2026-10-15"
---

# AgentSystem Application 开发参考

面向应用开发者与自进化 Agent，覆盖框架支持的配置、资产、扩展接口和验证。

## 文档范围

Application 的标准资产类别为 config、prompt、Skill 和 plugins。
Skill 目录由 skill_roots 指定；其他目录按业务职责和工程需要组织。

参考区分配置字段、扩展接口和内置实现；内置能力不限定自定义扩展。

接口基线见 metadata.verified-at；版本差异以对应模型、协议和已确认设计核对。

## 使用流程

1. 确定目标与变更范围，按导航读取所需模块。
2. 修改配置、资产或扩展，核对注册、启用和引用关系。
3. 验证解析、加载、装配及目标行为；自进化任务另遵循评测与评审合约。

## 模块导航

| 层次 | 内容 | 参考 |
| --- | --- | --- |
| 应用结构 | 目录职责、路径解析、加载与更新生命周期 | [应用目录与加载](references/application-layout.md) |
| config | 全部顶层字段、能力选择与公开输出 | [AgentConfig](references/agent-config.md) |
| config / 内置能力 | Memory 作用域与额度、知识库引用、Todo 启用 | [内置能力配置](references/agent-config.md#内置能力配置) |
| config / 模型 | 模型连接、生成参数、供应商扩展参数 | [模型配置](references/model.md) |
| config / 上下文 | 输入预算、工具剪枝、摘要与历史重放 | [上下文压缩](references/compaction.md) |
| config / 媒体 | 归档规则、传输策略与 Asset 引用 | [多模态配置](references/media.md) |
| prompt | 文档格式、环境插值与 SYSTEM 组成 | [Prompt](references/prompt.md) |
| Skill | 元数据、资源组织、激活与渐进式披露 | [Skill](references/skills.md) |
| plugins / 装配 | 插件发现、参数模型、注册与依赖注入 | [Plugin](references/plugins.md) |
| plugins / Tool | 工具定义、参数 schema 与执行合约 | [Tool](references/tools.md) |
| plugins / Middleware | 注册参数、生命周期挂点与决策合约 | [Middleware](references/middlewares.md) |
| plugins / Provider | 动态上下文、注入与结果回写 | [ContextProvider](references/context-providers.md) |
| 应用演进 | 证据分析、变更范围、候选验证与评审 | [自进化流程](references/evolution.md) |
| 集成与验证 | 测试分层、Skill 挂载与接口维护 | [验证和挂载](references/validation.md) |

## 渐进式读取

仅读取任务涉及的参考模块及其依赖；每个模块独立说明配置、接口和使用要求。

在 AgentSystem 中，先调用 skill_view 读取本文件，再从 linked_files 中选择参考资源。
资源调用使用 name="agentsystem-meta-skill"、file_path="references/模块名.md"。
offset 为从 0 开始的 Unicode 字符偏移，limit 上限为 8000；
返回 truncated=true 时可以按需继续读取该资源的下一页。其他文件工具宿主可直接读取同一资源。

## 适用约束

配置须有消费实现，能力须完成注册和启用。宿主与 Runtime 管理基础设施及运行态一致性。
写入权限、Read-only Paths、数据访问范围和 Review 格式遵循任务或宿主约定。
实现定位均相对于仓库根。
