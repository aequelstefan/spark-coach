# Voice setup & weekly refresh for spark-coach
import datetime as dt
import json
import os

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def _data_dir() -> str:
    root = os.path.dirname(os.path.abspath(__file__))
    # coach.py stores data at repo_root/../../data relative to this file as well
    data_dir = os.path.abspath(os.path.join(root, "..", "..", "..", "data"))
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


def run_setup_interview(channel_env: str = "SLACK_CHANNEL_ID") -> None:
    channel = os.getenv(channel_env)
    if not channel:
        raise RuntimeError("Missing SLACK_CHANNEL_ID for setup interview")

    header = (
        "[coach] Voice calibration interview (8 questions)\n"
        "Reply to this thread with your answers. I will compile your voice profile."
    )
    ch, ts = slack_post(channel, header)

    answers: dict[str, object] = {}
    for _key, q in QUESTIONS:
        slack_post(channel, q, thread_ts=ts)

    # Wait up to ~10 minutes for answers (poll every 20s)
    import time

    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
    asked = {k for k, _ in QUESTIONS}
    while dt.datetime.now(dt.timezone.utc) < deadline and asked:
        replies = slack_thread_replies(channel, ts, limit=200)
        # parse latest answer for each key heuristically by prefix (Q1/Q2/etc) or by order
        for r in replies:
            text = (r.get("text") or "").strip()
            if not text:
                continue
            for idx, (key, _q) in enumerate(QUESTIONS, start=1):
                tag = f"q{idx}"  # allow lowercase q1, q2
                if text.lower().startswith(f"{tag}:") or text.lower().startswith(f"{tag} "):
                    val = text.split(":", 1)[1].strip() if ":" in text else text[len(tag) :].strip()
                    if key in ("example_tweets",):
                        # split by newlines or bullets
                        parts: list[str] = []
                        for _ln in val.splitlines():
                            t = _ln.lstrip()
                            t = t.lstrip("-")
                            t = t.lstrip("•")
                            t = t.lstrip()
                            s = t.strip()
                            if s:
                                parts.append(s)
                        answers[key] = parts
                    elif key in ("blocklist", "banned_words"):
                        parts = [
                            p.strip() for p in val.replace(",", "\n").splitlines() if p.strip()
                        ]
                        answers[key] = parts
                    else:
                        answers[key] = val
        # if still missing, also capture first N freeform replies in order
        missing = [k for k in asked if k not in answers]
        if not missing:
            break
        time.sleep(20)

    # Build profile
    now = dt.datetime.now(dt.timezone.utc).date().isoformat()
    profile = {
        "name": os.getenv("PROFILE_NAME", "Stefan"),
        "handle": os.getenv("PROFILE_HANDLE", "thestefanl"),
        "product": answers.get("product", ""),
        "recent_work": answers.get("recent_work", ""),
        "contrarian_view": answers.get("contrarian_view", ""),
        "style": answers.get("style", ""),
        "example_tweets": answers.get("example_tweets", []),
        "blocklist": answers.get("blocklist", []),
        "banned_words": answers.get("banned_words", []),
        "goal": answers.get("goal", ""),
        "created_at": now,
        "updated_at": now,
    }

    path = _voice_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    slack_post(channel, f"[coach] Voice profile saved to {path}.", thread_ts=ts)


def run_weekly_refresh_prompt() -> None:
    """Ask user to provide weekly_context in Slack and update the voice profile."""
    channel = os.getenv("SLACK_CHANNEL_ID")
    if not channel:
        raise RuntimeError("Missing SLACK_CHANNEL_ID for weekly refresh")

    ch, ts = slack_post(
        channel, "🔄 WEEKLY REFRESH (2 min): What did you ship this week? Reply with 2-3 bullets."
    )
    import time

    # Wait up to 10 minutes for a reply
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
    weekly_context = None
    while dt.datetime.now(dt.timezone.utc) < deadline:
        replies = slack_thread_replies(channel, ts, limit=50)
        if replies:
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
    profile["weekly_context"] = weekly_context or ""
    profile["updated_at"] = dt.datetime.now(dt.timezone.utc).date().isoformat()
    with open(_voice_path(), "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    slack_post(channel, "[coach] Weekly context updated.", thread_ts=ts)
