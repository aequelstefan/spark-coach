#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import os
import re
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

import json
from dataclasses import dataclass

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


# ---- Content style (CEO) ----
# Default style can be overridden by env ANTHROPIC_STYLE_CEO
_STYLE_CEO_DEFAULT = (
    "Voice: casual, immediate, like Sherry Jang—starts mid-thought; lowercase sometimes OK; no polish.\n"
    "Structure: jump right in ('just realized', 'shipped X today', 'honestly'); 2–4 short sentences; personal story or moment.\n"
    "Tone: honest, warm, real; show the messy bits; no corporate speak ever.\n"
    "Emoji: max 1, only ✨💡😅🧵; use sparingly.\n"
    "Closing: small insight or unfinished thought; invite replies naturally.\n"
    "NEVER use: 'strategic', 'optimize', 'leverage', 'trade-offs', 'first principles', 'synergy', formal business words.\n"
    "Examples: 'just realized...', 'shipped this yesterday...', 'honestly the hardest part...', 'tbh...'\n"
)


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
    style = os.getenv("ANTHROPIC_STYLE_CEO", _STYLE_CEO_DEFAULT)
    prompt = (
        "You are my social writing partner. Follow this style strictly:\n"
        + style
        + "\nGenerate content for today. Output EXACTLY two sections with bullets only:\n"
        "Tweets:\n"
        "- Three tweet options written in the above style (4–6 short lines each).\n"
        "Reply Opportunities:\n"
        "- Three brief targets (handle + one‑line why).\n"
        "Constraints: avoid hype words (e.g., 'first principles'), everyday language, one emoji max per tweet."
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


def twitter_api_v1():
    if tweepy is None:
        raise RuntimeError("tweepy package is not installed")
    api_key = env_required("TWITTER_API_KEY")
    api_secret = env_required("TWITTER_API_SECRET")
    access_token = env_required("TWITTER_ACCESS_TOKEN")
    access_secret = env_required("TWITTER_ACCESS_SECRET")
    auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
    return tweepy.API(auth)


def twitter_client_v2():
    if tweepy is None:
        raise RuntimeError("tweepy package is not installed")
    bearer = env_required("X_BEARER_TOKEN")
    api_key = env_required("TWITTER_API_KEY")
    api_secret = env_required("TWITTER_API_SECRET")
    access_token = env_required("TWITTER_ACCESS_TOKEN")
    access_secret = env_required("TWITTER_ACCESS_SECRET")
    return tweepy.Client(
        bearer_token=bearer,
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
        wait_on_rate_limit=True,
    )


def _ensure_data_dir() -> str:
    root = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.abspath(os.path.join(root, "..", "..", "data"))
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _text_features(text: str) -> dict[str, object]:
    return {
        "len": len(text),
        "has_numbers": bool(re.search(r"\d", text)),
        "asks_question": "?" in text,
        "emoji_count": len(re.findall(r"[\U0001F300-\U0001FAFF]", text)),
        "lines": len(text.splitlines()),
    }


def _log_event(event: dict[str, object]) -> None:
    data_dir = _ensure_data_dir()
    path = os.path.join(data_dir, "log.jsonl")
    event.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def post_to_x(text: str) -> str:
    api = twitter_api_v1()
    status = api.update_status(status=text[:280])
    tid = str(getattr(status, "id", ""))
    _log_event(
        {
            "type": "post",
            "channel": "CEO",
            "kind": "tweet",
            "tweet_id": tid,
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "features": _text_features(text),
        }
    )
    return tid


def reply_to_tweet(tweet_id: str, text: str, *, handle: str | None = None) -> str:
    client = twitter_client_v2()
    resp = client.create_tweet(text=text[:280], in_reply_to_tweet_id=tweet_id)
    rid = str(getattr(resp, "data", {}).get("id", ""))
    _log_event(
        {
            "type": "post",
            "channel": "CEO",
            "kind": "reply",
            "tweet_id": rid,
            "in_reply_to": tweet_id,
            "target_handle": handle,
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "features": _text_features(text),
        }
    )
    return rid


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


def _pick_theme() -> str:
    s = _load_budget_state()
    themes = s.get(
        "themes",
        {
            "metrics": 1,
            "build_in_public": 1,
            "positioning": 1,
            "technical": 1,
            "hot_take": 1,
        },
    )
    # epsilon-greedy: 25% explore
    import random

    if random.random() < 0.25:
        theme = random.choice(list(themes.keys()))
    else:
        theme = max(themes, key=lambda k: themes[k])
    s["themes"] = themes
    _save_budget_state(s)
    return theme


def run_suggest_and_monitor() -> None:
    channel = env_required("SLACK_CHANNEL_ID")

    today = dt.datetime.now(TZ).strftime("%Y-%m-%d")
    suggestions = generate_suggestions()
    tweets = _extract_tweets_from_suggestions(suggestions)

    if not tweets:
        print("No tweets generated, skipping post")
        return

    # Ensure we have exactly 3 tweets
    while len(tweets) < 3:
        tweets.append("[placeholder tweet]")
    tweets = tweets[:3]

    theme = _pick_theme()
    # Build single message with all options
    text = (
        f"{COACH_TAG} Suggestions for {today}\nTODAY'S THEME: {theme.replace('_',' ').title()}\n\n"
    )
    text += f"1️⃣ {tweets[0]}\n\n"
    text += f"2️⃣ {tweets[1]}\n\n"
    text += f"3️⃣ {tweets[2]}\n\n"
    text += "React: 1️⃣ 2️⃣ 3️⃣ to post now · ✏️ to edit · 👎 to skip"

    ch, header_ts = slack_post(channel, text)
    print(f"Posted suggestions to Slack at ts={header_ts}")

    # Add numbered reactions immediately
    try:
        slack_add_reaction(channel, header_ts, "one")
        slack_add_reaction(channel, header_ts, "two")
        slack_add_reaction(channel, header_ts, "three")
        slack_add_reaction(channel, header_ts, "pencil2")
        slack_add_reaction(channel, header_ts, "thumbsdown")
    except Exception as e:
        print(f"Could not add reactions: {e}", file=sys.stderr)

    # Monitor Creator Map and send urgent alerts (inline)
    alerts = monitor_creator_map()
    for a in alerts:
        slack_post(channel, f"{COACH_TAG} URGENT: {a}")

    # Scan recent top-level messages for suggestion headers with numbered reactions
    oldest = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).timestamp()
    messages = slack_history(channel, oldest_ts=oldest, limit=200)
    for m in messages:
        parent_text = m.get("text") or ""
        parent_ts = m.get("ts")
        if not parent_ts:
            continue
        if COACH_TAG not in parent_text or "Suggestions for" not in parent_text:
            continue
        # Check if already posted (robot reaction)
        reactions = {rv.get("name"): rv.get("count", 0) for rv in m.get("reactions", [])}
        if reactions.get(ROBOT_REACTION, 0) > 0:
            continue
        # Parse tweet options from message text
        lines = parent_text.splitlines()
        tweet_options = {}
        for line in lines:
            if line.strip().startswith("1️⃣ "):
                tweet_options[1] = line.split("1️⃣ ", 1)[1].strip()
            elif line.strip().startswith("2️⃣ "):
                tweet_options[2] = line.split("2️⃣ ", 1)[1].strip()
            elif line.strip().startswith("3️⃣ "):
                tweet_options[3] = line.split("3️⃣ ", 1)[1].strip()
        # Check which number was reacted
        selected = None
        if reactions.get("one", 0) > 0:
            selected = 1
        elif reactions.get("two", 0) > 0:
            selected = 2
        elif reactions.get("three", 0) > 0:
            selected = 3
        if selected and selected in tweet_options:
            tweet = tweet_options[selected]
            try:
                tid = post_to_x(tweet)
                slack_add_reaction(channel, parent_ts, ROBOT_REACTION)
                slack_post(
                    channel,
                    f"{COACH_TAG} Posted option {selected} to X (id={tid})",
                    thread_ts=parent_ts,
                )
                print(f"Posted option {selected} to X for Slack ts={parent_ts}")
            except Exception as e:
                slack_post(channel, f"{COACH_TAG} Error posting to X: {e}", thread_ts=parent_ts)
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


