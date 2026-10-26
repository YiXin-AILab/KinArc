"""对话列表、人工改名和生成标题的 HTTP 合约。"""

from fastapi.testclient import TestClient
from server.app import create_api_app
from tests.unit.service.test_session_catalog import CatalogMessages, service


def test_catalog_endpoints_and_default_title() -> None:
    app = service(CatalogMessages())
    with TestClient(create_api_app(app)) as client:
        scope = {"app_id": "app", "user_id": "user", "agent_id": "agent"}
        submitted = client.post(
            "/v1/sessions/session/messages",
            json={
                **scope,
                "message_id": "m",
                "content": [{"type": "text", "text": "中" * 17}],
            },
        )
        assert submitted.status_code == 202
        response = client.get("/v1/sessions", params=scope)
        assert response.status_code == 200
        item = response.json()["sessions"][0]
        assert item["session_id"] == "session" and item["title"] == "中" * 16 + "..."
        assert set(item) == {
            "app_id",
            "user_id",
            "agent_id",
            "session_id",
            "title",
            "title_version",
            "created_at",
            "updated_at",
        }
        renamed = client.post(
            "/v1/sessions/session", json={**scope, "title": " 新标题 "}
        )
        assert renamed.status_code == 200 and renamed.json()["title"] == "新标题"
        generated = client.post("/v1/sessions/session/title/generate", json=scope)
        assert generated.status_code == 200 and generated.json()["title"] == "生成标题"
        assert (
            client.post("/v1/sessions/session/title/optimize", json=scope).status_code
            == 404
        )
        assert (
            client.get("/v1/sessions", params={**scope, "user_id": "other"}).json()[
                "sessions"
            ]
            == []
        )
        assert (
            client.post(
                "/v1/sessions/session", json={**scope, "user_id": "other", "title": "x"}
            ).status_code
            == 404
        )


def test_invalid_inputs_and_missing_session() -> None:
    with TestClient(create_api_app(service(CatalogMessages()))) as client:
        scope = {"app_id": "app", "user_id": "user", "agent_id": "agent"}
        assert (
            client.post(
                "/v1/sessions/session/delete",
                json={**scope, "hard_delete": "true"},
            ).status_code
            == 422
        )
        for title in ("", "   ", "x" * 201):
            assert (
                client.post(
                    "/v1/sessions/session", json={**scope, "title": title}
                ).status_code
                == 422
            )
        assert (
            client.post(
                "/v1/sessions/session", json={**scope, "title": "x", "extra": True}
            ).status_code
            == 422
        )
        assert client.get("/v1/sessions", params={"app_id": "app"}).status_code == 422
        assert (
            client.get("/v1/sessions", params={**scope, "limit": 101}).status_code
            == 422
        )
        assert (
            client.get("/v1/sessions", params={**scope, "cursor": "bad"}).status_code
            == 400
        )
        assert (
            client.post("/v1/sessions/session/title/generate", json=scope).status_code
            == 404
        )
