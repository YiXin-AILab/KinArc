# 验证与宿主集成

应用验证覆盖配置、资产、装配和运行行为。Skill 挂载须满足资源可访问、已激活和阅读工具已启用。

## 验证分层

| 层次 | 验证内容 |
| --- | --- |
| 文档与结构 | frontmatter、资源路径、主文件大小、YAML 语法 |
| 配置 | 字段、环境插值和插件参数 |
| 资产与装配 | Prompt、SkillIndex、Plugin、Provider 工厂及能力筛选 |
| 模块行为 | 正常、边界、失败、timeout、取消及回归 |
| 外部集成 | 授权的模型、存储、工具服务和公开输出 |
| 任务评测 | 目标质量、资源使用及成功场景回归 |

测试使用显式配置和临时资产；外部依赖须显式 opt-in，不隐式读取部署 YAML 或 .env。
无法执行的验证应列明缺失条件。

## 配置与资产检查

- 顶层字段和子模型字段均通过校验，自定义参数有明确的消费实现。
- Prompt 路径、skill_roots 和关联文件在宿主允许的范围内。
- Skill 目录名、frontmatter、资源索引及工具依赖一致。
- Tool/Middleware 名称筛选均有匹配项，Provider type 与实例名称一致。
- 插件 import、register、工厂创建及冻结过程均成功。
- 模型、压缩和媒体预算保持一致，远端专属参数另行验证。

更改 Python 实现时，按项目工程规范执行格式、静态类型和对应风险的测试。
真实 Redis/数据库等一致性行为使用真实依赖验证；模拟对象用于接口或故障路径测试，
不替代原子性与并发合约测试。

## 仓库验证入口

以下命令从仓库根执行，覆盖配置、插件和 Skill 的基础行为：

```bash
rtk uv run pytest tests/unit/config tests/unit/plugin tests/unit/skills
rtk git diff --check
```

按风险补充测试，避免固定断言部署取值；局部示例需合并到完整配置。

## 标准 Application 挂载

标准发现器根据应用根解析 prompt 和 skill_roots。将 Skill 目录发布到宿主允许的
资产路径，例如 skills/agentsystem-meta-skill/，并合并以下局部配置：

```yaml
skill_roots: [skills]
enabled_skills: [agentsystem-meta-skill]
enabled_tools: [skill_view]
```

该片段提供 Skill 阅读能力；业务工具、文件工具或证据工具由宿主另行注册和选择。
集成时保留应用仍需使用的其他 Skill、搜索根和工具。

上述示例是应用私有 Skill。标准 CLI 在发现之后追加项目级 skills/ 作为兜底根，
应用根内的同名 Skill 优先；使用全局 Skill 时配置 enabled_skills 即可。
发现器本身只解析应用根内的显式路径；程序化宿主须自行传入全局根。

## 程序化宿主集成

程序化宿主通过 AgentRuntimeSpec 传入 AgentConfig、基础 Prompt、Skill 根和扩展能力。
集成流程如下：

1. 读取并验证配置，解析宿主允许的资产路径。
2. 将解析后的根列表传入 AgentRuntimeSpec.skill_roots。
3. 保持 AgentConfig.enabled_skills 与已发布资产一致。
4. 装配所需插件，并确保 skill_view 通过工具筛选。
5. 使用新 Session 验证目录、正文和关联资源读取。

专用加载器必须将 skill_roots 传入 Spec；仅修改 YAML 不保证生效。
enabled_skills 非空且无有效搜索根时加载失败。

自进化宿主将 meta-skill 挂载到优化者自己的资产集合。
优化者和目标业务 Agent 分别配置 enabled_skills；修改目标候选不改变优化者的资产或权限。
宿主集成属于获授权的部署或代码变更，不通过候选文件操作绕过保护规则。

当前自进化加载器 load_evolution_agent_spec 只使用 evolution/agent/ 下的包内资产。
维护 skills/evolution-meta-skill 后同步 evolution/agent/skills/evolution-meta-skill，
并校验两个副本的文件集合与正文一致。

## 挂载验收

- SkillIndex 可找到激活名称。
- skill_view 已注册并通过筛选。
- Catalog 中的 linked_files 包含全部参考资源。
- 相关模块可按分页接口完整读取。
- 宿主原有工具和任务功能仍然可用。
- 资产更新后重新装配，新旧 Session 的行为符合生命周期约定。

## 文档维护

接口升级时核对字段、默认值、范围、生命周期和示例；内置实现不限定扩展能力。
入口维护导航，各参考独立维护完整合约。发布副本随维护源更新，资源保留在 Skill 内。
