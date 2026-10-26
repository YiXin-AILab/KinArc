"""Banking Agent 官方业务插件与业务工具集合。"""

import inspect
import os
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, Literal, cast

from agentsystem.plugin.context import PluginSetupContext
from agentsystem.tool.models import ToolOutput
from pydantic import Field

from application.banking_agent.services.banking_db_service import BankingDBService
from application.banking_agent.services.knowledge_service import KnowledgeService
from application.banking_agent.services.tau2_tool_client import Tau2ToolClient

TransferReason = Literal[
    "fraud_or_security_concern",
    "account_closure_request",
    "deceased_account_holder",
    "legal_or_regulatory_matter",
    "account_ownership_dispute",
    "complex_billing_dispute",
    "abusive_customer_behavior",
    "third_party_inquiry",
    "technical_system_error",
    "unconfirmed_external_communication",
    "customer_demands_after_unavailable_offer_refusal",
    "kb_search_unsuccessful_customer_requests_transfer",
    "specialized_department_required",
    "accessibility_or_special_needs",
    "customer_frustrated_demands_human",
    "supervisor_request_service_complaint",
    "customer_requests_human_no_specific_reason",
    "request_completed_customer_wants_human_followup",
    "other",
]


def register(context: PluginSetupContext) -> None:
    """注册 Banking Agent 的全部 16 个业务 Tool。"""
    context.register_tool(
        _tool_handler(kb_search, name="KB_search"),
        name="KB_search",
        description="Search the knowledge base for relevant documents.",
    )
    context.register_tool(
        _tool_handler(grep, name="grep"),
        name="grep",
        description=(
            "Search for a regex pattern in all knowledge base documents.\n\n"
            "Returns documents ranked by number of matches, with full content."
        ),
    )
    context.register_tool(
        _tool_handler(
            transfer_to_human_agents,
            name="transfer_to_human_agents",
        ),
        name="transfer_to_human_agents",
        description=(
            "Transfer the user to a human agent.\n\n"
            "The proper transfer reason enum can be found in the knowledge base: "
            "search it before calling this tool to select the proper applicable reason."
        ),
    )
    context.register_tool(
        _tool_handler(get_current_time, name="get_current_time"),
        name="get_current_time",
        description=(
            "Get the current time. Use this to get the current timestamp for "
            "logging verification records."
        ),
    )
    context.register_tool(
        _tool_handler(
            get_user_information_by_id,
            name="get_user_information_by_id",
        ),
        name="get_user_information_by_id",
        description=(
            "Get the information (date of birth, email, phone number, address) "
            "for a user by their user id."
        ),
    )
    context.register_tool(
        _tool_handler(
            get_user_information_by_name,
            name="get_user_information_by_name",
        ),
        name="get_user_information_by_name",
        description=(
            "Get the information (date of birth, email, phone number, address) "
            "for a user by their name. Case Sensitive."
        ),
    )
    context.register_tool(
        _tool_handler(
            get_user_information_by_email,
            name="get_user_information_by_email",
        ),
        name="get_user_information_by_email",
        description=(
            "Get the information (date of birth, email, phone number, address) "
            "for a user by their email."
        ),
    )
    context.register_tool(
        _tool_handler(change_user_email, name="change_user_email"),
        name="change_user_email",
        description="Change the email address for a user.",
    )
    context.register_tool(
        _tool_handler(get_referrals_by_user, name="get_referrals_by_user"),
        name="get_referrals_by_user",
        description="Get all referrals made by a user.",
    )
    context.register_tool(
        _tool_handler(
            get_credit_card_transactions_by_user,
            name="get_credit_card_transactions_by_user",
        ),
        name="get_credit_card_transactions_by_user",
        description="Get all credit card transactions for a user.",
    )
    context.register_tool(
        _tool_handler(
            get_credit_card_accounts_by_user,
            name="get_credit_card_accounts_by_user",
        ),
        name="get_credit_card_accounts_by_user",
        description=(
            "Get all credit card accounts for a user.\n\n"
            "Returns information about each credit card account including card type,\n"
            "date opened, current balance, and reward points."
        ),
    )
    context.register_tool(
        _tool_handler(log_verification, name="log_verification"),
        name="log_verification",
        description=(
            "Log a verification record after successfully verifying a user's "
            "identity.\n\n"
            "Call this tool after you have verified a user by confirming 2 out of "
            "4 identity fields\n"
            "(date of birth, email, phone number, address). This creates an audit "
            "record of the verification."
        ),
    )
    context.register_tool(
        _tool_handler(
            give_discoverable_user_tool,
            name="give_discoverable_user_tool",
        ),
        name="give_discoverable_user_tool",
        description=(
            "Pass a tool to the user so they can execute it themselves.\n\n"
            "Use this when the knowledge base indicates that the user should "
            "perform\n"
            'an action themselves (e.g., "to do X, have the user '
            'call tool_name(args)").\n\n'
            "The user will then be able to call `call_discoverable_tool` with "
            "the same\n"
            "tool name and arguments to simulate executing the action."
        ),
    )
    context.register_tool(
        _tool_handler(
            unlock_discoverable_agent_tool,
            name="unlock_discoverable_agent_tool",
        ),
        name="unlock_discoverable_agent_tool",
        description=(
            "Unlock an agent discoverable tool that was found in the knowledge "
            "base.\n\n"
            "Use this when the knowledge base indicates that you have access to a "
            "specialized\ninternal tool. The knowledge base will tell you the tool "
            "name to unlock.\n\n"
            "After unlocking, you can use the tool by calling "
            "`call_discoverable_agent_tool` with\nthe tool name and required arguments."
        ),
    )
    context.register_tool(
        _tool_handler(
            call_discoverable_agent_tool,
            name="call_discoverable_agent_tool",
        ),
        name="call_discoverable_agent_tool",
        description=(
            "Call an agent discoverable tool that you have previously unlocked.\n\n"
            "Use this after unlocking a tool with `unlock_discoverable_agent_tool`. "
            "The knowledge base\nwill tell you which tool to use and what arguments "
            "to provide."
        ),
    )
    context.register_tool(
        _tool_handler(
            list_discoverable_agent_tools,
            name="list_discoverable_agent_tools",
        ),
        name="list_discoverable_agent_tools",
        description=(
            "List all agent discoverable tools that you have called.\n\n"
            "Use this to see what specialized tools you have used."
        ),
    )