def _load_creators() -> dict[str, list[str]]:
    path = os.path.join(os.path.dirname(__file__), "..", "..", "creators.json")
    path = os.path.abspath(path)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"tier1": [], "tier2": [], "tier3": []}


@dataclass
class Opportunity:
    idx: int
    user: str
    followers: int
    tweeted_at: str
    summary: str
    why: str
    metrics: dict
    score: int
    tweet_id: str


def _score_opportunity(tier: int, metrics: dict, minutes_ago: float) -> int:
    likes = metrics.get("like_count", 0)
    rts = metrics.get("retweet_count", 0) + metrics.get("repost_count", 0)
    replies = metrics.get("reply_count", 0)
    base = likes * 1 + rts * 2 + replies * 3
    recency = max(0, 120 - minutes_ago)  # fresh boost
    tier_boost = 50 if tier == 1 else (20 if tier == 2 else 0)
    score = int(min(100, (base**0.5) + recency * 0.2 + tier_boost))
    return score


def _fetch_opportunities() -> list[tuple[Opportunity, int]]:
    creators = _load_creators()
    users_by_tier: list[tuple[int, list[str]]] = [
        (1, creators.get("tier1", [])),
        (2, creators.get("tier2", [])),
        (3, creators.get("tier3", [])),
    ]
    client = twitter_client_v2()
    # Resolve usernames to IDs
    usernames = [u for _, lst in users_by_tier for u in lst]
    if not usernames:
        return []
    users = client.get_users(usernames=usernames, user_fields=["public_metrics"]).data or []
    id_by_username = {u.username.lower(): u.id for u in users}
    followers_by_username = {
        u.username.lower(): (u.public_metrics or {}).get("followers_count", 0) for u in users
    }

    now = dt.datetime.now(dt.timezone.utc)
    opps: list[tuple[Opportunity, int]] = []
    for tier, names in users_by_tier:
        for name in names:
            uid = id_by_username.get(name.lower())
            if not uid:
                continue
            tweets = (
                client.get_users_tweets(
                    id=uid,
                    max_results=5,
                    tweet_fields=["created_at", "public_metrics"],
                    exclude=["retweets", "replies"],
                ).data
                or []
            )
            for tw in tweets:
                created_at = tw.created_at if hasattr(tw, "created_at") else None
                minutes_ago = (now - created_at).total_seconds() / 60 if created_at else 9999
                metrics = tw.public_metrics or {}
                score = _score_opportunity(tier, metrics, minutes_ago)
                why = (
                    "Tier1 priority"
                    if tier == 1
                    else ("High score" if score >= 80 else "Watch list")
                )
                summary = (str(getattr(tw, "text", "")).replace("\n", " ")[:140]).strip()
                opp = Opportunity(
                    idx=0,
                    user=name,
                    followers=followers_by_username.get(name.lower(), 0),
                    tweeted_at=created_at.isoformat() if created_at else "",
                    summary=summary,
                    why=why,
                    metrics=metrics,
                    score=score,
                    tweet_id=str(tw.id),
                )
                opps.append((opp, tier))
    # Select shortlist per rules
    # Always show all tier1; tier2 if score>80; tier3 if score>90; cap 15
    shortlist = [o for o, t in opps if t == 1]
    shortlist += [o for o, t in opps if t == 2 and o.score >= 80]
    shortlist += [o for o, t in opps if t == 3 and o.score >= 90]
    shortlist.sort(key=lambda o: o.score, reverse=True)
    for i, o in enumerate(shortlist[:15], start=1):
        o.idx = i
    return [(o, 0) for o in shortlist[:15]]


