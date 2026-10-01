#!/usr/bin/env python3
"""Black-box evaluation harness for POST /generate_goal_track.

Runs against a live server and produces results.json, report.md and report.html:

  1. API tests         - status codes, validation, no 5xx, malformed input
  2. LLM tests         - grounding (IDs exist, titles/types/descriptions match
                         catalog), correctness vs gold labels (hit@k, top-1,
                         precision, forbidden picks), step count, optional judge
  3. Calendar tests    - no false success, event_id present when claimed,
                         timestamp matches the request, not scheduled when not
                         asked, direct probe of the mock calendar service
  4. Latency           - per request, per category, with/without calendar
  5. Async health      - concurrent load while polling a cheap endpoint to
                         prove the event loop isn't blocked
  6. Consistency       - repeat one case N times: outcome mix + ID stability

Usage:
  uvicorn main:app --port 8000          # in another terminal
  python eval/run_eval.py               # full run
  python eval/run_eval.py --quick       # fewer repeats / probes
  python eval/run_eval.py --only quality --skip-calendar-probe

Exit code is 1 if any hard check fails, so it can gate CI.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import itertools
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from judge import Judge
from report import write_reports

HERE = Path(__file__).resolve().parent
ID_RE = re.compile(r"goal-\d{3}")
LONDON = ZoneInfo("Europe/London")
CAL_URL = "https://ignitus-mock-calendar.azurewebsites.net/calendar/schedule"
ENDPOINT = "/generate_goal_track"

STEP_KEYS = {"steps", "actions", "track", "goal_track", "plan"}
CAL_HINTS = ("calendar", "check_in", "checkin", "check-in")
SUCCESS_WORDS = {"scheduled", "success", "succeeded", "booked", "confirmed", "created", "ok"}
NOT_REQ_WORDS = {"not_requested", "not requested", "skipped", "none", "no_request"}
STOPWORDS = set("""a an the and or of to for in on at by with your you when it is are be as that this
from into about their them they then than use using useful also can how what which""".split())


# --------------------------------------------------------------------------- stats
def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def latency_stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.mean(values)),
        "p50": round(pct(values, 50)),
        "p95": round(pct(values, 95)),
        "p99": round(pct(values, 99)),
        "max": round(max(values)),
    }


# --------------------------------------------------------------------------- response adapter
# Your response shape is yours to choose, so the harness reads it heuristically.
# If something isn't picked up, adjust extract() / classify_calendar() only.
def extract(body) -> dict:
    """Pull recommended IDs (in order), resource objects, steps and calendar block."""
    ordered_ids = list(dict.fromkeys(ID_RE.findall(json.dumps(body))))
    resources, found = [], {"steps": None, "calendar": None}

    def rec(o):
        if isinstance(o, dict):
            if isinstance(o.get("id"), str) and ID_RE.fullmatch(o["id"]) or (
                isinstance(o.get("resource_id"), str) and ID_RE.fullmatch(o["resource_id"])
            ):
                resources.append(o)
            for k, v in o.items():
                kl = k.lower()
                if (found["steps"] is None and kl in STEP_KEYS and isinstance(v, list)
                        and v and all(isinstance(x, dict) for x in v)):
                    found["steps"] = v
                if found["calendar"] is None and isinstance(v, dict) and any(h in kl for h in CAL_HINTS):
                    found["calendar"] = v
                rec(v)
        elif isinstance(o, list):
            for x in o:
                rec(x)

    rec(body)
    return {"ids": ordered_ids, "resources": resources, **found}


def resource_fields(o: dict):
    if isinstance(o.get("id"), str) and ID_RE.fullmatch(o["id"]):
        return o["id"], o.get("title"), o.get("description"), o.get("type")
    return (o.get("resource_id"), o.get("resource_title"),
            o.get("resource_description"), o.get("resource_type"))


def find_key(o, names: set):
    if isinstance(o, dict):
        for k, v in o.items():
            if k.lower() in names and v is not None and v != "":
                return v
        for v in o.values():
            r = find_key(v, names)
            if r is not None:
                return r
    elif isinstance(o, list):
        for x in o:
            r = find_key(x, names)
            if r is not None:
                return r
    return None


def classify_calendar(cal) -> dict:
    if cal is None:
        return {"state": "absent"}
    event_id = find_key(cal, {"event_id", "eventid"})
    status = str(find_key(cal, {"status", "state", "outcome"}) or "").lower()
    flag = find_key(cal, {"success", "scheduled", "ok"})
    requested = find_key(cal, {"requested", "check_in_requested"})
    ts = find_key(cal, {"timestamp", "scheduled_for", "start", "time", "datetime", "when"})
    if event_id:
        state = "scheduled"
    elif status in SUCCESS_WORDS or flag is True:
        state = "claimed_success_without_event_id"
    elif requested is False or status in NOT_REQ_WORDS:
        state = "not_requested"
    else:
        state = "failed_or_unscheduled"
    return {"state": state, "event_id": event_id, "status": status, "timestamp": ts}


# --------------------------------------------------------------------------- helpers
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def content_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if len(w) > 3 and w not in STOPWORDS}


def support_ratio(claim: str, source: str) -> float:
    """Share of the response's description words that appear in the catalog text."""
    words = content_words(claim)
    if not words:
        return 1.0
    return len(words & content_words(source)) / len(words)


