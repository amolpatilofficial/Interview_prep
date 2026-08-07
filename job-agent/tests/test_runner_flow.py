#!/usr/bin/env python3
"""Drives the full orchestrator against the local fixture with a stubbed planner.

Verifies: batch fan-out over ||| URLs, DuckDB recording, screenshot capture,
answer-bank learning and reuse, and the review gate.

    python tests/test_runner_flow.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import runner as runner_mod  # noqa: E402
from app.db import db  # noqa: E402
from app.runner import orchestrator, parse_urls  # noqa: E402
from app.settings import settings  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures" / "sample_form.html").as_uri()

ANSWERS = {
    "first name": "Amol",
    "last name": "Patil",
    "email": "amol@example.com",
    "phone": "+91 90000 00000",
    "how many years of experience do you have with python": "8",
    "why do you want to work at acme": "The data platform is the product here.",
    "notice period": "30 days",
    "are you legally authorized to work in the united states": "No",
    "which of these do you have production experience with": "Apache Spark",
    "preferred work location": "Pune",
    "i accept the privacy policy": "true",
}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


class StubPlanner:
    """Stands in for Claude: deterministic, records what it was asked."""

    def __init__(self) -> None:
        self.asked: list[list[str]] = []
        self.precedents_seen: list[int] = []

    async def plan(self, *, profile, page, fields, precedents, already_answered):
        pending = [f for f in fields if f["jaa_id"] not in already_answered]
        self.asked.append([f["normalized_question"] for f in pending])
        self.precedents_seen.append(len(precedents))
        actions = []
        for f in pending:
            key = f["normalized_question"]
            if f["type"] == "file":
                actions.append({"field_id": f["jaa_id"], "action": "upload",
                                "value": "resume", "confidence": 0.95, "note": ""})
            elif key in ANSWERS:
                action = "check" if f["type"] == "checkbox" else (
                    "select" if f["type"] in {"select", "radio", "checkbox_group", "custom_select"} else "fill")
                actions.append({"field_id": f["jaa_id"], "action": action,
                                "value": ANSWERS[key], "confidence": 0.9, "note": ""})
            else:
                actions.append({"field_id": f["jaa_id"], "action": "skip",
                                "value": "", "confidence": 0.1, "note": "not in profile"})
        return {
            "job": {"company": "Acme Corp", "role": "Senior Data Engineer", "location": "Pune, India"},
            "actions": actions,
            "blockers": [],
        }


async def main() -> int:
    if not settings.api_key:
        # The orchestrator only needs a planner; stub it before it asks for one.
        pass

    stub = StubPlanner()

    async def fake_get_planner():
        return stub

    runner_mod.get_planner = fake_get_planner  # type: ignore[assignment]

    resume = Path(__file__).parent / "fixtures" / "fake_resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake resume for tests\n")
    profile = runner_mod.get_profile(refresh=True)
    profile.documents["resume"] = resume
    runner_mod.get_profile = lambda refresh=False: profile  # type: ignore[assignment]

    print("\n[url parsing]")
    urls = parse_urls(f"{FIXTURE} ||| {FIXTURE}")
    check("||| splits but dedupes identical URLs", len(urls) == 1, str(len(urls)))

    print("\n[run 1 — cold, empty answer bank]")
    res = orchestrator.start_batch([FIXTURE], "dry_run", concurrency=1, headless=True)
    batch = res["batch_id"]
    while batch in orchestrator.active_batches:
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)

    apps = db.list_applications(batch_id=batch)
    check("one application recorded", len(apps) == 1)
    app = apps[0]
    check("status is filled_not_submitted", app["status"] == "filled_not_submitted", str(app["status"]))
    check("company captured", app["company"] == "Acme Corp", str(app["company"]))
    check("role captured", app["role"] == "Senior Data Engineer", str(app["role"]))
    check("fields discovered", (app["fields_total"] or 0) >= 12, str(app["fields_total"]))
    check("fields filled", (app["fields_filled"] or 0) >= 11, str(app["fields_filled"]))
    check("file uploaded", (app["files_uploaded"] or 0) == 1, str(app["files_uploaded"]))
    check("screenshot written",
          bool(app["screenshot"]) and (settings.screenshot_dir / app["screenshot"]).exists())

    rows = db.application_fields(app["id"])
    check("per-field audit rows stored", len(rows) == app["fields_total"], str(len(rows)))
    marketing = next((r for r in rows if "marketing" in (r["question"] or "").lower()), None)
    check("marketing checkbox recorded as skipped", marketing is not None and not marketing["filled"])
    email_row = next((r for r in rows if r["normalized_question"] == "email"), None)
    check("email answer stored in audit", email_row and email_row["answer"] == "amol@example.com")

    bank = {a["normalized_question"]: a for a in db.all_answers()}
    check("reusable answer learned", "email" in bank, ", ".join(list(bank)[:6]))
    check("per-company answer NOT learned",
          "why do you want to work at acme" not in bank)

    print("\n[run 2 — warm, should reuse the bank]")
    before = len(stub.asked[-1])
    res2 = orchestrator.start_batch([FIXTURE], "dry_run", concurrency=1, headless=True)
    while res2["batch_id"] in orchestrator.active_batches:
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)
    after = len(stub.asked[-1])
    check("model asked about fewer fields the second time", after < before, f"{before} -> {after}")
    reused = [r for r in db.application_fields(db.list_applications(batch_id=res2["batch_id"])[0]["id"])
              if r["source"] == "memory"]
    check("answers sourced from the bank", len(reused) > 0, f"{len(reused)} reused")

    print("\n[run 3 — review gate]")
    res3 = orchestrator.start_batch([FIXTURE], "review", concurrency=1, headless=True)
    app3 = db.list_applications(batch_id=res3["batch_id"])[0]["id"]
    for _ in range(200):
        await asyncio.sleep(0.25)
        if db.get_application(app3)["status"] == "awaiting_review":
            break
    check("run paused for review", db.get_application(app3)["status"] == "awaiting_review",
          str(db.get_application(app3)["status"]))
    check("gate is registered", app3 in orchestrator.awaiting_review())
    check("reject is honoured", orchestrator.decide(app3, False))
    while res3["batch_id"] in orchestrator.active_batches:
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)
    check("rejected application not submitted", db.get_application(app3)["status"] == "rejected",
          str(db.get_application(app3)["status"]))

    resume.unlink(missing_ok=True)
    print(f"\n{'ALL CHECKS PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
