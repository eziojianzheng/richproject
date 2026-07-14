#!/usr/bin/env python
"""Debug-only Playwright inspector for /monitor. Produces screenshot, trace and JSON report."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[2]
SENSITIVE = {"authorization", "cookie", "set-cookie", "api_key", "token", "password", "secret"}


def load_config() -> tuple[dict, dict]:
    path = ROOT / "config.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    debug = (data or {}).get("debug") or {}
    return debug, debug.get("playwright") or {}


def safe_headers(headers: dict) -> dict:
    return {k: ("<redacted>" if k.lower() in SENSITIVE else v) for k, v in headers.items()}


def json_safe(value, limit=200_000):
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
        return value if len(text) <= limit else {"truncated": True, "chars": len(text)}
    except Exception:
        return str(value)[:2000]


def parse_args(defaults: dict) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect the monitor page in an isolated Chromium instance.")
    parser.add_argument("--base-url", default=defaults.get("base_url", "http://127.0.0.1:5000"))
    parser.add_argument("--headed", action="store_true", help="Show the Chromium window.")
    parser.add_argument("--headless", action="store_true", help="Force headless Chromium.")
    parser.add_argument("--watch-seconds", type=int, default=int(defaults.get("watch_seconds", 60)))
    parser.add_argument("--concept", default="机器人概念")
    parser.add_argument("--output-dir", default=defaults.get("output_dir", "artifacts/debug"))
    return parser.parse_args()

def main() -> int:
    debug, pw_cfg = load_config()
    if debug.get("enabled") is not True:
        print("Refusing to start: set debug.enabled: true in config.yml.", file=sys.stderr)
        return 2
    args = parse_args(pw_cfg)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Install debug dependencies: py -m pip install -r requirements-debug.txt", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = (ROOT / args.output_dir / f"monitor-{stamp}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at": datetime.now().isoformat(timespec="seconds"), "base_url": base_url,
        "concept": args.concept, "console": [], "page_errors": [], "requests_failed": [],
        "responses": [], "samples": [], "assertions": {}, "artifacts": {},
    }
    exit_code = 1
    with sync_playwright() as pw:
        headless = args.headless or (not args.headed and bool(pw_cfg.get("headless", False)))
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1600, "height": 1000}, no_viewport=args.headed)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        timeout = int(pw_cfg.get("timeout_ms", 30000))
        page.set_default_timeout(timeout)

        page.on("console", lambda msg: report["console"].append({"type": msg.type, "text": msg.text[:4000]}))
        page.on("pageerror", lambda err: report["page_errors"].append(str(err)[:4000]))
        page.on("requestfailed", lambda req: report["requests_failed"].append({"url": req.url, "error": req.failure}))

        def on_response(response):
            if not response.url.startswith(origin) or "/api/monitor/" not in response.url:
                return
            item = {"url": response.url, "status": response.status, "headers": safe_headers(response.headers)}
            if "/api/monitor/concept/zt-stats" in response.url:
                try:
                    item["json"] = json_safe(response.json())
                except Exception as exc:
                    item["body_error"] = str(exc)
            report["responses"].append(item)
        page.on("response", on_response)

        try:
            runtime_response = context.request.get(base_url + "/api/debug/runtime", timeout=timeout)
            report["runtime_status"] = runtime_response.status
            report["runtime"] = runtime_response.json() if runtime_response.ok else runtime_response.text()
            if runtime_response.status != 200:
                raise RuntimeError("Server debug mode is disabled or the Flask process was not restarted.")

            page.goto(base_url + "/monitor?pw_debug=" + stamp, wait_until="domcontentloaded")
            page.locator('.mon-tab[data-mtab="dist"]').click()
            page.locator("#mon-view-dist").wait_for(state="visible")
            page.wait_for_function("typeof echarts !== 'undefined' && echarts.getInstanceByDom(document.querySelector('#conceptZtChart'))")
            page.wait_for_function("typeof _conceptZtData !== 'undefined' && _conceptZtData && _conceptZtData.concepts")

            deadline = time.time() + max(1, args.watch_seconds)
            while time.time() < deadline:
                sample = page.evaluate("""concept => {
                  const data = (typeof _conceptZtData !== 'undefined' && _conceptZtData) ? _conceptZtData : {};
                  const item = (data.concepts || []).find(x => x.concept === concept) || null;
                  const last = (data.ranks_timeline || []).at(-1) || {};
                  const card = [...document.querySelectorAll('.ever-concept-card')]
                    .find(x => x.dataset.concept === concept);
                  const chart = echarts.getInstanceByDom(document.querySelector('#conceptZtChart'));
                  const option = chart ? chart.getOption() : null;
                  return { at: new Date().toISOString(), apiTs: data.ts, item,
                    finalRank: last.ranks ? last.ranks[concept] : null,
                    finalRankTs: last.ts || null, cardText: card ? card.innerText : null,
                    chartSize: chart ? [chart.getWidth(), chart.getHeight()] : null,
                    chartOption: option ? {xAxis: option.xAxis, series: option.series} : null,
                    debug: window.__MONITOR_DEBUG__, errors: window._errlog || [] };
                }""", args.concept)
                report["samples"].append(json_safe(sample))
                print(f"[{sample.get('apiTs')}] {args.concept}: zt={((sample.get('item') or {}).get('zt_count'))} rank={sample.get('finalRank')} card={sample.get('cardText')!r}")
                page.wait_for_timeout(2000)

            screenshot = run_dir / "monitor-dist.png"
            page.screenshot(path=str(screenshot), full_page=True)
            report["artifacts"]["screenshot"] = str(screenshot.relative_to(ROOT))
            report["local_storage"] = page.evaluate("Object.fromEntries(Object.entries(localStorage))")
            report["page_debug"] = page.evaluate("window.__MONITOR_DEBUG__")
            last = report["samples"][-1] if report["samples"] else {}
            item = (last.get("item") or {}) if isinstance(last, dict) else {}
            rank = last.get("finalRank") if isinstance(last, dict) else None
            card_text = last.get("cardText") if isinstance(last, dict) else None
            report["assertions"] = {
                "debug_enabled": bool((report.get("runtime") or {}).get("debug")),
                "server_instance_matches": (report.get("runtime") or {}).get("instance_id") == (last.get("debug") or {}).get("serverInstance"),
                "target_concept_present": bool(item), "final_rank_present": rank is not None,
                "card_present": bool(card_text), "chart_has_size": all(v > 0 for v in (last.get("chartSize") or [0, 0])),
                "no_page_errors": not report["page_errors"], "no_request_failures": not report["requests_failed"],
            }
            rows = (page.evaluate("(typeof _conceptZtData !== 'undefined' && _conceptZtData) ? _conceptZtData.concepts : []") or [])[:10]
            target_count = item.get("zt_count") if item else None
            expected_rank = None
            if target_count is not None:
                expected_rank = 1 + sum(1 for row in rows if row.get("zt_count", 0) > target_count)
            report["assertions"]["rank_matches_current_order"] = rank == expected_rank
            exit_code = 0 if all(report["assertions"].values()) else 1
        except Exception as exc:
            report["fatal_error"] = f"{type(exc).__name__}: {exc}"
            try:
                failure = run_dir / "failure.png"
                page.screenshot(path=str(failure), full_page=True)
                report["artifacts"]["failure_screenshot"] = str(failure.relative_to(ROOT))
            except Exception:
                pass
            print(report["fatal_error"], file=sys.stderr)
        finally:
            trace = run_dir / "trace.zip"
            context.tracing.stop(path=str(trace))
            report["artifacts"]["trace"] = str(trace.relative_to(ROOT))
            report["finished_at"] = datetime.now().isoformat(timespec="seconds")
            report_path = run_dir / "report.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(f"Report: {report_path}")
            context.close()
            browser.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
