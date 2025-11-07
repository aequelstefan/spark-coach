import os

import httpx


async def send_webhook(
    text: str, webhook_url: str | None = None, **extra_payload: object
) -> dict[str, object]:
    """Send a message to Slack via Incoming Webhook.

    Args:
        text: Message text.
        webhook_url: Optional explicit webhook URL; falls back to env var SLACK_WEBHOOK_URL.
        **extra_payload: Optional extra fields (e.g., blocks, attachments).
    """
    url = webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        raise RuntimeError("SLACK_WEBHOOK_URL is not configured")

    payload: dict[str, object] = {"text": text}
    if extra_payload:
        payload.update(extra_payload)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
    resp.raise_for_status()
    return {"ok": True}
