import respx
from fastapi.testclient import TestClient

from spark_coach.app import app


def test_slack_notify_success(monkeypatch):
    webhook_url = "https://hooks.slack.com/services/T000/B000/XXX"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", webhook_url)
    client = TestClient(app)

    with respx.mock(assert_all_called=True) as respx_mock:
        route = respx_mock.post(webhook_url).respond(200, json={"ok": True})
        resp = client.post("/v1/slack/notify", json={"text": "hello"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "sent"}
        assert route.called


def test_slack_notify_missing_webhook(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    client = TestClient(app)

    resp = client.post("/v1/slack/notify", json={"text": "hi"})
    assert resp.status_code == 400