def _post_opportunity_shortlist(channel: str, opps: list[Opportunity]) -> tuple[str, list[str]]:
    header = f'{COACH_TAG} Opportunities shortlist (reply "create: 1,4,6" to draft)'
    _, ts = slack_post(channel, header)
    posted_ts: list[str] = []
    for o in opps:
        line = (
            f"{o.idx}) @{o.user} — {o.summary}\n"
            f"Why: {o.why} | Score: {o.score} | Metrics: {o.metrics} | Followers: {o.followers}\n"
            f"tweet_id={o.tweet_id}"
        )
        _, child_ts = slack_post(channel, line, thread_ts=ts)
        posted_ts.append(child_ts)
    return ts, posted_ts


def _parse_selection(text: str) -> list[int]:
    # e.g., "create: 1,4,6" or "1 4 6"
    import re

    m = re.findall(r"\d+", text)
    return [int(x) for x in m]


def _generate_reply_single(tweet_text: str, username: str, tone: str = "safe") -> str:
    """Generate a single reply (safe by default). tone in {"safe","spicy"}."""
    style = os.getenv("ANTHROPIC_STYLE_CEO", _STYLE_CEO_DEFAULT)
    base = (
        "Draft ONE concise Twitter reply (<=280 chars) to the tweet below.\n"
        "Voice/style: follow this strictly:\n"
        + style
        + "\nUse everyday language; avoid buzzwords (e.g., 'first principles')."
    )
    if tone == "spicy":
        base += "\nMake it slightly contrarian/spicy but respectful; one sharp point; no fluff."
    prompt = base + "\nTweet by @" + username + ":\n" + tweet_text + "\nReturn only the reply text."
    return _anthropic_complete(prompt, task="replies")


def _load_budget_state() -> dict:
    # Persist very small state in data/state.json (ignored from git)
    root = os.path.dirname(os.path.abspath(__file__))
    state_dir = os.path.abspath(os.path.join(root, "..", "..", "data"))
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "state.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_budget_state(s: dict) -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    state_dir = os.path.abspath(os.path.join(root, "..", "..", "data"))
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(s, f)


