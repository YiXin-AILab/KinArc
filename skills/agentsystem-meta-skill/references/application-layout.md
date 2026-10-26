# 应用目录与加载

Application 是包含配置、指令资产和应用扩展的独立部署单元。

## 职责边界

Application 管理 Agent 的指令、模型请求、能力选择及业务逻辑。
宿主管理依赖装配与基础设施生命周期；Runtime 管理执行、调度和持久化协议。
应用资产的写入权限由部署或演进任务单独配置。

## 标准资产类别

标准资产为 config、prompt、Skill、plugins；下表用 skills/ 表示可配置的 Skill 根。

| 位置（相对单个 Application 根） | 内容 | 适合解决的问题 |
| --- | --- | --- |
| `config/agent.yaml` | AgentConfig、能力选择、插件参数 | 能力缺失、生成和上下文预算不合适 |
| `prompt/system.md` | 基础指令；实际路径由 `prompt` 指定 | 任务策略、工具选择、澄清与完成条件 |
| `skills/<name>/SKILL.md` | 专项流程与触发描述；可按需附带参考资源 | 可复用、多步骤的领域方法 |
| `plugins/*.py` | 同步注册入口，贡献 Tool/Middleware/ContextProvider | 工具接口、执行流程、动态上下文 |

services/、data/、docs/、tests/ 等附加目录按需创建，无通用自动加载语义。

默认发现 `application/` 直属非隐藏子目录中的 `config/agent.yaml`，不递归发现 Agent。
所有已发现的 `agent_id` 必须唯一；一个 Agent 资产加载失败会阻止返回不完整的发现结果。
目录名不必等于 `agent_id`，但修改 ID 会影响路由及调用方，应保持稳定。

`prompt` 和 `skill_roots` 按单个 Application 根解析，最终路径必须存在且位于该根内。
资产路径宜使用应用根相对路径，以支持目录复制、部署迁移和候选发布。
发现器不自动补上应用内 `skills/`，并拒绝 ../../skills 等根外路径。
标准 CLI entrypoint 在生成 AgentRuntimeSpec 时追加项目级 `skills/`（目录存在时），
应用显式根优先、全局根兜底；两者均需通过 enabled_skills 按名称启用。
程序化宿主通过 AgentRuntimeSpec.skill_roots 显式传入授权搜索根。

配置、Prompt 和能力注册按加载/装配生命周期生效，没有通用文件热更新协议。
资产更新后，通过重新装配和新 Session 应用变更。
已保存的 SYSTEM 消息与压缩历史保持原有语义，不随文件修改自动重写。

## 最小目录形态

```text
application/<application>/
  config/agent.yaml
  prompt/system.md
  skills/<skill-name>/SKILL.md   # 有专项 Skill 时添加
  plugins/<plugin>.py           # 有应用扩展时添加
```

config/agent.yaml 是标准发现入口；Prompt 路径由配置指定，Skill 和 Plugin 按需添加。
插件可在直属模块中实现能力，也可导入业务模块。

## 加载顺序与排错

1. 发现应用，加载 YAML 并插值环境变量，验证 AgentConfig 与 agent_id 唯一性。
2. 在应用根内解析 Prompt 和显式 skill_roots，读取 Prompt，发现应用 Plugin。
3. 宿主根据 enabled_skills 构建 SkillIndex；注册内置、应用及宿主明确提供的能力。
4. 选择 Provider、筛选 Tool/Middleware，校验冲突并冻结运行容器。
5. 新 Session 构建 SYSTEM；运行时读取的是已装配能力及已保存消息。

按阶段定位文件缺失、注册失败、筛选零匹配或运行时失败；配置解析与装配分别验证。

## 验证要求

使用测试临时目录加载最小应用；覆盖缺失 Prompt、越界路径、重复 agent_id、
无效插件和 Skill 激活失败。验证资产复制后的路径解析，以及重新装配后的新 Session 行为。
