# Skill：配置与资源使用

## 概念概览

Skill 是可复用的任务知识目录，由宿主按三个阶段使用：

1. **发现**：读取 name 和 description，判断 Skill 是否适用于当前任务。
2. **激活**：选中 Skill 后读取 SKILL.md 正文，获取任务步骤和约束。
3. **使用资源**：根据正文中的条件读取参考资料、使用资产或调用脚本，避免默认加载全部内容。

这里的“激活”指读取任务指令；AgentSystem 的 enabled_skills 则是启动时的可用 Skill 清单。

## 格式规范

SKILL.md 以 `---` 包围的 YAML frontmatter 开头，随后是 Markdown 正文。
frontmatter 的必填和可选字段见配置表；正文说明适用条件、步骤、完成标准和资源读取时机。

| 路径 | 必需 | 用途 |
| --- | --- | --- |
| SKILL.md | 是 | 元数据与主要任务指令 |
| references/ | 否 | 按需读取的规范、操作说明和详细示例 |
| scripts/ | 否 | 可由宿主执行能力调用的脚本 |
| assets/ | 否 | 模板、图像及其他任务资产 |

目录无需逐项配置；资源引用相对于 Skill 根，主文档直接列出目标文件。
运行依赖写入 compatibility 或正文，不会自动安装或授权。

## AgentSystem 支持范围

| 内容 | AgentSystem 行为 |
| --- | --- |
| 元数据与主文档 | 仅索引 enabled_skills 指定的 Skill；SYSTEM 展示名称、描述和 linked_files，正文通过 skill_view 读取 |
| references/ | 随 Skill 递归索引，按精确资源路径读取；不单独配置 references 列表 |
| scripts/ | 可索引并作为文本读取；skill_view 不执行脚本，执行能力需宿主另行提供 |
| assets/ | 可索引；skill_view 仅支持 UTF-8 文本，不渲染二进制媒体或自动使用模板 |
| allowed-tools | 官方定义为实验性、依宿主而异的预批准工具字段；本实现不解析、不校验，仅在主文档原文中保留，不启用或授权工具 |
| 大小限制 | 主文档及资源包无内容配额；单次读取最多 8000 Unicode 字符，可续读 |

## 配置与校验

| 位置 / 字段 | 要求 |
| --- | --- |
| AgentConfig.skill_roots | 默认 []；显式根按 Application 根解析并限制在该根内；标准 CLI 另追加项目级 skills/ 兜底，程序化宿主自行传入 |
| AgentConfig.enabled_skills | 默认 []；按名称激活，无重复项；多根同名时首个命中，不合并或损坏回退 |
| AgentConfig.enabled_tools | 需包含可用的 skill_view 及业务工具；省略或 null 表示选择全部已注册工具 |
| SKILL.md | 普通文件，以 YAML frontmatter 开始，后接 Markdown 正文 |
| name | 必填；与目录同名，1–64 个 a-z、0-9、连字符；不可首尾连字符或连续连字符 |
| description | 必填，非空字符串，说明用途和触发条件 |
| license | 可选字符串，说明许可证或附带许可证文件 |
| compatibility | 可选非空字符串，说明运行环境要求，不安装依赖 |
| metadata | 可选字符串到字符串的 mapping；版本号等值应加引号 |
| allowed-tools | 不做字段检查，可使用任意合法 YAML 值，不参与依赖或授权判断 |
| 未知 frontmatter | 接受并忽略，不告警；重复 YAML key 被拒绝 |
| 资源边界 | 不限制正文长度、资源数量或总大小，不统计每个文件大小 |
| 资源路径 | 初始化检查 Skill 文件树；读取时相对路径以 Skill 根为基准，也接受绝对路径和 ..，不检查清单归属 |

linked_files 只提供发现信息，不作为读取白名单。
工具依赖须由插件注册和 enabled_tools 满足；allowed-tools 不与 ToolRegistry 交叉校验。
skill_view 注册不依赖 enabled_skills 是否为空；是否可调用由 enabled_tools 决定。

## 配置到使用的完整示例

### 1. 创建资产

以下路径均相对于单个 Application：

```text
skills/task-check/
  SKILL.md
  references/exceptions.md
```

skills/task-check/SKILL.md：

```markdown
---
name: task-check
description: 当任务包含多个可独立核验的步骤时，记录完成项和待确认项。
metadata:
  version: "1.0"
---
按实际执行结果核对任务完成条件。
缺少步骤必需信息时，先读取 [例外处理](references/exceptions.md)。
```

skills/task-check/references/exceptions.md：

```text
记录缺失信息及其影响的步骤，向用户说明待确认项。
只有可验证的执行结果计为完成；计划和建议不计为完成。
```

linked_files 由启动时文件枚举生成并展示在 Catalog 中。
正文链接说明读取条件。

### 2. 启用 Skill 和阅读工具

将以下片段合并到 config/agent.yaml，保留仍需使用的其他能力：

```yaml
skill_roots: [skills]
enabled_skills: [task-check]
enabled_tools: [skill_view]
```

本例不需要业务工具；其他 Skill 若调用业务工具，应同时注册并选择这些工具。

### 3. 按需读取

先调用 skill_view，参数为：

```json
{"name": "task-check"}
```

当主文档所述条件成立时，按清单或正文引用路径调用：

```json
{"name": "task-check", "file_path": "references/exceptions.md"}
```

file_path 相对于 task-check 根，不含 skills/task-check 前缀；
也可以传入绝对路径；读取范围由运行进程的文件系统权限决定。

## skill_view 完整接口

| 参数 | 默认值 | 约束 |
| --- | --- | --- |
| name | 必填 | 字符串；工具按根目录/name 自行定位，不检查启用名称枚举 |
| file_path | null | null 读取 SKILL.md；相对路径以 Skill 根为基准，绝对路径直接读取 |
| offset | 0 | 严格非负整数，Unicode 字符偏移 |
| limit | 8000 | 严格整数，1..8000 字符 |

返回 success、name、file、content、offset、limit、truncated、next_offset。
非法参数由 ToolExecutor 返回 invalid_tool_arguments。
linked_files 由 Catalog 提供，是启动时预扫描的相对路径数组；skill_view 按输入路径读取文件。
truncated=true 时，以 next_offset 继续读取同一文件；末尾 next_offset 为 null；取得完整相关内容后再执行其流程。
分页缓存为 60 秒 TTL、最多 1024 页；按路径、mtime 和分页参数缓存，不记录文件大小。
每次先打开文件；mtime 变化或 TTL 到期后刷新，文件消失时报找不到请求的文件。
资源按 UTF-8 原文读取。

## 发布与验证

Skill 是运行时只读资产。更新后重新装配；新增 Skill 需同时更新激活配置和工具依赖。
受控工作区的写入权限由任务规定；新增 Skill 仍需更新 enabled_skills；已启用 Skill 的新增文件可直接读取，但清单需重新装配才刷新。
底层允许符合约束的根内资源链接；拒绝符号链接的宿主应使用普通文件发布。

验证 frontmatter、资源索引、工具选择和实际读取，再验证任务触发和行为。