def parse_ts(s):
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def check_timestamp(ts, spec: dict, now: dt.datetime) -> tuple[bool, str]:
    d = parse_ts(ts)
    if d is None:
        return False, f"unparseable timestamp {ts!r}"
    if d <= now:
        return False, f"timestamp {d.isoformat()} is in the past"
    if (d - now).days > spec.get("max_days_ahead", 30):
        return False, f"timestamp {d.isoformat()} is more than {spec.get('max_days_ahead', 30)} days ahead"
    lo, hi = spec.get("hour_range", [0, 23])
    views = {"UTC": dt.timezone.utc, "Europe/London": LONDON}
    for name, tz in views.items():
        v, today = d.astimezone(tz), now.astimezone(tz).date()
        ok = lo <= v.hour <= hi
        if "weekday" in spec:
            ok &= v.weekday() == spec["weekday"]
        if "days_ahead" in spec:
            ok &= (v.date() - today).days == spec["days_ahead"]
        if ok:
            return True, f"ok in {name}: {v:%a %d %b %H:%M}"
    u, l = d.astimezone(dt.timezone.utc), d.astimezone(LONDON)
    return False, f"wrong day/time: UTC {u:%a %d %b %H:%M}, London {l:%a %d %b %H:%M}"


def jaccard(a: set, b: set) -> float:
    return 1.0 if not a and not b else len(a & b) / len(a | b)


# --------------------------------------------------------------------------- http
async def call_endpoint(client: httpx.AsyncClient, base: str, payload=None, raw: str | None = None) -> dict:
    t0 = time.perf_counter()
    try:
        if raw is not None:
            r = await client.post(base + ENDPOINT, content=raw, headers={"content-type": "application/json"})
        else:
            r = await client.post(base + ENDPOINT, json=payload)
        lat = (time.perf_counter() - t0) * 1000
        try:
            body = r.json()
        except ValueError:
            body = {"_non_json_body": r.text[:2000]}
        return {"status": r.status_code, "latency_ms": round(lat), "body": body, "error": None}
    except Exception as e:
        return {"status": None, "latency_ms": round((time.perf_counter() - t0) * 1000),
                "body": None, "error": f"{type(e).__name__}: {e}"}


def build_request(case: dict):
    if "raw_body" in case:
        return None, case["raw_body"], "<invalid json>"
    if "payload_repeat" in case:
        p = case["payload_repeat"]
        msg = p["text"] * p["times"]
        return {"message": msg}, None, f"<{len(msg)} chars>"
    if "payload" in case:
        return case["payload"], None, json.dumps(case["payload"])
    return {"message": case["message"]}, None, case["message"]


