"""LocalScheduleManager 行为合约。"""

import pytest
from agentsystem.schedule.local.manager import LocalScheduleManager
from agentsystem.schedule.manager import ScheduleManager
from agentsystem.schedule.scheduler import CronSchedule
from tests.contract.schedule_manager_contract import ScheduleManagerContract


class TestLocalScheduleManager(ScheduleManagerContract):
    """在进程内权威存储上运行共享合约。"""

    @pytest.fixture
    def schedule_manager(self) -> ScheduleManager:
        """为每个测试创建独立的 Local manager。"""
        return LocalScheduleManager(cron=CronSchedule("Asia/Shanghai"))
