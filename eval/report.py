"""Render results.json into report.md and a self-contained report.html."""
from __future__ import annotations

import html
import json
from pathlib import Path


def _lat_row(name, s):
    return [name, s.get("n", 0), s.get("p50", "–"), s.get("p95", "–"), s.get("p99", "–"), s.get("max", "–"), s.get("mean", "–")]


LAT_HEAD = ["Scope", "n", "p50 ms", "p95 ms", "p99 ms", "max ms", "mean ms"]


def build_sections(r: dict) -> list[tuple[str, str | None, list, list]]:
    """Return (title, intro, headers, rows) tuples shared by both renderers."""
    s = r["summary"]
    ac = s["answer_correctness"]
    secs = []

    overview = [
        ["Verdict", s["verdict"]],
        ["Hard checks failed", f"{len(s['hard_failures'])} of {s['hard_checks_total']}+"],
        ["Quality checks passed", s["quality_checks_passed"]],
        ["Correct resource hit rate", ac["hit_rate"]],
        ["Top-1 accuracy", ac["top1_accuracy"]],
        ["Mean precision vs acceptable set", ac["mean_precision"]],
        ["Cases with forbidden picks", ac["cases_with_forbidden_picks"]],
        ["Endpoint latency p50 / p95", f"{s['latency']['all_endpoint_calls'].get('p50')} / "
                                       f"{s['latency']['all_endpoint_calls'].get('p95')} ms"],
        ["Calendar booked in endpoint", s["calendar"]["in_endpoint_booked"]],
        ["Calendar timestamps correct", s["calendar"]["timestamp_correct"]],
        ["False calendar success claims", s["calendar"]["false_success_claims"]],
    ]
    if s.get("judge_means"):
        overview.append(["LLM judge means (1–5)", ", ".join(f"{k} {v}" for k, v in s["judge_means"].items())])
    secs.append(("Overview", f"Run {r['run_at']} against {r['base_url']}.", ["Metric", "Value"], overview))

    secs.append(("Hard failures", "Contract, grounding and honesty checks. Any row here fails the run.",
                 ["Case", "Check", "Detail"],
                 [[f["case"], f["check"], f["detail"]] for f in s["hard_failures"]] or [["–", "none", ""]]))

    q_rows = []
    for c in r["cases"]:
        if c["category"] != "quality":
            continue
        m = c["metrics"]
        cal = m.get("calendar") or {}
        j = c.get("judge") or {}
        q_rows.append([
            c["id"], c["input"],
            ", ".join(m.get("expected_any_of", [])),
            ", ".join(f"{i} ({t})" for i, t in zip(m.get("recommended", []), m.get("recommended_titles", []))),
            "yes" if m.get("hit") else "no", "yes" if m.get("top1") else "no",
            m.get("precision", "–"), ", ".join(m.get("forbidden_picked", [])) or "–",
            m.get("n_steps", "–"),
            f"{cal.get('state', '–')} {cal.get('timestamp') or ''}".strip(),
            f"U{j['usefulness']} F{j['faithfulness']}" if "usefulness" in j else "–",
            m.get("latency_ms", "–"),
        ])
    secs.append(("LLM answers vs expected", "What the API recommended for each message, against the gold labels.",
                 ["Case", "Message", "Expected (any of)", "Recommended", "Hit", "Top-1", "Precision",
                  "Forbidden", "Steps", "Calendar", "Judge", "ms"], q_rows))

    rb_rows = []
    for c in r["cases"]:
        if c["category"] != "robustness":
            continue
        failed = [f"{x['check']} ({x['severity']})" for x in c["checks"] if not x["passed"]]
        rb_rows.append([c["id"], c["input"][:80], c["metrics"].get("status"),
                        (c["metrics"].get("calendar") or {}).get("state", "–"),
                        "pass" if not failed else "; ".join(failed), c["metrics"].get("latency_ms")])
    secs.append(("API robustness", "Malformed input, edge cases, prompt injection and ambiguous check-ins.",
                 ["Case", "Input", "Status", "Calendar", "Result", "ms"], rb_rows))

    qf = s.get("quality_failures") or []
    secs.append(("Quality check failures", "Soft checks: they don't fail the run but show where answers could improve.",
                 ["Case", "Check", "Detail"],
                 [[f["case"], f["check"], f["detail"]] for f in qf] or [["–", "none", ""]]))

    lat = s["latency"]
    lat_rows = [_lat_row("All endpoint calls", lat["all_endpoint_calls"]),
                _lat_row("With calendar attempt", lat["with_calendar"]),
                _lat_row("Without calendar", lat["without_calendar"])]
    lat_rows += [_lat_row(f"Category: {k}", v) for k, v in lat["by_category"].items()]
    secs.append(("Latency", "Sequential golden-case calls unless --case-concurrency was raised.", LAT_HEAD, lat_rows))

    cal = s["calendar"]
    p = cal.get("probe")
    if p:
        secs.append(("Calendar service probe",
                     "Direct calls to the mock calendar, showing the baseline failure rate your endpoint has to absorb.",
                     ["Metric", "Value"],
                     [["Calls", p["calls"]], ["Success rate (2xx with event_id)", p["success_rate"]],
                      ["Status counts", json.dumps(p["status_counts"])],
                      ["Transport errors", json.dumps(p["errors"])],
                      ["2xx without event_id", p["success_without_event_id"]],
                      ["Latency p50 / p95 / max", f"{p['latency_all'].get('p50')} / {p['latency_all'].get('p95')} / "
                                                  f"{p['latency_all'].get('max')} ms"],
                      ["Invalid timestamp response", f"{p['invalid_timestamp_response']['status']} "
                                                     f"{json.dumps(p['invalid_timestamp_response']['body'])[:160]}"]]))

    cc = s.get("concurrency")
    if cc:
        secs.append(("Async responsiveness",
                     f"{cc['requests']} concurrent requests while polling {cc['health_path']} every 200 ms. "
                     "Parallelism near 1 means requests are being processed one at a time (blocking calls in async code).",
                     ["Metric", "Value"],
                     [["Wall time", f"{cc['wall_ms']} ms"],
                      ["Effective parallelism", cc["effective_parallelism"]],
                      ["Status counts", json.dumps(cc["status_counts"])],
                      ["Request latency p50 / p95 / max", f"{cc['latency'].get('p50')} / {cc['latency'].get('p95')} / "
                                                          f"{cc['latency'].get('max')} ms"],
                      ["Health latency p50 / max", f"{cc['health_latency'].get('p50')} / "
                                                   f"{cc['health_latency'].get('max')} ms"],
                      ["Health failures", cc["health_failures"]],
                      ["Event loop responsive", f"{cc['event_loop_responsive']} (threshold {cc['threshold_ms']:.0f} ms)"]]))

    cs = s.get("consistency")
    if cs:
        rows = [[i + 1, run["status"], ", ".join(run["ids"]) or "–", run["calendar_state"],
                 ", ".join(run["hard_failures"]) or "–", run["latency_ms"]] for i, run in enumerate(cs["runs"])]
        secs.append(("Consistency",
                     f"'{cs['case']}' repeated {cs['repeats']} times. Mean pairwise Jaccard of resource sets: "
                     f"{cs['mean_pairwise_jaccard']}. Calendar outcomes: {json.dumps(cs['calendar_outcomes'])}.",
                     ["Run", "Status", "Resources", "Calendar", "Hard failures", "ms"], rows))
    return secs


