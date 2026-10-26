"""Todo HTTP 查询只依赖清单内容，不查询会话。"""

from unittest.mock import AsyncMock

from agentsystem.common.errors import AdapterError
from agentsystem.service.application import ApplicationService
from agentsystem.todo.local.store import LocalTodoStore
from agentsystem.todo.models import TodoItem
from agentsystem.todo.service import TodoService
from fastapi.testclient import TestClient
from server.app import create_api_app
from tests.unit.todo.test_store import CTX


def service(todos: TodoService) -> ApplicationService:
    """未使用端口均以会失败的替身验证没有会话查询。"""
    unavailable = AsyncMock(side_effect=AssertionError("must not query sessions"))
    return ApplicationService(
        inbox=unavailable,
        states=unavailable,
        agents=unavailable,
        asset_service=unavailable,
        messages=unavailable,
        event_bus=unavailable,
        cancellation_bus=unavailable,
        todo_service=todos,
    )


async def test_read_without_session_and_isolation() -> None:
    todos = TodoService(LocalTodoStore())
    app = create_api_app(service(todos))
    client = TestClient(app)
    params = {"app_id": "a", "user_id": "u", "agent_id": "g"}
    url = "/v1/sessions/s/todos"
    assert client.get(url, params=params).json() == {"todos": []}
    await todos.replace(CTX, (TodoItem(content="same", status="in_progress"),) * 2)
    response = client.get(url, params=params)
    assert response.status_code == 200
    assert len(response.json()["todos"]) == 2
    assert client.get(url, params={**params, "user_id": "other"}).json() == {
        "todos": []
    }
    await todos.replace(CTX, ())
    assert client.get(url, params=params).json() == {"todos": []}
    assert client.get(url, params={**params, "app_id": "x" * 192}).status_code == 422


def test_storage_error_not_empty() -> None:
    todos = AsyncMock(spec=TodoService)
    todos.read.side_effect = AdapterError(operation="todo.read")
    client = TestClient(create_api_app(service(todos)))
    response = client.get(
        "/v1/sessions/s/todos", params={"app_id": "a", "user_id": "u", "agent_id": "g"}
    )
    assert response.status_code == 503
    assert "todos" not in response.json()
