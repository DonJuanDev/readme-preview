#!/usr/bin/env python3
"""
Collect real daily contribution counts and write data/contributions.json.

Prefers GitHub GraphQL via `gh` (same calendar GitHub shows you when logged
in, including private-repo activity). Falls back to the public HTML endpoint
if `gh` is missing or unauthenticated.

The public HTML endpoint only serves a rolling 12-month window, so the HTML
fallback walks year-by-year from ACCOUNT_START_YEAR using ?from=&to=.

Output carries both:
  - all-time stats (total, true current/longest streak, best day)
  - a last-53-week window ("days") that the heatmap grid renders, with
    GitHub's own contributionLevel (0-4) so the greens match the profile graph

Run daily by .github/workflows/update-profile-art.yml.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

import requests
from bs4 import BeautifulSoup

from profile_config import ACCOUNT_START_YEAR, GITHUB_USER

USERNAME = os.environ.get("GH_PROFILE_USER", GITHUB_USER)
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "contributions.json")

HEADERS = {"User-Agent": "profile-readme-bot/1.0"}
GRID_WEEKS = 53  # what the heatmap actually draws

LEVEL_MAP = {
    "NONE": 0,
    "FIRST_QUARTILE": 1,
    "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3,
    "FOURTH_QUARTILE": 4,
}

GRAPHQL = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks {
          contributionDays {
            date
            contributionCount
            contributionLevel
          }
        }
      }
    }
  }
}
"""


def _today():
    return datetime.date.today()


def fetch_graphql_window(from_date, to_date):
    """Return [{date, count, level}] for one <=1y window via `gh api graphql`."""
    payload = {
        "query": GRAPHQL,
        "variables": {
            "login": USERNAME,
            "from": f"{from_date}T00:00:00Z",
            "to": f"{to_date}T23:59:59Z",
        },
    }
    proc = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "gh graphql failed")
    body = json.loads(proc.stdout)
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    weeks = (
        body["data"]["user"]["contributionsCollection"]
        ["contributionCalendar"]["weeks"]
    )
    out = []
    for week in weeks:
        for day in week["contributionDays"]:
            out.append({
                "date": day["date"],
                "count": int(day["contributionCount"]),
                "level": LEVEL_MAP.get(day.get("contributionLevel"), 0),
            })
    return out


def fetch_all_days_graphql():
    """Walk year-by-year for counts; one rolling-year query for GitHub levels."""
    today = _today()
    today_s = today.isoformat()
    merged = {}

    for year in range(ACCOUNT_START_YEAR, today.year + 1):
        start = datetime.date(year, 1, 1)
        end = min(datetime.date(year, 12, 31), today)
        got = fetch_graphql_window(start.isoformat(), end.isoformat())
        year_days = [d for d in got if d["date"][:4] == str(year) and d["date"] <= today_s]
        for d in year_days:
            merged[d["date"]] = {"date": d["date"], "count": d["count"]}
        print(f"  {year}: {len(year_days)} days, "
              f"{sum(d['count'] for d in year_days)} contributions (graphql)",
              file=sys.stderr)

    window_start = today - datetime.timedelta(days=365)
    for d in fetch_graphql_window(window_start.isoformat(), today_s):
        if d["date"] <= today_s:
            slot = merged.setdefault(d["date"], {"date": d["date"], "count": d["count"]})
            slot["count"] = max(slot.get("count", 0), d["count"])
            slot["level"] = d["level"]

    days = [merged[k] for k in sorted(merged)]
    first_active = next((i for i, d in enumerate(days) if d["count"] > 0), 0)
    return days[first_active:]


