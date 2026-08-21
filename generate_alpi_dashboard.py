#!/usr/bin/env python3
"""
Alpi's fitness dashboard generator.

Reuses generate_dashboard.py's rendering pipeline (render_html, build_day,
and the trend/period/correlation math) but feeds it Alpi's own Notion meal
log instead of Chitrak's, via her own integration key (ALPI_NOTION_KEY) --
never the household NOTION_TOKEN. Output is a separate, password-locked
page (docs/alpi.html) linked from index.html's header toggle, not a
profile switch inside the same page.

Data-model gap: Chitrak's dashboard expects one Notion row per day with
pre-aggregated Total Calories/Protein/Fat/Carbs. Alpi's meal log has one
row PER MEAL with only a numeric calorie field -- so this script fetches
her raw per-meal rows, groups them into daily rollups itself, and
estimates protein per meal via Claude from the free-text food description
(same approach as meal_digest.js), caching estimates locally since old
meals never change.

Alpi has no Garmin/weight/workout data source shared with her integration,
so those panels render with generate_dashboard.py's existing "no data"
empty states -- this was a deliberate choice (full mirrored layout with
blanks) rather than a stripped-down layout.

Password gate: the entire rendering <script> block (which embeds her
actual data as JSON) is AES-256-GCM encrypted with a key derived via
PBKDF2 from ALPI_DASHBOARD_PASSWORD, and swapped for an inert placeholder
+ a small unlock bootstrap using the browser's native Web Crypto API. This
is real client-side encryption (the page source contains no plaintext
data without the password), not just a hidden div -- but it's still a
single shared password baked into a static file, so treat it as a
deterrent against casual/accidental exposure on the public GitHub Pages
site, not as strong access control against a determined attacker who
captures the HTML and brute-forces it offline.

Usage:
  python3 generate_alpi_dashboard.py
"""

import os
import re
import sys
import json
import base64
import secrets
import datetime as dt

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

import generate_dashboard as dash

ALPI_NOTION_KEY = os.environ.get("ALPI_NOTION_KEY")
ALPI_MEAL_DATA_SOURCE_ID = os.environ.get("ALPI_MEAL_DATA_SOURCE_ID")
ALPI_DASHBOARD_PASSWORD = os.environ.get("ALPI_DASHBOARD_PASSWORD")
NOTION_VERSION = "2025-09-03"

# Same defaults as pipeline/meal_digest.js -- keep these two files in sync
# if Alpi's targets change. Not sourced from her Notion "Diet Profile"
# page (which only documents a maintenance calorie range); calories and
# protein are treated as floors since the goal is muscle growth, not loss.
ALPI_CALORIE_TARGET = 1800
ALPI_PROTEIN_TARGET = 105

OUTPUT_PATH = os.path.join(dash.SCRIPT_DIR, "docs", "alpi.html")
PROTEIN_CACHE_PATH = os.path.join(dash.SCRIPT_DIR, "alpi_protein_cache.json")  # not in docs/ -- not public

PBKDF2_ITERATIONS = 200_000

PROFILE_NAV_BACK = '<a href="index.html" style="font-size:12px;color:var(--text-dim);text-decoration:none;border:1px solid var(--border);border-radius:6px;padding:4px 10px;white-space:nowrap;">← Chitrak\'s dashboard</a>'


