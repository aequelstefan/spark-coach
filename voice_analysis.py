#!/usr/bin/env python3
"""
Voice Analysis Script
Fetches recent tweets from key X accounts and analyzes patterns for Sherry-style voice calibration.
Run once or periodically to refine CEO tone.
"""

import os
import sys

import tweepy


def twitter_client() -> tweepy.Client:
    """Initialize Twitter API v2 client"""
    api_key = os.getenv("TWITTER_API_KEY")
    api_secret = os.getenv("TWITTER_API_SECRET")
    access_token = os.getenv("TWITTER_ACCESS_TOKEN")
    access_secret = os.getenv("TWITTER_ACCESS_SECRET")
    bearer_token = os.getenv("X_BEARER_TOKEN")

    if not all([api_key, api_secret, access_token, access_secret]):
        raise OSError("Missing Twitter API credentials")

    return tweepy.Client(
        bearer_token=bearer_token,
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )


def fetch_recent_tweets(username: str, count: int = 20) -> list[str]:
    """Fetch recent tweets from a user"""
    client = twitter_client()
    try:
        user = client.get_user(username=username)
        if not user.data:
            print(f"User {username} not found", file=sys.stderr)
            return []
        user_id = user.data.id
        tweets = client.get_users_tweets(
            id=user_id, max_results=count, exclude=["retweets", "replies"]
        )
        if not tweets.data:
            return []
        return [tweet.text for tweet in tweets.data]
    except Exception as e:
        print(f"Error fetching tweets for {username}: {e}", file=sys.stderr)
        return []


def analyze_patterns(tweets: list[str]) -> dict:
    """Analyze common patterns in tweets"""
    total = len(tweets)
    if total == 0:
        return {}

    patterns = {
        "avg_length": sum(len(t) for t in tweets) / total,
        "starts_lowercase": sum(1 for t in tweets if t[0].islower()) / total * 100,
        "has_emoji": sum(1 for t in tweets if any(ord(c) > 127 for c in t)) / total * 100,
        "has_question": sum(1 for t in tweets if "?" in t) / total * 100,
        "line_count": sum(len(t.splitlines()) for t in tweets) / total,
        "casual_starts": sum(
            1
            for t in tweets
            if any(t.lower().startswith(s) for s in ["just", "honestly", "tbh", "shipped", "so"])
        )
        / total
        * 100,
    }
    return patterns


def main() -> int:
    """Main entry point"""
    # Key accounts for voice analysis
    accounts = [
        "Sherry__Jang",
        "levelsio",
        "gregisenberg",
        "thisiskp_",
        "dannypostmaa",
    ]

    print("Voice Analysis Report")
    print("=" * 60)
    print()

    for username in accounts:
        print(f"Analyzing @{username}...")
        tweets = fetch_recent_tweets(username, count=20)
        if not tweets:
            print("  No tweets found\n")
            continue

        patterns = analyze_patterns(tweets)
        print(f"  Sample size: {len(tweets)} tweets")
        print(f"  Avg length: {patterns.get('avg_length', 0):.0f} chars")
        print(f"  Starts lowercase: {patterns.get('starts_lowercase', 0):.1f}%")
        print(f"  Has emoji: {patterns.get('has_emoji', 0):.1f}%")
        print(f"  Has question: {patterns.get('has_question', 0):.1f}%")
        print(f"  Avg lines: {patterns.get('line_count', 0):.1f}")
        print(f"  Casual starts: {patterns.get('casual_starts', 0):.1f}%")
        print()

        # Show 3 example tweets
        print("  Top 3 examples:")
        for i, tweet in enumerate(tweets[:3], 1):
            preview = tweet.replace("\n", " ")[:80]
            if len(tweet) > 80:
                preview += "..."
            print(f"    {i}. {preview}")
        print()

    print("=" * 60)
    print("Analysis complete. Use these patterns to calibrate CEO voice in coach.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
