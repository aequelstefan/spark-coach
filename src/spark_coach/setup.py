# Voice setup & weekly refresh for spark-coach
import datetime as dt
import json
import os

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def _data_dir() -> str:
    root = os.path.dirname(os.path.abspath(__file__))
    # Data directory lives at repo_root/data; from src/spark_coach that's ../../data
    data_dir = os.path.abspath(os.path.join(root, "..", "..", "data"))
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _voice_path() -> str:
    return os.path.join(_data_dir(), "voice_profile.json")


def slack_client() -> WebClient:
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing SLACK_BOT_TOKEN for setup interview")
    return WebClient(token=token)


def slack_post(channel: str, text: str, *, thread_ts: str | None = None) -> tuple[str, str]:
    client = slack_client()
    try:
        resp = client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
        return resp["channel"], resp["ts"]
    except SlackApiError as e:
        raise RuntimeError(f"Slack post failed during setup: {e.response['error']}") from e


def slack_thread_replies(channel: str, thread_ts: str, limit: int = 100) -> list[dict]:
    client = slack_client()
    resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
    msgs = list(resp.get("messages", []))
    # exclude parent
    return msgs[1:] if msgs and msgs[0].get("ts") == thread_ts else msgs


QUESTIONS = [
    ("product", "Q1: What's your product in one sentence?"),
    ("recent_work", "Q2: What did you ship this week?"),
    ("contrarian_view", "Q3: What's your contrarian insight that others don't get?"),
    ("style", "Q4: Describe your writing style (or name someone you sound like)"),
    ("example_tweets", "Q5: Paste 3-5 of your best tweets that felt right"),
    ("blocklist", "Q6: What should I NEVER write about? (books, politics, personal life, etc)"),
    (
        "banned_words",
        "Q7: What words/phrases should I avoid? (optimize, leverage, synergy, first principles)",
    ),
    ("goal", "Q8: What's your growth goal? (X followers in Y days)"),
]

_QNUM_TO_KEY = {f"q{i}": key for i, (key, _t) in enumerate(QUESTIONS, start=1)}


