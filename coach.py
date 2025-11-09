#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import os
import re
import sys
import time
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


def _voice_profile_path() -> str:
    return os.path.join(_ensure_data_dir(), "voice_profile.json")


def _load_voice_profile() -> dict:
    try:
        with open(_voice_profile_path(), encoding="utf-8") as f:
            prof = json.load(f)
            if not prof.get("product"):
                raise ValueError("incomplete profile")
            return prof
    except Exception:
        return {}


def _save_voice_profile(p: dict) -> None:
    p.setdefault("updated_at", dt.datetime.now(dt.timezone.utc).date().isoformat())
    with open(_voice_profile_path(), "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def generate_suggestions() -> str:
    """Generate tweets using calibrated voice profile; error if profile missing."""
    prof = _load_voice_profile()
    if not prof:
        return "- ERROR: Voice profile missing. Run: python coach.py --task setup"
    name = prof.get("name", "")
    handle = prof.get("handle", "")
    product = prof.get("product", "")
    recent_work = prof.get("recent_work", "")
    contrarian = prof.get("contrarian_view", "")
    style = prof.get("style", _STYLE_CEO_DEFAULT)
    examples = (
        "\n".join(f"- {t}" for t in (prof.get("example_tweets") or [])[:5]) or "- (no examples)"
    )
    blocklist = ", ".join(prof.get("blocklist", [])) or "(none)"
    banned = ", ".join(prof.get("banned_words", [])) or "(none)"
    weekly_ctx = prof.get("weekly_context") or recent_work

    prompt = (
        f"You are writing tweets for {name} (@{handle}).\n\n"
        f"CONTEXT:\nProduct: {product}\nRecent work: {recent_work}\nKey insight: {contrarian}\n\n"
        f"VOICE STYLE:\n{style}\n\n"
        f"EXAMPLES TO LEARN FROM:\n{examples}\n\n"
        f"NEVER WRITE ABOUT:\n{blocklist}\n\n"
        f"NEVER USE THESE WORDS:\n{banned}\n\n"
        f"CURRENT WEEK CONTEXT:\n{weekly_ctx}\n\n"
        "Generate 3 tweet options about their CURRENT WORK.\n"
        "Use their voice. Reference their actual product updates.\nBe specific with numbers/data when available.\n"
        "Output EXACTLY two sections with bullets only:\nTweets:\n- three options\nReply Opportunities:\n- three targets (handle + one line why)."
    )
    return _anthropic_complete(prompt, task="suggest") or "- (no suggestions returned)"


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


def slack_get_message(channel: str, ts: str) -> dict | None:
    """Fetch a single message (parent) including reactions."""
    client = slack_client()
    resp = client.conversations_replies(channel=channel, ts=ts, limit=1)
    msgs = list(resp.get("messages", []))
    return msgs[0] if msgs else None


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


def _fetch_tweet_metrics(tweet_id: str) -> dict:
    """Fetch current metrics for a tweet (free X API call)."""
    try:
        client = twitter_client_v2()
        tw = client.get_tweet(id=tweet_id, tweet_fields=["public_metrics"]).data
        if not tw:
            return {}
        return tw.public_metrics or {}
    except Exception as e:
        print(f"Error fetching metrics for tweet {tweet_id}: {e}", file=sys.stderr)
        return {}


def _store_metrics_snapshot(tweet_id: str, metrics: dict, age_label: str) -> None:
    """Store metrics snapshot in data/metrics.jsonl."""
    data_dir = _ensure_data_dir()
    path = os.path.join(data_dir, "metrics.jsonl")
    snapshot = {
        "tweet_id": tweet_id,
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "age_label": age_label,
        "metrics": metrics,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def _background_metrics_fetch() -> None:
    """Background job: fetch metrics for recent posts at 30min, 2h, 6h, 24h intervals."""
    data_dir = _ensure_data_dir()
    log_path = os.path.join(data_dir, "log.jsonl")
    now = dt.datetime.now(dt.timezone.utc)

    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("type") != "post" or not ev.get("tweet_id"):
                    continue
                tweet_id = ev["tweet_id"]
                ts = ev.get("ts")
                if not ts:
                    continue
                posted_at = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_minutes = (now - posted_at).total_seconds() / 60

                # Fetch at 30min, 2h, 6h, 24h
                intervals = [(30, "30m"), (120, "2h"), (360, "6h"), (1440, "24h")]
                for target_mins, label in intervals:
                    # Fetch if within 5min window of target
                    if abs(age_minutes - target_mins) < 5:
                        metrics = _fetch_tweet_metrics(tweet_id)
                        if metrics:
                            _store_metrics_snapshot(tweet_id, metrics, label)
    except FileNotFoundError:
        pass


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


def _detect_urgent_opportunities() -> list[str]:
    """Detect urgent opportunities: AMA/Q&A posts <5min old with >20 replies."""
    creators = _load_creators()
    tier1 = creators.get("tier1", [])
    if not tier1:
        return []
    client = twitter_client_v2()
    users = client.get_users(usernames=tier1, user_fields=["public_metrics"]).data or []
    id_by_username = {u.username.lower(): u.id for u in users}

    now = dt.datetime.now(dt.timezone.utc)
    alerts: list[str] = []

    for name in tier1:
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
            text = str(getattr(tw, "text", "")).lower()
            created_at = tw.created_at if hasattr(tw, "created_at") else None
            if not created_at:
                continue
            minutes_ago = (now - created_at).total_seconds() / 60
            if minutes_ago > 5:
                continue
            metrics = tw.public_metrics or {}
            replies = metrics.get("reply_count", 0)
            if replies < 20:
                continue
            # Check for AMA/Q&A patterns
            if any(kw in text for kw in ["ama", "ask me", "q&a", "questions", "answer anything"]):
                summary = str(getattr(tw, "text", ""))[:100].replace("\n", " ")
                alerts.append(
                    f"@{name} — {summary} | {int(minutes_ago)}m ago | {replies} replies | tweet_id={tw.id}"
                )
    return alerts


def monitor_creator_map() -> list[str]:
    """Return urgent alerts (strings). Uses _detect_urgent_opportunities()."""
    try:
        return _detect_urgent_opportunities()
    except Exception as e:
        print(f"Error detecting urgent opportunities: {e}", file=sys.stderr)
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


def _learning_path() -> str:
    return os.path.join(_ensure_data_dir(), "learning.json")


def _load_learning() -> dict:
    try:
        with open(_learning_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"features": {}, "updated_at": dt.datetime.now(dt.timezone.utc).date().isoformat()}


def _save_learning(s: dict) -> None:
    s["updated_at"] = dt.datetime.now(dt.timezone.utc).date().isoformat()
    with open(_learning_path(), "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def track_user_choice(option_idx: int, text: str) -> None:
    s = _load_learning()
    feats = _text_features(text)
    feats["is_personal_story"] = any(w in text.lower() for w in ["i ", " we ", "shipped", "today "])
    s.setdefault("features", {})
    for key in ["has_numbers", "asks_question", "emoji_count", "len", "is_personal_story"]:
        d = s["features"].setdefault(key, {"picks": 0, "successes": 0, "weight": 0.5})
        d["picks"] += 1
        d["weight"] = min(0.95, d.get("weight", 0.5) * 0.99 + (1 if feats.get(key) else 0) * 0.01)
    _save_learning(s)


def generate_learning_insights() -> str:
    s = _load_learning()
    out: list[str] = []
    for feat, d in s.get("features", {}).items():
        if d.get("weight", 0) > 0.7:
            out.append(f"✅ {feat}: {d['weight']:.0%} success rate")
    return "\n".join(out[:3])


def generate_coaching_card() -> str:
    now_cet = dt.datetime.now(TZ)
    hour = now_cet.hour
    if 8 <= hour <= 10:
        timing = "NOW"
    elif hour < 8:
        timing = f"in {8 - hour} hours"
    else:
        timing = "later today"

    urgent = _detect_urgent_opportunities()[:1]

    try:
        opps_pairs = _fetch_opportunities()
        opps = [o for o, _ in opps_pairs][:3]
    except Exception:
        opps = []

    insights = generate_learning_insights() or "(learning in progress)"

    picked_theme = _pick_theme().replace("_", " ").title()

    lines = [
        "🎯 MORNING SESSION - Stefan's To-Do List",
        "",
        f"MUST DO ({timing}):",
        "1. Post morning tweet (optimal window)",
    ]
    if urgent:
        lines.append(f"2. Urgent reply: {urgent[0]}")
    else:
        lines.append("2. —")
    lines += [
        "",
        "HIGH VALUE (today):",
    ]
    if opps:
        for i, o in enumerate(opps, 1):
            lines.append(f"{i + 2}. @{o.user} — {o.summary} (score {o.score})")
    else:
        lines.append("- No high-value targets found yet")
    lines += [
        "",
        f"TODAY'S THEME: {picked_theme}",
        "LAST WEEK INSIGHT:",
        insights,
    ]
    return "\n".join(lines)


# ---- True Coach helpers ----


def wait_for_user_reaction(
    channel: str, ts: str, valid_reactions: list[str], timeout: int = 1800
) -> str | None:
    """Compatibility wrapper that supports multiple emoji name variants."""
    # Map variants to canonical names
    variant_map = {
        "thumbsup": ["thumbsup", "+1"],
        "thumbsdown": ["thumbsdown", "-1"],
        "one": ["one", "keycap_1"],
        "two": ["two", "keycap_2"],
        "three": ["three", "keycap_3"],
        "fast_forward": ["fast_forward", "next_track_button"],
        "pencil2": ["pencil2"],
    }
    expected: dict[str, list[str]] = {}
    for key in valid_reactions:
        expected[key] = variant_map.get(key, [key])
    return _wait_for_user_response(channel, ts, expected, timeout)


def _reaction_selected(msg: dict, names: list[str]) -> bool:
    reactions = {rv.get("name"): rv.get("count", 0) for rv in msg.get("reactions", [])}
    return any(reactions.get(n, 0) > 0 for n in names)


def _wait_for_user_response(
    channel: str, ts: str, expected: dict[str, list[str]], timeout_sec: int = 1800
) -> str | None:
    """Poll Slack reactions on a message until one of the expected keys is reacted or timeout."""
    start = time.time()
    while time.time() - start < timeout_sec:
        parent = slack_get_message(channel, ts)
        if parent:
            for key, names in expected.items():
                if _reaction_selected(parent, names):
                    return key
        time.sleep(5)
    return None


def _post_action_card(channel: str, number: int, total: int, title: str, body: str) -> str:
    text = (
        f"────────────────────────────────────\n"
        f"ACTION #{number}: {title}\n"
        f"────────────────────────────────────\n{body}"
    )
    _, ts = slack_post(channel, text)
    return ts


def _determine_morning_actions() -> list[dict]:
    actions: list[dict] = []
    urgent = _detect_urgent_opportunities()
    if urgent:
        # Parse first urgent item: format '@name — summary | Xm ago | N replies | tweet_id=12345'
        first = urgent[0]
        tid = None
        user = None
        summary = first
        for part in first.split():
            if part.startswith("tweet_id="):
                tid = part.split("=", 1)[1]
        if first.startswith("@"):  # get handle
            user = first.split(" ", 1)[0].lstrip("@")
        actions.append(
            {
                "type": "reply",
                "tweet_id": tid,
                "user": user,
                "summary": summary,
                "priority": "urgent",
            }
        )
    # Always include tweet action
    actions.insert(0, {"type": "tweet", "priority": "high"})
    # Add up to 2 more high-value replies
    try:
        opps_pairs = _fetch_opportunities()
        opps = [o for o, _ in opps_pairs][:3]
        for o in opps:
            actions.append(
                {
                    "type": "reply",
                    "tweet_id": o.tweet_id,
                    "user": o.user,
                    "summary": o.summary,
                    "priority": "high",
                }
            )
    except Exception:
        pass
    return actions[:4]


def run_morning_session() -> None:
    channel = env_required("SLACK_CHANNEL_ID")
    # Session header
    try:
        coaching = generate_coaching_card()
    except Exception:
        coaching = f"{COACH_TAG} Morning session"
    slack_post(channel, coaching)

    actions = _determine_morning_actions()
    total = len(actions)

    for idx, action in enumerate(actions, start=1):
        if action["type"] == "tweet":
            # Ask before generating (cost control)
            ts = _post_action_card(
                channel,
                idx,
                total,
                "POST YOUR MORNING TWEET",
                "Shall I generate 3 options? React 👍 to generate · 👎 to skip",
            )
            # Try to add reactions (non-fatal)
            try:
                slack_add_reaction(channel, ts, "+1")
                slack_add_reaction(channel, ts, "thumbsdown")
            except Exception:
                pass
            resp = _wait_for_user_response(
                channel,
                ts,
                {"yes": ["+1", "thumbsup"], "no": ["thumbsdown"]},
                timeout_sec=1800,
            )
            if resp != "yes":
                continue
            # Generate on-demand
            suggestions = generate_suggestions()
            tweets = _extract_tweets_from_suggestions(suggestions)[:3]
            while len(tweets) < 3:
                tweets.append("[placeholder tweet]")
            opt_text = f"1️⃣ {tweets[0]}\n\n2️⃣ {tweets[1]}\n\n3️⃣ {tweets[2]}\n\nReact 1️⃣2️⃣3️⃣ to post"
            _, opt_ts = slack_post(channel, opt_text)
            try:
                for r in ("one", "two", "three"):
                    slack_add_reaction(channel, opt_ts, r)
            except Exception:
                pass
            resp = _wait_for_user_response(
                channel,
                opt_ts,
                {"1": ["one", "keycap_1"], "2": ["two", "keycap_2"], "3": ["three", "keycap_3"]},
                timeout_sec=1800,
            )
            if resp in {"1", "2", "3"}:
                choice = int(resp)
                text = tweets[choice - 1]
                try:
                    track_user_choice(choice, text)
                except Exception:
                    pass
                tid = post_to_x(text)
                slack_post(channel, f"✅ Posted morning tweet (id={tid})")
        elif action["type"] == "reply" and action.get("tweet_id"):
            body = f"Target: @{action.get('user')}\nWhy: {action.get('summary')}\nDraft reply? 👍 yes · 👎 skip · ⏭️ next"
            ts = _post_action_card(channel, idx, total, "REPLY TO HIGH-VALUE POST", body)
            try:
                for r in ("+1", "thumbsdown", "next_track_button"):
                    slack_add_reaction(channel, ts, r)
            except Exception:
                pass
            resp = _wait_for_user_response(
                channel,
                ts,
                {"yes": ["+1", "thumbsup"], "no": ["thumbsdown"], "next": ["next_track_button"]},
                timeout_sec=1800,
            )
            if resp != "yes":
                continue
            # Generate one draft on demand
            try:
                client = twitter_client_v2()
                tw = client.get_tweet(id=action["tweet_id"], tweet_fields=["text"]).data
                source_text = getattr(tw, "text", action.get("summary", ""))
                draft = _generate_reply_single(source_text, action.get("user") or "user")
            except Exception as e:
                slack_post(channel, f"{COACH_TAG} Failed to generate draft: {e}")
                continue
            draft_text = f"Draft:\n{draft}\n\nReact ✅ to post · ✏️ to edit (reply 'edit: ...') · 🔄 to regenerate · 👎 to skip"
            _, dts = slack_post(channel, draft_text)
            try:
                for r in ("white_check_mark", "pencil2", "arrows_counterclockwise", "thumbsdown"):
                    slack_add_reaction(channel, dts, r)
            except Exception:
                pass
            resp = _wait_for_user_response(
                channel,
                dts,
                {
                    "post": ["white_check_mark", "heavy_check_mark"],
                    "edit": ["pencil2"],
                    "regen": ["arrows_counterclockwise"],
                    "skip": ["thumbsdown"],
                },
                timeout_sec=1800,
            )
            if resp == "edit":
                # Look for a thread reply starting with 'edit:'
                edits = slack_thread_replies(channel, dts, limit=50)
                for r in edits[::-1]:
                    t = (r.get("text") or "").strip()
                    if t.lower().startswith("edit:"):
                        draft = t.split(":", 1)[1].strip()
                        break
                resp = "post"
            if resp == "regen":
                draft = _generate_reply_single(source_text, action.get("user") or "user")
                slack_post(channel, f"Regenerated:\n{draft}")
                resp = "post"
            if resp == "post":
                rid = reply_to_tweet(action["tweet_id"], draft, handle=action.get("user"))
                slack_post(channel, f"✅ Posted reply (id={rid}) to @{action.get('user')}")

    slack_post(channel, "🎉 MORNING SESSION COMPLETE — see you at 14:00 CET")


def run_afternoon_session() -> None:
    channel = env_required("SLACK_CHANNEL_ID")
    # Step 1: context refresh
    _, ts = slack_post(
        channel,
        "🕑 AFTERNOON SESSION\nWhat did you ship/learn today?\nReply in this thread with 2-3 bullets, or react ⏭️ to skip.",
    )
    try:
        slack_add_reaction(channel, ts, "next_track_button")
    except Exception:
        pass
    # Wait up to 10 minutes for user text
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
    new_ctx = None
    while dt.datetime.now(dt.timezone.utc) < deadline:
        replies = slack_thread_replies(channel, ts, limit=50)
        text_blobs = [r.get("text") or "" for r in replies if (r.get("text") or "").strip()]
        if text_blobs:
            new_ctx = "\n".join(text_blobs).strip()
            break
        # If user reacted skip, break
        parent = slack_get_message(channel, ts)
        if parent and any(
            rv.get("name") == "next_track_button" and rv.get("count", 0) > 0
            for rv in parent.get("reactions", [])
        ):
            break
        time.sleep(10)
    # Update weekly_context if provided
    try:
        prof = _load_voice_profile()
        if new_ctx:
            prof["weekly_context"] = new_ctx
            _save_voice_profile(prof)
            slack_post(channel, f"{COACH_TAG} Updated context saved.")
        else:
            new_ctx = prof.get("weekly_context") or prof.get("recent_work") or ""
    except Exception:
        new_ctx = new_ctx or ""

    # Step 2: Ask to generate afternoon tweet
    ats = _post_action_card(
        channel,
        1,
        1,
        "POST AFTERNOON UPDATE",
        "Generate 3 options based on today’s context? React 👍 to generate · 👎 to skip",
    )
    try:
        slack_add_reaction(channel, ats, "+1")
        slack_add_reaction(channel, ats, "thumbsdown")
    except Exception:
        pass
    resp = _wait_for_user_response(
        channel, ats, {"yes": ["+1", "thumbsup"], "no": ["thumbsdown"]}, timeout_sec=1800
    )
    if resp != "yes":
        slack_post(channel, "Afternoon session done. Monitoring for urgent opportunities.")
        return

    # Temporarily inject context into profile for this generation
    prof = _load_voice_profile()
    if new_ctx:
        prof["weekly_context"] = new_ctx
        _save_voice_profile(prof)
    suggestions = generate_suggestions()
    tweets = _extract_tweets_from_suggestions(suggestions)[:3]
    while len(tweets) < 3:
        tweets.append("[placeholder tweet]")
    opt_text = f"1️⃣ {tweets[0]}\n\n2️⃣ {tweets[1]}\n\n3️⃣ {tweets[2]}\n\nReact 1️⃣2️⃣3️⃣ to post"
    _, opt_ts = slack_post(channel, opt_text)
    try:
        for r in ("one", "two", "three"):
            slack_add_reaction(channel, opt_ts, r)
    except Exception:
        pass
    resp = _wait_for_user_response(
        channel,
        opt_ts,
        {"1": ["one", "keycap_1"], "2": ["two", "keycap_2"], "3": ["three", "keycap_3"]},
        timeout_sec=1800,
    )
    if resp in {"1", "2", "3"}:
        choice = int(resp)
        text = tweets[choice - 1]
        try:
            track_user_choice(choice, text)
        except Exception:
            pass
        tid = post_to_x(text)
        slack_post(channel, f"✅ Posted afternoon update (id={tid})")
    slack_post(channel, "Afternoon session done. Monitoring for urgent opportunities.")


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


def _get_recent_metrics() -> list[dict]:
    """Load recent metrics snapshots from data/metrics.jsonl."""
    data_dir = _ensure_data_dir()
    path = os.path.join(data_dir, "metrics.jsonl")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    snapshots: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    snap = json.loads(line)
                except Exception:
                    continue
                ts = snap.get("ts")
                if not ts:
                    continue
                when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if when >= cutoff:
                    snapshots.append(snap)
    except FileNotFoundError:
        pass
    return snapshots


def run_summary() -> None:
    """Daily analytics report: raw metrics, theme reinforcement, separate thread."""
    channel = env_required("SLACK_CHANNEL_ID")

    # Count posts from last 24h
    posts = _collect_recent_posts(24)
    tweets = [p for p in posts if p.get("kind") == "tweet"]
    replies = [p for p in posts if p.get("kind") == "reply"]

    # Fetch latest metrics for today's tweets
    tweet_ids = [p["tweet_id"] for p in tweets]
    metrics_summary: list[str] = []
    for tid in tweet_ids:
        metrics = _fetch_tweet_metrics(tid)
        if metrics:
            likes = metrics.get("like_count", 0)
            rts = metrics.get("retweet_count", 0)
            replies_count = metrics.get("reply_count", 0)
            impressions = metrics.get("impression_count", "N/A")
            metrics_summary.append(
                f"Tweet {tid}: {likes} likes, {rts} RTs, {replies_count} replies, {impressions} impressions"
            )

    # Reinforcement update
    best_theme, best_score = _update_theme_weights_from_metrics()

    # Build report
    text = (
        f"{COACH_TAG} Daily Analytics ({dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')})\n\n"
    )
    text += "**Posts Today:**\n"
    text += f"- Tweets: {len(tweets)}\n"
    text += f"- Replies: {len(replies)}\n\n"
    text += "**Metrics (latest):**\n"
    if metrics_summary:
        text += "\n".join(f"- {m}" for m in metrics_summary) + "\n\n"
    else:
        text += "- No metrics available yet\n\n"
    text += "**Reinforcement:**\n"
    text += f"- Best theme: {best_theme} (score {best_score:.1f})\n"
    text += "- Boosted weight for tomorrow's content\n\n"
    text += "_Automated daily report. No token cost for metrics._"

    # Post in separate thread
    slack_post(channel, text)
    try:
        health = system_health_check()
        if health:
            slack_post(channel, health)
    except Exception as e:
        _log_event({"type": "error", "where": "health_check", "error": str(e)})


def run_weekly_brief() -> None:
    """Weekly strategy brief: aggregate stats, top performers, theme trends."""
    channel = env_required("SLACK_CHANNEL_ID")

    # Collect last 7 days
    posts = _collect_recent_posts(168)  # 7 days
    tweets = [p for p in posts if p.get("kind") == "tweet"]
    replies = [p for p in posts if p.get("kind") == "reply"]

    # Aggregate metrics from snapshots
    snapshots = _get_recent_metrics()
    metrics_by_tweet: dict[str, dict] = {}
    for snap in snapshots:
        tid = snap.get("tweet_id")
        if not tid or snap.get("age_label") != "24h":
            continue
        metrics_by_tweet[tid] = snap.get("metrics", {})

    # Find top performer
    top_tweet = ""
    top_score = 0
    for tid, metrics in metrics_by_tweet.items():
        likes = metrics.get("like_count", 0)
        rts = metrics.get("retweet_count", 0)
        score = likes + rts * 2
        if score > top_score:
            top_score = score
            top_tweet = tid

    # Theme distribution
    s = _load_budget_state()
    themes = s.get("themes", {})
    sorted_themes = sorted(themes.items(), key=lambda x: x[1], reverse=True)

    # Build report
    text = f"{COACH_TAG} Weekly Brief ({dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')})\n\n"
    text += "**Activity:**\n"
    text += f"- Tweets: {len(tweets)}\n"
    text += f"- Replies: {len(replies)}\n\n"
    text += "**Top Performer:**\n"
    if top_tweet:
        text += f"- Tweet {top_tweet} ({top_score} engagement)\n\n"
    else:
        text += "- No data yet\n\n"
    text += "**Theme Weights (current):**\n"
    for theme, weight in sorted_themes:
        text += f"- {theme.replace('_', ' ').title()}: {weight}\n"
    text += "\n_Weekly automated report. No LLM tokens used._"

    slack_post(channel, text)


def run_ad_hoc_stats(query: str = "") -> None:
    """Ad-hoc stats command: show simple aggregated metrics (no LLM, just scrape)."""
    channel = env_required("SLACK_CHANNEL_ID")
    posts = _collect_recent_posts(168)  # last 7 days
    tweets = [p for p in posts if p.get("kind") == "tweet"]
    replies = [p for p in posts if p.get("kind") == "reply"]

    text = f"{COACH_TAG} Ad-hoc Stats\n"
    text += f"Last 7 days: {len(tweets)} tweets, {len(replies)} replies\n"
    text += f"Features: {sum(1 for p in tweets if p.get('features', {}).get('has_numbers'))} with numbers, "
    text += (
        f"{sum(1 for p in tweets if p.get('features', {}).get('asks_question'))} with questions\n"
    )
    text += "_No LLM cost._"

    slack_post(channel, text)


def run_learning_loop() -> None:
    # Placeholder: would aggregate selections and outcomes
    pass


def system_health_check() -> str:
    try:
        oldest = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).timestamp()
        messages = slack_history(env_required("SLACK_CHANNEL_ID"), oldest_ts=oldest, limit=100)
        morning = any("Suggestions for" in (m.get("text") or "") for m in messages)
    except Exception:
        morning = False
    posts = _collect_recent_posts(24)
    tweets = [p for p in posts if p.get("kind") == "tweet"]
    replies = [p for p in posts if p.get("kind") == "reply"]
    metrics_ok = bool(_get_recent_metrics())
    lines = [
        "🏥 SYSTEM HEALTH",
        f"{'✅' if morning else '⚠️'} Morning: {'Found suggestions' if morning else 'Missing'}",
        f"✅ Posts: {len(tweets)} tweets, {len(replies)} replies (24h)",
        f"{'✅' if metrics_ok else '⚠️'} Metrics: {'ok' if metrics_ok else 'no snapshots'}",
    ]
    return "\n".join(lines)


def _process_pending_number_posts() -> None:
    """Watcher: if a suggestions/options message has 1️⃣/2️⃣/3️⃣ reactions later, post it."""
    channel = env_required("SLACK_CHANNEL_ID")
    oldest = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).timestamp()
    messages = slack_history(channel, oldest_ts=oldest, limit=300)
    for m in messages:
        text = m.get("text") or ""
        ts = m.get("ts")
        if not ts or "1️⃣ " not in text or "2️⃣ " not in text or "3️⃣ " not in text:
            continue
        reactions = {rv.get("name"): rv.get("count", 0) for rv in m.get("reactions", [])}
        if reactions.get(ROBOT_REACTION, 0) > 0:
            continue
        # Parse options
        options: dict[int, str] = {}
        for line in text.splitlines():
            ls = line.strip()
            if ls.startswith("1️⃣ "):
                options[1] = ls.split("1️⃣ ", 1)[1].strip()
            elif ls.startswith("2️⃣ "):
                options[2] = ls.split("2️⃣ ", 1)[1].strip()
            elif ls.startswith("3️⃣ "):
                options[3] = ls.split("3️⃣ ", 1)[1].strip()
        selected = (
            1
            if reactions.get("one", 0) > 0
            else 2
            if reactions.get("two", 0) > 0
            else 3
            if reactions.get("three", 0) > 0
            else None
        )
        if selected and selected in options:
            try:
                tid = post_to_x(options[selected])
                slack_add_reaction(channel, ts, ROBOT_REACTION)
                slack_post(
                    channel, f"{COACH_TAG} Posted option {selected} to X (id={tid})", thread_ts=ts
                )
            except Exception as e:
                slack_post(channel, f"{COACH_TAG} Error posting to X: {e}", thread_ts=ts)


def _update_learning_success_from_snapshot(tweet_id: str, metrics: dict) -> None:
    """At 24h snapshot, update learning successes using logged features for that tweet."""
    try:
        data_dir = _ensure_data_dir()
        path = os.path.join(data_dir, "log.jsonl")
        feats: dict | None = None
        with open(path, encoding="utf-8") as f:
            for line in f:
                ev = json.loads(line)
                if (
                    ev.get("type") == "post"
                    and ev.get("kind") == "tweet"
                    and ev.get("tweet_id") == tweet_id
                ):
                    feats = ev.get("features") or {}
        if not feats:
            return
        likes = metrics.get("like_count", 0)
        replies = metrics.get("reply_count", 0)
        success = (likes >= 5) or (replies >= 1)
        if not success:
            return
        s = _load_learning()
        s.setdefault("features", {})
        for key in ["has_numbers", "asks_question", "emoji_count", "len", "is_personal_story"]:
            if feats.get(key) is None:
                continue
            d = s["features"].setdefault(key, {"picks": 0, "successes": 0, "weight": 0.5})
            d["successes"] = d.get("successes", 0) + 1
            # Recompute simple weight
            picks = max(1, d.get("picks", 1))
            d["weight"] = min(0.95, max(0.05, d["successes"] / picks))
        _save_learning(s)
    except Exception:
        pass


def run_background_metrics() -> None:
    """Run background metrics fetch (called every 30min by GitHub Actions)."""
    # First, process any pending number-selected posts from options
    try:
        _process_pending_number_posts()
    except Exception as e:
        _log_event({"type": "error", "where": "watcher", "error": str(e)})
    _background_metrics_fetch()
    # Update learning from 24h snapshots
    try:
        snaps = _get_recent_metrics()
        for s in snaps:
            if s.get("age_label") == "24h":
                _update_learning_success_from_snapshot(str(s.get("tweet_id")), s.get("metrics", {}))
    except Exception as e:
        _log_event({"type": "error", "where": "learning_update", "error": str(e)})
    print("Background metrics fetch complete")


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--task",
        choices=[
            "suggest",
            "afternoon",
            "scan",
            "summary",
            "weekly",
            "replies",
            "recs",
            "stats",
            "metrics",
            "setup",
            "refresh",
            "none",
        ],
        default="suggest",
    )
    args = p.parse_args(argv)

    if args.task == "none":
        print("No task scheduled at this time")
        return 0
    elif args.task == "suggest":
        # True Coach morning session
        run_morning_session()
    elif args.task == "afternoon":
        run_afternoon_session()
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
    elif args.task == "stats":
        run_ad_hoc_stats()
    elif args.task == "metrics":
        run_background_metrics()
    elif args.task == "setup":
        try:
            from spark_coach.setup import run_setup_interview

            run_setup_interview()
        except Exception as e:
            print(f"Setup failed: {e}", file=sys.stderr)
    elif args.task == "refresh":
        try:
            from spark_coach.setup import run_weekly_refresh_prompt

            run_weekly_refresh_prompt()
        except Exception as e:
            print(f"Refresh failed: {e}", file=sys.stderr)
    run_learning_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
