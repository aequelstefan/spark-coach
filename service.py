#!/usr/bin/env python3
"""
Service wrapper for spark-coach that runs continuously.
Handles scheduled tasks and monitors Slack for reactions.
"""

import datetime as dt
import sys
import threading
import time

from coach import (
    run_afternoon_session,
    run_background_metrics,
    run_morning_session,
    run_opportunity_scan,
    run_summary,
    run_weekly_brief,
)

TZ = dt.timezone(dt.timedelta(hours=1))  # CET


def should_run_task(task: str, now: dt.datetime) -> bool:
    """Check if a task should run at the current time."""
    hour = now.hour
    minute = now.minute
    dow = now.weekday()  # 0=Mon, 6=Sun

    if task == "suggest":
        return hour == 7 and minute == 30
    elif task == "afternoon":
        return hour == 13 and minute == 0
    elif task == "summary":
        return hour == 18 and minute == 0
    elif task == "weekly":
        return dow == 6 and hour == 19 and minute == 0  # Sunday
    elif task == "scan":
        return hour in [9, 12, 15] and minute == 0
    elif task == "metrics":
        return minute % 30 == 0  # Every 30 minutes
    return False


def run_task_safe(task_name: str, task_func):
    """Run a task and catch exceptions."""
    try:
        print(f"[{dt.datetime.now(TZ)}] Running task: {task_name}")
        task_func()
        print(f"[{dt.datetime.now(TZ)}] Completed: {task_name}")
    except Exception as e:
        print(f"[{dt.datetime.now(TZ)}] Error in {task_name}: {e}", file=sys.stderr)


def main_loop():
    """Main service loop - runs every minute and checks what tasks to execute."""
    last_run: dict[str, dt.datetime] = {}

    print(f"[{dt.datetime.now(TZ)}] Spark-Coach service started")

    while True:
        now = dt.datetime.now(dt.timezone.utc)
        current_minute = now.replace(second=0, microsecond=0)

        # Check each task
        tasks = {
            "suggest": run_morning_session,
            "afternoon": run_afternoon_session,
            "summary": run_summary,
            "weekly": run_weekly_brief,
            "scan": run_opportunity_scan,
            "metrics": run_background_metrics,
        }

        for task_name, task_func in tasks.items():
            if should_run_task(task_name, now):
                # Only run once per minute
                if last_run.get(task_name) != current_minute:
                    last_run[task_name] = current_minute
                    # Run in thread to not block
                    thread = threading.Thread(
                        target=run_task_safe, args=(task_name, task_func), daemon=True
                    )
                    thread.start()

        # Sleep until next minute
        time.sleep(60)


if __name__ == "__main__":
    main_loop()