def fetch_html_window(from_date, to_date):
    """Fetch one <=1y window from the public contributions fragment."""
    url = (f"https://github.com/users/{USERNAME}/contributions"
           f"?from={from_date}&to={to_date}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    cells = soup.select("td.ContributionCalendar-day")
    if not cells:
        print(f"no calendar cells for {from_date}..{to_date} -- markup may have changed",
              file=sys.stderr)
        return {}

    out = {}
    for td in cells:
        date = td.get("data-date")
        if not date:
            continue
        td_id = td.get("id")
        tooltip_el = soup.find("tool-tip", attrs={"for": td_id}) if td_id else None
        text = tooltip_el.get_text(strip=True) if tooltip_el else ""
        if re.search(r"no contributions", text, re.I):
            count = 0
        else:
            m = re.match(r"([\d,]+)", text)
            count = int(m.group(1).replace(",", "")) if m else 0
        try:
            level = int(td.get("data-level") or 0)
        except ValueError:
            level = 0
        out[date] = {"count": count, "level": max(0, min(level, 4))}
    return out


def fetch_all_days_html():
    """Walk year-by-year so streaks/totals aren't clipped to a rolling year."""
    today = _today()
    merged = {}
    for year in range(ACCOUNT_START_YEAR, today.year + 1):
        start = datetime.date(year, 1, 1)
        end = min(datetime.date(year, 12, 31), today)
        got = fetch_html_window(start.isoformat(), end.isoformat())
        today_s = today.isoformat()
        got = {d: c for d, c in got.items()
               if d[:4] == str(year) and d <= today_s}
        merged.update(got)
        print(f"  {year}: {len(got)} days, "
              f"{sum(v['count'] for v in got.values())} contributions (html)",
              file=sys.stderr)

    if not merged:
        print("no contribution data found at all", file=sys.stderr)
        sys.exit(1)

    days = [{"date": d, **merged[d]} for d in sorted(merged)]
    first_active = next((i for i, d in enumerate(days) if d["count"] > 0), 0)
    return days[first_active:]


def fetch_all_days():
    if shutil.which("gh"):
        try:
            days = fetch_all_days_graphql()
            if days:
                print("source: github graphql via gh", file=sys.stderr)
                return days
        except Exception as exc:
            print(f"graphql fetch failed ({exc}); falling back to public html",
                  file=sys.stderr)
    else:
        print("gh not on PATH; using public html scrape", file=sys.stderr)
    return fetch_all_days_html()


def compute_current_streak(days):
    idx = len(days) - 1
    if days[idx]["count"] == 0:
        idx -= 1  # today isn't over yet -- don't break the streak on it
    streak = 0
    end_idx = idx
    while idx >= 0 and days[idx]["count"] > 0:
        streak += 1
        idx -= 1
    start_idx = idx + 1
    if streak == 0:
        return 0, None, None
    return streak, days[start_idx]["date"], days[end_idx]["date"]


def compute_longest_streak(days):
    longest = run = 0
    longest_start = longest_end = None
    run_start_idx = None
    for i, d in enumerate(days):
        if d["count"] > 0:
            if run == 0:
                run_start_idx = i
            run += 1
            if run > longest:
                longest = run
                longest_start = days[run_start_idx]["date"]
                longest_end = days[i]["date"]
        else:
            run = 0
    return longest, longest_start, longest_end


def relative_level(count, max_count):
    if count <= 0 or max_count <= 0:
        return 0
    step = max_count / 4
    return min(4, max(1, int((count + step - 1e-9) / step)))


def build_data(all_days):
    total = sum(d["count"] for d in all_days)
    active_days = sum(1 for d in all_days if d["count"] > 0)
    best = max(all_days, key=lambda d: d["count"])
    cur_len, cur_start, cur_end = compute_current_streak(all_days)
    long_len, long_start, long_end = compute_longest_streak(all_days)

    today = _today()
    window_start = today - datetime.timedelta(weeks=GRID_WEEKS)
    window_start -= datetime.timedelta(days=(window_start.weekday() + 1) % 7)
    grid_days = [dict(d) for d in all_days
                 if datetime.date.fromisoformat(d["date"]) >= window_start]
    last_year_total = sum(d["count"] for d in grid_days)

    max_count = max((d["count"] for d in grid_days), default=0)
    for d in grid_days:
        if "level" not in d:
            d["level"] = relative_level(d["count"], max_count)

    monthly = {}
    for d in all_days:
        key = d["date"][:7]
        monthly[key] = monthly.get(key, 0) + d["count"]
    monthly_list = [{"month": k, "total": v} for k, v in sorted(monthly.items())]

    return {
        "username": USERNAME,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "range": {"start": all_days[0]["date"], "end": all_days[-1]["date"]},
        "total_contributions": total,
        "last_year_contributions": last_year_total,
        "active_days": active_days,
        "avg_per_active_day": round(total / active_days, 1) if active_days else 0,
        "current_streak": {"length": cur_len, "start": cur_start, "end": cur_end},
        "longest_streak": {"length": long_len, "start": long_start, "end": long_end},
        "best_day": {"date": best["date"], "count": best["count"]},
        "monthly": monthly_list,
        "grid_range": {"start": grid_days[0]["date"], "end": grid_days[-1]["date"]},
        "days": grid_days,
    }


def keep_richer_snapshot(new_data):
    """Don't let a token-less CI scrape erase private-activity greens."""
    if not os.path.exists(OUT_PATH):
        return new_data
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, json.JSONDecodeError):
        return new_data
    old_year = old.get("last_year_contributions", 0)
    new_year = new_data.get("last_year_contributions", 0)
    if old_year and new_year < old_year * 0.5:
        print(
            f"warning: fetched last-year total {new_year} is much lower than "
            f"saved {old_year}; keeping the richer snapshot (likely missing "
            f"private contribs). Set repo secret CONTRIB_TOKEN (a classic PAT "
            f"with `read:user`) or enable "
            f"'Include private contributions on my profile'.",
            file=sys.stderr,
        )
        return old
    return new_data


if __name__ == "__main__":
    all_days = fetch_all_days()
    data = keep_richer_snapshot(build_data(all_days))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"wrote {OUT_PATH}: {data['total_contributions']:,} all-time contributions "
          f"({data['last_year_contributions']:,} in the last year), "
          f"current streak {data['current_streak']['length']}, "
          f"longest streak {data['longest_streak']['length']}")
