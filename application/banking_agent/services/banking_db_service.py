"""Banking 独立数据库服务。基于内存/JSON 完全按 Tau2 数据集规范实现。"""

import contextlib
import copy
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any, ClassVar

from application.banking_agent.services.discoverable_registry import (
    DiscoverableToolDefinition,
    invoke_discoverable_tool,
    parse_discoverable_arguments,
)

_TAU_TABLES = (
    "users",
    "accounts",
    "debit_cards",
    "referrals",
    "credit_card_applications",
    "user_discoverable_tools",
    "user_discoverable_tool_calls",
    "verification_history",
    "credit_card_transaction_history",
    "cash_back_disputes",
    "bank_account_transaction_history",
    "credit_card_accounts",
    "agent_discoverable_tools",
    "task_config",
    "human_transfer_requests",
    "transaction_disputes",
    "credit_card_orders",
    "debit_card_orders",
    "credit_card_closure_reasons",
    "credit_card_account_flags",
    "credit_limit_increase_requests",
    "payment_history",
    "debit_card_disputes",
)


class BankingDBService:
    """独立银行数据服务（单例模式）。"""

    _instance: ClassVar["BankingDBService | None"] = None
    _lock: ClassVar[Lock] = Lock()

    def __init__(
        self,
        db_path: Path | None = None,
        discoverable_tools_path: Path | None = None,
    ) -> None:
        if db_path is None:
            db_path = Path(__file__).resolve().parent.parent / "data" / "db.json"
        if discoverable_tools_path is None:
            discoverable_tools_path = (
                Path(__file__).resolve().parent.parent
                / "data"
                / "discoverable_tools.json"
            )
        self._db_path = db_path
        self._discoverable_tools_path = discoverable_tools_path
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._discoverable_tools_meta: dict[str, dict[str, Any]] = {}
        self._unlocked_agent_tools: dict[str, dict[str, Any]] = {}
        self._given_user_tools: dict[str, dict[str, Any]] = {}
        self._called_agent_tools: list[dict[str, Any]] = []
        self._mutex = RLock()
        self._load_database()
        self._load_discoverable_tools_meta()
        from application.banking_agent.services.discoverable_agent_tools import (
            AGENT_DISCOVERABLE_REGISTRY,
            AgentDiscoverableTools,
        )
        from application.banking_agent.services.discoverable_user_tools import (
            USER_DISCOVERABLE_REGISTRY,
            UserDiscoverableTools,
        )

        self._agent_discoverable_registry: dict[str, DiscoverableToolDefinition] = (
            AGENT_DISCOVERABLE_REGISTRY
        )
        self._agent_discoverable_tools = AgentDiscoverableTools(self)
        self._user_discoverable_registry: dict[str, DiscoverableToolDefinition] = (
            USER_DISCOVERABLE_REGISTRY
        )
        self._user_discoverable_tools = UserDiscoverableTools(self)

    @classmethod
    def get_instance(cls) -> "BankingDBService":
        """获取或创建银行数据服务单例。"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _load_database(self) -> None:
        """从 JSON 文件加载初始数据到内存中。"""
        self._data.clear()
        if not self._db_path.exists():
            return
        with contextlib.suppress(Exception):
            raw = json.loads(self._db_path.read_text(encoding="utf-8"))
            for table_name, table_obj in raw.items():
                if isinstance(table_obj, dict) and "data" in table_obj:
                    self._data[table_name] = copy.deepcopy(table_obj["data"])
                elif isinstance(table_obj, dict):
                    self._data[table_name] = copy.deepcopy(table_obj)
            for table_name in _TAU_TABLES:
                self._data.setdefault(table_name, {})

    def reset(self) -> None:
        """从初始 JSON 恢复业务数据并清空临时工具状态。"""
        with self._mutex:
            self._load_database()
            self._unlocked_agent_tools.clear()
            self._given_user_tools.clear()
            self._called_agent_tools.clear()

    def has_table(self, table_name: str) -> bool:
        """判断 Tau 兼容数据库表是否存在。"""
        with self._mutex:
            return table_name in self._data

    def __getattr__(self, table_name: str) -> "_TableView":
        """提供 Tau ``db.<table>.data`` 形式的受限兼容视图。"""
        if table_name.startswith("_"):
            raise AttributeError(table_name)
        with self._mutex:
            table = self._data.get(table_name)
            if table is None:
                raise AttributeError(table_name)
            return _TableView(table)

    def get_record(self, table_name: str, record_id: str) -> dict[str, Any] | None:
        """读取一条记录的深拷贝。"""
        with self._mutex:
            record = self._data.get(table_name, {}).get(record_id)
            if not isinstance(record, dict):
                return None
            return copy.deepcopy(record)

    def query_records(
        self,
        table_name: str,
        **constraints: Any,
    ) -> list[tuple[str, dict[str, Any]]]:
        """按字段精确查询并返回记录深拷贝。"""
        with self._mutex:
            return copy.deepcopy(self._query_table(table_name, **constraints))

    def add_record(
        self,
        table_name: str,
        record_id: str,
        record: dict[str, Any],
    ) -> bool:
        """在记录 ID 不存在时插入一条记录。"""
        with self._mutex:
            table = self._data.setdefault(table_name, {})
            if record_id in table:
                return False
            table[record_id] = copy.deepcopy(record)
            return True

    def update_record(
        self,
        table_name: str,
        record_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """更新已存在记录并返回更新后的深拷贝。"""
        with self._mutex:
            record = self._data.get(table_name, {}).get(record_id)
            if not isinstance(record, dict):
                return None
            record.update(copy.deepcopy(updates))
            return copy.deepcopy(record)

    def delete_record(self, table_name: str, record_id: str) -> bool:
        """删除一条已存在记录。"""
        with self._mutex:
            table = self._data.get(table_name)
            if table is None or record_id not in table:
                return False
            del table[record_id]
            return True

    @contextmanager
    def transaction(self) -> Iterator[dict[str, dict[str, dict[str, Any]]]]:
        """在同一可重入锁边界内暴露业务表供隐藏工具原子修改。"""
        with self._mutex:
            yield self._data

    def _load_discoverable_tools_meta(self) -> None:
        """加载 44 个 discoverable tools 的标准元数据。"""
        if not self._discoverable_tools_path.exists():
            return
        with contextlib.suppress(Exception):
            self._discoverable_tools_meta = json.loads(
                self._discoverable_tools_path.read_text(encoding="utf-8")
            )

    def _query_table(
        self, table_name: str, **constraints: Any
    ) -> list[tuple[str, dict[str, Any]]]:
        """在指定表中执行精确过滤查询。"""
        table = self._data.get(table_name, {})
        results: list[tuple[str, dict[str, Any]]] = []
        for record_id, record in table.items():
            if not isinstance(record, dict):
                continue
            matched = True
            for k, expected in constraints.items():
                actual = record.get(k)
                if actual != expected:
                    matched = False
                    break
            if matched:
                results.append((str(record_id), record))
        return results

    def _format_query_results(
        self, table_name: str, results: list[tuple[str, dict[str, Any]]]
    ) -> str:
        """格式化查询结果输出。"""
        if not results:
            return f"No records found in '{table_name}'."

        formatted_lines = [f"Found {len(results)} record(s) in '{table_name}':\n"]
        for i, (record_id, record) in enumerate(results, 1):
            formatted_lines.append(f"{i}. Record ID: {record_id}")
            for field, value in record.items():
                formatted_lines.append(f"   {field}: {value}")
            formatted_lines.append("")

        return "\n".join(formatted_lines).strip()

    def get_user_by_id(self, user_id: str) -> str:
        """按 user_id 查询用户信息。"""
        with self._mutex:
            results = self._query_table("users", user_id=user_id)
            return self._format_query_results("users", results)

    def get_user_by_name(self, customer_name: str) -> str:
        """按客户姓名查询用户信息（区分大小写）。"""
        with self._mutex:
            results = self._query_table("users", name=customer_name)
            return self._format_query_results("users", results)

    def get_user_by_email(self, email: str) -> str:
        """按电子邮箱查询用户信息。"""
        with self._mutex:
            results = self._query_table("users", email=email)
            return self._format_query_results("users", results)

    def change_user_email(self, user_id: str, new_email: str) -> str:
        """修改用户的电子邮箱。"""
        with self._mutex:
            users = self._data.get("users", {})
            matched_id: str | None = None
            for rec_id, rec in users.items():
                if rec.get("user_id") == user_id or str(rec_id) == user_id:
                    matched_id = str(rec_id)
                    break

            if matched_id is None:
                return f"Error: User with ID '{user_id}' not found."

            users[matched_id]["email"] = new_email
            return (
                f"Email updated successfully.\n"
                f"  - User ID: {user_id}\n"
                f"  - New Email: {new_email}"
            )

    def get_referrals_by_user(self, user_id: str) -> str:
        """获取用户的所有推荐记录。"""
        with self._mutex:
            results = self._query_table("referrals", referrer_id=user_id)
            return self._format_query_results("referrals", results)

    def get_credit_card_accounts_by_user(self, user_id: str) -> str:
        """获取用户的所有信用卡账户。"""
        with self._mutex:
            results = self._query_table("credit_card_accounts", user_id=user_id)
            return self._format_query_results("credit_card_accounts", results)

    def get_credit_card_transactions_by_user(self, user_id: str) -> str:
        """获取用户的所有信用卡交易流水。"""
        with self._mutex:
            results = self._query_table(
                "credit_card_transaction_history", user_id=user_id
            )
            return self._format_query_results(
                "credit_card_transaction_history", results
            )

    def log_verification(
        self,
        name: str,
        user_id: str,
        address: str,
        email: str,
        phone_number: str,
        date_of_birth: str,
        time_verified: str,
    ) -> str:
        """记录用户身份核验结果（审计记录）。"""
        with self._mutex:
            if "verification_history" not in self._data:
                self._data["verification_history"] = {}

            verification_table = self._data["verification_history"]
            rec_id = f"ver_{user_id}_{len(verification_table) + 1}"
            verification_table[rec_id] = {
                "name": name,
                "user_id": user_id,
                "address": address,
                "email": email,
                "phone_number": phone_number,
                "date_of_birth": date_of_birth,
                "time_verified": time_verified,
                "created_at": datetime.now(UTC).isoformat(),
            }

            return (
                f"Verification logged successfully.\n"
                f"  - User: {name} (ID: {user_id})\n"
                f"  - Verified at: {time_verified}"
            )

    def transfer_to_human_agents(self, summary: str, reason: str) -> str:
        """记录转接人工客服请求。"""
        with self._mutex:
            if "human_transfer_requests" not in self._data:
                self._data["human_transfer_requests"] = {}

            req_table = self._data["human_transfer_requests"]
            req_id = f"trans_{len(req_table) + 1}"
            req_table[req_id] = {
                "summary": summary,
                "reason": reason,
                "created_at": datetime.now(UTC).isoformat(),
            }

            return (
                f"Transfer successful (reason: {reason}). "
                f"A human agent will assist you shortly."
            )

    def give_discoverable_user_tool(
        self, discoverable_tool_name: str, arguments: str = "{}"
    ) -> str:
        """将可由用户执行的操作工具传递给用户。"""
        with self._mutex:
            try:
                args_dict = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError as e:
                return f"Error: Invalid JSON in arguments: {e}"

            if discoverable_tool_name not in self._user_discoverable_registry:
                return f"Error: Unknown user tool '{discoverable_tool_name}'."

            self._given_user_tools[discoverable_tool_name] = {
                "arguments": args_dict,
                "given_at": datetime.now(UTC).isoformat(),
            }

            args_str = (
                json.dumps(args_dict, indent=2) if args_dict else "(no arguments)"
            )
            return (
                f"Tool given to user: {discoverable_tool_name}\n"
                f"Arguments: {args_str}\n\n"
                "The user can now execute this action by calling "
                "`call_discoverable_user_tool` with "
                f"discoverable_tool_name='{discoverable_tool_name}' and the "
                "same arguments."
            )

    def call_discoverable_user_tool(
        self, discoverable_tool_name: str, arguments: str = "{}"
    ) -> str:
        """执行已由 Agent 授予用户的 discoverable 工具。"""
        with self._mutex:
            if discoverable_tool_name not in self._given_user_tools:
                return (
                    f"Error: Tool '{discoverable_tool_name}' has not been given to "
                    "you by the agent. The agent must first use "
                    "`give_discoverable_user_tool` to give this tool to you."
                )
            definition = self._user_discoverable_registry.get(discoverable_tool_name)
            if definition is None:
                return f"Error: Unknown user tool '{discoverable_tool_name}'."
            try:
                parsed_arguments = parse_discoverable_arguments(arguments)
            except ValueError as error:
                return f"Error: Invalid JSON in arguments: {error}"
            try:
                result = invoke_discoverable_tool(
                    definition,
                    self._user_discoverable_tools,
                    parsed_arguments,
                )
            except ValueError as error:
                return f"Error: Invalid arguments: {error}"

            call_id = hashlib.sha256(
                (
                    f"{discoverable_tool_name}:"
                    f"{json.dumps(parsed_arguments, sort_keys=True)}"
                ).encode()
            ).hexdigest()[:16]
            self._data.setdefault("user_discoverable_tool_calls", {})[call_id] = {
                "tool_name": discoverable_tool_name,
                "arguments": parsed_arguments,
                "called_at": datetime.now(UTC).isoformat(),
                "status": "CALLED",
            }
            return result

    def unlock_discoverable_agent_tool(self, agent_tool_name: str) -> str:
        """解锁从知识库发现的专业内部工具。完全符合 Tau2 数据集返回格式。"""
        with self._mutex:
            definition = self._agent_discoverable_registry.get(agent_tool_name)
            if definition is None:
                return f"Error: Unknown agent tool '{agent_tool_name}'."
            tool_meta = self._discoverable_tools_meta.get(agent_tool_name)
            if tool_meta is None:
                return f"Error: Agent tool metadata missing for '{agent_tool_name}'."
            self._unlocked_agent_tools[agent_tool_name] = {
                "unlocked_at": datetime.now(UTC).isoformat(),
                "tool_info": tool_meta,
                "definition": definition,
            }

            param_strs: list[str] = []
            for param_name, param_def in tool_meta.get("parameters", {}).items():
                required = param_def.get("required", True)
                param_type = param_def.get("type", "string")
                desc = param_def.get("description", "")
                req_str = " (required)" if required else " (optional)"
                param_strs.append(f"  - {param_name}: {param_type}{req_str} - {desc}")

            params_section = (
                "\n".join(param_strs) if param_strs else "  (no parameters)"
            )
            formatted_tool = (
                f"Tool: {tool_meta['name']}\n"
                f"Description: {tool_meta['description']}\n"
                f"Parameters:\n{params_section}"
            )
            return (
                f"Tool unlocked: {agent_tool_name}\n"
                f"Description: {tool_meta['description']}\n\n"
                f"{formatted_tool}\n\n"
                "You can now use this tool by calling "
                "`call_discoverable_agent_tool` with "
                f"agent_tool_name='{agent_tool_name}' and the required arguments."
            )

    def call_discoverable_agent_tool(
        self, agent_tool_name: str, arguments: str = "{}"
    ) -> str:
        """执行已解锁的专业内部工具。"""
        with self._mutex:
            unlocked = self._unlocked_agent_tools.get(agent_tool_name)
            if unlocked is None:
                return (
                    f"Error: Tool '{agent_tool_name}' has not been unlocked. "
                    "You must first use `unlock_discoverable_agent_tool` to unlock "
                    "this tool before calling it."
                )
            definition = self._agent_discoverable_registry.get(agent_tool_name)
            if definition is None:
                return f"Error: Unknown agent tool '{agent_tool_name}'."
            try:
                parsed_arguments = parse_discoverable_arguments(arguments)
            except ValueError as error:
                return f"Error: Invalid JSON in arguments: {error}"
            try:
                result = invoke_discoverable_tool(
                    definition,
                    self._agent_discoverable_tools,
                    parsed_arguments,
                    strict_types=False,
                )
            except ValueError as error:
                return f"Error: Invalid arguments: {error}"

            if definition.kind.value == "write":
                from application.banking_agent.services.tau_compat import (
                    generate_agent_discoverable_tool_id,
                )

                call_id = generate_agent_discoverable_tool_id(agent_tool_name)
                self._data["agent_discoverable_tools"].setdefault(
                    call_id,
                    {"tool_name": agent_tool_name, "status": "CALLED"},
                )
            self._called_agent_tools.append(
                {
                    "tool_name": agent_tool_name,
                    "arguments": parsed_arguments,
                    "called_at": datetime.now(UTC).isoformat(),
                }
            )
            return result

    def list_discoverable_agent_tools(self) -> str:
        """列出已调用/已解锁的专业内部工具。"""
        with self._mutex:
            from application.banking_agent.services.tau_compat import (
                query_database_tool,
            )

            result = query_database_tool("agent_discoverable_tools", db=self)
            if "No results found" in result:
                return (
                    "No agent tools have been called yet. "
                    "Search the knowledge base to discover available tools."
                )
            return f"Your called agent tools:\n{result}"


class _TableView:
    """Tau 工具访问单张内存表所需的最小视图。"""

    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self.data = data
        self.notes = ""
