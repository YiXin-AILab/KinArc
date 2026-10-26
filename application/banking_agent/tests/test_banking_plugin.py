"""Banking 顶层工具与 Tau3 协议兼容性测试。"""

from application.banking_agent.plugins import banking_plugin


def test_get_current_time_uses_tau3_benchmark_clock() -> None:
    """评测 Agent 必须使用 Tau3 的固定时钟。"""
    result = banking_plugin.get_current_time()

    assert result.content == "The current time is 2025-11-14 03:40:00 EST."


def test_expected_business_failure_remains_successful_tool_result() -> None:
    """Tau 以 Error 文本表达业务失败，而不是 ToolMessage.error。"""
    result = banking_plugin._expected_output(  # noqa: SLF001
        "Error: Invalid arguments",
        error_code="unused",
    )

    assert result.content == "Error: Invalid arguments"