# --------------------------------------------------------------------------- evaluation
def evaluate(case: dict, resp: dict, catalog: dict, now: dt.datetime) -> tuple[list[dict], dict]:
    checks: list[dict] = []
    severity_default = case.get("severity", "hard")

    def add(name, ok, severity, detail=""):
        checks.append({"check": name, "passed": bool(ok), "severity": severity, "detail": detail})

    status = resp["status"]
    add("no_5xx_or_crash", status is not None and status < 500, "hard",
        f"status={status} {resp['error'] or ''}".strip())

    if case["category"] == "robustness":
        allowed = case.get("expect_status", [200])
        add("expected_status", status in allowed, severity_default, f"got {status}, allowed {allowed}")
    else:
        add("http_200", status == 200, "hard", f"status={status}")

    metrics: dict = {"status": status, "latency_ms": resp["latency_ms"]}
    if status != 200 or not isinstance(resp["body"], (dict, list)):
        return checks, metrics

    ex = extract(resp["body"])
    ids = ex["ids"]
    unknown = [i for i in ids if i not in catalog]
    valid_ids = [i for i in ids if i in catalog]
    metrics.update({
        "recommended": valid_ids,
        "recommended_titles": [catalog[i]["title"] for i in valid_ids],
        "unknown_ids": unknown,
        "n_steps": len(ex["steps"]) if ex["steps"] else None,
    })

    # --- grounding (hard)
    add("grounded_ids_exist_in_catalog", not unknown, "hard",
        f"unknown ids: {unknown}" if unknown else f"{len(valid_ids)} valid ids")
    if case.get("expect_resources", True):
        add("returns_resources", bool(valid_ids), "hard", f"{len(valid_ids)} resources")

    mismatches, weak = [], []
    for o in ex["resources"]:
        rid, title, desc, typ = resource_fields(o)
        cat = catalog.get(rid)
        if not cat:
            continue
        if title and norm(title) != norm(cat["title"]):
            mismatches.append(f"{rid} title '{title}' != '{cat['title']}'")
        if typ and norm(typ) != norm(cat["type"]):
            mismatches.append(f"{rid} type '{typ}' != '{cat['type']}'")
        if desc and norm(desc) != norm(cat["description"]):
            r = support_ratio(desc, cat["title"] + " " + cat["description"])
            if r < 0.5:
                weak.append(f"{rid}: {r:.0%} of words supported")
    add("titles_and_types_match_catalog", not mismatches, "hard", "; ".join(mismatches) or "ok")
    add("descriptions_supported_by_catalog", not weak, "quality",
        "; ".join(weak) or "ok (lexical check; judge covers paraphrase)")

    # --- correctness vs gold labels (quality)
    rec = set(valid_ids)
    must = case.get("must_hit_any") or []
    acceptable = set(case.get("acceptable") or must)
    forbidden = set(case.get("forbidden") or [])
    if must:
        hit = bool(rec & set(must))
        top1 = bool(valid_ids) and valid_ids[0] in must
        precision = len(rec & acceptable) / len(rec) if rec else 0.0
        bad = sorted(rec & forbidden)
        metrics.update({"hit": hit, "top1": top1, "precision": round(precision, 2),
                        "forbidden_picked": bad, "expected_any_of": must})
        add("hit_expected_resource", hit, "quality",
            f"expected one of {must}, got {valid_ids}")
        add("no_forbidden_resources", not bad, "quality", f"picked {bad}" if bad else "ok")
        add("precision_at_least_0.5", precision >= 0.5, "quality", f"precision={precision:.2f}")
    if ex["steps"] is not None and case.get("expect_resources", True):
        n = len(ex["steps"])
        add("step_count_2_to_6", 2 <= n <= 6, "quality", f"{n} steps")

    # --- calendar
    cal = classify_calendar(ex["calendar"])
    metrics["calendar"] = cal
    add("no_false_calendar_success", cal["state"] != "claimed_success_without_event_id", "hard",
        f"state={cal['state']}")
    if cal["state"] == "scheduled":
        eid = str(cal.get("event_id") or "")
        add("event_id_not_fabricated", eid.upper() != "FAKE-1", "hard", f"event_id={eid}")
        if cal.get("timestamp"):
            d = parse_ts(cal["timestamp"])
            add("scheduled_time_in_future", d is not None and d > now, "hard", f"timestamp={cal['timestamp']}")
    exp = case.get("expect_calendar", "any")
    if exp == "none":
        add("no_calendar_when_not_requested", cal["state"] != "scheduled", "hard", f"state={cal['state']}")
    elif exp == "scheduled":
        add("calendar_request_handled", cal["state"] in ("scheduled", "failed_or_unscheduled"), "hard",
            f"state={cal['state']} (absent means the check-in was ignored)")
        add("calendar_booked", cal["state"] == "scheduled", "info",
            "mock service fails intermittently; see calendar probe for baseline")
        spec = case.get("calendar_check")
        if spec and cal["state"] == "scheduled":
            if cal.get("timestamp"):
                ok, detail = check_timestamp(cal["timestamp"], spec, now)
                add("timestamp_matches_request", ok, "quality", detail)
            else:
                add("timestamp_returned", False, "quality", "scheduled but no timestamp in response")
    return checks, metrics