def alpi_headers():
    return {
        "Authorization": f"Bearer {ALPI_NOTION_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def plain_text(rich_text_array):
    return "".join(rt.get("plain_text", "") for rt in (rich_text_array or []))


def amsterdam_date_str(iso_datetime_str):
    """Alpi's Date property is sometimes a full timestamp with offset,
    sometimes just a date -- either way we only care about the Amsterdam
    calendar date, matching meal_digest.js's entryDateStr()."""
    if not iso_datetime_str:
        return None
    if len(iso_datetime_str) == 10:
        return iso_datetime_str
    # Python's fromisoformat handles "+02:00" offsets directly (3.11+).
    instant = dt.datetime.fromisoformat(iso_datetime_str)
    amsterdam = instant.astimezone(dt.timezone(dt.timedelta(hours=2)))  # CEST approx; fine for day-bucketing
    return amsterdam.date().isoformat()


def fetch_meals_since(start_date_str):
    """Pages through Alpi's meal-log data source sorted by Date descending,
    stopping once entries fall before start_date_str -- same early-stop
    approach as meal_digest.js's fetchMealsForDate, generalized to a range."""
    matched = []
    cursor = None
    keep_going = True
    while keep_going:
        payload = {
            "page_size": 50,
            "sorts": [{"property": "Date", "direction": "descending"}],
        }
        if cursor:
            payload["start_cursor"] = cursor
        resp = requests.post(
            f"{dash.NOTION_API}/data_sources/{ALPI_MEAL_DATA_SOURCE_ID}/query",
            headers=alpi_headers(),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("results", []):
            p = page["properties"]
            date_str = amsterdam_date_str((p.get("Date", {}).get("date") or {}).get("start"))
            if not date_str:
                continue
            if date_str < start_date_str:
                keep_going = False
                break
            matched.append({
                "page_id": page["id"],
                "date": date_str,
                "title": plain_text(p.get("Meal", {}).get("title")).strip(),
                "description": plain_text(p.get("Foods / Description", {}).get("rich_text")).strip(),
                "calories": (p.get("Est. Calories") or {}).get("number"),
                "meal_type": (p.get("Meal Type", {}).get("select") or {}).get("name"),
                "flags": [s["name"] for s in (p.get("Protocol Flags", {}).get("multi_select") or [])],
            })

        cursor = data.get("next_cursor") if data.get("has_more") else None
        if not cursor:
            keep_going = False
    return matched


def load_protein_cache():
    try:
        with open(PROTEIN_CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_protein_cache(cache):
    with open(PROTEIN_CACHE_PATH, "w") as f:
        json.dump(cache, f)


def estimate_protein(meals, cache):
    """Fills in meal['protein'] for every meal, reusing cached estimates
    for page_ids seen before (historical meals never change) and only
    calling Claude for genuinely new ones -- keeps a 30-day trailing
    window cheap on repeat daily runs instead of re-estimating everything."""
    to_estimate = [m for m in meals if m["page_id"] not in cache and m["description"]]

    if to_estimate and dash.ANTHROPIC_API_KEY:
        prompt = (
            "Estimate grams of protein for each of these logged meals/snacks, "
            "based on the description and (if given) total calories. Be a "
            "reasonable nutrition estimator, not overly precise - round to "
            "the nearest gram.\n\n"
            "Return ONLY a JSON array of integers, same order, no markdown "
            "fences, no preamble.\n\n"
            + "\n".join(
                f"{i + 1}. {m['description']}" + (f" (~{m['calories']} kcal total)" if m["calories"] else "")
                for i, m in enumerate(to_estimate)
            )
        )
        try:
            resp = requests.post(
                dash.ANTHROPIC_API,
                headers={
                    "x-api-key": dash.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": dash.TLDR_MODEL,
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            resp.raise_for_status()
            text_block = next(b for b in resp.json()["content"] if b["type"] == "text")
            cleaned = re.sub(r"^```(json)?\s*|```\s*$", "", text_block["text"].strip())
            grams = json.loads(cleaned)
            if isinstance(grams, list) and len(grams) == len(to_estimate):
                for m, g in zip(to_estimate, grams):
                    cache[m["page_id"]] = g
            else:
                print("WARN: protein estimate response had unexpected shape, dropping")
        except Exception as e:
            print(f"WARN: protein estimation failed, dropping estimates: {e}")

    for m in meals:
        m["protein"] = cache.get(m["page_id"])


def build_alpi_data():
    if not ALPI_NOTION_KEY or not ALPI_MEAL_DATA_SOURCE_ID:
        sys.exit("ERROR: set ALPI_NOTION_KEY and ALPI_MEAL_DATA_SOURCE_ID (see whatsapp-listener/pipeline/.env)")

    # Alpi has no Garmin/weight/workout data -- calorie/protein are floors
    # (muscle growth goal), not Chitrak's maintenance-range numbers.
    dash.GOAL_PROTEIN = ALPI_PROTEIN_TARGET
    dash.TARGET_CALORIES = ALPI_CALORIE_TARGET
    dash.TDEE_FALLBACK = ALPI_CALORIE_TARGET

    today = dt.date.today()
    start = today - dt.timedelta(days=dash.TRAILING_DAYS - 1)

    raw_meals = fetch_meals_since(start.isoformat())
    cache = load_protein_cache()
    estimate_protein(raw_meals, cache)
    save_protein_cache(cache)

    meals_by_date = {}
    for m in raw_meals:
        meals_by_date.setdefault(m["date"], []).append(m)

    days = []
    for i in range(dash.TRAILING_DAYS):
        d = start + dt.timedelta(days=i)
        ds = d.isoformat()
        day_meals = meals_by_date.get(ds, [])

        if day_meals:
            total_cal = sum(m["calories"] for m in day_meals if m["calories"] is not None) or None
            total_protein = sum(m["protein"] for m in day_meals if m["protein"] is not None) or None
            meal_rollup = {
                "Total Calories (kcal)": total_cal,
                "Total Protein (g)": total_protein,
                "Total Fat (g)": None,
                "Total Carbs (g)": None,
                "Meal Count": len(day_meals),
            }
        else:
            meal_rollup = None

        day = dash.build_day(d, meal_rollup, None, [], None, None, today)

        # build_day() only populates items/notes/has_alcohol via a
        # Chitrak-style day-page fetch (meal["_page_id"]), which doesn't
        # apply here since Alpi's rows already *are* the meal items --
        # fill them in directly instead.
        total_cal_for_pct = sum(m["calories"] for m in day_meals if m["calories"]) or 1
        day["items"] = [
            {
                "name": m["title"],
                "slot": m["meal_type"] or "",
                "calories": m["calories"] or 0,
                "protein": m["protein"] or 0,
                "fat": 0,
                "carbs": 0,
                "pct_of_day": round((m["calories"] or 0) / total_cal_for_pct * 100, 1),
            }
            for m in day_meals
        ]
        day["notes"] = "; ".join(
            f"{m['title']}: {', '.join(f for f in m['flags'] if f != 'On Track')}"
            for m in day_meals if any(f != "On Track" for f in m["flags"])
        )
        day["has_alcohol"] = any(dash.mentions_alcohol(m["title"] + " " + m["description"]) for m in day_meals)
        days.append(day)

    for i, day in enumerate(days):
        day["eaten_trend"] = dash.trailing_avg(days, "eaten", i)
        day["deficit_trend"] = dash.trailing_avg(days, "deficit", i)
        day["protein_trend"] = dash.trailing_avg(days, "protein", i)
        day["carbs_trend"] = dash.trailing_avg(days, "carbs", i)
        day["fat_trend"] = dash.trailing_avg(days, "fat", i)
        day["sleep_trend"] = dash.trailing_avg(days, "sleep_hours", i)
        day["rhr_trend"] = dash.trailing_avg(days, "resting_hr", i)
        day["hrv_trend"] = dash.trailing_avg(days, "hrv", i)
        day["body_battery_trend"] = dash.trailing_avg(days, "body_battery", i)

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "goal_calories": ALPI_CALORIE_TARGET,
        "goal_protein": ALPI_PROTEIN_TARGET,
        "goal_fat": dash.GOAL_FAT,   # cosmetic only -- fat is never logged, so this target never has data to compare against
        "goal_carbs": dash.GOAL_CARBS,  # cosmetic only, same reason
        "weight_trend": dash.compute_weight_trend({}, today),
        "streak": dash.compute_streak(days),
        "periods": {
            str(w): dash.compute_period_view(days, {}, w, today) for w in dash.PERIOD_WINDOWS
        },
        "kcal_per_kg": dash.KCAL_PER_KG_FAT,
        "correlations": dash.compute_correlations(days),
        "corr_min_n": dash.CORR_MIN_N,
        "days": days,
        "tldr": None,
        "profile_nav_html": PROFILE_NAV_BACK,
    }


# ------------------------------------------------------------ password lock

def encrypt(plaintext, password):
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS)
    key = kdf.derive(password.encode("utf-8"))
    ciphertext = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return {
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


LOCK_OVERLAY_HTML = """
<div id="alpi-lock-overlay" style="position:fixed;inset:0;z-index:9999;background:#0a0b0e;display:flex;align-items:center;justify-content:center;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <form id="alpi-lock-form" style="background:#131419;border:1px solid #2a2c33;border-radius:12px;padding:28px 32px;width:280px;text-align:center;">
    <div style="font-size:28px;margin-bottom:8px;">🔒</div>
    <div style="color:#e8e9ec;font-size:15px;font-weight:600;margin-bottom:16px;">Alpi's Dashboard</div>
    <input id="alpi-lock-password" type="password" placeholder="Password" autofocus
      style="width:100%;box-sizing:border-box;background:#0a0b0e;border:1px solid #2a2c33;border-radius:8px;padding:10px 12px;color:#e8e9ec;font-size:14px;margin-bottom:12px;">
    <button type="submit" style="width:100%;background:#3b82f6;border:none;border-radius:8px;padding:10px;color:#fff;font-size:14px;font-weight:600;cursor:pointer;">Unlock</button>
    <div id="alpi-lock-error" style="display:none;color:#ef4444;font-size:12px;margin-top:10px;">Incorrect password.</div>
  </form>
</div>
"""

UNLOCK_BOOTSTRAP_TEMPLATE = """
<script id="alpi-locked-payload" type="application/octet-stream">__CIPHERTEXT_B64__</script>
<script>
(function() {
  const salt = Uint8Array.from(atob('__SALT_B64__'), c => c.charCodeAt(0));
  const iv = Uint8Array.from(atob('__IV_B64__'), c => c.charCodeAt(0));
  const ciphertext = Uint8Array.from(atob(document.getElementById('alpi-locked-payload').textContent), c => c.charCodeAt(0));

  async function tryUnlock(password) {
    try {
      const keyMaterial = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
      const key = await crypto.subtle.deriveKey(
        { name: 'PBKDF2', salt, iterations: __ITERATIONS__, hash: 'SHA-256' },
        keyMaterial, { name: 'AES-GCM', length: 256 }, false, ['decrypt']
      );
      const decrypted = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, key, ciphertext);
      const jsSource = new TextDecoder().decode(decrypted);
      document.getElementById('alpi-lock-overlay').remove();
      const s = document.createElement('script');
      s.textContent = jsSource;
      document.body.appendChild(s);
      return true;
    } catch (e) {
      return false;
    }
  }

  document.getElementById('alpi-lock-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const pw = document.getElementById('alpi-lock-password').value;
    const ok = await tryUnlock(pw);
    if (!ok) document.getElementById('alpi-lock-error').style.display = 'block';
  });
})();
</script>
"""


def lock_html(html, password):
    match = re.search(r"<script>\n(const DATA = .*?)\n</script>", html, re.S)
    if not match:
        raise RuntimeError("Could not find the dashboard's main <script> block to encrypt")

    enc = encrypt(match.group(1), password)
    bootstrap = (
        UNLOCK_BOOTSTRAP_TEMPLATE
        .replace("__CIPHERTEXT_B64__", enc["ciphertext"])
        .replace("__SALT_B64__", enc["salt"])
        .replace("__IV_B64__", enc["iv"])
        .replace("__ITERATIONS__", str(PBKDF2_ITERATIONS))
    )
    return html[: match.start()] + LOCK_OVERLAY_HTML + bootstrap + html[match.end():]


def main():
    if not ALPI_DASHBOARD_PASSWORD:
        sys.exit("ERROR: set ALPI_DASHBOARD_PASSWORD (see whatsapp-listener/pipeline/.env)")

    data = build_alpi_data()
    html = dash.render_html(data)
    locked_html = lock_html(html, ALPI_DASHBOARD_PASSWORD)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(locked_html)
    print(f"Wrote {OUTPUT_PATH} (password-locked)")


if __name__ == "__main__":
    main()