def run_setup_interview(channel_env: str = "SLACK_CHANNEL_ID") -> None:
    channel = os.getenv(channel_env)
    if not channel:
        raise RuntimeError("Missing SLACK_CHANNEL_ID for setup interview")

    header = (
        "[coach] Voice calibration interview (8 questions)\n"
        "Reply to this thread with your answers. You can answer one-by-one or paste all at once using Q1:/Q2:/... prefixes."
    )
    ch, ts = slack_post(channel, header)

    answers: dict[str, object] = {}
    # Post all questions once
    for _key, q in QUESTIONS:
        slack_post(channel, q, thread_ts=ts)

    # Wait up to ~10 minutes for answers (poll every 10s)
    import time

    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)

    def _parse_block(text: str) -> None:
        import re as _re

        blob = text.strip()
        if not blob:
            return
        # Parse Qn: sections if present (multi-answer paste)
        pattern = _re.compile(r"(?im)^q(\d)\s*:\s*(.*?)(?=^q\d\s*:|\Z)", _re.DOTALL)
        found = list(pattern.finditer(blob))
        if found:
            for m in found:
                idx = int(m.group(1))
                val = m.group(2).strip()
                key = QUESTIONS[idx - 1][0] if 1 <= idx <= len(QUESTIONS) else None
                if not key:
                    continue
                if key == "example_tweets":
                    parts: list[str] = []
                    for _ln in val.splitlines():
                        t = _ln.lstrip().lstrip("-").lstrip("•").strip()
                        if t:
                            parts.append(t)
                    answers[key] = parts
                elif key in ("blocklist", "banned_words"):
                    parts = [p.strip() for p in val.replace(",", "\n").splitlines() if p.strip()]
                    answers[key] = parts
                else:
                    answers[key] = val
            return
        # Otherwise try to detect single Qn: prefix
        for idx, (key, _q) in enumerate(QUESTIONS, start=1):
            tag = f"q{idx}"
            if blob.lower().startswith(f"{tag}:") or blob.lower().startswith(f"{tag} "):
                val = blob.split(":", 1)[1].strip() if ":" in blob else blob[len(tag) :].strip()
                if key == "example_tweets":
                    parts: list[str] = []
                    for _ln in val.splitlines():
                        t = _ln.lstrip().lstrip("-").lstrip("•").strip()
                        if t:
                            parts.append(t)
                    answers[key] = parts
                elif key in ("blocklist", "banned_words"):
                    parts = [p.strip() for p in val.replace(",", "\n").splitlines() if p.strip()]
                    answers[key] = parts
                else:
                    answers[key] = val
                return

    while dt.datetime.now(dt.timezone.utc) < deadline:
        replies = slack_thread_replies(channel, ts, limit=200)
        for r in replies:
            text = (r.get("text") or "").strip()
            if not text:
                continue
            _parse_block(text)
        # Check if we have all required core fields
        required = ["product", "recent_work", "contrarian_view", "style"]
        if all(k in answers and answers[k] for k in required):
            break
        time.sleep(10)

    # Build profile with sensible defaults
    now = dt.datetime.now(dt.timezone.utc).date().isoformat()
    profile = {
        "name": os.getenv("PROFILE_NAME", "Stefan"),
        "handle": os.getenv("PROFILE_HANDLE", "thestefanl"),
        "product": answers.get("product", ""),
        "recent_work": answers.get("recent_work", ""),
        "contrarian_view": answers.get("contrarian_view", ""),
        "style": answers.get("style", ""),
        "example_tweets": answers.get("example_tweets", []),
        "examples": answers.get("example_tweets", []),
        "blocklist": answers.get("blocklist", []),
        "banned_words": answers.get("banned_words", []),
        "goal": answers.get("goal", ""),
        "created_at": now,
        "updated_at": now,
    }

    # Validation feedback
    missing = [k for k in ["product", "recent_work", "style"] if not profile.get(k)]
    if missing:
        slack_post(
            channel,
            f"⚠️ Missing fields: {', '.join(missing)}. You can reply here with Q1:/Q2: etc to add them anytime.",
            thread_ts=ts,
        )

    with open(_voice_path(), "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    # Confirmation summary
    summary_lines = [
        "✅ VOICE CALIBRATION SAVED",
        f"Product: {profile.get('product') or '(missing)'}",
        f"Recent work: {profile.get('recent_work') or '(missing)'}",
        f"Style: {profile.get('style') or '(missing)'}",
        f"Examples: {len(profile.get('example_tweets', []))} tweets",
        f"Blocklist: {', '.join(profile.get('blocklist', [])) or '(none)'}",
        f"Banned: {', '.join(profile.get('banned_words', [])) or '(none)'}",
        f"Goal: {profile.get('goal') or '(missing)'}",
    ]
    slack_post(channel, "\n".join(summary_lines), thread_ts=ts)


def run_weekly_refresh_prompt() -> None:
    """Ask user to provide weekly_context in Slack and update the voice profile."""
    channel = os.getenv("SLACK_CHANNEL_ID")
    if not channel:
        raise RuntimeError("Missing SLACK_CHANNEL_ID for weekly refresh")

    ch, ts = slack_post(
        channel,
        """
🔄 WEEKLY REFRESH (2 min)

What did you ship/learn this week? 2–3 bullets preferred.
Examples:
- Shipped AI personalization (40% faster onboarding)
- 5 user interviews on retention
- New positioning test for guilt-free giving
""".strip(),
    )
    import time

    # Wait up to 10 minutes for a reply
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
    weekly_context = None
    while dt.datetime.now(dt.timezone.utc) < deadline:
        replies = slack_thread_replies(channel, ts, limit=50)
        text = "\n".join([r.get("text") or "" for r in replies]).strip()
        if text:
            weekly_context = text
            break
        time.sleep(20)

    # Update profile
    try:
        with open(_voice_path(), encoding="utf-8") as f:
            profile = json.load(f)
    except Exception:
        profile = {}
    if weekly_context:
        profile["weekly_context"] = weekly_context
        profile["weekly_context_updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
    profile["updated_at"] = dt.datetime.now(dt.timezone.utc).date().isoformat()
    with open(_voice_path(), "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    slack_post(
        channel,
        (
            "✅ Weekly context updated."
            if weekly_context
            else "⚠️ No response; using last week’s context."
        ),
        thread_ts=ts,
    )

    slack_post(channel, "[coach] Weekly context updated.", thread_ts=ts)