def _tool_handler(
    local_handler: Callable[..., ToolOutput],
    *,
    name: str,
) -> Callable[..., ToolOutput | Awaitable[ToolOutput]]:
    """评测时选择 Tau2 Bridge，并保留本地函数的完整 Tool schema。"""
    socket_value = os.environ.get("TAU2_TOOL_BRIDGE_SOCKET")
    if socket_value is None:
        return local_handler
    client = Tau2ToolClient(Path(socket_value))
    signature = inspect.signature(local_handler)

    @wraps(local_handler)
    async def remote_handler(*args: object, **kwargs: object) -> ToolOutput:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return await client.execute(name, dict(bound.arguments))

    return cast(Callable[..., Awaitable[ToolOutput]], remote_handler)


def kb_search(
    query: Annotated[
        str,
        Field(description="The search query to find relevant documents"),
    ],
) -> ToolOutput:
    """Search the knowledge base for relevant documents."""
    kb = KnowledgeService.get_instance()
    content = kb.search(query=query)
    return ToolOutput(content=content)


def grep(
    pattern: Annotated[
        str,
        Field(
            description=(
                "The regex pattern to search for (e.g., 'credit.*card', 'fee|charge')"
            )
        ),
    ],
) -> ToolOutput:
    """Search for a regex pattern in all knowledge base documents."""
    kb = KnowledgeService.get_instance()
    content = kb.grep(pattern=pattern)
    return ToolOutput(content=content)


def transfer_to_human_agents(
    summary: Annotated[
        str,
        Field(
            description=(
                "A summary of the user's issue and what was attempted before transfer."
            )
        ),
    ],
    reason: Annotated[
        TransferReason,
        Field(description="The specific reason code for the transfer."),
    ] = "other",
) -> ToolOutput:
    """Transfer the user to a human agent."""
    db = BankingDBService.get_instance()
    content = db.transfer_to_human_agents(summary=summary, reason=str(reason))
    return ToolOutput(content=content)


def get_current_time() -> ToolOutput:
    """Get the current time."""
    return ToolOutput(content="The current time is 2025-11-14 03:40:00 EST.")


def get_user_information_by_id(
    user_id: Annotated[str, Field(description="The ID of the user")],
) -> ToolOutput:
    """Get user information by user id."""
    db = BankingDBService.get_instance()
    content = db.get_user_by_id(user_id=user_id)
    return ToolOutput(content=content)


def get_user_information_by_name(
    customer_name: Annotated[str, Field(description="The name of the user")],
) -> ToolOutput:
    """Get user information by customer full name."""
    db = BankingDBService.get_instance()
    content = db.get_user_by_name(customer_name=customer_name)
    return ToolOutput(content=content)


def get_user_information_by_email(
    email: Annotated[str, Field(description="The email of the user")],
) -> ToolOutput:
    """Get user information by email."""
    db = BankingDBService.get_instance()
    content = db.get_user_by_email(email=email)
    return ToolOutput(content=content)


def change_user_email(
    user_id: Annotated[
        str,
        Field(description="The ID of the user whose email should be changed"),
    ],
    new_email: Annotated[
        str,
        Field(description="The new email address to set for the user"),
    ],
) -> ToolOutput:
    """Change the email address for a user."""
    db = BankingDBService.get_instance()
    content = db.change_user_email(user_id=user_id, new_email=new_email)
    return _expected_output(content, error_code="banking_user_not_found")