# --------------------------------------------------------------------------- suites
async def run_cases(client, base, cases, catalog, judge, now, case_concurrency) -> list[dict]:
    sem = asyncio.Semaphore(case_concurrency)

    async def one(case):
        payload, raw, shown = build_request(case)
        async with sem:
            resp = await call_endpoint(client, base, payload, raw)
        checks, metrics = evaluate(case, resp, catalog, now)
        judged = None
        if judge.enabled and case["category"] == "quality" and resp["status"] == 200:
            judged = await judge.score(case["message"], resp["body"], catalog)
            if "faithfulness" in judged:
                checks.append({"check": "judge_faithfulness_>=4", "passed": judged["faithfulness"] >= 4,
                               "severity": "quality", "detail": f"{judged}"})
                checks.append({"check": "judge_usefulness_>=4", "passed": judged["usefulness"] >= 4,
                               "severity": "quality", "detail": ""})
        print(f"  {'FAIL' if any(not c['passed'] and c['severity']=='hard' for c in checks) else 'ok  '} "
              f"{case['id']:<30} {resp['status']} {resp['latency_ms']:>6} ms  {metrics.get('recommended', '')}")
        return {"id": case["id"], "category": case["category"], "input": shown,
                "note": case.get("note"), "checks": checks, "metrics": metrics,
                "judge": judged, "response": resp["body"], "error": resp["error"]}

    return await asyncio.gather(*(one(c) for c in cases))


async def concurrency_suite(client, base, messages, n, health_path, threshold_ms) -> dict:
    health: list[tuple[float, bool]] = []
    stop = asyncio.Event()

    async def poll():
        while not stop.is_set():
            t = time.perf_counter()
            try:
                r = await client.get(base + health_path, timeout=10)
                ok = r.status_code < 500
            except Exception:
                ok = False
            health.append(((time.perf_counter() - t) * 1000, ok))
            await asyncio.sleep(0.2)

    poller = asyncio.create_task(poll())
    await asyncio.sleep(0.3)
    t0 = time.perf_counter()
    results = await asyncio.gather(*(call_endpoint(client, base, {"message": messages[i % len(messages)]})
                                     for i in range(n)))
    wall = (time.perf_counter() - t0) * 1000
    stop.set()
    await poller
    lats = [r["latency_ms"] for r in results]
    hl = [h for h, _ in health]
    return {
        "requests": n,
        "wall_ms": round(wall),
        "status_counts": dict(Counter(str(r["status"]) for r in results)),
        "latency": latency_stats(lats),
        "effective_parallelism": round(sum(lats) / wall, 2) if wall else None,
        "health_path": health_path,
        "health_latency": latency_stats(hl),
        "health_failures": sum(1 for _, ok in health if not ok),
        "event_loop_responsive": bool(hl) and max(hl) < threshold_ms and all(ok for _, ok in health),
        "threshold_ms": threshold_ms,
    }