def _md_cell(v) -> str:
    return str(v if v is not None else "–").replace("|", "\\|").replace("\n", " ")


def render_md(r: dict) -> str:
    out = ["# Goal track evaluation report", ""]
    for title, intro, head, rows in build_sections(r):
        out += [f"## {title}", ""]
        if intro:
            out += [intro, ""]
        out.append("| " + " | ".join(head) + " |")
        out.append("|" + "---|" * len(head))
        out += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
        out.append("")
    return "\n".join(out)


CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#ddd;--head:#f4f4f4;--pass:#1a7f37;--fail:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#e8e8e8;--muted:#999;--line:#333;--head:#222;--pass:#4caf50;--fail:#ef5350}}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:1400px;margin:auto}
h1{font-size:22px}h2{font-size:17px;margin-top:32px}p{color:var(--muted)}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:var(--head)}.PASS{color:var(--pass);font-weight:600}.FAIL{color:var(--fail);font-weight:600}
"""


def render_html(r: dict) -> str:
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Goal Track Eval</title><style>{CSS}</style></head><body>",
             "<h1>Goal track evaluation report</h1>"]
    for title, intro, head, rows in build_sections(r):
        parts.append(f"<h2>{html.escape(title)}</h2>")
        if intro:
            parts.append(f"<p>{html.escape(intro)}</p>")
        parts.append("<div class='wrap'><table><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in head) + "</tr>")
        for row in rows:
            cells = []
            for c in row:
                t = html.escape(str(c if c is not None else "–"))
                cls = f" class='{t}'" if t in ("PASS", "FAIL") else ""
                cells.append(f"<td{cls}>{t}</td>")
            parts.append("<tr>" + "".join(cells) + "</tr>")
        parts.append("</table></div>")
    parts.append("</body></html>")
    return "".join(parts)


def write_reports(results: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / "results.json", out_dir / "report.md", out_dir / "report.html"]
    paths[0].write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    paths[1].write_text(render_md(results), encoding="utf-8")
    paths[2].write_text(render_html(results), encoding="utf-8")
    return paths
