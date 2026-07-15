"""
Garmin -> Life OS sync script.

Logs into Garmin Connect (unofficial API via the `garminconnect` library),
fetches today's sleep, resting heart rate, and activities, and writes them
into the Life OS `health-data` record in Supabase.

Required environment variables:
  GARMIN_EMAIL, GARMIN_PASSWORD, SB_URL, SB_SERVICE_ROLE_KEY
"""
import os
import sys
from datetime import date, datetime, timezone

import requests
from garminconnect import Garmin

GARMIN_EMAIL = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
SB_URL = os.environ["SB_URL"].rstrip("/")
SB_SERVICE_ROLE_KEY = os.environ["SB_SERVICE_ROLE_KEY"]


def sleep_score_to_quality(score):
    if score is None:
        return ""
    if score >= 90:
        return "Izcila"
    if score >= 75:
        return "Laba"
    if score >= 50:
        return "Vidēja"
    return "Slikta"


def fetch_sleep(client, today):
    try:
        data = client.get_sleep_data(today)
        dto = (data or {}).get("dailySleepDTO", {}) or {}
        seconds = dto.get("sleepTimeSeconds")
        hours = round(seconds / 3600, 1) if seconds else None
        score = None
        scores = dto.get("sleepScores") or {}
        overall = scores.get("overall") or {}
        score = overall.get("value")
        return hours, sleep_score_to_quality(score)
    except Exception as e:
        print(f"[warn] sleep fetch failed: {e}")
        return None, ""


def fetch_resting_hr(client, today):
    try:
        data = client.get_rhr_day(today)
        metrics = (data or {}).get("allMetrics", {}).get("metricsMap", {})
        rhr_list = metrics.get("WELLNESS_RESTING_HEART_RATE") or []
        if rhr_list:
            return rhr_list[0].get("value")
    except Exception as e:
        print(f"[warn] resting HR fetch failed: {e}")
    return None


def fetch_workouts(client, today):
    workouts = []
    try:
        activities = client.get_activities_by_date(today, today)
        for a in activities or []:
            activity_id = a.get("activityId")
            type_key = (a.get("activityType") or {}).get("typeKey", "Treniņš")
            duration_sec = a.get("duration") or 0
            workouts.append({
                "id": f"garmin-{activity_id}",
                "type": type_key.replace("_", " ").title(),
                "duration": round(duration_sec / 60),
            })
    except Exception as e:
        print(f"[warn] activities fetch failed: {e}")
    return workouts


def main():
    today = date.today().isoformat()

    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    print("Logged into Garmin Connect.")

    sleep_hours, sleep_quality = fetch_sleep(client, today)
    resting_hr = fetch_resting_hr(client, today)
    workouts = fetch_workouts(client, today)

    headers = {
        "apikey": SB_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SB_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }

    resp = requests.get(
        f"{SB_URL}/rest/v1/life_os_data",
        headers=headers,
        params={"key": "eq.health-data", "select": "id,value", "limit": 1},
        timeout=20,
    )
    resp.raise_for_status()
    rows = resp.json()

    if not rows:
        print("No existing 'health-data' record found. Add at least one entry "
              "in the Life OS Health section first, then re-run this sync.")
        sys.exit(1)

    row = rows[0]
    health_data = row.get("value") or {"entries": {}}
    entries = health_data.get("entries") or {}
    existing_entry = entries.get(today, {}) or {}

    existing_workouts = existing_entry.get("workouts") or []
    existing_ids = {w.get("id") for w in existing_workouts}
    merged_workouts = existing_workouts + [w for w in workouts if w["id"] not in existing_ids]

    entries[today] = {
        "sleepHours": sleep_hours if sleep_hours is not None else existing_entry.get("sleepHours"),
        "sleepQuality": sleep_quality or existing_entry.get("sleepQuality", ""),
        "restingHR": resting_hr if resting_hr is not None else existing_entry.get("restingHR"),
        "workouts": merged_workouts,
    }
    health_data["entries"] = entries

    update_resp = requests.patch(
        f"{SB_URL}/rest/v1/life_os_data",
        headers=headers,
        params={"id": f"eq.{row['id']}"},
        json={"value": health_data, "updated_at": datetime.now(timezone.utc).isoformat()},
        timeout=20,
    )
    update_resp.raise_for_status()
    print(f"Synced Garmin data for {today}: sleep={sleep_hours}h, RHR={resting_hr}, workouts={len(workouts)}")


if __name__ == "__main__":
    main()