def _budget_allow(cost: float) -> bool:
    budget = float(os.getenv("DAILY_TOKEN_BUDGET_USD", "0.50"))
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    s = _load_budget_state()
    if s.get("date") != today:
        s = {"date": today, "spend": 0.0, "drafts": 0}
    if s["spend"] + cost > budget:
        _save_budget_state(s)
        return False
    s["spend"] += cost
    s["drafts"] += 1
    _save_budget_state(s)
    return True


_SINGLE_VARIANT = os.getenv("REPLIES_SINGLE_VARIANT", "true").lower() == "true"
_COST_PER_DRAFT = float(os.getenv("ESTIMATED_COST_PER_DRAFT_USD", "0.04"))


def run_opportunity_scan() -> None:
    channel = env_required("SLACK_CHANNEL_ID")
    # Stage 1: fetch and shortlist
    pairs = _fetch_opportunities()
    opps = [p[0] for p in pairs]
    if not opps:
        print("No opportunities found, skipping Slack post")
        return
    header_ts, _ = _post_opportunity_shortlist(channel, opps)

    # Stage 2 trigger: check for selection replies under header
    replies = slack_thread_replies(channel, header_ts, limit=100)
    selected: list[int] = []
    want_spicy = False
    for r in replies:
        txt_low = (r.get("text") or "").lower()
        if "create" in txt_low or any(ch.isdigit() for ch in txt_low):
            selected = _parse_selection(txt_low)
            if "spicy" in txt_low:
                want_spicy = True
            break
    if not selected:
        return  # wait for user selection next run

    by_idx = {o.idx: o for o in opps}
    for idx in selected:
        o = by_idx.get(idx)
        if not o:
            continue
        # Budget check before generating
        if not _budget_allow(_COST_PER_DRAFT):
            slack_post(
                channel,
                f"{COACH_TAG} Budget reached; drafts paused for today.",
                thread_ts=header_ts,
            )
            return
        # Get tweet text for generation
        client = twitter_client_v2()
        tw = client.get_tweet(id=o.tweet_id, tweet_fields=["text"]).data
        tweet_text = getattr(tw, "text", o.summary)
        tone = "spicy" if want_spicy else "safe"
        reply = _generate_reply_single(tweet_text, o.user, tone=tone)
        # Post single draft in thread; 👍 to post
        slack_post(
            channel,
            f"{COACH_TAG} Reply {idx} (tweet_id={o.tweet_id}):\n{reply}\n\nActions: 👍 to post · or reply in thread: 'post {idx}' or 'edit {idx}: <text>'",
            thread_ts=header_ts,
        )

    # Reaction-based posting for single drafts (👍) and typed commands (post/edit)
    replies = slack_thread_replies(channel, header_ts, limit=200)
    # Index latest draft by idx
    latest_by_idx: dict[int, tuple[str, str]] = {}
    for msg in replies:
        mtxt = msg.get("text") or ""
        if COACH_TAG in mtxt and "Reply" in mtxt and "tweet_id=" in mtxt:
            # Parse idx and tweet_id
            try:
                # e.g., "[coach] Reply 3 (tweet_id=123):\n..."
                idx_part = mtxt.split("Reply", 1)[1].strip()
                idx_num = int(idx_part.split(" ", 1)[0])
            except Exception:
                continue
            tid = None
            for part in mtxt.split():
                if part.startswith("tweet_id="):
                    tid = part.split("=", 1)[1].rstrip("):")
                    break
            body = mtxt.split(":\n", 1)
            reply_text = body[1].strip() if len(body) == 2 else ""
            if tid and reply_text:
                latest_by_idx[idx_num] = (tid, reply_text)

    for r in replies:
        txt = (r.get("text") or "").strip()
        ts = r.get("ts")
        if not ts:
            continue
        # Handle typed commands first
        if txt.lower().startswith("post "):
            try:
                idx = int(txt.split()[1])
            except Exception:
                idx = -1
            if idx in latest_by_idx:
                tid, reply_text = latest_by_idx[idx]
                try:
                    rid = reply_to_tweet(tid, reply_text)
                    slack_add_reaction(channel, ts, ROBOT_REACTION)
                    slack_post(
                        channel,
                        f"{COACH_TAG} Replied on X (id={rid}) to {tid}",
                        thread_ts=header_ts,
                    )
                except Exception as e:
                    slack_post(
                        channel, f"{COACH_TAG} Error replying to {tid}: {e}", thread_ts=header_ts
                    )
            continue
        if txt.lower().startswith("edit "):
            # format: edit 3: new text
            parts = txt.split(None, 2)
            if len(parts) >= 2:
                try:
                    idx = int(parts[1].rstrip(":"))
                except Exception:
                    idx = -1
                new_text = ""
                if len(parts) == 3:
                    new_text = parts[2].lstrip(": ")
                if idx in latest_by_idx and new_text:
                    tid, _old = latest_by_idx[idx]
                    try:
                        rid = reply_to_tweet(tid, new_text)
                        slack_add_reaction(channel, ts, ROBOT_REACTION)
                        slack_post(
                            channel,
                            f"{COACH_TAG} Replied on X (id={rid}) to {tid}",
                            thread_ts=header_ts,
                        )
                    except Exception as e:
                        slack_post(
                            channel,
                            f"{COACH_TAG} Error replying to {tid}: {e}",
                            thread_ts=header_ts,
                        )
            continue
        # Then handle 👍 on a draft message itself
        if "tweet_id=" not in txt:
            continue
        reactions = {rv.get("name"): rv.get("count", 0) for rv in r.get("reactions", [])}
        if reactions.get(ROBOT_REACTION, 0) > 0:
            continue
        if reactions.get("+1", 0) > 0 or reactions.get("thumbsup", 0) > 0:
            # Extract tweet_id
            tid = None
            for part in txt.split():
                if part.startswith("tweet_id="):
                    tid = part.split("=", 1)[1]
                    break
            if tid:
                # Extract reply text after colon
                body = txt.split(":\n", 1)
                reply_text = body[1].strip() if len(body) == 2 else txt
                try:
                    rid = reply_to_tweet(tid, reply_text)
                    slack_add_reaction(channel, ts, ROBOT_REACTION)
                    slack_post(
                        channel,
                        f"{COACH_TAG} Replied on X (id={rid}) to {tid}",
                        thread_ts=header_ts,
                    )
                except Exception as e:
                    slack_post(
                        channel, f"{COACH_TAG} Error replying to {tid}: {e}", thread_ts=header_ts
                    )


