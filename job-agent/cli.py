#!/usr/bin/env python3
"""Headless CLI — same engine as the UI, no browser tab required.

    python cli.py --mode dry_run "https://a/apply ||| https://b/apply"
    python cli.py --stats
    python cli.py --setup-login https://www.linkedin.com
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.db import db
from app.events import hub
from app.profile import get_profile
from app.runner import orchestrator, parse_urls
from app.settings import VALID_MODES, settings

COLOR = {"info": "\033[0m", "warn": "\033[33m", "error": "\033[31m"}
RESET = "\033[0m"


async def stream_logs() -> None:
    q = hub.subscribe()
    try:
        while True:
            ev = await q.get()
            if ev.get("kind") != "log":
                continue
            level = ev.get("level", "info")
            print(f"{COLOR.get(level, '')}{ev['message']}{RESET}", flush=True)
    except asyncio.CancelledError:
        pass
    finally:
        hub.unsubscribe(q)


async def run(urls: list[str], mode: str, concurrency: int, headless: bool) -> int:
    profile = get_profile(refresh=True)
    if profile.error:
        print(f"error: {profile.error}", file=sys.stderr)
        return 1
    if not settings.api_key:
        print("error: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1
    if mode == "review":
        print("note: 'review' mode needs the web UI to approve. Using 'dry_run'.", file=sys.stderr)
        mode = "dry_run"

    logger = asyncio.create_task(stream_logs())
    result = orchestrator.start_batch(urls, mode, concurrency, headless)
    batch_id = result["batch_id"]

    while batch_id in orchestrator.active_batches:
        await asyncio.sleep(0.5)
    await asyncio.sleep(0.5)
    logger.cancel()

    print("\n--- results ---")
    failures = 0
    for a in db.list_applications(limit=500, batch_id=batch_id):
        if a["status"] in {"failed", "rejected"}:
            failures += 1
        print(
            f"[{a['status']:<22}] {a['company'] or '?':<24} "
            f"{a['fields_filled']}/{a['fields_total']} fields  {a['url']}"
        )
        if a["error"]:
            print(f"    {a['error']}")
    return 1 if failures else 0


async def setup_login(url: str) -> int:
    """Open a real browser so you can log in once; saves cookies for later runs."""
    from playwright.async_api import async_playwright

    print(f"Opening {url}. Log in, then press Enter here to save the session.")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        page = await context.new_page()
        await page.goto(url)
        await asyncio.get_running_loop().run_in_executor(None, input)
        settings.storage_state.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(settings.storage_state))
        await browser.close()
    print(f"Saved session to {settings.storage_state}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="AI job application agent")
    p.add_argument("urls", nargs="?", default="", help="URLs separated by ||| or newlines")
    p.add_argument("--mode", choices=VALID_MODES, default="dry_run")
    p.add_argument("--concurrency", type=int, default=settings.concurrency)
    p.add_argument("--headless", action="store_true", default=settings.headless)
    p.add_argument("--stats", action="store_true", help="print stored stats and exit")
    p.add_argument("--setup-login", metavar="URL", help="log in once and save the browser session")
    args = p.parse_args()

    if args.stats:
        s = db.stats()
        print(f"applications: {s['total']}   companies: {s['companies']}")
        print(f"fields filled: {s['fields_filled']}   answers remembered: {s['answers_remembered']}")
        for status, n in sorted(s["by_status"].items()):
            print(f"  {status:<24} {n}")
        return 0

    if args.setup_login:
        return asyncio.run(setup_login(args.setup_login))

    urls = parse_urls(args.urls)
    if not urls:
        p.error("no URLs given")
    return asyncio.run(run(urls, args.mode, args.concurrency, args.headless))


if __name__ == "__main__":
    raise SystemExit(main())