async def consistency_suite(client, base, case, catalog, now, repeats) -> dict:
    runs = []
    for _ in range(repeats):
        resp = await call_endpoint(client, base, {"message": case["message"]})
        checks, metrics = evaluate(case, resp, catalog, now)
        runs.append({"status": resp["status"], "latency_ms": resp["latency_ms"],
                     "ids": metrics.get("recommended", []),
                     "calendar_state": (metrics.get("calendar") or {}).get("state"),
                     "hard_failures": [c["check"] for c in checks if not c["passed"] and c["severity"] == "hard"]})
    sets = [set(r["ids"]) for r in runs if r["status"] == 200]
    pairs = list(itertools.combinations(sets, 2))
    return {
        "case": case["id"], "repeats": repeats, "runs": runs,
        "mean_pairwise_jaccard": round(statistics.mean(jaccard(a, b) for a, b in pairs), 2) if pairs else None,
        "calendar_outcomes": dict(Counter(r["calendar_state"] for r in runs)),
        "status_counts": dict(Counter(str(r["status"]) for r in runs)),
        "latency": latency_stats([r["latency_ms"] for r in runs]),
    }


async def calendar_probe(n: int, concurrency: int) -> dict:
    """Hit the mock calendar directly to measure its own failure rate and latency."""
    sem = asyncio.Semaphore(concurrency)
    ts = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with httpx.AsyncClient(timeout=30) as c:
        async def one(i, body):
            async with sem:
                t = time.perf_counter()
                try:
                    r = await c.post(CAL_URL, json=body)
                    lat = (time.perf_counter() - t) * 1000
                    try:
                        data = r.json()
                    except ValueError:
                        data = {"_text": r.text[:300]}
                    return {"status": r.status_code, "latency_ms": round(lat),
                            "event_id": data.get("event_id") if isinstance(data, dict) else None,
                            "body": data, "error": None}
                except Exception as e:
                    return {"status": None, "latency_ms": round((time.perf_counter() - t) * 1000),
                            "event_id": None, "body": None, "error": f"{type(e).__name__}: {e}"}

        rs = await asyncio.gather(*(one(i, {"title": f"Eval probe {i}", "timestamp": ts}) for i in range(n)))
        bad_ts = await one(-1, {"title": "Eval probe invalid timestamp", "timestamp": "not-a-date"})

    ok = [r for r in rs if r["status"] and 200 <= r["status"] < 300 and r["event_id"]]
    return {
        "calls": n,
        "success_rate": round(len(ok) / n, 2) if n else None,
        "status_counts": dict(Counter(str(r["status"]) for r in rs)),
        "errors": dict(Counter(r["error"].split(":")[0] for r in rs if r["error"])),
        "success_without_event_id": sum(1 for r in rs if r["status"] and 200 <= r["status"] < 300 and not r["event_id"]),
        "latency_all": latency_stats([r["latency_ms"] for r in rs]),
        "latency_success": latency_stats([r["latency_ms"] for r in ok]),
        "sample_failures": [r for r in rs if r not in ok][:3],
        "invalid_timestamp_response": bad_ts,
    }


