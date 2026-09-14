#!/usr/bin/env python3
"""
Pulls Mommy Di's real Calendly availability and rewrites two spots in
index.html so neither ever goes stale:
  1. The "Earliest Open Slots" snapshot box inside the booking modal.
  2. The "Next opening: ..." line under Step 1 (Reserve your slot) in
     the Process section.

Runs hourly via .github/workflows/update-availability.yml on GitHub Actions.
Requires a Calendly Personal Access Token in the CALENDLY_TOKEN env var
(set as a GitHub Actions secret — see the setup instructions delivered
alongside this file).
"""

import os
import sys
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback, shouldn't happen on GH runners
    sys.exit("Python 3.9+ with zoneinfo is required.")

import requests

# --- Configuration -----------------------------------------------------

# Mommy Di's "US Visa Strategy call" Calendly event type.
# If she ever creates a new event type for this call, update this URI
# (Calendly dashboard -> Event Type -> the UUID at the end of its API URI,
# or ask Claude to look it up again via the Calendly integration).
EVENT_TYPE_URI = "https://api.calendly.com/event_types/dde73507-14a8-4bf2-b021-69cc706c037a"

MANILA = ZoneInfo("Asia/Manila")
HTML_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")

START_MARKER = "<!-- AVAILABILITY:START (auto-updated hourly by .github/workflows/update-availability.yml — do not hand-edit) -->"
END_MARKER = "<!-- AVAILABILITY:END -->"

PROCESS_START_MARKER = "<!-- PROCESS_AVAILABILITY:START (auto-updated hourly — do not hand-edit) -->"
PROCESS_END_MARKER = "<!-- PROCESS_AVAILABILITY:END -->"

MAX_WEEKS_LOOKAHEAD = 16   # ~4 months forward before giving up
TARGET_DISTINCT_DATES = 3  # how many bullet lines to show, matching the site's design


def calendly_get(path, token, params=None):
    resp = requests.get(
        f"https://api.calendly.com/{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_available_slots(token):
    """Walk forward week by week and collect available start times (UTC)."""
    slots = []
    now = datetime.now(timezone.utc)
    # Calendly requires start_time to be in the future; pad by a couple minutes.
    window_start = now + timedelta(minutes=2)

    for _ in range(MAX_WEEKS_LOOKAHEAD):
        window_end = window_start + timedelta(days=7)
        data = calendly_get(
            "event_type_available_times",
            token,
            params={
                "event_type": EVENT_TYPE_URI,
                "start_time": window_start.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "end_time": window_end.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            },
        )
        for item in data.get("collection", []):
            if item.get("status") == "available":
                slots.append(item["start_time"])

        # Stop early once we have enough distinct dates to show.
        distinct_dates = {
            datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(MANILA).date()
            for s in slots
        }
        if len(distinct_dates) >= TARGET_DISTINCT_DATES:
            break

        window_start = window_end

    return slots


def format_snapshot(slots):
    """Group ISO UTC start times by Manila date and build the HTML block."""
    by_date = {}
    for s in slots:
        dt_utc = datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt_manila = dt_utc.astimezone(MANILA)
        by_date.setdefault(dt_manila.date(), []).append(dt_manila)

    dates_sorted = sorted(by_date.keys())[:TARGET_DISTINCT_DATES]

    if not dates_sorted:
        list_html = "          <li>No open slots found in the next few months — please message us directly to check.</li>"
    else:
        lines = []
        for d in dates_sorted:
            times = sorted(by_date[d])
            time_strs = [t.strftime("%-I:%M %p") for t in times]
            time_joined = " &amp; ".join(time_strs)
            date_str = d.strftime("%a, %b %-d, %Y")
            lines.append(f"          <li><strong>{date_str}</strong> — {time_joined} (Manila time)</li>")
        list_html = "\n".join(lines)

    now_manila = datetime.now(timezone.utc).astimezone(MANILA)
    stamp = now_manila.strftime("%b %-d, %Y, %-I:%M %p")

    block = (
        f"{START_MARKER}\n"
        f"        <ul>\n"
        f"{list_html}\n"
        f"        </ul>\n"
        f"        <p class=\"avail-note\">Snapshot of Mommy Di's calendar as of {stamp} (Manila time) — "
        f"slots are limited and fill on a first-paid, first-booked basis, so more openings may appear "
        f"closer to your date. You'll pick your exact day and time on Calendly once your payment is "
        f"confirmed.</p>\n"
        f"        {END_MARKER}"
    )
    return block


def format_next_opening(slots):
    """Single-line 'Next opening: ...' string for the Process section."""
    if not slots:
        return "No open slots found in the next few months — message us directly to check."

    earliest = min(
        datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(MANILA) for s in slots
    )
    date_str = earliest.strftime("%a, %b %-d, %Y")
    time_str = earliest.strftime("%-I:%M %p")
    return f"Next opening: {date_str} — {time_str} (Manila time)"


def replace_between(html, start_marker, end_marker, new_inner, label):
    pattern = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker), re.DOTALL)
    if not pattern.search(html):
        sys.exit(
            f"Could not find the {label} markers in index.html. "
            "Has that section been edited or removed?"
        )
    replacement = f"{start_marker}{new_inner}{end_marker}"
    return pattern.sub(lambda _m: replacement, html, count=1)


def main():
    token = os.environ.get("CALENDLY_TOKEN")
    if not token:
        sys.exit("CALENDLY_TOKEN environment variable is not set.")

    slots = fetch_available_slots(token)
    avail_box_block = format_snapshot(slots)
    next_opening_text = format_next_opening(slots)

    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    # 1. The full snapshot box inside the booking modal.
    pattern = re.compile(re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER), re.DOTALL)
    if not pattern.search(html):
        sys.exit(
            "Could not find the AVAILABILITY:START / AVAILABILITY:END markers in index.html. "
            "Has the avail-box been edited or removed?"
        )
    html = pattern.sub(lambda _m: avail_box_block, html, count=1)

    # 2. The single-line "Next opening: ..." under Step 1 in the Process section.
    html = replace_between(
        html, PROCESS_START_MARKER, PROCESS_END_MARKER, next_opening_text, "PROCESS_AVAILABILITY"
    )

    with open(HTML_PATH, "r", encoding="utf-8") as f:
        original_html = f.read()

    if html != original_html:
        with open(HTML_PATH, "w", encoding="utf-8") as f:
            f.write(html)
        print("index.html updated with fresh availability.")
    else:
        print("No change — availability snapshot already up to date.")


if __name__ == "__main__":
    main()
