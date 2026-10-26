"""测试会话级约束：阻断 LiteLLM import 时隐式加载仓库 .env。

LiteLLM 在 ``LITELLM_MODE`` 缺省（DEV）时，import 即调用
``load_dotenv()``，把仓库根目录的 .env 写进进程环境。pytest 收集阶段
任何 import litellm 的测试模块都会触发该副作用，使本应显式 opt-in 的
live 测试（如 OTEL_ENDPOINT、TEST_AGENT_LLM_API_KEY 门控的集成测试）
被 .env 中的部署值意外激活，违反"测试不得隐式读取仓库 .env"的约定。

pyproject.toml 通过 ``-p tests.pytest_bootstrap`` 在收集测试前加载本插件，把
``LITELLM_MODE`` 缺省为 PRODUCTION 可阻断上述行为；库用法下
``litellm_mode`` 仅 Proxy 使用，对测试无其他影响。显式导出
``LITELLM_MODE`` 的环境（如部署）不受 setdefault 影响。
"""

import os

os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