# --------------------------------------------------------------------------- summary
def summarise(case_results, concurrency, consistency, probe) -> dict:
    all_checks = [c | {"case": r["id"]} for r in case_results for c in r["checks"]]
    hard_fail = [c for c in all_checks if c["severity"] == "hard" and not c["passed"]]
    quality = [c for c in all_checks if c["severity"] == "quality"]
    q = [r["metrics"] for r in case_results if r["category"] == "quality" and "hit" in r["metrics"]]

    def rate(key):
        return round(sum(1 for m in q if m.get(key)) / len(q), 2) if q else None

    by_cat: dict[str, list] = {}
    cal_lat, nocal_lat = [], []
    for r in case_results:
        lat = r["metrics"].get("latency_ms")
        if lat is None:
            continue
        by_cat.setdefault(r["category"], []).append(lat)
        if (r["metrics"].get("calendar") or {}).get("state") in ("scheduled", "failed_or_unscheduled"):
            cal_lat.append(lat)
        elif r["metrics"].get("status") == 200:
            nocal_lat.append(lat)

    ts_checks = [c for c in all_checks if c["check"] == "timestamp_matches_request"]
    booked = [c for c in all_checks if c["check"] == "calendar_booked"]
    judged = [r["judge"] for r in case_results if r.get("judge") and "usefulness" in r["judge"]]
    judge_means = {k: round(statistics.mean(j[k] for j in judged), 2)
                   for k in ("usefulness", "faithfulness", "resource_fit", "conciseness")} if judged else None

    hard_total = sum(1 for c in all_checks if c["severity"] == "hard")
    if concurrency and not concurrency["event_loop_responsive"]:
        hard_fail.append({"case": "concurrency", "check": "event_loop_responsive", "passed": False,
                          "severity": "hard", "detail": f"max health latency {concurrency['health_latency'].get('max')} ms"})
    if consistency:
        for i, run in enumerate(consistency["runs"]):
            for f in run["hard_failures"]:
                hard_fail.append({"case": f"consistency run {i + 1}", "check": f, "passed": False,
                                  "severity": "hard", "detail": ""})

    return {
        "verdict": "PASS" if not hard_fail else "FAIL",
        "hard_checks_total": hard_total,
        "hard_failures": hard_fail,
        "quality_checks_passed": f"{sum(c['passed'] for c in quality)}/{len(quality)}",
        "quality_failures": [c for c in quality if not c["passed"]],
        "answer_correctness": {
            "cases": len(q),
            "hit_rate": rate("hit"),
            "top1_accuracy": rate("top1"),
            "mean_precision": round(statistics.mean(m["precision"] for m in q), 2) if q else None,
            "cases_with_forbidden_picks": sum(1 for m in q if m.get("forbidden_picked")),
        },
        "judge_means": judge_means,
        "latency": {
            "all_endpoint_calls": latency_stats([x for v in by_cat.values() for x in v]),
            "by_category": {k: latency_stats(v) for k, v in by_cat.items()},
            "with_calendar": latency_stats(cal_lat),
            "without_calendar": latency_stats(nocal_lat),
        },
        "calendar": {
            "in_endpoint_booked": f"{sum(c['passed'] for c in booked)}/{len(booked)}",
            "timestamp_correct": f"{sum(c['passed'] for c in ts_checks)}/{len(ts_checks)}",
            "false_success_claims": sum(1 for c in all_checks
                                        if c["check"] == "no_false_calendar_success" and not c["passed"]),
            "probe": probe,
        },
        "concurrency": concurrency,
        "consistency": consistency,
    }


