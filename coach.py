#!/usr/bin/env python3
import argparse
import datetime as dt
import os
import sys
from typing import Iterable, List, Optional, Tuple

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

try:
    from anthropic import Anthropic
except Exception:  # pragma: no cover - optional at runtime
    Anthropic = None  # type: ignore

try:
    import tweepy
except Exception:  # pragma: no cover
    tweepy = None  # type: ignore

# ---- Config helpers ----

TZ = dt.timezone(dt.timedelta(hours=1))  # CET (simplified; ignores DST)
COACH_TAG = "[coach]"
ROBOT_REACTION = "robot_face"
THUMBS_UP = "+1"


def env_required(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env: {name}")
    return val


# ---- Claude content generation ----


def generate_suggestions() -> str:
    """Call Claude to generate today's tweets + reply opportunities."""
    api_key = env_required("ANTHROPIC_API_KEY")
    if Anthropic is None:
        raise RuntimeError("anthropic package is not installed")
    client = Anthropic(api_key=api_key)

    prompt = (
        "Generate 3 high-signal tweets I should post today and 3 reply opportunities "
        "(account mentions + suggested reply). Output as a concise list with bullets."
    )
    msg = client.messages.create(
        model="claude-3-5-sonnet-latest",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    # anthropic python SDK returns content as list of blocks; take plain text
    content_parts = []
    for part in msg.content:
        # each part may be TextBlock
        text = getattr(part, "text", None) or getattr(part, "content", None)
        if isinstance(text, str):
            content_parts.append(text)
    text = "\n".join(content_parts).strip()
    return text or "- (no suggestions returned)"


# ---- Slack helpers ----


def slack_client() -> WebClient:
    return WebClient(token=env_required("SLACK_BOT_TOKEN"))


def slack_post(channel: str, text: str) -> Tuple[str, str]:
    """Post a message; returns (channel, ts)."""
    client = slack_client()
    try:
        resp = client.chat_postMessage(channel=channel, text=text)
        return resp["channel"], resp["ts"]
    except SlackApiError as e:
        raise RuntimeError(f"Slack post failed: {e.response['error']}") from e


def slack_add_reaction(channel: str, ts: str, name: str) -> None:
    client = slack_client()
    client.reactions_add(channel=channel, timestamp=ts, name=name)


def slack_history(channel: str, oldest_ts: Optional[float] = None, limit: int = 100) -> List[dict]:
    client = slack_client()
    resp = client.conversations_history(channel=channel, limit=limit, oldest=oldest_ts)
    return list(resp.get("messages", []))


# ---- X (Twitter) ----


def twitter_api():
    if tweepy is None:
        raise RuntimeError("tweepy package is not installed")
    api_key = env_required("TWITTER_API_KEY")
    api_secret = env_required("TWITTER_API_SECRET")
    access_token = env_required("TWITTER_ACCESS_TOKEN")
    access_secret = env_required("TWITTER_ACCESS_SECRET")
    auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
    return tweepy.API(auth)


def post_to_x(text: str) -> None:
    api = twitter_api()
    api.update_status(status=text[:280])


# ---- Creator Map monitor (stub) ----


def monitor_creator_map() -> List[str]:
    """Return urgent alerts (strings). Stub for now; integrate data source later."""
    return []


# ---- Main flows ----


def run_suggest_and_monitor() -> None:
    channel = env_required("SLACK_CHANNEL_ID")

    today = dt.datetime.now(TZ).strftime("%Y-%m-%d")
    suggestions = generate_suggestions()
    header = f"{COACH_TAG} Suggestions for {today}\nReact with :+1: to auto-post"
    text = f"{header}\n\n{suggestions}"

    ch, ts = slack_post(channel, text)
    print(f"Posted suggestions to Slack at ts={ts}")

    # Monitor Creator Map and send urgent alerts (inline)
    alerts = monitor_creator_map()
    for a in alerts:
        slack_post(channel, f"{COACH_TAG} URGENT: {a}")

    # Scan recent messages for :+1: and auto-post to X if not yet processed
    oldest = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).timestamp()
    messages = slack_history(channel, oldest_ts=oldest, limit=200)
    for m in messages:
        text = m.get("text", "") or ""
        if COACH_TAG not in text:
            continue
        ts = m.get("ts")
        if not ts:
            continue
        reactions = {r.get("name"): r.get("count", 0) for r in m.get("reactions", [])}
        # Skip if already handled
        if reactions.get(ROBOT_REACTION, 0) > 0:
            continue
        # Post if any +1
        if reactions.get(THUMBS_UP, 0) > 0:
            # Choose first bullet as the tweet (super simple)
            lines = []
            for ln in text.splitlines():
                stripped = ln.lstrip("- ")
                stripped = stripped.lstrip("• ")
                stripped = stripped.lstrip("\t ")
                if ln.strip().startswith(("-", "•")) and stripped:
                    lines.append(stripped)
            tweet = lines[0] if lines else text[:240]
            try:
                post_to_x(tweet)
                slack_add_reaction(channel, ts, ROBOT_REACTION)
                slack_post(channel, f"{COACH_TAG} Posted to X for ts={ts}")
                print(f"Posted to X for Slack ts={ts}")
            except Exception as e:  # keep simple
                slack_post(channel, f"{COACH_TAG} Error posting to X for ts={ts}: {e}")
                print(f"Error posting to X: {e}", file=sys.stderr)


def run_afternoon_bip() -> None:
    channel = env_required("SLACK_CHANNEL_ID")
    text = (
        f"{COACH_TAG} Build-in-public prompts for today\n"
        "- Share progress update on current feature\n"
        "- Show a behind-the-scenes screenshot\n"
        "- Ask for feedback on a naming decision"
    )
    slack_post(channel, text)


def run_reply_engine() -> None:
    channel = env_required("SLACK_CHANNEL_ID")
    # Minimal placeholder: real impl would search X for targets
    text = (
        f"{COACH_TAG} Reply targets (5) with suggested drafts\n"
        "1) @target1: <draft>\n2) @target2: <draft>\n3) @target3: <draft>\n4) @target4: <draft>\n5) @target5: <draft>"
    )
    slack_post(channel, text)


def run_opportunity_scan() -> None:
    channel = env_required("SLACK_CHANNEL_ID")
    alerts = monitor_creator_map()
    for a in alerts:
        slack_post(channel, f"{COACH_TAG} Opportunity: {a}")


def run_follow_recs() -> None:
    channel = env_required("SLACK_CHANNEL_ID")
    slack_post(channel, f"{COACH_TAG} Follow/DM recommendations:\n- @example1\n- @example2")


def run_summary() -> None:
    channel = env_required("SLACK_CHANNEL_ID")
    oldest = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).timestamp()
    messages = slack_history(channel, oldest_ts=oldest, limit=200)

    suggestions = [
        m
        for m in messages
        if COACH_TAG in (m.get("text") or "") and "Suggestions" in (m.get("text") or "")
    ]
    posted = 0
    for m in messages:
        reactions = {r.get("name"): r.get("count", 0) for r in m.get("reactions", [])}
        if reactions.get(ROBOT_REACTION, 0) > 0:
            posted += 1

    text = (
        f"{COACH_TAG} Daily summary\n"
        f"Suggestions in last 24h: {len(suggestions)}\n"
        f"Auto-posted to X: {posted}\n"
        f"Notes: Add analytics integration to enhance summary."
    )
    slack_post(channel, text)


def run_weekly_brief() -> None:
    channel = env_required("SLACK_CHANNEL_ID")
    slack_post(channel, f"{COACH_TAG} Weekly strategy brief (stub)\n- Wins\n- Misses\n- Plan")


def run_learning_loop() -> None:
    # Placeholder: would aggregate selections and outcomes
    pass


def main(argv: Optional[Iterable[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--task",
        choices=["suggest", "afternoon", "scan", "summary", "weekly", "replies", "recs"],
        default="suggest",
    )
    args = p.parse_args(argv)

    if args.task == "suggest":
        run_suggest_and_monitor()
    elif args.task == "afternoon":
        run_afternoon_bip()
    elif args.task == "scan":
        run_opportunity_scan()
    elif args.task == "summary":
        run_summary()
    elif args.task == "weekly":
        run_weekly_brief()
    elif args.task == "replies":
        run_reply_engine()
    elif args.task == "recs":
        run_follow_recs()
    run_learning_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