def get_referrals_by_user(
    user_id: Annotated[
        str,
        Field(description="The ID of the user (referrer) to look up referrals for"),
    ],
) -> ToolOutput:
    """Get all referrals made by a user."""
    db = BankingDBService.get_instance()
    content = db.get_referrals_by_user(user_id=user_id)
    return ToolOutput(content=content)


def get_credit_card_transactions_by_user(
    user_id: Annotated[
        str,
        Field(description="The ID of the user to look up transactions for"),
    ],
) -> ToolOutput:
    """Get all credit card transactions for a user."""
    db = BankingDBService.get_instance()
    content = db.get_credit_card_transactions_by_user(user_id=user_id)
    return ToolOutput(content=content)


def get_credit_card_accounts_by_user(
    user_id: Annotated[
        str,
        Field(description="The ID of the user to look up credit card accounts for"),
    ],
) -> ToolOutput:
    """Get all credit card accounts for a user."""
    db = BankingDBService.get_instance()
    content = db.get_credit_card_accounts_by_user(user_id=user_id)
    return ToolOutput(content=content)


def log_verification(
    name: Annotated[str, Field(description="The verified user's full name")],
    user_id: Annotated[str, Field(description="The verified user's ID")],
    address: Annotated[str, Field(description="The verified user's address")],
    email: Annotated[str, Field(description="The verified user's email")],
    phone_number: Annotated[
        str,
        Field(description="The verified user's phone number"),
    ],
    date_of_birth: Annotated[
        str,
        Field(description="The verified user's date of birth (MM/DD/YYYY format)"),
    ],
    time_verified: Annotated[
        str,
        Field(
            description=(
                'The timestamp of the verification (e.g., "2025-11-14 03:40:00 EST")'
            )
        ),
    ],
) -> ToolOutput:
    """Log a verification record after successfully verifying a user's identity."""
    db = BankingDBService.get_instance()
    content = db.log_verification(
        name=name,
        user_id=user_id,
        address=address,
        email=email,
        phone_number=phone_number,
        date_of_birth=date_of_birth,
        time_verified=time_verified,
    )
    return ToolOutput(content=content)


def give_discoverable_user_tool(
    discoverable_tool_name: Annotated[
        str,
        Field(
            description=(
                'The name of the discoverable tool (e.g., "open_webpage", '
                '"navigate_to_section")'
            )
        ),
    ],
    arguments: Annotated[
        str,
        Field(
            description=(
                "JSON string of arguments for the tool (e.g., "
                '\'{"url": "https://example.com"}\')'
            )
        ),
    ] = "{}",
) -> ToolOutput:
    """Pass a tool to the user so they can execute it themselves."""
    db = BankingDBService.get_instance()
    content = db.give_discoverable_user_tool(
        discoverable_tool_name=discoverable_tool_name,
        arguments=arguments,
    )
    return _expected_output(
        content,
        error_code="invalid_discoverable_tool_arguments",
        error_message="Invalid JSON in arguments",
    )


def unlock_discoverable_agent_tool(
    agent_tool_name: Annotated[
        str,
        Field(
            description=(
                "The name of the agent discoverable tool to unlock\n"
                '(e.g., "calculate_apr_adjustment_7842")'
            )
        ),
    ],
) -> ToolOutput:
    """Unlock an agent discoverable tool that was found in the knowledge base."""
    db = BankingDBService.get_instance()
    content = db.unlock_discoverable_agent_tool(agent_tool_name=agent_tool_name)
    return ToolOutput(content=content)


def call_discoverable_agent_tool(
    agent_tool_name: Annotated[
        str,
        Field(description="The name of the agent discoverable tool to call"),
    ],
    arguments: Annotated[
        str,
        Field(
            description=(
                "JSON string of arguments for the tool (e.g., "
                '\'{"user_id": "abc123"}\')'
            )
        ),
    ] = "{}",
) -> ToolOutput:
    """Call an agent discoverable tool that you have previously unlocked."""
    db = BankingDBService.get_instance()
    content = db.call_discoverable_agent_tool(
        agent_tool_name=agent_tool_name,
        arguments=arguments,
    )
    if content.startswith("Error: Tool "):
        return _expected_output(
            content,
            error_code="discoverable_tool_not_unlocked",
        )
    return _expected_output(
        content,
        error_code="invalid_discoverable_tool_arguments",
        error_message="Invalid JSON in arguments",
    )


def list_discoverable_agent_tools() -> ToolOutput:
    """List all agent discoverable tools that you have called."""
    db = BankingDBService.get_instance()
    content = db.list_discoverable_agent_tools()
    return ToolOutput(content=content)


def _expected_output(
    content: str,
    *,
    error_code: str,
    error_message: str | None = None,
) -> ToolOutput:
    """保留 Tau 以普通 ToolResult 表达的业务失败。"""
    del error_code, error_message
    return ToolOutput(content=content)
