"""Banking Agent 测试夹具。"""

from pathlib import Path

import pytest

from application.banking_agent.services.banking_db_service import BankingDBService


@pytest.fixture
def banking_service() -> BankingDBService:
    """用测试自有的最小账户数据构建服务，部署 DB 不参与行为测试。"""
    test_root = Path(__file__).resolve().parent
    return BankingDBService(
        db_path=test_root / "fixtures" / "db.json",
        discoverable_tools_path=test_root.parent / "data" / "discoverable_tools.json",
    )
