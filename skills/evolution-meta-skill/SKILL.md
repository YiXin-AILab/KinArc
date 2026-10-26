---
name: evolution-meta-skill
description: 在 AgentSystem 自进化中分析评测失败、选择优化方式，或修改目标 Agent 的 Prompt、Skill、配置和应用插件时使用。
metadata:
  version: "1.1"
  verified-at: "2026-10-15"
---

# 选择优化方式

先确认失败步骤中缺少或错误的具体环节，再选择优化方式。
已有规则未执行时，先核对它是否被读取、所需信息是否可得、
是否存在冲突；不要直接追加规则。

根据根因选择组件；需要新增能力时，完成实现、注册和启用配置。

| 发现问题类型 | 建议操作内容 | 参考资料 |
| --- | --- | --- |
| 需要始终可见的跨任务决策原则或完成条件缺失、冲突 | 补充或修订系统提示词中的决策原则与完成条件 | [Prompt](references/prompt.md) |
| 已启用的专项流程未被选中，或所需参考资料未被读取 | 改写 Skill 的触发描述或资源读取指引 | [Skill](references/skills.md) |
| 某类任务缺少可复用的知识、步骤或例外处理，或已有内容错误 | 补充或修订 Skill 正文与参考资料 | [Skill](references/skills.md) |
| 单个工具的用途、参数含义或返回状态被误解 | 澄清工具描述、参数 schema 或返回结构 | [Tool](references/tools.md) |
| 工具收到正确参数仍执行错误，或必须在执行时保证业务条件 | 修正工具实现或补充业务条件校验 | [Tool](references/tools.md) |
| 缺少可执行操作、确定性计算或操作时的最新状态查询能力 | 新增或扩展 Tool | [Tool](references/tools.md) |
| 需要用户记忆、知识库查询或任务进度跟踪 | 先核对内置 Memory、kb_search、Todo 的宿主依赖与启用配置，再判断是否需要扩展 | [配置](references/agent-config.md#内置能力配置)、[Tool](references/tools.md) |
| 需要随用户输入自动注入动态背景 | 新增或调整 ContextProvider；按当前输入查询和注入，区别于首次 SYSTEM 记忆快照及按需 Tool 查询 | [ContextProvider](references/context-providers.md) |
| 需要在明确生命周期节点以程序统一检查、提醒或拦截，超出单个工具职责 | 新增或调整 Middleware 的挂点与处理逻辑 | [Middleware](references/middlewares.md) |
| 能力已注册但未启用，或已支持的配置值不合适 | 调整 Agent 的能力启用项或配置值 | [配置](references/agent-config.md) |
| 应用能力已实现但未注册，或插件未读取对应配置 | 补齐 Plugin 的能力注册与配置读取 | [Plugin](references/plugins.md) |
| 模型请求参数不兼容，或生成因输出预算、停止条件而截断 | 调整模型请求参数、输出预算或停止条件 | [模型](references/model.md) |
| 输入预算不足，或剪枝、摘要造成关键内容丢失 | 调整上下文预算、剪枝阈值或内容保留策略 | [压缩](references/compaction.md)、[模型](references/model.md) |
| 媒体传输方式不适配，或后续处理缺少稳定的附件引用 | 调整媒体传输方式、归档规则与资产引用配置 | [媒体](references/media.md) |

# 按需查阅

修改前，通过 skill_view 读取所选组件的参考：
name="evolution-meta-skill"，file_path="references/文件名.md"。
返回 truncated=true 时按 next_offset 续读；本轮已读内容无需重读。

参考路径属于本 Skill。依据当前 Workspace 核对实际接口与权限。
创建文件不等于能力生效，须确认注册、启用和依赖链路。