# --------------------------------------------------------------------------- main
def find_catalog(explicit: str | None) -> Path:
    for p in [explicit, HERE.parent / "catalog.json", HERE / "catalog.json", Path("catalog.json")]:
        if p and Path(p).exists():
            return Path(p)
    sys.exit("catalog.json not found; pass --catalog")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--cases", default=str(HERE / "golden_cases.json"))
    ap.add_argument("--catalog")
    ap.add_argument("--out", default=str(HERE / "reports"))
    ap.add_argument("--only", choices=["quality", "robustness"], help="run one case category")
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--case-concurrency", type=int, default=1,
                    help="parallel golden cases (1 keeps latency numbers clean)")
    ap.add_argument("--concurrency", type=int, default=10, help="requests in the async load test (0 = skip)")
    ap.add_argument("--repeats", type=int, default=5, help="consistency repeats (0 = skip)")
    ap.add_argument("--calendar-probes", type=int, default=20)
    ap.add_argument("--skip-calendar-probe", action="store_true")
    ap.add_argument("--health-path", default="/openapi.json")
    ap.add_argument("--health-threshold-ms", type=float, default=1000)
    ap.add_argument("--quick", action="store_true", help="repeats=2, concurrency=5, probes=8")
    args = ap.parse_args()
    if args.quick:
        args.repeats, args.concurrency, args.calendar_probes = 2, 5, 8

    catalog_path = find_catalog(args.catalog)
    catalog = {c["id"]: c for c in json.loads(catalog_path.read_text(encoding="utf-8"))}
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    if args.only:
        cases = [c for c in cases if c["category"] == args.only]
    base = args.base_url.rstrip("/")
    judge = Judge()
    now = dt.datetime.now(dt.timezone.utc)

    async with httpx.AsyncClient(timeout=args.timeout, limits=httpx.Limits(max_connections=100)) as client:
        try:
            await client.get(base + args.health_path, timeout=5)
        except Exception as e:
            sys.exit(f"Server not reachable at {base} ({e}). Start it with: uvicorn main:app --port 8000")

        print(f"Catalog: {catalog_path} ({len(catalog)} items) | judge: {'on' if judge.enabled else 'off'}")
        print(f"\n[1/4] Golden cases ({len(cases)})")
        case_results = await run_cases(client, base, cases, catalog, judge, now, args.case_concurrency)

        concurrency = None
        if args.concurrency > 0:
            print(f"\n[2/4] Async load test ({args.concurrency} concurrent, polling {args.health_path})")
            msgs = [c["message"] for c in cases if c["category"] == "quality"][:5] or ["I keep missing deadlines."]
            concurrency = await concurrency_suite(client, base, msgs, args.concurrency,
                                                  args.health_path, args.health_threshold_ms)
            print(f"  wall {concurrency['wall_ms']} ms, parallelism x{concurrency['effective_parallelism']}, "
                  f"health max {concurrency['health_latency'].get('max')} ms, "
                  f"responsive={concurrency['event_loop_responsive']}")

        consistency = None
        anchor = next((c for c in cases if c["id"] == "deadlines_checkin"), None)
        if args.repeats > 0 and anchor:
            print(f"\n[3/4] Consistency ({args.repeats}x '{anchor['id']}')")
            consistency = await consistency_suite(client, base, anchor, catalog, now, args.repeats)
            print(f"  jaccard {consistency['mean_pairwise_jaccard']}, calendar {consistency['calendar_outcomes']}")

    probe = None
    if not args.skip_calendar_probe and args.calendar_probes > 0:
        print(f"\n[4/4] Calendar probe ({args.calendar_probes} direct calls)")
        probe = await calendar_probe(args.calendar_probes, concurrency=5)
        print(f"  success rate {probe['success_rate']}, statuses {probe['status_counts']}, "
              f"p95 {probe['latency_all'].get('p95')} ms")

    summary = summarise(case_results, concurrency, consistency, probe)
    results = {
        "run_at": now.isoformat(), "base_url": base, "catalog": str(catalog_path),
        "judge_model": judge.model if judge.enabled else None,
        "summary": summary, "cases": case_results,
    }
    out_dir = Path(args.out) / now.strftime("%Y%m%d-%H%M%S")
    paths = write_reports(results, out_dir)

    ac = summary["answer_correctness"]
    print(f"\nVerdict: {summary['verdict']}  | hard failures: {len(summary['hard_failures'])} "
          f"| quality {summary['quality_checks_passed']}")
    print(f"Correctness: hit {ac['hit_rate']}, top-1 {ac['top1_accuracy']}, precision {ac['mean_precision']}")
    print(f"Latency p50/p95: {summary['latency']['all_endpoint_calls'].get('p50')}/"
          f"{summary['latency']['all_endpoint_calls'].get('p95')} ms")
    for p in paths:
        print(f"Report: {p}")
    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
