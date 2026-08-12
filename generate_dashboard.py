#!/usr/bin/env python3
"""
Fitness dashboard generator.

Pulls the trailing 7 days from four Notion data sources (Daily Meal Log,
Daily Weight Log, Garmin Log, Workout Log), computes per-day eaten/target/
deficit/protein/activity numbers, and renders a single self-contained
dark-mode HTML dashboard (inline CSS/JS, data embedded as JSON, no build
step) to site/index.html.

Environment variables required (reuses the same .env as garmin_notion_sync.py):
  NOTION_TOKEN

Usage:
  python3 generate_dashboard.py
"""

import os
import re
import sys
import json
import html as html_lib
import statistics
import datetime as dt

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
    # ANTHROPIC_API_KEY lives in the whatsapp-listener pipeline's .env, not
    # this project's -- load it as a fallback (won't override anything
    # already set from the line above) rather than duplicating the secret
    # into this repo's .env.
    load_dotenv(os.path.expanduser("~/whatsapp-listener/pipeline/.env"))
except ImportError:
    pass

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
TLDR_MODEL = "claude-haiku-4-5-20251001"  # same model/cost tier as house_digest.js's joke generation

MEAL_LOG_ID = "f553a32e-de2b-49fe-a098-3c2bc21ff2de"
WEIGHT_LOG_ID = "43e0af8d-53cd-4f23-be35-5f1f119161bc"
GARMIN_LOG_ID = "257e2cc4-baab-43a3-b691-ecaa78386241"
WORKOUT_LOG_ID = "b51ac742-2be5-4b15-b793-7209e534f32a"
FITNESS_PLAN_PAGE_ID = "39054896-5421-8148-8769-cdf10ca47ebe"  # source for health.html's lab summary

# ---- Goals / targets (confirmed with user 2026-08-13) ----
# Target = what to eat, fixed every day regardless of activity type.
# Maintenance = Garmin's actual estimated burn for that day when it looks
# plausible, otherwise the same 2,500 fallback. These are two different
# numbers that happen to share a value today -- kept separate so a future
# change to one doesn't silently change the other.
GOAL_PROTEIN = 152          # g, fixed daily protein goal
# Fat/carb goals aren't documented anywhere in Notion (checked -- only
# qualitative nutrition direction on the Fitness Plan page, no gram figures)
# so these are derived, not sourced: fat at ~30% of the 2,500 kcal target
# (750 kcal / 9 kcal per g), carbs filling the remainder after protein + fat.
# Confirmed with user 2026-08-13.
GOAL_FAT = 83               # g (~747 kcal, ~30% of TARGET_CALORIES)
GOAL_CARBS = 286            # g (~1144 kcal, remainder of TARGET_CALORIES)
TARGET_CALORIES = 2500      # kcal, flat daily eating target (no activity-based shifting)
TDEE_FALLBACK = 2500        # kcal, assumed maintenance when Garmin is missing/implausible
GARMIN_MIN_PLAUSIBLE = 1500 # below this, Garmin's Calories Burned is treated
                             # as implausible / device-not-worn (same heuristic
                             # as the "Device Off" flag in Energy Balance
                             # Reconciliation)

TRAILING_DAYS = 30
WEIGHT_LOOKBACK_BUFFER_DAYS = 14  # extra history pulled so the 7-day rolling
                                  # avg is populated even on day 1 of the window
KCAL_PER_KG_FAT = 7700  # standard approximation for kcal per kg of body fat;
                         # doesn't account for water/glycogen swings, so
                         # "expected" and "actual" loss will diverge some weeks

# Personal-trainer-led sessions are named "PT-<trainer>" (e.g. "PT-Nabil") in
# the Workout Log, and the trainer's name sometimes shows up in Notes on
# related entries even when the session itself isn't titled "PT-...".
PT_NAME_HINTS = ["nabil"]

ALCOHOL_KEYWORDS = [
    "beer", "wine", "whisky", "whiskey", "vodka", "rum", "gin", "cocktail",
    "alcohol", "cider", "prosecco", "champagne",
]
# This person drinks 0.0% versions a lot ("alcohol-free beer/wine/spritz") --
# strip the modifier *and* the noun it qualifies (e.g. "alcohol-free beers"
# -> both words gone) before keyword matching, or "beer"/"wine" still hits.
ALCOHOL_MODIFIER_RE = re.compile(
    r"(alcohol[- ]free|non[- ]?alcoholic|zero[- ]proof|0\.0%?)\s*\S*", re.I
)
# Named non-alcoholic drinks / serving-vessel references that contain a
# keyword as a genuine whole word (so word-boundary matching alone won't
# save them): "ginger beer" and "root beer" are soft drinks, "wine glass"
# here is just describing the glass size, not wine itself.
ALCOHOL_PHRASE_EXCLUSIONS = ["ginger beer", "root beer", "wine glass"]
ALCOHOL_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in ALCOHOL_KEYWORDS) + r")\b", re.I
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "docs", "index.html")
HEALTH_OUTPUT_PATH = os.path.join(SCRIPT_DIR, "docs", "health.html")  # not linked from index.html on purpose


# ---------------------------------------------------------------- fetching

