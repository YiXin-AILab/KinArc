# Banking Discoverable Tools Design

## 目标

在不把 44 个 Banking Agent discoverable tools 单独注册进 Agent Tool Schema
的前提下，复现 Tau2 Banking 的隐藏工具发现与执行机制。Agent 继续只通过
`unlock_discoverable_agent_tool` 和 `call_discoverable_agent_tool` 两个通用入口
使用隐藏工具；通用入口必须执行真实业务逻辑，不能再返回虚假的固定成功文案。

本次只修改 `application/banking_agent/`。暂不实现 session-scoped capability 状态，
保留当前进程级单例生命周期。

## 范围

### 本次实现

- 44 个 Agent discoverable tools 的真实方法、参数校验、读写行为和稳定业务失败结果；
- 隐藏工具注册表，以及工具名称到可调用方法和元数据的映射；
- `unlock_discoverable_agent_tool` 对真实工具存在性的校验；
- `call_discoverable_agent_tool` 的 JSON 解析、参数绑定、真实调用和结果返回；
- 4 个 User discoverable tools 的真实方法与通用执行服务；
- `give_discoverable_user_tool` 的存在性和参数名校验；
- Banking 初始数据的显式重置能力，供场景复现和测试使用；
- 位于 `application/banking_agent/` 内的行为测试。

### 本次不实现

- Tau2 tasks、用户模拟器、评分器或排行榜兼容；
- session-scoped capability 状态；
- 将 44 个隐藏工具逐个注册到 Runtime ToolRegistry；
- Redis、SQL 或其他持久化后端；
- `application/banking_agent/` 以外的 Runtime 或 Server 改动。

## 架构

### 文件职责

- `services/banking_db_service.py`
  - 加载和持有 Banking 业务数据；
  - 提供受锁保护的表查询、插入、更新和删除原语；
  - 持有当前全局 unlocked/given/called capability 状态；
  - 编排隐藏工具解锁和调用。
- `services/discoverable_agent_tools.py`
  - 定义 44 个 Agent discoverable tool 方法；
  - 每个方法只实现一个明确的 Banking 业务动作；
  - 通过统一装饰器声明名称、类型和参数元数据。
- `services/discoverable_user_tools.py`
  - 定义 4 个 User discoverable tool 方法；
  - 校验 `give` 阶段预填参数，并在用户执行阶段完成真实读写。
- `services/discoverable_registry.py`
  - 从装饰器构建不可变注册表；
  - 负责名称查找、签名绑定和元数据导出；
  - 不持有业务数据或 capability 状态。
- `plugins/banking_plugin.py`
  - 保持现有 16 个常驻 Tool 注册形态；
  - 不注册 44 个隐藏工具；
  - 将通用入口映射到 Banking 服务。
- `tests/`
  - 放在 `application/banking_agent/tests/`；
  - 覆盖注册表完整性、查询、状态变更、失败条件和重置行为。

### 调用流程

1. Agent 从知识库获得隐藏工具的精确名称。
2. Agent 调用 `unlock_discoverable_agent_tool(name)`。
3. 服务只允许解锁注册表中真实存在的工具，并返回描述和参数。
4. Agent 调用 `call_discoverable_agent_tool(name, arguments_json)`。
5. 服务要求工具已解锁，解析 JSON 对象，并通过 Python 签名执行参数绑定。
6. 注册表调用对应隐藏方法；方法查询或修改 BankingDBService 的当前数据。
7. 服务返回真实结果；未知工具、无效参数和业务前置条件失败返回明确错误。

不存在的工具不会创建 fallback，也不会返回成功。

## 数据与状态语义

- `db.json` 继续作为初始业务数据快照，只读加载；运行时修改保存在内存中。
- 业务表修改必须由 BankingDBService 的受锁原语完成。
- `reset()` 从初始快照重新建立完整内存数据，并清空 unlocked/given/called 状态。
- 账户、卡片、交易、争议和验证记录属于业务状态。
- unlocked/given/called 仍按当前要求保持进程级状态，暂不按 session 隔离。
- 失败调用不得留下部分状态；涉及多个表或多个余额的动作必须先完成全部校验，
  再在同一锁边界内写入。

## 工具元数据

官方方法签名是参数定义的权威来源。现有 `discoverable_tools.json` 不再作为可执行工具
的唯一事实来源，避免元数据存在但方法缺失。注册表在启动时验证：

- 44 个官方 Agent discoverable tool 名称全部存在；
- 工具名称唯一；
- 参数具有受支持的类型注解；
- JSON 元数据与真实签名一致；
- User discoverable tool 名称和签名有效。

验证失败时 Banking Agent 初始化失败，不使用 fallback。

## 错误处理

- JSON 不是对象或无法解析：`invalid_discoverable_tool_arguments`；
- 工具不存在：`discoverable_tool_not_found`；
- 工具未解锁：`discoverable_tool_not_unlocked`；
- 参数缺失、额外或类型错误：`invalid_discoverable_tool_arguments`；
- 账户、卡片或交易不存在等业务失败：返回工具定义的明确失败内容；
- 非预期异常继续交给 Runtime 所有权边界记录，不在业务工具中吞掉。

通用入口只把预期输入错误转换为 `ToolError`，不会把程序错误伪装为业务成功。

## User Discoverable Tools

`give_discoverable_user_tool` 只授予注册表中存在的 User tool，并验证预填参数名称。
服务提供通用 `call_discoverable_user_tool(name, arguments_json)`，要求该工具已经 given，
将预填参数与用户提交参数合并后执行真实方法。当前 Runtime 没有独立 User ToolRegistry，
因此本次只在 Banking 服务层提供可调用能力，不把它注册成 Agent Tool，也不修改 Server
或 Runtime；后续接入真实用户操作入口时可以直接复用这一服务方法。

## 测试策略

采用测试优先实现：

1. 注册表测试证明 44 个 Agent 和 4 个 User 工具全部存在且签名有效；
2. 先以账户查询和内部转账建立通用执行骨架，复现当前 trace 的失败；
3. 按信用卡、借记卡、账户和争议四组覆盖全部隐藏工具；
4. 每个写工具至少测试成功状态变化和一个关键失败条件；
5. 每个读工具断言返回真实数据而非固定成功文案；
6. 测试未知工具、未解锁、无效 JSON、参数缺失和额外参数；
7. 测试 `reset()` 恢复初始业务数据并清空 capability 状态；
8. 运行 `application/banking_agent/` 范围的 Ruff、Mypy 和 pytest，再运行不修改
   其他目录的相关既有测试进行回归验证。

## 完成标准

- 44 个 Agent discoverable tools 都有真实可调用实现；
- 4 个 User discoverable tools 都有真实可调用实现；
- 读工具返回业务数据，写工具产生可观察且原子的状态变化；
- 不存在虚假成功和未知工具 fallback；
- 账户查询场景不再因缺少账户 ID 和余额进入搜索循环；
- 所有改动均位于 `application/banking_agent/`；
- 目标测试、Ruff 和 Mypy 通过。
