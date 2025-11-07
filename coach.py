#!/usr/bin/env python3
import argparse
import datetime as dt
import os
import sys
from collections.abc import Iterable

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


def _choose_model(task: str) -> list[str]:
    # Allow manual override
    override = os.getenv("ANTHROPIC_MODEL")
    if override:
        return [override]

    # Defaults by task: Sonnet 3.5 for core content; Haiku 3.5 for light tasks; fallbacks included
    if task in {"suggest", "summary", "weekly"}:
        return [
            "claude-3-5-sonnet-20241022",  # Sonnet 3.5
            "claude-3-5-sonnet-latest",
            "claude-3-opus-latest",  # reasoning backup
            "claude-3-5-haiku-20241022",
            "claude-3-haiku-20240307",
        ]
    # lighter tasks
    return [
        "claude-3-5-haiku-20241022",
        "claude-3-haiku-20240307",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-sonnet-latest",
    ]


def _anthropic_complete(prompt: str, task: str) -> str:
    api_key = env_required("ANTHROPIC_API_KEY")
    if Anthropic is None:
        raise RuntimeError("anthropic package is not installed")
    client = Anthropic(api_key=api_key)

    candidate_models = _choose_model(task)
    last_err: Exception | None = None
    msg = None
    for model in candidate_models:
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            break
        except Exception as e:
            last_err = e
            continue
    if msg is None and last_err is not None:
        raise last_err

    content_parts: list[str] = []
    for part in msg.content:  # type: ignore[union-attr]
        text = getattr(part, "text", None) or getattr(part, "content", None)
        if isinstance(text, str):
            content_parts.append(text)
    return "\n".join(content_parts).strip()


def generate_suggestions() -> str:
    """Call Claude to generate today's tweets + reply opportunities."""
    prompt = (
        "Generate 3 high-signal tweets I should post today and 3 reply opportunities "
        "(account mentions + suggested reply). Output as a concise list with bullets."
    )
    text = _anthropic_complete(prompt, task="suggest")
    return text or "- (no suggestions returned)"


# ---- Slack helpers ----


def slack_client() -> WebClient:
    return WebClient(token=env_required("SLACK_BOT_TOKEN"))


def slack_post(channel: str, text: str, *, thread_ts: str | None = None) -> tuple[str, str]:
    """Post a message; returns (channel, ts)."""
    client = slack_client()
    try:
        resp = client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
        return resp["channel"], resp["ts"]
    except SlackApiError as e:
        raise RuntimeError(f"Slack post failed: {e.response['error']}") from e


def slack_add_reaction(channel: str, ts: str, name: str) -> None:
    client = slack_client()
    client.reactions_add(channel=channel, timestamp=ts, name=name)


def slack_history(channel: str, oldest_ts: float | None = None, limit: int = 100) -> list[dict]:
    client = slack_client()
    resp = client.conversations_history(channel=channel, limit=limit, oldest=oldest_ts)
    return list(resp.get("messages", []))


def slack_thread_replies(channel: str, thread_ts: str, limit: int = 100) -> list[dict]:
    client = slack_client()
    resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
    # First element is the parent; include replies only
    msgs = list(resp.get("messages", []))
    if msgs and msgs[0].get("ts") == thread_ts:
        return msgs[1:]
    return msgs


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


def monitor_creator_map() -> list[str]:
    """Return urgent alerts (strings). Stub for now; integrate data source later."""
    return []


# ---- Main flows ----


def _extract_tweets_from_suggestions(s: str) -> list[str]:
    lines = [ln.rstrip() for ln in s.splitlines()]
    tweets: list[str] = []
    in_tweets = False
    for ln in lines:
        if ln.strip().lower().startswith("tweets"):
            in_tweets = True
            continue
        if in_tweets and ln.strip().lower().startswith("reply"):
            break
        if in_tweets and ln.strip().startswith(("-", "•")):
            stripped = ln.lstrip("- ").lstrip("• ").strip()
            if stripped:
                tweets.append(stripped)
    # Fallback: if none parsed, take first three bullet-like lines anywhere
    if not tweets:
        for ln in lines:
            if ln.strip().startswith(("-", "•")):
                stripped = ln.lstrip("- ").lstrip("• ").strip()
                if stripped:
                    tweets.append(stripped)
            if len(tweets) >= 3:
                break
    return tweets[:5]


def run_suggest_and_monitor() -> None:
    channel = env_required("SLACK_CHANNEL_ID")

    today = dt.datetime.now(TZ).strftime("%Y-%m-%d")
    suggestions = generate_suggestions()
    tweets = _extract_tweets_from_suggestions(suggestions)

    header = f"{COACH_TAG} Suggestions for {today}"
    ch, header_ts = slack_post(
        channel, f"{header}\nReply Opportunities below; react on a tweet to auto-post"
    )

    # Post each tweet as a thread reply so a 👍 applies to that specific option
    for idx, tw in enumerate(tweets, start=1):
        slack_post(
            channel,
            f"{COACH_TAG} Tweet {idx}: {tw}\nReact with :+1: to auto-post",
            thread_ts=header_ts,
        )

    print(f"Posted suggestions to Slack at ts={header_ts}")

    # Monitor Creator Map and send urgent alerts (inline)
    alerts = monitor_creator_map()
    for a in alerts:
        slack_post(channel, f"{COACH_TAG} URGENT: {a}")

    # Scan recent top-level messages for suggestion headers; then process thread replies
    oldest = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).timestamp()
    messages = slack_history(channel, oldest_ts=oldest, limit=200)
    for m in messages:
        parent_text = m.get("text") or ""
        parent_ts = m.get("ts")
        if not parent_ts:
            continue
        if COACH_TAG in parent_text and "Suggestions for" in parent_text:
            replies = slack_thread_replies(channel, parent_ts, limit=100)
            for r in replies:
                ts = r.get("ts")
                if not ts:
                    continue
                text = r.get("text") or ""
                if COACH_TAG not in text or "Tweet" not in text:
                    continue
                reactions = {rv.get("name"): rv.get("count", 0) for rv in r.get("reactions", [])}
                if reactions.get(ROBOT_REACTION, 0) > 0:
                    continue
                if reactions.get(THUMBS_UP, 0) > 0:
                    # Extract content after 'Tweet N:'
                    after = text.split(":", 1)
                    tweet = after[1].strip() if len(after) == 2 else text[:240]
                    try:
                        post_to_x(tweet)
                        slack_add_reaction(channel, ts, ROBOT_REACTION)
                        slack_post(channel, f"{COACH_TAG} Posted to X for ts={ts}")
                        print(f"Posted to X for Slack ts={ts}")
                    except Exception as e:
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


def main(argv: Iterable[str] | None = None) -> int:
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