def run_follow_recs() -> None:
    channel = env_required("SLACK_CHANNEL_ID")
    slack_post(channel, f"{COACH_TAG} Follow/DM recommendations:\n- @example1\n- @example2")


def _collect_recent_posts(hours: int = 24) -> list[dict]:
    data_dir = _ensure_data_dir()
    path = os.path.join(data_dir, "log.jsonl")
    out: list[dict] = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                ts = ev.get("ts")
                if not ts:
                    continue
                when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if when >= cutoff and ev.get("type") == "post":
                    out.append(ev)
    except FileNotFoundError:
        pass
    return out


def _update_theme_weights_from_metrics() -> tuple[str, float]:
    # Very simple: compute like velocity and reward the most successful theme in last 24h
    s = _load_budget_state()
    themes = s.get(
        "themes",
        {
            "metrics": 1,
            "build_in_public": 1,
            "positioning": 1,
            "technical": 1,
            "hot_take": 1,
        },
    )
    posts = _collect_recent_posts(24)
    # If we had theme per post, we'd use it; for now, reward 'metrics' if numbers present, else 'positioning' when asks_question, otherwise 'build_in_public'
    scores = {k: 0.0 for k in themes}
    for ev in posts:
        feats = ev.get("features", {})
        if feats.get("has_numbers"):
            scores["metrics"] += 1.0
        elif feats.get("asks_question"):
            scores["positioning"] += 0.7
        else:
            scores["build_in_public"] += 0.5
    best = max(scores, key=lambda k: scores[k]) if scores else "metrics"
    themes[best] = min(5, themes.get(best, 1) + 1)
    # decay others
    for k in themes:
        if k != best:
            themes[k] = max(1, int(themes[k] * 0.9))
    s["themes"] = themes
    _save_budget_state(s)
    return best, scores.get(best, 0.0)


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

    # Reinforcement update
    best_theme, best_score = _update_theme_weights_from_metrics()

    text = (
        f"{COACH_TAG} Daily summary\n"
        f"Suggestions in last 24h: {len(suggestions)}\n"
        f"Auto-posted to X: {posted}\n"
        f"Reinforcement: boosting theme → {best_theme} (score {best_score:.1f}).\n"
        f"React 👍 to clone today’s best vibe for tomorrow; 👎 to skip."
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
        choices=["suggest", "afternoon", "scan", "summary", "weekly", "replies", "recs", "none"],
        default="suggest",
    )
    args = p.parse_args(argv)

    if args.task == "none":
        print("No task scheduled at this time")
        return 0
    elif args.task == "suggest":
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