def notion_post(path, payload):
    resp = requests.post(f"{NOTION_API}{path}", headers=HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def notion_get(path):
    resp = requests.get(f"{NOTION_API}{path}", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def extract_properties(props):
    """Flatten Notion property objects into plain Python values."""
    out = {}
    for name, val in props.items():
        t = val.get("type")
        if t == "number":
            out[name] = val.get("number")
        elif t == "select":
            out[name] = val.get("select", {}).get("name") if val.get("select") else None
        elif t == "date":
            out[name] = val.get("date", {}).get("start") if val.get("date") else None
        elif t == "title":
            texts = val.get("title", [])
            out[name] = "".join(t.get("plain_text", "") for t in texts)
        elif t == "rich_text":
            texts = val.get("rich_text", [])
            out[name] = "".join(t.get("plain_text", "") for t in texts)
        elif t == "checkbox":
            out[name] = val.get("checkbox")
    return out


def query_range(data_source_id, start_date, end_date):
    """Query a data source with a proper `Date` date-type property."""
    payload = {
        "filter": {
            "and": [
                {"property": "Date", "date": {"on_or_after": start_date}},
                {"property": "Date", "date": {"on_or_before": end_date}},
            ]
        },
        "sorts": [{"property": "Date", "direction": "ascending"}],
        "page_size": 100,
    }
    results = notion_post(f"/data_sources/{data_source_id}/query", payload).get("results", [])
    out = []
    for r in results:
        props = extract_properties(r["properties"])
        props["_page_id"] = r["id"]
        props["_url"] = r["url"]
        out.append(props)
    return out


WEIGHT_DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y"]


def parse_weight_title_date(title_text):
    for fmt in WEIGHT_DATE_FORMATS:
        try:
            return dt.datetime.strptime(title_text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def query_weight_log(start_date, end_date, page_size=100):
    """Daily Weight Log's `Date` property is a title (text), not a real date
    property, so we can't filter server-side by date -- pull recent rows
    sorted by creation time and parse the title client-side instead."""
    payload = {
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "page_size": page_size,
    }
    results = notion_post(f"/data_sources/{WEIGHT_LOG_ID}/query", payload).get("results", [])
    out = {}
    for r in results:
        props = extract_properties(r["properties"])
        d = parse_weight_title_date(props.get("Date", ""))
        if d and start_date <= d <= end_date:
            out[d.isoformat()] = props.get("Weight (kg)")
    return out


def rolling_7day_avg(weight_by_date, d):
    """Average of whatever weigh-ins fall in the 7 calendar days ending on
    `d` (inclusive). Doesn't require exactly 7 data points -- sparse logging
    is normal here -- but returns None if there are zero in that window."""
    vals = []
    for i in range(7):
        day = d - dt.timedelta(days=i)
        w = weight_by_date.get(day.isoformat())
        if w is not None:
            vals.append(w)
    return round(sum(vals) / len(vals), 2) if vals else None


WEIGHT_TREND_FLAT_THRESHOLD_KG = 0.2  # smaller than this change over a week = "flat", not noise


def compute_weight_trend(weight_by_date, today):
    """Compares this week's rolling avg to the rolling avg from 7 days
    earlier. Returns None if either side has no data at all."""
    latest_avg = rolling_7day_avg(weight_by_date, today)
    prior_avg = rolling_7day_avg(weight_by_date, today - dt.timedelta(days=7))
    if latest_avg is None or prior_avg is None:
        return None
    change = round(latest_avg - prior_avg, 2)
    if abs(change) < WEIGHT_TREND_FLAT_THRESHOLD_KG:
        direction = "flat"
    elif change < 0:
        direction = "down"
    else:
        direction = "up"
    return {
        "latest_7day_avg": latest_avg,
        "prior_7day_avg": prior_avg,
        "change_kg": change,
        "direction": direction,
    }


TREND_WINDOW_DAYS = 7  # smoothing window for the chart trend lines


def trailing_avg(days, key, i, window=TREND_WINDOW_DAYS):
    """Rolling average of `days[j][key]` over the `window` days ending at
    index i, skipping missing values. None if nothing in that window has data
    -- doesn't interpolate across gaps."""
    vals = [days[j][key] for j in range(max(0, i - window + 1), i + 1) if days[j].get(key) is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def compute_streak(days):
    """Consecutive-day run of same-sign deficit, walking backward from the
    most recent *complete* day (today is excluded while it's still partial --
    a day in progress can't confirm or break a streak yet). Stops at the
    first missing/partial day too, so a data gap honestly breaks the streak
    instead of silently skipping over it."""
    start = None
    for i in range(len(days) - 1, -1, -1):
        d = days[i]
        if d["has_data"] and not d["is_partial"] and d["deficit"] is not None:
            start = i
            break
    if start is None:
        return None

    sign_deficit = days[start]["deficit"] >= 0
    count = 0
    recent_deficits = []
    i = start
    while i >= 0:
        d = days[i]
        if not d["has_data"] or d["is_partial"] or d["deficit"] is None:
            break
        if (d["deficit"] >= 0) != sign_deficit:
            break
        count += 1
        recent_deficits.append(d["deficit"])
        i -= 1

    last7 = recent_deficits[:7]
    return {
        "count": count,
        "direction": "deficit" if sign_deficit else "surplus",
        "avg_deficit_7d": round(sum(last7) / len(last7)) if last7 else None,
    }


def compute_window_summary(days, weight_by_date, start, today):
    """Whole-window (trailing TRAILING_DAYS) rollup: total logged deficit,
    the weight loss that deficit implies, and what actually happened on the
    scale. Today is excluded from the deficit total while it's still partial
    -- an incomplete day would understate today's real deficit and throw the
    total off. Actual change compares 7-day rolling averages at each end of
    the window (not single readings) so one noisy weigh-in doesn't dominate."""
    counted = [d for d in days if d["deficit"] is not None and not d["is_partial"]]
    total_deficit = sum(d["deficit"] for d in counted)
    expected_loss_kg = round(total_deficit / KCAL_PER_KG_FAT, 2)

    start_avg = rolling_7day_avg(weight_by_date, start)
    end_avg = rolling_7day_avg(weight_by_date, today)
    actual_loss_kg = round(start_avg - end_avg, 2) if (start_avg is not None and end_avg is not None) else None

    return {
        "total_deficit": round(total_deficit),
        "days_counted": len(counted),
        "days_total": len(days),
        "expected_loss_kg": expected_loss_kg,
        "actual_loss_kg": actual_loss_kg,
        "start_weight_avg": start_avg,
        "end_weight_avg": end_avg,
    }


def compute_macro_stats(days):
    """Distribution of logged days into distance-from-target buckets, for
    protein (target is a floor -- more is fine, less is the problem) and
    carbs/fat (target is a ceiling -- more is the problem). Buckets are
    pct-of-goal, not absolute grams, so the same bucket logic works across
    goals of very different sizes (83g fat vs 286g carbs). The two "off
    target" buckets on each macro's bad side share a color but differ in
    opacity, so severity still reads at a glance without needing a 5th
    color."""
    specs = [("protein", GOAL_PROTEIN, "floor"), ("carbs", GOAL_CARBS, "ceiling"), ("fat", GOAL_FAT, "ceiling")]
    out = []
    for key, goal, kind in specs:
        vals = [d[key] for d in days if d["has_data"] and not d["is_partial"] and d.get(key) is not None]
        total = len(vals)
        # "upper" is each bucket's boundary in grams (None on the open-ended
        # last bucket) -- used to label where the bar's color actually
        # switches, so a reader isn't left guessing what "well under" means
        # in absolute terms for this macro.
        if kind == "floor":
            hit = sum(1 for v in vals if v >= goal)
            bucket_defs = [
                ("well under", "bad", 1.0, round(goal * 0.75), lambda v: v < goal * 0.75),
                ("under", "bad", 0.5, round(goal * 0.90), lambda v: goal * 0.75 <= v < goal * 0.90),
                ("just under", "accent", 1.0, round(goal * 1.00), lambda v: goal * 0.90 <= v < goal * 1.00),
                ("at/above target", "good", 1.0, None, lambda v: v >= goal * 1.00),
            ]
        else:
            hit = sum(1 for v in vals if v <= goal)
            bucket_defs = [
                ("at/under limit", "good", 1.0, round(goal * 1.00), lambda v: v <= goal * 1.00),
                ("slightly over", "accent", 1.0, round(goal * 1.15), lambda v: goal * 1.00 < v <= goal * 1.15),
                ("over", "bad", 0.5, round(goal * 1.30), lambda v: goal * 1.15 < v <= goal * 1.30),
                ("well over", "bad", 1.0, None, lambda v: v > goal * 1.30),
            ]
        buckets = []
        for label, cls, opacity, upper, test in bucket_defs:
            count = sum(1 for v in vals if test(v))
            buckets.append({
                "label": label, "cls": cls, "opacity": opacity, "count": count,
                "pct": round(100 * count / total) if total else 0, "upper": upper,
            })
        out.append({
            "key": key, "goal": goal, "kind": kind, "hit": hit, "total": total,
            "hit_pct": round(100 * hit / total) if total else None,
            "buckets": buckets,
        })
    return out


CORR_MIN_N = 5  # both groups need at least this many data points to even be
                # considered -- below this, it's an anecdote, not a finding
CORR_MEANINGFUL_DELTA = {  # minimum |avg difference| worth reporting, per unit --
    "kcal deficit": 150,   # smaller than this is noise-level given how much
    "kcal eaten": 150,     # day-to-day calorie estimates already bounce around
    "g protein": 6,
    "h sleep": 0.3,
}


def _corr_groups(days, key_fn, outcome_fn, offset):
    """Groups days by a boolean key_fn (evaluated on day i) and collects
    outcome_fn's value on day (i + offset) into a yes/no list --
    offset=0 same-day, +1 next-day, -1 previous-day."""
    yes_vals, no_vals = [], []
    for i in range(len(days)):
        j = i + offset
        if j < 0 or j >= len(days):
            continue
        key = key_fn(days[i])
        if key is None:
            continue
        outcome_day = days[j]
        if not outcome_day["has_data"] or outcome_day["is_partial"]:
            continue
        val = outcome_fn(outcome_day)
        if val is None:
            continue
        (yes_vals if key else no_vals).append(val)
    return yes_vals, no_vals


def _corr_row(label, unit, group_names, yes_vals, no_vals, headline_fn):
    """Turns two value lists into a finished insight, or None if it isn't
    actually one: too little data on either side, or a difference too small
    to mean anything, gets dropped entirely -- not shown with a caveat."""
    if len(yes_vals) < CORR_MIN_N or len(no_vals) < CORR_MIN_N:
        return None
    avg_yes = round(sum(yes_vals) / len(yes_vals), 1)
    avg_no = round(sum(no_vals) / len(no_vals), 1)
    delta = round(abs(avg_yes - avg_no), 1)
    if delta < CORR_MEANINGFUL_DELTA.get(unit, 0):
        return None
    return {
        "label": label,
        "unit": unit,
        "headline": headline_fn(avg_yes, avg_no, delta),
        "groups": [
            {"name": group_names[0], "n": len(yes_vals), "avg": avg_yes},
            {"name": group_names[1], "n": len(no_vals), "avg": avg_no},
        ],
    }


def compute_correlations(days):
    """Grouped-average comparisons using only fields actually present in the
    data -- no invented factors (no sauna/bedroom-temp/recovery% -- this
    dataset doesn't track those). Each candidate is dropped if the
    underlying field never appears, if either side has too few days, or if
    the two sides barely differ -- what survives is meant to be worth
    reading, not a full dump of every comparison that could be computed."""
    rows = []

    yes, no = _corr_groups(days, lambda d: d["is_pt"] if d["has_data"] else None, lambda d: d["protein"], 0)
    rows.append(_corr_row(
        "PT-led workout -> same-day protein intake", "g protein", ("PT day", "Solo/rest day"), yes, no,
        lambda ay, an, d: f"On PT days you eat about {d:g}g {'more' if ay > an else 'less'} protein than on solo/rest days",
    ))

    yes, no = _corr_groups(days, lambda d: d["is_pt"] if d["has_data"] else None, lambda d: d["deficit"], 0)
    rows.append(_corr_row(
        "PT-led workout -> same-day deficit", "kcal deficit", ("PT day", "Solo/rest day"), yes, no,
        lambda ay, an, d: f"PT days run a {'bigger' if ay > an else 'smaller'} deficit than solo/rest days, by about {d:g} kcal",
    ))

    yes, no = _corr_groups(days, lambda d: d["has_alcohol"] if d["has_data"] else None, lambda d: d["deficit"], 1)
    rows.append(_corr_row(
        "Alcohol logged -> next-day deficit", "kcal deficit", ("After alcohol", "No alcohol"), yes, no,
        lambda ay, an, d: f"The day after alcohol, your deficit runs {'bigger' if ay > an else 'smaller'} by about {d:g} kcal",
    ))

    yes, no = _corr_groups(days, lambda d: d["has_alcohol"] if d["has_data"] else None, lambda d: d["sleep_hours"], 1)
    rows.append(_corr_row(
        "Alcohol logged -> next-day sleep", "h sleep", ("After alcohol", "No alcohol"), yes, no,
        lambda ay, an, d: f"The day after alcohol, you sleep {'more' if ay > an else 'less'} by about {d:g}h",
    ))

    yes, no = _corr_groups(
        days, lambda d: (d["sleep_hours"] < 7) if d["sleep_hours"] is not None else None, lambda d: d["deficit"], 1
    )
    rows.append(_corr_row(
        "Sleep under 7h -> next-day deficit", "kcal deficit", ("Short sleep (<7h)", "7h+ sleep"), yes, no,
        lambda ay, an, d: f"After a night under 7h sleep, next day's deficit runs {'bigger' if ay > an else 'smaller'} by about {d:g} kcal than after 7h+",
    ))

    eaten_complete = [d["eaten"] for d in days if d["eaten"] is not None and not d["is_partial"]]
    median_eaten = statistics.median(eaten_complete) if eaten_complete else None
    if median_eaten is not None:
        yes, no = _corr_groups(
            days,
            lambda d: (d["eaten"] > median_eaten) if (d["eaten"] is not None and not d["is_partial"]) else None,
            lambda d: d["eaten"], 1,
        )
        rows.append(_corr_row(
            "Above-median calorie day -> next-day calories eaten", "kcal eaten",
            ("After high-cal day", "After low-cal day"), yes, no,
            lambda ay, an, d: f"After an above-median calorie day, you eat {'more' if ay > an else 'less'} the next day, by about {d:g} kcal"
                               + (" -- a rebound pattern worth watching" if ay > an else ""),
        ))

    protein_share = [
        d["protein_kcal"] / d["eaten"] for d in days
        if d["protein_kcal"] is not None and d["eaten"] and not d["is_partial"]
    ]
    median_share = statistics.median(protein_share) if protein_share else None
    if median_share is not None:
        yes, no = _corr_groups(
            days,
            lambda d: ((d["protein_kcal"] / d["eaten"]) > median_share)
                      if (d["protein_kcal"] is not None and d["eaten"] and not d["is_partial"]) else None,
            lambda d: d["deficit"], 0,
        )
        rows.append(_corr_row(
            "Higher protein share of calories -> same-day deficit", "kcal deficit",
            ("Higher protein %", "Lower protein %"), yes, no,
            lambda ay, an, d: f"Days with a higher share of calories from protein run a {'bigger' if ay > an else 'smaller'} deficit, by about {d:g} kcal",
        ))

    rhr_vals = [d["resting_hr"] for d in days if d["resting_hr"] is not None]
    median_rhr = statistics.median(rhr_vals) if rhr_vals else None
    if median_rhr is not None:
        yes, no = _corr_groups(
            days, lambda d: (d["resting_hr"] > median_rhr) if d["resting_hr"] is not None else None,
            lambda d: d["deficit"], -1,
        )
        rows.append(_corr_row(
            "Elevated resting HR -> previous-day deficit", "kcal deficit",
            ("Elevated RHR day", "Typical RHR day"), yes, no,
            lambda ay, an, d: f"{'A bigger' if ay > an else 'A smaller'} deficit the day before tends to show up as elevated resting HR the next morning (~{d:g} kcal difference)",
        ))

    return [r for r in rows if r is not None]


def fetch_meal_items(page_id):
    """Parse the day's meal table (and any Notes paragraph) from page content."""
    blocks = notion_get(f"/blocks/{page_id}/children").get("results", [])
    items = []
    notes_lines = []
    for b in blocks:
        btype = b.get("type")
        if btype == "table" and b.get("has_children"):
            rows = notion_get(f"/blocks/{b['id']}/children").get("results", [])
            data_rows = [r for r in rows if r.get("type") == "table_row"]
            if not data_rows:
                continue
            header_cells = [
                "".join(rt.get("plain_text", "") for rt in cell)
                for cell in data_rows[0]["table_row"]["cells"]
            ]
            for row in data_rows[1:]:
                cells = [
                    "".join(rt.get("plain_text", "") for rt in cell)
                    for cell in row["table_row"]["cells"]
                ]
                rec = dict(zip(header_cells, cells))

                def num(key):
                    try:
                        return float(rec.get(key, "").strip())
                    except (ValueError, AttributeError):
                        return 0.0

                items.append({
                    "name": rec.get("Meal", "").strip(),
                    "slot": rec.get("Time of Day", "").strip(),
                    "calories": num("Calories"),
                    "protein": num("Protein (g)"),
                    "fat": num("Fat (g)"),
                    "carbs": num("Carbs (g)"),
                })
        elif btype == "paragraph":
            text = "".join(
                rt.get("plain_text", "") for rt in b.get("paragraph", {}).get("rich_text", [])
            ).strip()
            if text.lower().startswith("notes:") and text.lower() != "notes:":
                notes_lines.append(text)

    total_cal = sum(i["calories"] for i in items) or 1
    for i in items:
        i["pct_of_day"] = round(i["calories"] / total_cal * 100, 1)

    has_alcohol = any(mentions_alcohol(i["name"]) for i in items)

    return items, " ".join(notes_lines), has_alcohol


def mentions_alcohol(text):
    """Whole-word keyword match (so "gin" doesn't hit "ginger", "rum" doesn't
    hit "crumble"), with "alcohol-free <noun>" and named non-alcoholic
    drinks/phrases stripped out first."""
    lowered = text.lower()
    lowered = ALCOHOL_MODIFIER_RE.sub("", lowered)
    for phrase in ALCOHOL_PHRASE_EXCLUSIONS:
        lowered = lowered.replace(phrase, "")
    return bool(ALCOHOL_KEYWORD_RE.search(lowered))


def block_plain_text(block):
    t = block.get("type")
    rich_text = block.get(t, {}).get("rich_text", [])
    return "".join(rt.get("plain_text", "") for rt in rich_text)


def fetch_health_summary(page_id):
    """Pulls the "Summary of key findings" section of the Fitness Plan page
    verbatim -- it's prose written by a person, not a clean properties
    table, so this carries it over as-is (grouped exactly as Notion has it)
    rather than force-fitting invented per-value dates that don't exist in
    the source. Re-fetched every run, so editing the Notion page is all it
    takes to refresh health.html's lab summary."""
    blocks = notion_get(f"/blocks/{page_id}/children?page_size=100").get("results", [])

    intro = None
    groups = []
    current = None
    in_summary = False
    for b in blocks:
        t = b["type"]
        if t == "heading_2":
            heading = block_plain_text(b).strip().lower()
            if heading == "summary of key findings":
                in_summary = True
                continue
            elif in_summary:
                break  # left the summary section
            continue
        if not in_summary:
            if t == "paragraph" and intro is None:
                text = block_plain_text(b).strip()
                if text:
                    intro = text
            continue
        if t == "paragraph":
            text = block_plain_text(b).strip()
            if text.endswith(":"):
                current = {"label": text.rstrip(":"), "items": []}
                groups.append(current)
        elif t == "bulleted_list_item" and current is not None:
            item = block_plain_text(b).strip()
            if item:
                current["items"].append(item)

    return {"intro": intro, "groups": groups}


def blocks_to_text(blocks, indent=0):
    """Recursively flattens a list of Notion blocks into plain indented
    text -- headings and paragraphs as their own line, list items prefixed
    with "- ". Recurses into has_children (the Weekly structure section
    nests its bullets under each paragraph header, unlike the flat
    sibling layout in the Summary section above)."""
    lines = []
    for b in blocks:
        t = b["type"]
        text = block_plain_text(b).strip()
        prefix = "  " * indent
        if t in ("heading_1", "heading_2", "heading_3") and text:
            lines.append(f"{prefix}{text}")
        elif t in ("bulleted_list_item", "numbered_list_item") and text:
            lines.append(f"{prefix}- {text}")
        elif t == "paragraph" and text:
            lines.append(f"{prefix}{text}")
        if b.get("has_children"):
            children = notion_get(f"/blocks/{b['id']}/children?page_size=100").get("results", [])
            lines.extend(blocks_to_text(children, indent + 1))
    return lines


def fetch_plan_text(page_id, section_title):
    """Extracts one heading_2 section of the Fitness Plan page as plain
    text (e.g. "The plan" -> Goals/Weekly structure/Nutrition direction),
    stopping at the next heading_2 or the trailing embedded databases. Used
    to ground the TL;DR's workout/nutrition recommendation in what's
    actually written there instead of generic fitness advice."""
    blocks = notion_get(f"/blocks/{page_id}/children?page_size=100").get("results", [])
    section_blocks = []
    capturing = False
    for b in blocks:
        if b["type"] == "heading_2":
            heading = block_plain_text(b).strip().lower()
            if heading == section_title.lower():
                capturing = True
                continue
            elif capturing:
                break
            continue
        if b["type"] == "child_database" and capturing:
            break
        if capturing:
            section_blocks.append(b)
    return "\n".join(blocks_to_text(section_blocks))


def find_last_complete_day(days):
    """Most recent day with a real, finished log -- "today" is usually
    still PENDING when this runs early morning, so this is effectively
    "yesterday" but doesn't assume anything about what time the cron job
    runs."""
    for d in reversed(days):
        if d["has_data"] and not d["is_partial"]:
            return d
    return None


def generate_tldr(data, plan_text):
    """One Claude Haiku call that turns yesterday's numbers + this week's
    trends + the actual weekly-structure text into a short daily briefing.
    Returns None (never raises) on any failure -- missing key, network
    error, empty response -- so a TL;DR outage never breaks the rest of the
    dashboard generation; the card just doesn't render that day."""
    if not ANTHROPIC_API_KEY:
        print("WARN: ANTHROPIC_API_KEY not set, skipping TL;DR")
        return None

    days = data["days"]
    yesterday = find_last_complete_day(days)
    if yesterday is None:
        return None

    recent = [d for d in days if d["has_data"] and not d["is_partial"]][-7:]
    week_activity = [
        {"date": d["date"], "workout": d["activity_badge"], "detail": d["activity_detail"]}
        for d in recent
    ]

    context = {
        "targets": {
            "calories": data["goal_calories"], "protein_g": data["goal_protein"],
            "carbs_g": data["goal_carbs"], "fat_g": data["goal_fat"],
        },
        "yesterday": {
            "date": yesterday["date"],
            "workout": yesterday["activity_badge"], "workout_detail": yesterday["activity_detail"],
            "eaten_kcal": yesterday["eaten"], "deficit_kcal": yesterday["deficit"],
            "protein_g": yesterday["protein"], "carbs_g": yesterday["carbs"], "fat_g": yesterday["fat"],
            "sleep_hours": yesterday["sleep_hours"], "resting_hr": yesterday["resting_hr"],
            "hrv": yesterday["hrv"], "body_battery": yesterday["body_battery"],
            "alcohol": yesterday["has_alcohol"],
        },
        "trailing_7day_avg_as_of_yesterday": {
            "deficit_kcal": yesterday.get("deficit_trend"), "protein_g": yesterday.get("protein_trend"),
            "carbs_g": yesterday.get("carbs_trend"), "fat_g": yesterday.get("fat_trend"),
            "resting_hr": yesterday.get("rhr_trend"), "hrv": yesterday.get("hrv_trend"),
            "body_battery": yesterday.get("body_battery_trend"),
        },
        "weight_trend": data.get("weight_trend"),
        "last_7_logged_days_activity": week_activity,
    }

    prompt = f"""You write a short daily fitness TL;DR for someone tracking calories, macros, weight, workouts, and Garmin recovery metrics (resting HR, HRV, body battery). It's read as a scannable list of short sections, not a paragraph -- so each field below must stand alone.

Their actual weekly plan, from their own notes -- ground your workout call in this, not generic advice:
---
{plan_text}
---

Data (yesterday = their most recent complete logged day; trailing_7day_avg_as_of_yesterday = 7-day rolling averages as of yesterday):
{json.dumps(context, indent=2)}

Fill in the emit_tldr tool call:
- workout: did they train yesterday (what kind, or rest)? 1 short sentence.
- diet: calories/deficit and macros vs target yesterday -- call out anything notably over/under. 1 short sentence.
- weight: why the weight trend moved (or held), tied to the deficit trend -- not just restating the kg number. 1 short sentence. Leave empty if there isn't enough weight data.
- recovery: weigh resting HR, HRV, and body battery TOGETHER against their trailing averages -- say plainly if recovery looks fine, shows elevated stress, or looks under-recovered. 1 short sentence.
- today: concrete instructions for today -- a calorie/carb note plus a specific workout call (rest / strength / cardio / mobility), reasoned from what's due per their weekly plan and how recovery looks. 1-2 short sentences.

Rules: only use numbers actually given above, never invent or estimate a figure that isn't present. If a data point is missing (null), just don't mention it -- don't guess or say "unknown" out loud. Plain prose only, no markdown (no asterisks, bold, bullets, headers) -- each field is rendered as its own already-labeled line, second person ("you")."""

    tool = {
        "name": "emit_tldr",
        "description": "Emit the structured daily TL;DR, one short plain-prose sentence per section.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workout": {"type": "string"},
                "diet": {"type": "string"},
                "weight": {"type": "string"},
                "recovery": {"type": "string"},
                "today": {"type": "string"},
            },
            "required": ["workout", "diet", "weight", "recovery", "today"],
        },
    }

    try:
        resp = requests.post(
            ANTHROPIC_API,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": TLDR_MODEL,
                "max_tokens": 500,
                "tools": [tool],
                "tool_choice": {"type": "tool", "name": "emit_tldr"},
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        tool_use = next((b for b in result.get("content", []) if b.get("type") == "tool_use"), None)
        if not tool_use:
            print("WARN: TL;DR response had no tool_use block, omitting")
            return None
        fields = tool_use.get("input", {})
        # belt-and-suspenders: strip stray markdown even though the prompt
        # asks for plain prose -- these render via textContent, not an HTML
        # parser, so literal asterisks/backticks would otherwise show up
        cleaned = {k: re.sub(r"[*_`#]+", "", (v or "").strip()) for k, v in fields.items()}
        if not any(cleaned.values()):
            return None
        return cleaned
    except Exception as e:
        print(f"WARN: TL;DR generation failed, omitting: {e}")
        return None


# ------------------------------------------------------------- derivations

WORKOUT_NAME_HINTS = [
    ("cycl", "CYCLING"), ("bike", "CYCLING"),
    ("swim", "SWIMMING"),
    ("row", "ROWING"),
    ("walk", "WALK"),
    ("squash", "SQUASH"),
    ("padel", "PADEL"),
    ("mobility", "MOBILITY"), ("stretch", "MOBILITY"), ("yoga", "MOBILITY"),
]

GARMIN_ACTIVITY_BADGE = {
    "Strength": "LIFTING",
    "Zone2 Cardio": "CARDIO",
    "Squash": "SQUASH",
    "Padel": "PADEL",
    "Rowing": "ROWING",
    "Rest": "REST",
    "Other": "ACTIVE",
}

WORKOUT_ACTIVITY_BADGE = {
    "Zone 2 Cardio": "CARDIO",
    "Strength": "LIFTING",
    "Squash": "SQUASH",
    "Padel": "PADEL",
    "Rowing": "ROWING",
    "Weighted Walk": "WALK",
    "Mobility": "MOBILITY",
    "Other": "ACTIVE",
}


GENERIC_ACTIVITY_TYPES = {"Zone 2 Cardio", "Other", None}


def derive_badge(workout_entries, garmin_entry):
    """Workout Log (manual, specific) takes priority over Garmin Log's
    coarser Activity Type; falls back to Garmin, then to a pending marker.

    Name-based hints (e.g. "swim", "cycl") only apply when the structured
    Activity Type is generic ("Zone 2 Cardio" / "Other") -- otherwise a
    specific type like "Strength" is trusted as-is, since exercise names
    (e.g. "Lat Rows") can spuriously contain hint substrings like "row"."""
    if workout_entries:
        we = workout_entries[0]
        activity_type = we.get("Activity Type")
        name = (we.get("Name") or "").lower()

        if activity_type in GENERIC_ACTIVITY_TYPES:
            for hint, badge in WORKOUT_NAME_HINTS:
                if hint in name:
                    return badge, we.get("Name")

        badge = WORKOUT_ACTIVITY_BADGE.get(activity_type, "ACTIVE")
        return badge, we.get("Name")

    if garmin_entry and garmin_entry.get("Activity Type"):
        badge = GARMIN_ACTIVITY_BADGE.get(garmin_entry["Activity Type"], "ACTIVE")
        return badge, None

    return "PENDING", None


def derive_pt_flag(workout_entries):
    """PT-led sessions are named "PT-<trainer>" in the Workout Log; the
    trainer's name can also show up in Notes on a related entry even when
    the entry itself isn't titled that way. Only flags what's actually
    written -- no PT sessions in the window just means this stays False."""
    for we in workout_entries:
        name = (we.get("Name") or "").lower()
        notes = (we.get("Notes") or "").lower()
        if re.search(r"\bpt\b", name) or re.search(r"\bpt-", name):
            return True
        if any(hint in name or hint in notes for hint in PT_NAME_HINTS):
            return True
    return False


WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def build_day(d, meal, garmin, workouts, weight, weight_7day_avg, today):
    date_str = d.isoformat()
    label = f"{WEEKDAY_ABBR[d.weekday()]} {d.day}"
    is_today = d == today

    eaten = (meal or {}).get("Total Calories (kcal)")
    protein = (meal or {}).get("Total Protein (g)")
    fat = (meal or {}).get("Total Fat (g)")
    carbs = (meal or {}).get("Total Carbs (g)")
    meal_count = (meal or {}).get("Meal Count")

    items, notes, has_alcohol = ([], "", False)
    if meal and meal.get("_page_id"):
        items, notes, has_alcohol = fetch_meal_items(meal["_page_id"])

    # Maintenance: Garmin's actual estimate when it looks real, else the
    # fixed fallback. Target: flat every day, independent of maintenance.
    garmin_cal = (garmin or {}).get("Calories Burned")
    garmin_valid = garmin_cal is not None and garmin_cal >= GARMIN_MIN_PLAUSIBLE
    maintenance = garmin_cal if garmin_valid else TDEE_FALLBACK

    deficit = (maintenance - eaten) if eaten is not None else None
    over_target = (eaten is not None and eaten > TARGET_CALORIES)

    badge, activity_detail = derive_badge(workouts, garmin)
    is_pt = derive_pt_flag(workouts)

    is_partial = is_today and bool(eaten is not None and (meal_count or 0) > 0)
    has_data = eaten is not None

    return {
        "date": date_str,
        "label": label,
        "is_today": is_today,
        "is_partial": is_partial,
        "has_data": has_data,
        "eaten": eaten,
        "protein": protein,
        "fat": fat,
        "carbs": carbs,
        "protein_kcal": round(protein * 4, 1) if protein is not None else None,
        "carbs_kcal": round(carbs * 4, 1) if carbs is not None else None,
        "fat_kcal": round(fat * 9, 1) if fat is not None else None,
        "meal_count": meal_count,
        "items": items,
        "notes": notes,
        "has_alcohol": has_alcohol,
        # Kept as tdee_garmin/tdee_fixed/tdee_primary for the existing HTML
        # template's JS -- tdee_primary is now "maintenance", not "target".
        "tdee_garmin": garmin_cal,
        "tdee_garmin_valid": garmin_valid,
        "tdee_fixed": TDEE_FALLBACK,
        "tdee_primary": maintenance,
        "maintenance": maintenance,
        "target_calories": TARGET_CALORIES,
        "over_target": over_target,
        "deficit": deficit,
        "steps": (garmin or {}).get("Steps"),
        "sleep_hours": (garmin or {}).get("Sleep Hours"),
        "resting_hr": (garmin or {}).get("Resting HR"),
        "hrv": (garmin or {}).get("HRV"),
        "body_battery": (garmin or {}).get("Body Battery"),
        "zone2_minutes": (garmin or {}).get("Zone 2 Minutes"),
        "weight": weight,
        "weight_7day_avg": weight_7day_avg,
        "activity_badge": badge,
        "activity_detail": activity_detail,
        "is_pt": is_pt,
    }


# ------------------------------------------------------------------- main

def collect():
    if not NOTION_TOKEN:
        sys.exit("ERROR: set NOTION_TOKEN environment variable (see .env).")

    today = dt.date.today()
    start = today - dt.timedelta(days=TRAILING_DAYS - 1)
    weight_fetch_start = start - dt.timedelta(days=WEIGHT_LOOKBACK_BUFFER_DAYS)

    meal_rows = query_range(MEAL_LOG_ID, start.isoformat(), today.isoformat())
    garmin_rows = query_range(GARMIN_LOG_ID, start.isoformat(), today.isoformat())
    workout_rows = query_range(WORKOUT_LOG_ID, start.isoformat(), today.isoformat())
    weight_by_date = query_weight_log(weight_fetch_start, today, page_size=100)

    meal_by_date = {r.get("Date"): r for r in meal_rows}
    garmin_by_date = {r.get("Date"): r for r in garmin_rows}
    workouts_by_date = {}
    for r in workout_rows:
        workouts_by_date.setdefault(r.get("Date"), []).append(r)

    days = []
    for i in range(TRAILING_DAYS):
        d = start + dt.timedelta(days=i)
        ds = d.isoformat()
        day = build_day(
            d,
            meal_by_date.get(ds),
            garmin_by_date.get(ds),
            workouts_by_date.get(ds, []),
            weight_by_date.get(ds),
            rolling_7day_avg(weight_by_date, d),
            today,
        )
        days.append(day)

    # Chart trend lines (7-day rolling averages) -- computed as a second pass
    # since each point needs its trailing neighbors already built.
    for i, day in enumerate(days):
        day["eaten_trend"] = trailing_avg(days, "eaten", i)
        day["deficit_trend"] = trailing_avg(days, "deficit", i)
        day["protein_trend"] = trailing_avg(days, "protein", i)
        day["carbs_trend"] = trailing_avg(days, "carbs", i)
        day["fat_trend"] = trailing_avg(days, "fat", i)
        day["sleep_trend"] = trailing_avg(days, "sleep_hours", i)
        day["rhr_trend"] = trailing_avg(days, "resting_hr", i)
        day["hrv_trend"] = trailing_avg(days, "hrv", i)
        day["body_battery_trend"] = trailing_avg(days, "body_battery", i)

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "goal_calories": TARGET_CALORIES,
        "goal_protein": GOAL_PROTEIN,
        "goal_fat": GOAL_FAT,
        "goal_carbs": GOAL_CARBS,
        "weight_trend": compute_weight_trend(weight_by_date, today),
        "streak": compute_streak(days),
        "window_summary": compute_window_summary(days, weight_by_date, start, today),
        "macro_stats": compute_macro_stats(days),
        "kcal_per_kg": KCAL_PER_KG_FAT,
        "correlations": compute_correlations(days),
        "corr_min_n": CORR_MIN_N,
        "days": days,
    }


# ------------------------------------------------------------- HTML render

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fitness Dashboard</title>
<style>
  :root {
    --bg: #0a0b0e;
    --surface: #131419;
    --surface-2: #1a1c22;
    --border: #242631;
    --text: #edeef2;
    --text-dim: #9497a3;
    --text-faint: #5a5c66;
    --accent: #e8a33d;
    --good: #33c692;
    --bad: #ef6259;
    --info: #5b93f0;
    --grid: rgba(255,255,255,0.06);
    --grid-strong: rgba(255,255,255,0.2);
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    padding: 18px 14px 56px;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 640px; margin: 0 auto; }

  .title-row {
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 14px;
  }
  .title-row h1 { font-size: 15px; font-weight: 650; letter-spacing: 0.2px; margin: 0; }
  .updated { font-size: 10px; color: var(--text-faint); font-variant-numeric: tabular-nums; }

  .tldr-card {
    background: linear-gradient(155deg, rgba(232,163,61,0.10), rgba(232,163,61,0.02));
    border: 1px solid rgba(232,163,61,0.28); border-radius: 14px;
    padding: 12px 14px; margin-bottom: 16px;
  }
  .tldr-label {
    font-size: 9px; font-weight: 700; letter-spacing: 1.1px; color: var(--accent);
    text-transform: uppercase; margin-bottom: 8px;
  }
  .tldr-row { display: flex; gap: 8px; padding: 5px 0; align-items: baseline; }
  .tldr-row + .tldr-row { border-top: 1px solid rgba(255,255,255,0.06); }
  .tldr-row-icon { font-size: 11.5px; flex-shrink: 0; width: 15px; text-align: center; }
  .tldr-row-body { min-width: 0; }
  .tldr-row-name {
    font-size: 9.5px; font-weight: 700; letter-spacing: 0.4px; color: var(--accent);
    text-transform: uppercase; margin-bottom: 1px;
  }
  .tldr-row-text { font-size: 12px; line-height: 1.5; color: var(--text); }
  .tldr-row.today { margin-top: 3px; padding-top: 9px; border-top: 1px dashed rgba(232,163,61,0.35); }
  .tldr-row.today .tldr-row-name { color: var(--text); }
  .tldr-row.today .tldr-row-text { font-weight: 550; }

  .period-label {
    font-size: 10px; font-weight: 700; letter-spacing: 1.1px; color: var(--text-faint);
    text-transform: uppercase; margin: 0 2px 6px;
  }
  .stat-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 10px; }
  .stat-grid + .period-label { margin-top: 14px; }
  .stat-tile {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 11px 10px; min-width: 0;
  }
  .stat-eyebrow {
    font-size: 9px; font-weight: 700; letter-spacing: 1px; color: var(--text-faint);
    text-transform: uppercase; white-space: nowrap;
  }
  .stat-value {
    font-size: 18px; font-weight: 650; margin-top: 5px; letter-spacing: -0.2px;
    font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .stat-value.good { color: var(--good); }
  .stat-value.bad { color: var(--bad); }
  .stat-sub { font-size: 9.5px; color: var(--text-dim); margin-top: 3px; }

  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
    padding: 13px 12px 8px; margin-bottom: 12px;
  }
  .card-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 6px; gap: 8px; }
  .card-label {
    font-size: 10px; font-weight: 700; letter-spacing: 1.1px; color: var(--text-faint);
    text-transform: uppercase; white-space: nowrap;
  }
  .legend { display: flex; gap: 9px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
  .legend-item { display: flex; align-items: center; font-size: 9px; color: var(--text-faint); white-space: nowrap; }
  .legend-toggle { cursor: pointer; padding: 2px 5px; border-radius: 5px; transition: background 0.1s, opacity 0.15s; }
  .legend-toggle:hover { background: var(--surface-2); }
  .legend-toggle.dimmed { opacity: 0.4; }
  .legend-toggle.active { background: var(--surface-2); color: var(--text); font-weight: 650; }
  .legend-item .dot { width: 6px; height: 6px; border-radius: 50%; margin-right: 3px; flex-shrink: 0; display: inline-block; }
  .legend-item .dash { width: 9px; height: 2px; margin-right: 3px; flex-shrink: 0; border-radius: 1px; display: inline-block; }
  .dot.good { background: var(--good); }
  .dot.bad { background: var(--bad); }
  .dot.info { background: var(--info); }
  .dot.accent { background: var(--accent); }
  .dash.accent { background: var(--accent); }

  .chart-svg { display: block; width: 100%; height: auto; touch-action: pan-y; }
  .bar { cursor: pointer; }
  .bar.good { fill: var(--good); }
  .bar.bad { fill: var(--bad); }
  .bar.info { fill: var(--info); }
  .bar.selected { filter: brightness(1.28); }
  .bar-empty { fill: var(--border); }
  .pt { fill: var(--info); cursor: pointer; }
  .pt.good { fill: var(--good); }
  .pt.bad { fill: var(--bad); }
  .pt.info { fill: var(--info); }
  .pt.accent { fill: var(--accent); }
  .pt.selected { fill: var(--text); }
  .hit { fill: transparent; cursor: pointer; }
  .sel-band { fill: rgba(232, 163, 61, 0.08); }
  .grid-line { stroke: var(--grid); stroke-width: 1; }
  .ref-line { stroke: var(--grid-strong); stroke-width: 1; stroke-dasharray: 3 3; }
  .ref-line.good { stroke: var(--good); opacity: 0.55; }
  .ref-line.info { stroke: var(--info); opacity: 0.55; }
  .ref-line.accent { stroke: var(--accent); opacity: 0.55; }
  .trend-line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  .trend-line.good { stroke: var(--good); }
  .trend-line.bad { stroke: var(--bad); }
  .trend-line.info { stroke: var(--info); }
  .trend-line.accent { stroke: var(--accent); }
  .macro-line { stroke-width: 1.5; opacity: 0.9; }
  .raw-line { fill: none; stroke: var(--info); stroke-width: 1; opacity: 0.5; }
  .axis-text { fill: var(--text-faint); font-size: 9px; font-variant-numeric: tabular-nums; }
  .axis-value { fill: var(--text-faint); font-size: 9px; font-variant-numeric: tabular-nums; }
  .ref-text { fill: var(--text-faint); font-size: 9px; }
  .ref-text.good { fill: var(--good); }
  .ref-text.info { fill: var(--info); }
  .ref-text.accent { fill: var(--accent); }

  .empty-note { font-size: 11.5px; color: var(--text-faint); padding: 14px 2px 6px; text-align: center; }

  .tooltip {
    position: fixed; z-index: 60; pointer-events: none;
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
    padding: 6px 10px; box-shadow: 0 6px 18px rgba(0,0,0,0.45);
    opacity: 0; transform: translate(-50%, calc(-100% - 10px)); transition: opacity 0.08s ease-out;
    white-space: nowrap; max-width: 220px; left: 0; top: 0;
  }
  .tooltip.visible { opacity: 1; }
  .tooltip .tt-date {
    font-size: 9.5px; color: var(--text-faint); margin-bottom: 2px; font-weight: 700;
    letter-spacing: 0.3px; text-transform: uppercase;
  }
  .tooltip .tt-value { font-size: 12px; color: var(--text); font-variant-numeric: tabular-nums; }

  .detail {
    margin-top: 18px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px;
  }
  .detail-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; gap: 8px; }
  .detail-date { font-size: 14.5px; font-weight: 650; }
  .badge {
    font-size: 10px; font-weight: 700; letter-spacing: 0.6px;
    padding: 3px 8px; border-radius: 6px; background: var(--surface-2);
    color: var(--text-dim); border: 1px solid var(--border); white-space: nowrap;
  }
  .metric-block { margin-bottom: 14px; }
  .metric-row {
    display: flex; justify-content: space-between; align-items: baseline;
    font-size: 12.5px; margin-bottom: 5px; gap: 10px;
  }
  .metric-row .label { color: var(--text-dim); white-space: nowrap; }
  .metric-row .value { font-variant-numeric: tabular-nums; text-align: right; }
  .progress {
    position: relative; height: 8px; background: var(--surface-2);
    border-radius: 4px; overflow: hidden;
  }
  .progress-fill { height: 100%; border-radius: 4px; background: var(--good); transition: width 0.2s; }
  .progress-fill.bad { background: var(--bad); }
  .progress-fill.info { background: var(--info); }
  .sub-note { font-size: 11px; color: var(--text-faint); margin-top: 5px; }
  .deficit-line { font-size: 13px; margin-top: 10px; font-weight: 600; }
  .deficit-line.pos { color: var(--good); }
  .deficit-line.neg { color: var(--bad); }

  .meal-list { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 12px; }
  .meal-item {
    display: flex; align-items: center; gap: 10px;
    padding: 7px 0; border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .meal-item:last-child { border-bottom: none; }
  .meal-main { flex: 1; min-width: 0; }
  .meal-name { font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meal-slot { font-size: 10px; color: var(--text-faint); margin-top: 1px; }
  .meal-pctbar { width: 40px; height: 4px; background: var(--surface-2); border-radius: 2px; overflow: hidden; flex-shrink: 0; }
  .meal-pctbar-fill { height: 100%; background: var(--accent); }
  .meal-nums { font-size: 11px; color: var(--text-dim); text-align: right; flex-shrink: 0; width: 78px; font-variant-numeric: tabular-nums; }
  .notes { font-size: 11px; color: var(--text-faint); margin-top: 12px; font-style: italic; }

  .insight-card { padding: 13px 0; border-bottom: 1px solid rgba(255,255,255,0.05); }
  .insight-card:first-child { padding-top: 2px; }
  .insight-card:last-child { border-bottom: none; padding-bottom: 2px; }
  .insight-headline { font-size: 12.5px; line-height: 1.5; color: var(--text); margin-bottom: 10px; }
  .insight-stats { display: flex; align-items: stretch; gap: 8px; }
  .insight-stat {
    flex: 1; min-width: 0; background: var(--surface-2); border-radius: 10px; padding: 8px 10px;
  }
  .insight-stat-name {
    font-size: 9px; font-weight: 700; letter-spacing: 0.5px; color: var(--text-faint);
    text-transform: uppercase; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .insight-stat-value {
    font-size: 14px; font-weight: 650; margin-top: 3px; font-variant-numeric: tabular-nums;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .insight-stat-n { font-size: 9px; color: var(--text-faint); margin-top: 2px; }
  .insight-vs { font-size: 9.5px; color: var(--text-faint); align-self: center; flex-shrink: 0; }

  .macro-stats { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border); }
  .macro-stat-row { margin-bottom: 10px; }
  .macro-stat-row:last-child { margin-bottom: 2px; }
  .macro-stat-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 5px; }
  .macro-stat-name { font-size: 11.5px; font-weight: 650; }
  .macro-stat-hit { font-size: 10.5px; color: var(--text-dim); font-variant-numeric: tabular-nums; }
  .macro-stat-bar-wrap { position: relative; padding-top: 11px; }
  .macro-stat-tick {
    position: absolute; top: 0; transform: translateX(-50%);
    font-size: 8px; color: var(--text-faint); font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .macro-stat-tick::after {
    content: ''; position: absolute; left: 50%; top: 9px; width: 1px; height: 4px; background: var(--border);
  }
  .macro-stat-bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: var(--surface-2); }
  .macro-stat-seg { height: 100%; }
  .macro-stat-seg.good { background: var(--good); }
  .macro-stat-seg.accent { background: var(--accent); }
  .macro-stat-seg.bad { background: var(--bad); }
  .macro-stat-legend { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 5px; }
  .macro-stat-legend-item { font-size: 9px; color: var(--text-faint); white-space: nowrap; }
  .macro-stat-legend-item b { color: var(--text-dim); font-weight: 650; }

  footer { text-align: center; color: var(--text-faint); font-size: 10px; margin-top: 26px; }

  @media (max-width: 360px) {
    .stat-value { font-size: 16px; }
    .meal-nums { width: 64px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="title-row">
    <h1>Fitness Dashboard</h1>
    <div class="updated" id="updated"></div>
  </div>

  <div class="tldr-card" id="tldr-card" style="display:none;">
    <div class="tldr-label">TL;DR</div>
    <div id="tldr-rows"></div>
  </div>

  <div class="period-label">Last 30 Days</div>
  <div class="stat-grid" id="stat-grid-window"></div>

  <div class="period-label">Today</div>
  <div class="stat-grid" id="stat-grid"></div>

  <div class="card">
    <div class="card-head">
      <div class="card-label">Calories</div>
      <div class="legend">
        <span class="legend-item"><i class="dot good"></i>deficit</span>
        <span class="legend-item"><i class="dot bad"></i>surplus</span>
        <span class="legend-item"><i class="dash accent"></i>7d avg eaten</span>
      </div>
    </div>
    <svg class="chart-svg" id="chart-calories"></svg>
  </div>

  <div class="card">
    <div class="card-head">
      <div class="card-label">Deficit Trend</div>
      <div class="legend">
        <span class="legend-item"><i class="dot good"></i>7d avg deficit</span>
        <span class="legend-item"><i class="dot bad"></i>7d avg surplus</span>
      </div>
    </div>
    <svg class="chart-svg" id="chart-deficit"></svg>
    <div class="empty-note" id="chart-deficit-empty" style="display:none;">Not enough data yet.</div>
  </div>

  <div class="card">
    <div class="card-head">
      <div class="card-label">Macros</div>
      <div class="legend" id="macro-legend">
        <span class="legend-item legend-toggle" data-macro="protein"><i class="dot good"></i>protein</span>
        <span class="legend-item legend-toggle" data-macro="carbs"><i class="dot info"></i>carbs</span>
        <span class="legend-item legend-toggle" data-macro="fat"><i class="dot accent"></i>fat</span>
      </div>
    </div>
    <svg class="chart-svg" id="chart-macros"></svg>
    <div class="empty-note" id="chart-macros-empty" style="display:none;">No macro data in this window.</div>
    <div class="macro-stats" id="macro-stats"></div>
  </div>

  <div class="card">
    <div class="card-head">
      <div class="card-label">Weight</div>
      <div class="legend">
        <span class="legend-item"><i class="dot info"></i>daily</span>
        <span class="legend-item"><i class="dash accent"></i>7d avg</span>
      </div>
    </div>
    <svg class="chart-svg" id="chart-weight"></svg>
    <div class="empty-note" id="chart-weight-empty" style="display:none;">No weight data in this window.</div>
  </div>

  <div class="card">
    <div class="card-head">
      <div class="card-label">Sleep</div>
      <div class="legend">
        <span class="legend-item"><i class="dot info"></i>hours</span>
        <span class="legend-item"><i class="dash accent"></i>7d avg</span>
      </div>
    </div>
    <svg class="chart-svg" id="chart-sleep"></svg>
    <div class="empty-note" id="chart-sleep-empty" style="display:none;">No sleep data in this window.</div>
  </div>

  <div class="card">
    <div class="card-head">
      <div class="card-label">Resting HR</div>
      <div class="legend">
        <span class="legend-item"><i class="dot info"></i>bpm</span>
        <span class="legend-item"><i class="dash accent"></i>7d avg</span>
      </div>
    </div>
    <svg class="chart-svg" id="chart-rhr"></svg>
    <div class="empty-note" id="chart-rhr-empty" style="display:none;">No resting HR data in this window.</div>
  </div>

  <div class="detail" id="detail"></div>

  <div class="card">
    <div class="card-head">
      <div class="card-label">Insights</div>
    </div>
    <div id="correlations"></div>
  </div>

  <footer id="footer"></footer>
</div>

<script>
const DATA = __DATA_JSON__;
const NS = 'http://www.w3.org/2000/svg';
const PAD_LEFT = 34;

let selectedIdx = DATA.days.length - 1;
for (let i = DATA.days.length - 1; i >= 0; i--) {
  if (DATA.days[i].has_data) { selectedIdx = i; break; }
}

function fmtInt(n) { return n === null || n === undefined ? '—' : Math.round(n).toLocaleString(); }
function fmt1(n) { return n === null || n === undefined ? '—' : n.toFixed(1); }
function fmtSigned(n) { return n === null || n === undefined ? '—' : (n >= 0 ? '+' : '') + Math.round(n).toLocaleString(); }
// kg is in the "positive = lost" convention used by DATA.window_summary
function fmtLoss(kg) { return kg === null || kg === undefined ? '—' : fmt1(Math.abs(kg)) + ' kg ' + (kg >= 0 ? 'lost' : 'gained'); }

function svgEl(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

// ---- tooltip (shows on hover/tap, no click required) --------------------

let tooltipEl = null;
function ensureTooltip() {
  if (!tooltipEl) {
    tooltipEl = document.createElement('div');
    tooltipEl.className = 'tooltip';
    document.body.appendChild(tooltipEl);
  }
  return tooltipEl;
}
function tooltipHtml(i, rows) {
  const d = DATA.days[i];
  return `<div class="tt-date">${escapeHtml(d.label)}${d.is_today ? ' · today' : ''}</div>` +
    rows.map(r => `<div class="tt-value">${r}</div>`).join('');
}
function positionTooltip(e) {
  const t = ensureTooltip();
  t.style.left = e.clientX + 'px';
  t.style.top = e.clientY + 'px';
}
function showTooltip(e, html) {
  const t = ensureTooltip();
  t.innerHTML = html;
  t.classList.add('visible');
  positionTooltip(e);
}
function hideTooltip() {
  if (tooltipEl) tooltipEl.classList.remove('visible');
}

// ---- stat tiles -----------------------------------------------------------

function renderWindowStats() {
  const grid = document.getElementById('stat-grid-window');
  const ws = DATA.window_summary;
  grid.innerHTML = '';

  const deficitGood = ws.total_deficit >= 0;
  const deficitValue = fmtInt(Math.abs(ws.total_deficit)) + ' kcal ' + (deficitGood ? 'deficit' : 'surplus');
  grid.appendChild(statTile('Deficit', deficitValue, ws.days_counted + '/' + ws.days_total + ' days logged', deficitGood ? 'good' : 'bad'));

  const expGood = ws.expected_loss_kg >= 0;
  grid.appendChild(statTile('Expected', fmtLoss(ws.expected_loss_kg), 'at ' + DATA.kcal_per_kg.toLocaleString() + ' kcal/kg', expGood ? 'good' : 'bad'));

  if (ws.actual_loss_kg !== null && ws.actual_loss_kg !== undefined) {
    const actGood = ws.actual_loss_kg >= 0;
    grid.appendChild(statTile('Actual', fmtLoss(ws.actual_loss_kg), 'vs ' + fmtLoss(ws.expected_loss_kg) + ' expected', actGood ? 'good' : 'bad'));
  } else {
    grid.appendChild(statTile('Actual', '—', 'not enough weigh-ins yet', ''));
  }
}

const TLDR_ROWS = [
  { key: 'workout', icon: '🏋️', name: 'Workout' },
  { key: 'diet', icon: '🍽️', name: 'Diet' },
  { key: 'weight', icon: '⚖️', name: 'Weight' },
  { key: 'recovery', icon: '🔋', name: 'Recovery' },
  { key: 'today', icon: '☀️', name: 'Today' },
];

function renderTLDR() {
  const card = document.getElementById('tldr-card');
  const t = DATA.tldr;
  if (!t) {
    card.style.display = 'none';
    return;
  }
  const rowsEl = document.getElementById('tldr-rows');
  rowsEl.innerHTML = '';
  TLDR_ROWS.forEach(r => {
    const text = t[r.key];
    if (!text) return; // e.g. "weight" is skipped when there isn't enough data
    const row = document.createElement('div');
    row.className = 'tldr-row' + (r.key === 'today' ? ' today' : '');
    row.innerHTML = `
      <div class="tldr-row-icon">${r.icon}</div>
      <div class="tldr-row-body">
        <div class="tldr-row-name">${r.name}</div>
        <div class="tldr-row-text"></div>
      </div>
    `;
    row.querySelector('.tldr-row-text').textContent = text;
    rowsEl.appendChild(row);
  });
  card.style.display = 'block';
}

function renderStats() {
  const grid = document.getElementById('stat-grid');
  const today = DATA.days[DATA.days.length - 1];
  const streak = DATA.streak;

  const eatenLabel = today.has_data ? (fmtInt(today.eaten) + (today.is_partial ? ' so far' : ' kcal')) : '—';
  const todaySub = today.has_data
    ? (today.eaten <= DATA.goal_calories
        ? (fmtInt(DATA.goal_calories - today.eaten) + ' kcal left')
        : (fmtInt(today.eaten - DATA.goal_calories) + ' kcal over'))
    : 'no log yet';

  let streakValue = '—', streakSub = 'not enough data', streakClass = '';
  if (streak) {
    streakValue = streak.count + '-day ' + streak.direction;
    streakClass = streak.direction === 'deficit' ? 'good' : 'bad';
    streakSub = streak.avg_deficit_7d !== null ? ('avg ' + fmtSigned(streak.avg_deficit_7d) + ' kcal/d') : '';
  }

  grid.innerHTML = '';
  grid.appendChild(statTile('Target', fmtInt(DATA.goal_calories) + ' kcal', DATA.goal_protein + ' g protein', ''));
  grid.appendChild(statTile('Today', eatenLabel, todaySub, ''));
  grid.appendChild(statTile('Streak', streakValue, streakSub, streakClass));
}

// 30-day distribution of days into distance-from-target buckets, one row
// per macro -- protein's target is a floor (more is fine), carbs/fat's is a
// ceiling (less is fine), so the "hit" phrasing and bucket order flip
// between them but the visual (segmented bar, worst-to-best left to right)
// stays the same shape for quick side-by-side reading.
function renderMacroStats() {
  const el = document.getElementById('macro-stats');
  el.innerHTML = '';
  DATA.macro_stats.forEach(m => {
    const row = document.createElement('div');
    row.className = 'macro-stat-row';
    const name = m.key.charAt(0).toUpperCase() + m.key.slice(1);
    const goalWord = m.kind === 'floor' ? 'target' : 'limit';

    if (!m.total) {
      row.innerHTML = `
        <div class="macro-stat-head">
          <div class="macro-stat-name">${name}</div>
          <div class="macro-stat-hit">no data yet</div>
        </div>`;
      el.appendChild(row);
      return;
    }

    const hitWord = m.kind === 'floor' ? 'at/above ' + goalWord : 'at/under ' + goalWord;
    const segs = m.buckets.map(b =>
      `<div class="macro-stat-seg ${b.cls}" style="flex-basis:${b.pct}%;opacity:${b.opacity}" title="${b.label}: ${b.count}/${m.total} days"></div>`
    ).join('');

    // gram range text per bucket, built from each bucket's own upper bound
    // and the previous bucket's upper bound as its lower bound
    let prevUpper = null;
    const ranges = m.buckets.map(b => {
      const r = b.upper === null ? '>' + prevUpper + 'g' : (prevUpper === null ? '<' + b.upper + 'g' : prevUpper + '-' + b.upper + 'g');
      prevUpper = b.upper;
      return r;
    });
    const legend = m.buckets.map((b, i) => b.count > 0
      ? `<span class="macro-stat-legend-item"><b>${b.pct}%</b> ${b.label} (${ranges[i]})</span>` : ''
    ).join('');

    // tick labels at each internal boundary (skip the open-ended last
    // bucket), placed at the cumulative day-% where the bar's color
    // actually switches
    let cum = 0;
    const ticks = m.buckets.slice(0, -1).map(b => {
      cum += b.pct;
      const t = `<div class="macro-stat-tick" style="left:${cum}%">${b.upper}g</div>`;
      return t;
    }).join('');

    row.innerHTML = `
      <div class="macro-stat-head">
        <div class="macro-stat-name">${name} <span style="color:var(--text-faint);font-weight:400;">(${m.goal}g ${goalWord})</span></div>
        <div class="macro-stat-hit">${m.hit}/${m.total} days ${hitWord} (${m.hit_pct}%)</div>
      </div>
      <div class="macro-stat-bar-wrap">
        ${ticks}
        <div class="macro-stat-bar">${segs}</div>
      </div>
      <div class="macro-stat-legend">${legend}</div>
    `;
    el.appendChild(row);
  });
}

function statTile(eyebrow, value, sub, cls) {
  const el = document.createElement('div');
  el.className = 'stat-tile';
  el.innerHTML = `
    <div class="stat-eyebrow">${eyebrow}</div>
    <div class="stat-value ${cls}">${value}</div>
    <div class="stat-sub">${sub}</div>
  `;
  return el;
}

// ---- shared chart scaffolding ---------------------------------------------
// Charts size their SVG viewBox to the container's *actual rendered pixel
// width* (not a fixed logical width), so 1 SVG user-unit == 1 real screen
// pixel. That keeps axis text and gridlines crisp and correctly sized on any
// screen, instead of shrinking unreadably small when a fixed-width viewBox
// gets squeezed into a narrow phone screen.

function chartLayout(svg, n) {
  const totalW = Math.max(260, Math.floor(svg.getBoundingClientRect().width) || 320);
  const slot = (totalW - PAD_LEFT) / n;
  return { totalW, slot };
}

function chartBase(svg, totalW, h, slot) {
  svg.setAttribute('viewBox', `0 0 ${totalW} ${h}`);
  svg.innerHTML = '';
  svg.appendChild(svgEl('rect', { x: PAD_LEFT + selectedIdx * slot, y: 0, width: slot, height: h, class: 'sel-band' }));
}

function hitColumns(svg, n, h, slot, tooltipFn) {
  for (let i = 0; i < n; i++) {
    const hit = svgEl('rect', { x: PAD_LEFT + i * slot, y: 0, width: slot, height: h, class: 'hit' });
    hit.addEventListener('pointerenter', e => showTooltip(e, tooltipFn(i)));
    hit.addEventListener('pointermove', positionTooltip);
    hit.addEventListener('pointerleave', hideTooltip);
    hit.addEventListener('click', () => selectDay(i));
    svg.appendChild(hit);
  }
}

function gridLines(svg, totalW, h, padTop, count, valueAt) {
  for (let g = 1; g <= count; g++) {
    const frac = g / (count + 0.4);
    const y = h - frac * (h - padTop);
    svg.appendChild(svgEl('line', { x1: PAD_LEFT, y1: y, x2: totalW, y2: y, class: 'grid-line' }));
    const t = svgEl('text', { x: PAD_LEFT - 6, y: y + 3, class: 'axis-value', 'text-anchor': 'end' });
    t.textContent = valueAt(frac);
    svg.appendChild(t);
  }
}

function refLine(svg, totalW, y, label) {
  svg.appendChild(svgEl('line', { x1: PAD_LEFT, y1: y, x2: totalW, y2: y, class: 'ref-line' }));
  const t = svgEl('text', { x: totalW - 2, y: y - 4, class: 'ref-text', 'text-anchor': 'end' });
  t.textContent = label;
  svg.appendChild(t);
}

function trendPath(points, slot) {
  let d = '', started = false;
  points.forEach((v, i) => {
    if (v === null || v === undefined) { started = false; return; }
    const x = PAD_LEFT + i * slot + slot / 2;
    d += (started ? 'L' : 'M') + x + ' ' + v.y + ' ';
    started = true;
  });
  return d;
}

function renderAxis(svg, n, h, slot) {
  const every = 5;
  for (let i = 0; i < n; i++) {
    if (i % every !== 0 && i !== n - 1) continue;
    const t = svgEl('text', { x: PAD_LEFT + i * slot + slot / 2, y: h + 10, class: 'axis-text', 'text-anchor': 'middle' });
    t.textContent = DATA.days[i].label.split(' ')[1];
    svg.appendChild(t);
  }
}

// ---- bar charts (calories / protein) ---------------------------------------

function renderBarChart(svgId, values, trend, colorFor, refValue, refLabel, tooltipFn) {
  const svg = document.getElementById(svgId);
  const n = DATA.days.length;
  const H = 122, padTop = 8;
  const { totalW, slot } = chartLayout(svg, n);
  const barW = Math.max(3, Math.min(22, slot * 0.62));
  chartBase(svg, totalW, H, slot);

  const nums = values.filter(v => v !== null);
  const trendNums = trend.filter(v => v !== null);
  const maxVal = Math.max(refValue || 0, ...nums, ...trendNums, 1) * 1.12;

  gridLines(svg, totalW, H, padTop, 3, frac => fmtInt(frac * maxVal));
  refLine(svg, totalW, H - (refValue / maxVal) * (H - padTop), refLabel);

  values.forEach((v, i) => {
    const x = PAD_LEFT + i * slot + (slot - barW) / 2;
    if (v === null) {
      svg.appendChild(svgEl('rect', { x, y: H - 2, width: barW, height: 2, rx: 1, class: 'bar-empty' }));
      return;
    }
    const barH = Math.max(2, (v / maxVal) * (H - padTop));
    svg.appendChild(svgEl('rect', {
      x, y: H - barH, width: barW, height: barH, rx: 2,
      class: 'bar ' + colorFor(i) + (i === selectedIdx ? ' selected' : '')
    }));
  });

  const pts = trend.map(v => v === null ? null : { y: H - (v / maxVal) * (H - padTop) });
  svg.appendChild(svgEl('path', { d: trendPath(pts, slot), class: 'trend-line' }));

  hitColumns(svg, n, H, slot, tooltipFn);
  renderAxis(svg, n, H, slot);
}

// ---- line charts (weight / sleep / rhr) ------------------------------------

function renderLineChart(svgId, emptyId, values, trend, valueFmt, tooltipFn) {
  const svg = document.getElementById(svgId);
  const emptyNote = document.getElementById(emptyId);
  const n = DATA.days.length;
  const nums = values.filter(v => v !== null);

  if (!nums.length) {
    svg.style.display = 'none';
    emptyNote.style.display = 'block';
    return;
  }
  svg.style.display = 'block';
  emptyNote.style.display = 'none';

  const H = 96, padTop = 10, padBottom = 8;
  const { totalW, slot } = chartLayout(svg, n);
  chartBase(svg, totalW, H, slot);

  const trendNums = trend.filter(v => v !== null);
  const allVals = nums.concat(trendNums);
  let lo = Math.min(...allVals), hi = Math.max(...allVals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.18;
  lo -= pad; hi += pad;
  const yOf = v => H - padBottom - ((v - lo) / (hi - lo)) * (H - padTop - padBottom);

  gridLines(svg, totalW, H, padTop, 2, frac => {
    const val = lo + frac * (hi - lo);
    return valueFmt(val);
  });

  const rawPts = values.map(v => v === null ? null : { y: yOf(v) });
  svg.appendChild(svgEl('path', { d: trendPath(rawPts, slot), class: 'raw-line' }));

  values.forEach((v, i) => {
    if (v === null) return;
    const cx = PAD_LEFT + i * slot + slot / 2;
    svg.appendChild(svgEl('circle', {
      cx, cy: yOf(v), r: i === selectedIdx ? 2.8 : 1.8, class: 'pt' + (i === selectedIdx ? ' selected' : '')
    }));
  });

  const trendPts = trend.map(v => v === null ? null : { y: yOf(v) });
  svg.appendChild(svgEl('path', { d: trendPath(trendPts, slot), class: 'trend-line' }));

  hitColumns(svg, n, H, slot, tooltipFn);
  renderAxis(svg, n, H, slot);
}

// ---- deficit trend chart ----------------------------------------------------
// A dedicated zero-crossing chart for the 7-day avg deficit, kept fully
// separate from the Calories chart's eaten-kcal axis. Plotting it on its own
// axis (instead of projecting it onto the calories axis) keeps its shape from
// visually mimicking the eaten trend just because deficit = maintenance -
// eaten shares a term with it -- here it only ever moves on real deficit
// swings. Green above zero (deficit), red below (surplus), colored per
// segment so the line visibly flips color right at a zero-crossing.

function renderDeficitChart(svgId, emptyId, trend, tooltipFn) {
  const svg = document.getElementById(svgId);
  const emptyNote = document.getElementById(emptyId);
  const n = DATA.days.length;
  const nums = trend.filter(v => v !== null && v !== undefined);

  if (!nums.length) {
    svg.style.display = 'none';
    emptyNote.style.display = 'block';
    return;
  }
  svg.style.display = 'block';
  emptyNote.style.display = 'none';

  const H = 96, padTop = 10, padBottom = 8;
  const { totalW, slot } = chartLayout(svg, n);
  chartBase(svg, totalW, H, slot);

  let lo = Math.min(0, ...nums), hi = Math.max(0, ...nums);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.18;
  lo -= pad; hi += pad;
  const yOf = v => H - padBottom - ((v - lo) / (hi - lo)) * (H - padTop - padBottom);

  gridLines(svg, totalW, H, padTop, 2, frac => fmtSigned(lo + frac * (hi - lo)));
  refLine(svg, totalW, yOf(0), '0');

  const pts = trend.map(v => v === null || v === undefined ? null : { y: yOf(v), v });
  for (let i = 0; i < n - 1; i++) {
    if (pts[i] === null || pts[i + 1] === null) continue;
    const x1 = PAD_LEFT + i * slot + slot / 2, x2 = PAD_LEFT + (i + 1) * slot + slot / 2;
    const cls = (pts[i].v + pts[i + 1].v) >= 0 ? 'good' : 'bad';
    svg.appendChild(svgEl('line', { x1, y1: pts[i].y, x2, y2: pts[i + 1].y, class: 'trend-line ' + cls }));
  }
  trend.forEach((v, i) => {
    if (v === null || v === undefined) return;
    svg.appendChild(svgEl('circle', {
      cx: PAD_LEFT + i * slot + slot / 2, cy: yOf(v), r: i === selectedIdx ? 2.8 : 1.6,
      class: 'pt ' + (v >= 0 ? 'good' : 'bad') + (i === selectedIdx ? ' selected' : '')
    }));
  });

  hitColumns(svg, n, H, slot, tooltipFn);
  renderAxis(svg, n, H, slot);
}

// ---- macro chart (protein / carbs / fat together) --------------------------
// All three share one gram axis instead of three separate bar+trend+ref
// charts (which would stack to 9 layered elements and be unreadable
// together). Each macro gets its own raw daily line and its own
// color-matched dashed target line, so the gap between a macro's line and
// its own reference line is directly readable at a glance.

const MACRO_SERIES = [
  { key: 'protein', cls: 'good', label: 'protein' },
  { key: 'carbs', cls: 'info', label: 'carbs' },
  { key: 'fat', cls: 'accent', label: 'fat' },
];

// Clicking a macro in the legend isolates it -- the other two macros' lines,
// points, and target lines fade way down instead of disappearing (keeping
// them faintly visible avoids the axis/gridlines jumping around when only
// one macro's range would otherwise drive the scale).
let macroFocus = null;

function renderMacroChart(svgId, emptyId, tooltipFn) {
  const svg = document.getElementById(svgId);
  const emptyNote = document.getElementById(emptyId);
  const n = DATA.days.length;

  const series = MACRO_SERIES.map(s => ({
    ...s,
    values: DATA.days.map(d => d[s.key]),
    goal: DATA['goal_' + s.key],
  }));
  const anyData = series.some(s => s.values.some(v => v !== null && v !== undefined));

  if (!anyData) {
    svg.style.display = 'none';
    emptyNote.style.display = 'block';
    return;
  }
  svg.style.display = 'block';
  emptyNote.style.display = 'none';

  const H = 132, padTop = 8, padBottom = 8;
  const { totalW, slot } = chartLayout(svg, n);
  chartBase(svg, totalW, H, slot);

  const allVals = series.flatMap(s => s.values.filter(v => v !== null && v !== undefined));
  const maxVal = Math.max(...allVals, ...series.map(s => s.goal), 1) * 1.12;
  const yOf = v => H - padBottom - (v / maxVal) * (H - padTop - padBottom);

  gridLines(svg, totalW, H, padTop, 3, frac => fmtInt(frac * maxVal) + 'g');

  // dashed, color-matched target line per macro, drawn before the data lines
  // so the data lines sit visually on top
  series.forEach(s => {
    const dim = macroFocus && s.key !== macroFocus;
    const y = yOf(s.goal);
    svg.appendChild(svgEl('line', {
      x1: PAD_LEFT, y1: y, x2: totalW, y2: y, class: 'ref-line ' + s.cls,
      style: dim ? 'opacity:0.12' : ''
    }));
    const t = svgEl('text', {
      x: totalW - 2, y: y - 3, class: 'ref-text ' + s.cls, 'text-anchor': 'end',
      style: dim ? 'opacity:0.12' : ''
    });
    t.textContent = s.label + ' ' + s.goal + 'g';
    svg.appendChild(t);
  });

  series.forEach(s => {
    const dim = macroFocus && s.key !== macroFocus;
    const pts = s.values.map(v => v === null || v === undefined ? null : { y: yOf(v) });
    svg.appendChild(svgEl('path', {
      d: trendPath(pts, slot), class: 'trend-line macro-line ' + s.cls,
      style: dim ? 'opacity:0.12' : ''
    }));
    s.values.forEach((v, i) => {
      if (v === null || v === undefined) return;
      svg.appendChild(svgEl('circle', {
        cx: PAD_LEFT + i * slot + slot / 2, cy: yOf(v), r: i === selectedIdx ? 2.6 : 1.5,
        class: 'pt ' + s.cls + (i === selectedIdx ? ' selected' : ''),
        style: dim ? 'opacity:0.12' : ''
      }));
    });
  });

  hitColumns(svg, n, H, slot, tooltipFn);
  renderAxis(svg, n, H, slot);
}

function setMacroFocus(key) {
  macroFocus = (macroFocus === key) ? null : key;
  document.querySelectorAll('#macro-legend .legend-toggle').forEach(el => {
    const isThis = el.dataset.macro === macroFocus;
    el.classList.toggle('active', isThis);
    el.classList.toggle('dimmed', macroFocus !== null && !isThis);
  });
  renderCharts();
}

document.querySelectorAll('#macro-legend .legend-toggle').forEach(el => {
  el.addEventListener('click', () => setMacroFocus(el.dataset.macro));
});

// ---- orchestration ----------------------------------------------------------

function renderCharts() {
  renderBarChart(
    'chart-calories',
    DATA.days.map(d => d.eaten),
    // Trend line here is the plain 7-day avg *eaten* -- kept on the same
    // kcal axis as the bars it's tracking. The deficit trend itself lives in
    // its own dedicated chart below, in its own kcal-deficit units, so it
    // never gets rendered as a disguised copy of this line.
    DATA.days.map(d => d.eaten_trend),
    i => (DATA.days[i].deficit !== null && DATA.days[i].deficit < 0) ? 'bad' : 'good',
    DATA.goal_calories, DATA.goal_calories.toLocaleString() + ' target',
    i => {
      const d = DATA.days[i];
      if (!d.has_data) return tooltipHtml(i, ['no log yet']);
      const rows = [fmtInt(d.eaten) + ' kcal eaten'];
      if (d.deficit !== null) rows.push((d.deficit < 0 ? 'surplus ' : 'deficit ') + fmtInt(Math.abs(d.deficit)) + ' kcal');
      if (d.eaten_trend !== null && d.eaten_trend !== undefined) rows.push('7d avg ' + fmtInt(d.eaten_trend) + ' kcal eaten');
      return tooltipHtml(i, rows);
    }
  );
  renderDeficitChart(
    'chart-deficit', 'chart-deficit-empty',
    DATA.days.map(d => d.deficit_trend),
    i => {
      const d = DATA.days[i];
      if (!d.has_data || d.deficit === null) return tooltipHtml(i, ['no log yet']);
      const rows = [(d.deficit < 0 ? 'surplus ' : 'deficit ') + fmtInt(Math.abs(d.deficit)) + ' kcal (today)'];
      if (d.deficit_trend !== null && d.deficit_trend !== undefined) {
        rows.push('7d avg ' + (d.deficit_trend < 0 ? 'surplus ' : 'deficit ') + fmtInt(Math.abs(d.deficit_trend)) + ' kcal');
      }
      return tooltipHtml(i, rows);
    }
  );
  renderMacroChart(
    'chart-macros', 'chart-macros-empty',
    i => {
      const d = DATA.days[i];
      if (!d.has_data) return tooltipHtml(i, ['no log yet']);
      const rows = [];
      MACRO_SERIES.forEach(s => {
        const v = d[s.key];
        if (v === null || v === undefined) return;
        rows.push(s.label + ' ' + fmtInt(v) + ' / ' + DATA['goal_' + s.key] + ' g');
      });
      if (!rows.length) rows.push('no macro data');
      return tooltipHtml(i, rows);
    }
  );
  renderLineChart(
    'chart-weight', 'chart-weight-empty',
    DATA.days.map(d => d.weight), DATA.days.map(d => d.weight_7day_avg), fmt1,
    i => {
      const d = DATA.days[i];
      if (d.weight === null || d.weight === undefined) return tooltipHtml(i, ['no weigh-in']);
      const rows = [fmt1(d.weight) + ' kg'];
      if (d.weight_7day_avg !== null && d.weight_7day_avg !== undefined) rows.push('7d avg ' + fmt1(d.weight_7day_avg) + ' kg');
      return tooltipHtml(i, rows);
    }
  );
  renderLineChart(
    'chart-sleep', 'chart-sleep-empty',
    DATA.days.map(d => d.sleep_hours), DATA.days.map(d => d.sleep_trend), v => fmt1(v) + 'h',
    i => {
      const d = DATA.days[i];
      if (d.sleep_hours === null || d.sleep_hours === undefined) return tooltipHtml(i, ['no data']);
      const rows = [fmt1(d.sleep_hours) + ' h'];
      if (d.sleep_trend !== null && d.sleep_trend !== undefined) rows.push('7d avg ' + fmt1(d.sleep_trend) + ' h');
      return tooltipHtml(i, rows);
    }
  );
  renderLineChart(
    'chart-rhr', 'chart-rhr-empty',
    DATA.days.map(d => d.resting_hr), DATA.days.map(d => d.rhr_trend), fmtInt,
    i => {
      const d = DATA.days[i];
      if (d.resting_hr === null || d.resting_hr === undefined) return tooltipHtml(i, ['no data']);
      const rows = [fmtInt(d.resting_hr) + ' bpm'];
      if (d.rhr_trend !== null && d.rhr_trend !== undefined) rows.push('7d avg ' + fmtInt(d.rhr_trend) + ' bpm');
      return tooltipHtml(i, rows);
    }
  );
}

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderCharts, 120);
});

function selectDay(i) {
  selectedIdx = i;
  renderCharts();
  renderDetail();
}

function renderDetail() {
  const d = DATA.days[selectedIdx];
  const el = document.getElementById('detail');

  if (!d.has_data) {
    el.innerHTML = `
      <div class="detail-head">
        <div class="detail-date">${d.label}${d.is_today ? ' (today)' : ''}</div>
        <div class="badge">${d.activity_badge}</div>
      </div>
      <div class="empty-note">No meal log entry yet for this day.</div>
    `;
    return;
  }

  const isSurplus = d.deficit !== null && d.deficit < 0;
  const calPct = Math.min(100, (d.eaten / d.tdee_primary) * 100);
  const proteinHit = d.protein !== null && d.protein >= DATA.goal_protein;
  const proteinPct = Math.min(100, (d.protein / DATA.goal_protein) * 100);
  const proteinCls = d.is_partial ? 'info' : (proteinHit ? '' : 'bad');

  let refNote = '';
  if (d.tdee_garmin_valid) {
    refNote = `maintenance: Garmin ${fmtInt(d.tdee_garmin)} kcal &middot; fixed ref ${d.tdee_fixed} kcal`;
  } else {
    refNote = `maintenance: fixed ${d.tdee_fixed} kcal (Garmin ${d.tdee_garmin ? 'implausible' : 'not synced'}${d.tdee_garmin ? ' — ' + fmtInt(d.tdee_garmin) + ' kcal' : ''})`;
  }

  const itemsHtml = d.items.length ? d.items.map(it => `
    <div class="meal-item">
      <div class="meal-main">
        <div class="meal-name">${escapeHtml(it.name)}</div>
        <div class="meal-slot">${escapeHtml(it.slot)}</div>
      </div>
      <div class="meal-pctbar"><div class="meal-pctbar-fill" style="width:${it.pct_of_day}%"></div></div>
      <div class="meal-nums">${fmtInt(it.calories)} kcal &middot; ${fmt1(it.protein)}g</div>
    </div>
  `).join('') : '<div class="empty-note">No itemized meals logged.</div>';

  el.innerHTML = `
    <div class="detail-head">
      <div class="detail-date">${d.label}${d.is_today ? ' (today, in progress)' : ''}</div>
      <div class="badge">${d.activity_badge}</div>
    </div>

    <div class="metric-block">
      <div class="metric-row">
        <span class="label">Calories</span>
        <span class="value">${fmtInt(d.eaten)} / ${fmtInt(d.tdee_primary)} kcal</span>
      </div>
      <div class="progress">
        <div class="progress-fill${isSurplus ? ' bad' : ''}" style="width:${calPct}%"></div>
      </div>
      <div class="sub-note">${refNote}</div>
      <div class="deficit-line ${isSurplus ? 'neg' : 'pos'}">
        ${isSurplus ? 'Surplus' : 'Deficit'} ${fmtInt(Math.abs(d.deficit))} kcal
      </div>
    </div>

    <div class="metric-block">
      <div class="metric-row">
        <span class="label">Protein</span>
        <span class="value">${fmt1(d.protein)} / ${DATA.goal_protein} g</span>
      </div>
      <div class="progress">
        <div class="progress-fill ${proteinCls}" style="width:${proteinPct}%"></div>
      </div>
    </div>

    <div class="metric-block">
      <div class="metric-row"><span class="label">Steps</span><span class="value">${fmtInt(d.steps)}</span></div>
      <div class="metric-row"><span class="label">Sleep</span><span class="value">${d.sleep_hours ? fmt1(d.sleep_hours) + ' h' : '—'}</span></div>
      <div class="metric-row"><span class="label">Resting HR</span><span class="value">${d.resting_hr ? fmtInt(d.resting_hr) + ' bpm' : '—'}</span></div>
      <div class="metric-row"><span class="label">Weight</span><span class="value">${d.weight ? fmt1(d.weight) + ' kg' : '—'}</span></div>
      ${d.activity_detail ? `<div class="metric-row"><span class="label">Workout</span><span class="value">${escapeHtml(d.activity_detail)}</span></div>` : ''}
    </div>

    <div class="meal-list">${itemsHtml}</div>
    ${d.notes ? `<div class="notes">${escapeHtml(d.notes)}</div>` : ''}
  `;
}

function escapeHtml(s) {
  if (!s) return '';
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtCorrVal(v, unit) {
  const sign = unit.includes('deficit') && v >= 0 ? '+' : '';
  return sign + v.toLocaleString() + ' ' + unit;
}

function renderCorrelations() {
  const el = document.getElementById('correlations');
  const rows = DATA.correlations;
  if (!rows.length) {
    el.innerHTML = '<div class="empty-note">Nothing meets the bar yet — comparisons need at least ' + DATA.corr_min_n + ' days on both sides and a real difference between them. Check back as more days log.</div>';
    return;
  }
  el.innerHTML = rows.map(r => `
    <div class="insight-card">
      <div class="insight-headline">${escapeHtml(r.headline)}</div>
      <div class="insight-stats">
        ${r.groups.map(g => `
          <div class="insight-stat">
            <div class="insight-stat-name">${escapeHtml(g.name)}</div>
            <div class="insight-stat-value">${fmtCorrVal(g.avg, r.unit)}</div>
            <div class="insight-stat-n">n=${g.n}</div>
          </div>
        `).join('<div class="insight-vs">vs</div>')}
      </div>
    </div>
  `).join('');
}

document.getElementById('updated').textContent = 'updated ' + DATA.generated_at.slice(0, 10);
renderTLDR();
renderWindowStats();
renderStats();
renderCharts();
renderMacroStats();
renderDetail();
renderCorrelations();
document.getElementById('footer').textContent = 'generated ' + DATA.generated_at;
</script>
</body>
</html>
"""


def render_html(data):
    return HTML_TEMPLATE.replace("__DATA_JSON__", json.dumps(data))


# ---------------------------------------------------------------- health.html
# Deliberately NOT linked from index.html's nav or anywhere in its markup --
# stays out of a casual browse of the public dashboard without being an
# access-controlled secret. Rendered server-side (no charts, no JS state)
# since it's just a static snapshot of a Notion page's prose.

HEALTH_GROUP_STYLE = {
    "doing well": "good",
    "flag for your doctor": "bad",
}


def split_group_label(raw):
    """"Needs attention (fitness/lifestyle responsive)" -> title + subtitle;
    a label with no parenthetical (like "Doing well") just gets a title."""
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return raw.strip(), None


HEALTH_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Health & Labs</title>
<style>
  :root {
    --bg: #0a0b0e;
    --surface: #131419;
    --surface-2: #1a1c22;
    --border: #242631;
    --text: #edeef2;
    --text-dim: #9497a3;
    --text-faint: #5a5c66;
    --accent: #e8a33d;
    --good: #33c692;
    --bad: #ef6259;
    --info: #5b93f0;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    padding: 18px 14px 56px;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 640px; margin: 0 auto; }
  .title-row { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 6px; }
  .title-row h1 { font-size: 15px; font-weight: 650; letter-spacing: 0.2px; margin: 0; }
  .updated { font-size: 10px; color: var(--text-faint); font-variant-numeric: tabular-nums; }
  .basis { font-size: 11px; color: var(--text-faint); margin-bottom: 16px; }
  .intro { font-size: 12.5px; color: var(--text-dim); line-height: 1.55; margin-bottom: 20px; }

  .health-card {
    background: var(--surface); border: 1px solid var(--border); border-left: 3px solid var(--border);
    border-radius: 14px; padding: 14px 14px 8px; margin-bottom: 12px;
  }
  .health-card.good { border-left-color: var(--good); }
  .health-card.accent { border-left-color: var(--accent); }
  .health-card.bad { border-left-color: var(--bad); }
  .health-card-title { font-size: 13.5px; font-weight: 650; }
  .health-card-subtitle { font-size: 10.5px; color: var(--text-faint); margin-top: 2px; margin-bottom: 10px; }
  .health-list { list-style: none; margin: 0 0 6px; padding: 0; }
  .health-list li {
    font-size: 12.5px; line-height: 1.55; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .health-list li:last-child { border-bottom: none; }

  footer { text-align: center; color: var(--text-faint); font-size: 10px; margin-top: 26px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="title-row">
    <h1>Health &amp; Labs</h1>
    <div class="updated">__UPDATED__</div>
  </div>
  <div class="basis">Source: Fitness Plan (Notion) &middot; a static snapshot, refreshed only when that page is edited</div>
  __INTRO__
  __GROUPS__
  <footer>Not linked from the dashboard &middot; update the Fitness Plan Notion page when you get a new report</footer>
</div>
</body>
</html>
"""


def render_health_html(summary, generated_at):
    intro_html = ""
    if summary.get("intro"):
        intro_html = f'<div class="intro">{html_lib.escape(summary["intro"])}</div>'

    groups_html = []
    for g in summary["groups"]:
        title, subtitle = split_group_label(g["label"])
        style = HEALTH_GROUP_STYLE.get(title.lower(), "accent")
        items_html = "".join(f"<li>{html_lib.escape(item)}</li>" for item in g["items"])
        subtitle_html = f'<div class="health-card-subtitle">{html_lib.escape(subtitle)}</div>' if subtitle else '<div class="health-card-subtitle">&nbsp;</div>'
        groups_html.append(f"""
  <div class="health-card {style}">
    <div class="health-card-title">{html_lib.escape(title)}</div>
    {subtitle_html}
    <ul class="health-list">{items_html}</ul>
  </div>""")

    html_out = HEALTH_TEMPLATE
    html_out = html_out.replace("__UPDATED__", "updated " + generated_at[:10])
    html_out = html_out.replace("__INTRO__", intro_html)
    html_out = html_out.replace("__GROUPS__", "".join(groups_html))
    return html_out


def main():
    data = collect()

    try:
        plan_text = fetch_plan_text(FITNESS_PLAN_PAGE_ID, "The plan")
    except Exception as e:
        print(f"WARN: could not fetch Fitness Plan text for TL;DR context: {e}")
        plan_text = ""
    try:
        data["tldr"] = generate_tldr(data, plan_text)
    except Exception as e:
        print(f"WARN: TL;DR generation failed, omitting: {e}")
        data["tldr"] = None
    if data["tldr"]:
        for k in ("workout", "diet", "weight", "recovery", "today"):
            v = data["tldr"].get(k)
            if v:
                print(f"TL;DR [{k}]: {v}")
    else:
        print("TL;DR: (skipped)")

    html = render_html(data)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT_PATH}")
    print_summary(data)

    health_summary = fetch_health_summary(FITNESS_PLAN_PAGE_ID)
    health_html = render_health_html(health_summary, data["generated_at"])
    with open(HEALTH_OUTPUT_PATH, "w") as f:
        f.write(health_html)
    print(f"Wrote {HEALTH_OUTPUT_PATH} ({len(health_summary['groups'])} groups, not linked from index.html)")


def print_summary(data):
    print(f"Target: {TARGET_CALORIES} kcal flat / {GOAL_PROTEIN}g protein  |  TDEE fallback: {TDEE_FALLBACK} kcal (Garmin implausible below {GARMIN_MIN_PLAUSIBLE})")
    wt = data["weight_trend"]
    if wt:
        print(
            f"Weight 7-day avg: {wt['latest_7day_avg']} kg "
            f"(was {wt['prior_7day_avg']} kg a week earlier, "
            f"{wt['change_kg']:+.2f} kg, trending {wt['direction'].upper()})"
        )
    else:
        print("Weight 7-day avg: not enough data to compute a trend")
    streak = data["streak"]
    if streak:
        print(
            f"Streak: {streak['count']}-day {streak['direction']} "
            f"(avg last {min(streak['count'], 7)}d: {streak['avg_deficit_7d']:+d} kcal)"
        )
    else:
        print("Streak: not enough complete days yet")
    ws = data["window_summary"]
    print(
        f"{TRAILING_DAYS}-day window: total deficit {ws['total_deficit']:+d} kcal "
        f"({ws['days_counted']}/{ws['days_total']} days logged) "
        f"-> expected {fmt_loss(ws['expected_loss_kg'])} (@ {KCAL_PER_KG_FAT} kcal/kg)"
    )
    if ws["actual_loss_kg"] is not None:
        print(
            f"  actual: {fmt_loss(ws['actual_loss_kg'])} "
            f"({ws['start_weight_avg']:.1f} -> {ws['end_weight_avg']:.1f} kg, 7-day avgs)"
        )
    else:
        print("  actual: not enough weight data at both ends of the window")
    print()
    print(f"Insights ({len(data['correlations'])} survived the n>={CORR_MIN_N} + meaningful-delta filter):")
    for r in data["correlations"]:
        g1, g2 = r["groups"]
        print(f"  {r['headline']}")
        fmt = (lambda v: f"{v:+g}") if "deficit" in r["unit"] else (lambda v: f"{v:g}")
        print(f"    {g1['name']}: {fmt(g1['avg'])} {r['unit']} (n={g1['n']})   vs   {g2['name']}: {fmt(g2['avg'])} {r['unit']} (n={g2['n']})")
    print()
    for d in data["days"]:
        status = "TODAY (partial)" if d["is_partial"] else ("no data" if not d["has_data"] else "")
        pt = " PT" if d["is_pt"] else ""
        alc = " ALC" if d["has_alcohol"] else ""
        print(
            f"  {d['date']} {d['label']:>6}  "
            f"eaten={fmt_or_dash(d['eaten'])}  "
            f"target={TARGET_CALORIES}{'*' if d['over_target'] else ' '}  "
            f"maint={fmt_or_dash(d['maintenance'])}{'(gar)' if d['tdee_garmin_valid'] else '(flt)'}  "
            f"deficit={fmt_or_dash(d['deficit'])}  "
            f"protein={fmt_or_dash(d['protein'])}g  "
            f"sleep={fmt_or_dash(d['sleep_hours'])}h  "
            f"rhr={fmt_or_dash(d['resting_hr'])}  "
            f"wt={fmt_or_dash(d['weight'])}  "
            f"wt7avg={fmt_or_dash(d['weight_7day_avg'])}  "
            f"badge={d['activity_badge']:<8}{pt}{alc}  {status}"
        )


def fmt_or_dash(v):
    if v is None:
        return "—"
    return f"{v:.0f}" if isinstance(v, float) else str(v)


def fmt_loss(kg):
    """kg is in the 'positive = lost' convention used by window_summary."""
    return f"{abs(kg):.2f} kg lost" if kg >= 0 else f"{abs(kg):.2f} kg gained"


if __name__ == "__main__":
    main()
