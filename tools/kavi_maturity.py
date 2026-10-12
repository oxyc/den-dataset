#!/usr/bin/env python3
"""Collect KAVI movie classifications in resumable, adaptively bounded date windows.

The public endpoint silently caps broad answers at 100 and offers no working page/offset parameter. A day
that reaches that cap is therefore refused, not recorded as complete. The JSONL may be resumed after any
interruption; already written IKLU ids are not duplicated.
"""
import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import cache, http  # noqa: E402

HOST = "luokittelu.kavi.fi"
PATH = "/agelimit/"
LIMIT = 100
WINDOW_DAYS = 31


def _state(path):
    if not os.path.exists(path):
        return {"schema": 1, "completed": []}
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if value.get("schema") != 1 or not isinstance(value.get("completed"), list):
        raise SystemExit(f"{path}: unknown collector state")
    return value


def _known(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as handle:
        return {json.loads(line)["id"] for line in handle if line.strip()}


def fetch_window(begin, end):
    return http.get_json(HOST, PATH, {"type": "movie",
        "registrationDateRange[begin]": begin.strftime("%d.%m.%Y"),
        "registrationDateRange[end]": end.strftime("%d.%m.%Y")})


def collect(out, begin, end, pause_ms=250, fetch=fetch_window):
    state_path = out + ".state.json"
    state, known = _state(state_path), _known(out)
    completed = set(state["completed"])
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    day, written = begin, 0
    while day <= end:
        key = day.isoformat()
        if key in completed:
            day += datetime.timedelta(days=1)
            continue
        window_end = min(day + datetime.timedelta(days=WINDOW_DAYS - 1), end)
        while window_end > day and any((day + datetime.timedelta(days=offset)).isoformat() in completed
                                       for offset in range((window_end - day).days + 1)):
            window_end -= datetime.timedelta(days=1)
        while True:
            rows = fetch(day, window_end)
            if not isinstance(rows, list):
                raise SystemExit(f"KAVI {key}..{window_end.isoformat()}: expected a JSON array")
            if len(rows) >= LIMIT:
                if window_end == day:
                    raise SystemExit(f"KAVI {key}: {len(rows)} rows reaches the API cap; "
                                     "refusing a truncated day")
                window_end = day + datetime.timedelta(days=(window_end - day).days // 2)
                continue
            break
        invalid = [row for row in rows if not isinstance(row, dict) or "id" not in row]
        if invalid:
            raise SystemExit(f"KAVI {key}..{window_end.isoformat()}: response row changed schema")
        fresh = [row for row in rows if row["id"] not in known]
        if fresh:
            with open(out, "a", encoding="utf-8") as handle:
                for row in fresh:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                    known.add(row["id"])
                    written += 1
        current = day
        while current <= window_end:
            completed.add(current.isoformat())
            current += datetime.timedelta(days=1)
        state["completed"] = sorted(completed)
        cache.write_atomically(state_path, (json.dumps(state, indent=1, sort_keys=True) + "\n").encode())
        if pause_ms:
            time.sleep(pause_ms / 1000)
        day = window_end + datetime.timedelta(days=1)
    return {"days": len(completed), "records": len(known), "written": written, "out": out}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--begin", required=True, type=datetime.date.fromisoformat)
    parser.add_argument("--end", required=True, type=datetime.date.fromisoformat)
    parser.add_argument("--pause-ms", type=int, default=250)
    args = parser.parse_args(argv)
    if args.end < args.begin or args.pause_ms < 0:
        parser.error("end must not precede begin and pause-ms must be non-negative")
    print(json.dumps(collect(args.out, args.begin, args.end, args.pause_ms), indent=2))


if __name__ == "__main__":
    main()
