"""Orchestrator: URLs in, filled (and optionally submitted) applications out."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Browser, async_playwright

from . import ats as ats_mod
from . import memory
from .db import db, new_id
from .events import hub
from .extractor import (
    APPLY_BUTTON_JS,
    SUBMIT_BUTTON_JS,
    extract_page,
)
from .filler import Filler, find_and_click
from .llm import LLMError, get_planner
from .profile import Profile, get_profile
from .settings import VALID_MODES, settings

URL_SPLIT = re.compile(r"\|\|\||[\r\n]+")
SUCCESS_MARKERS = re.compile(
    r"(thank you for (your )?appl|application (has been )?(submitted|received|sent)|"
    r"we('| ha)ve received your application|successfully submitted|"
    r"thanks for applying|your application is complete)",
    re.IGNORECASE,
)


def parse_urls(raw: str) -> list[str]:
    """Accepts `a ||| b ||| c`, newline-separated, or a mix of both."""
    out: list[str] = []
    seen: set[str] = set()
    for chunk in URL_SPLIT.split(raw or ""):
        url = chunk.strip().strip(",").strip()
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


@dataclass
class ReviewGate:
    event: asyncio.Event = dc_field(default_factory=asyncio.Event)
    approved: bool = False


class Orchestrator:
    def __init__(self) -> None:
        self._gates: dict[str, ReviewGate] = {}
        self._running: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ public
    @property
    def active_batches(self) -> list[str]:
        return [b for b, t in self._running.items() if not t.done()]

    def start_batch(
        self,
        urls: list[str],
        mode: str,
        concurrency: int | None = None,
        headless: bool | None = None,
    ) -> dict[str, Any]:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}")
        if not urls:
            raise ValueError("no valid URLs supplied")

        batch_id = new_id("batch")
        db.create_batch(batch_id, mode, len(urls))
        app_ids: list[str] = []
        for url in urls:
            app_id = new_id("app")
            db.create_application(app_id, batch_id, url, mode)
            app_ids.append(app_id)

        task = asyncio.create_task(
            self._run_batch(
                batch_id,
                list(zip(app_ids, urls)),
                mode,
                concurrency or settings.concurrency,
                settings.headless if headless is None else headless,
            )
        )
        self._running[batch_id] = task
        hub.log(
            f"Batch queued with {len(urls)} URL(s) in {mode} mode.",
            batch_id=batch_id,
        )
        hub.publish("batch", {"batch_id": batch_id, "status": "running", "urls": len(urls)})
        return {"batch_id": batch_id, "application_ids": app_ids, "urls": urls, "mode": mode}

    def decide(self, app_id: str, approved: bool) -> bool:
        gate = self._gates.get(app_id)
        if gate is None:
            return False
        gate.approved = approved
        gate.event.set()
        return True

    def awaiting_review(self) -> list[str]:
        return [k for k, g in self._gates.items() if not g.event.is_set()]

    # ----------------------------------------------------------------- internals
    async def _run_batch(
        self,
        batch_id: str,
        work: list[tuple[str, str]],
        mode: str,
        concurrency: int,
        headless: bool,
    ) -> None:
        profile = get_profile(refresh=True)
        if profile.error:
            for app_id, _ in work:
                db.update_application(app_id, status="failed", error=profile.error)
                hub.application_changed(app_id)
            hub.log(profile.error, level="error", batch_id=batch_id)
            db.finish_batch(batch_id, "failed")
            hub.publish("batch", {"batch_id": batch_id, "status": "failed"})
            return

        try:
            planner = await get_planner()
        except LLMError as exc:
            for app_id, _ in work:
                db.update_application(app_id, status="failed", error=str(exc))
                hub.application_changed(app_id)
            hub.log(str(exc), level="error", batch_id=batch_id)
            db.finish_batch(batch_id, "failed")
            hub.publish("batch", {"batch_id": batch_id, "status": "failed"})
            return

        sem = asyncio.Semaphore(max(1, concurrency))
        async with async_playwright() as pw:
            args = ["--disable-blink-features=AutomationControlled"]
            if settings.no_sandbox:
                # Required when Chromium runs inside an unprivileged container.
                args += ["--no-sandbox", "--disable-dev-shm-usage"]
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "slow_mo": settings.slow_mo_ms or 0,
                "args": args,
            }
            if settings.browser_executable:
                launch_kwargs["executable_path"] = settings.browser_executable
            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                async def bounded(app_id: str, url: str) -> None:
                    async with sem:
                        await self._process(
                            browser, batch_id, app_id, url, mode, profile, planner
                        )

                await asyncio.gather(
                    *(bounded(a, u) for a, u in work), return_exceptions=True
                )
            finally:
                await browser.close()

        db.finish_batch(batch_id, "done")
        hub.log("Batch finished.", batch_id=batch_id)
        hub.publish("batch", {"batch_id": batch_id, "status": "done"})

    async def _process(
        self,
        browser: Browser,
        batch_id: str,
        app_id: str,
        url: str,
        mode: str,
        profile: Profile,
        planner,
    ) -> None:
        def log(msg: str, level: str = "info") -> None:
            hub.log(msg, level=level, batch_id=batch_id, application_id=app_id)

        def touch(**fields: Any) -> None:
            db.update_application(app_id, **fields)
            hub.application_changed(app_id)

        touch(status="running")
        log(f"Opening {url}")

        context = None
        try:
            storage = settings.storage_state
            context = await browser.new_context(
                storage_state=str(storage) if storage.exists() else None,
                viewport={"width": 1440, "height": 1000},
                accept_downloads=False,
            )
            context.set_default_timeout(settings.nav_timeout_ms)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)

            html = ""
            try:
                html = await page.content()
            except Exception:
                pass
            vendor = ats_mod.detect(page.url, html)
            touch(ats=vendor.name, final_url=page.url)
            log(f"Detected ATS: {vendor.name}")

            await self._settle(page, vendor.settle_ms)

            snapshot = await extract_page(page)
            if len(snapshot["fields"]) < 3:
                clicked = await find_and_click(page, APPLY_BUTTON_JS, "data-jaa-apply")
                if clicked:
                    log(f'Clicked "{clicked}" to reach the application form.')
                    await self._settle(page, vendor.settle_ms + 800)
                    snapshot = await extract_page(page)

            fields = snapshot["fields"]
            if not fields:
                touch(
                    status="failed",
                    error="No form fields found on this page (login wall, or the form loads behind another click).",
                    final_url=page.url,
                )
                log("No form fields found — skipping.", "warn")
                await self._shot(page, app_id, touch)
                return

            log(f"Found {len(fields)} field(s) across {snapshot['frames']} frame(s).")
            touch(fields_total=len(fields))

            # ---- what do we already know? -----------------------------------
            exact, precedents = memory.resolve(fields)
            if exact:
                log(f"Reusing {len(exact)} answer(s) from previous applications.")

            plan = await planner.plan(
                profile=profile,
                page=snapshot,
                fields=fields,
                precedents=precedents,
                already_answered=exact,
            )

            job = plan.get("job") or {}
            company = (job.get("company") or "").strip() or ats_mod.company_from_url(page.url)
            touch(
                company=company,
                role=(job.get("role") or "").strip(),
                location=(job.get("location") or "").strip(),
            )
            for blocker in plan.get("blockers", []) or []:
                log(f"Blocker: {blocker}", "warn")

            actions = self._merge_actions(fields, exact, plan.get("actions", []))

            # ---- fill --------------------------------------------------------
            filler = Filler(page, lambda lvl, msg: log(msg, lvl))
            records, filled, uploaded = await self._fill_all(filler, fields, actions, profile, log)

            db.record_fields(app_id, records)
            learned = memory.learn(records)
            touch(fields_filled=filled, files_uploaded=uploaded)
            log(f"Filled {filled}/{len(fields)} field(s), uploaded {uploaded} file(s), "
                f"remembered {learned} answer(s).")

            shot = await self._shot(page, app_id, touch)

            missing_required = [
                r for r in records if r["required"] and not r["filled"]
            ]
            if missing_required:
                log(
                    "Required field(s) left blank: "
                    + "; ".join(r["question"] for r in missing_required[:6]),
                    "warn",
                )

            # ---- submit ------------------------------------------------------
            if mode == "dry_run":
                touch(status="filled_not_submitted", screenshot=shot)
                log("Dry run — form filled, nothing submitted.")
                return

            if mode == "review":
                gate = ReviewGate()
                self._gates[app_id] = gate
                touch(status="awaiting_review", screenshot=shot)
                hub.publish("review", {"application_id": app_id, "company": company})
                log("Waiting for your approval in the UI…")
                try:
                    await asyncio.wait_for(gate.event.wait(), timeout=3600)
                except asyncio.TimeoutError:
                    touch(status="filled_not_submitted", error="Review timed out after 1 hour.")
                    log("Review timed out — not submitted.", "warn")
                    return
                finally:
                    self._gates.pop(app_id, None)
                if not gate.approved:
                    touch(status="rejected")
                    log("You rejected this application — not submitted.", "warn")
                    return
                log("Approved. Submitting…")

            if missing_required and mode == "auto":
                touch(status="filled_not_submitted",
                      error=f"{len(missing_required)} required field(s) unanswered; not auto-submitted.")
                log("Refusing to auto-submit with required fields blank.", "warn")
                return

            await self._submit(page, app_id, touch, log)

        except LLMError as exc:
            touch(status="failed", error=str(exc))
            log(str(exc), "error")
        except Exception as exc:  # noqa: BLE001 - one bad URL must not kill the batch
            touch(status="failed", error=f"{type(exc).__name__}: {exc}")
            log(f"{type(exc).__name__}: {exc}", "error")
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ helpers
    @staticmethod
    async def _settle(page, ms: int) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=min(ms * 4, 15000))
        except Exception:
            pass
        await asyncio.sleep(ms / 1000)

    @staticmethod
    def _merge_actions(
        fields: list[dict], exact: dict[str, dict], llm_actions: list[dict]
    ) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        for jaa_id, hit in exact.items():
            merged[jaa_id] = {
                "field_id": jaa_id,
                "action": "fill",
                "value": hit["answer"],
                "confidence": 0.95 if hit["pinned"] else 0.85,
                "note": "reused from your answer bank",
                "source": "memory",
            }
        for a in llm_actions:
            fid = a.get("field_id")
            if not fid or fid in merged:
                continue
            merged[fid] = {**a, "source": "llm"}
        return merged

    async def _fill_all(
        self,
        filler: Filler,
        fields: list[dict],
        actions: dict[str, dict],
        profile: Profile,
        log,
    ) -> tuple[list[dict], int, int]:
        records: list[dict] = []
        filled = 0
        uploaded = 0

        for f in fields:
            action = actions.get(f["jaa_id"])
            base = {
                "field_id": f["jaa_id"],
                "question": f.get("question"),
                "normalized_question": f.get("normalized_question"),
                "field_type": f.get("type"),
                "required": bool(f.get("required")),
            }
            if action is None:
                records.append({**base, "action": "skip", "answer": None, "confidence": 0.0,
                                "source": "none", "filled": False,
                                "error": "no action returned for this field"})
                continue

            act = action.get("action", "skip")
            value = action.get("value", "")
            conf = float(action.get("confidence") or 0.0)
            source = action.get("source", "llm")

            if act == "skip":
                records.append({**base, "action": "skip", "answer": None, "confidence": conf,
                                "source": source, "filled": False,
                                "error": action.get("note") or "model chose to skip"})
                continue

            if conf < settings.min_confidence:
                records.append({**base, "action": act, "answer": value, "confidence": conf,
                                "source": source, "filled": False,
                                "error": f"confidence {conf:.2f} below threshold "
                                         f"{settings.min_confidence:.2f}"})
                log(f'Left "{f.get("question")}" blank — low confidence ({conf:.2f}).', "warn")
                continue

            try:
                await filler.apply(f, act, value, profile.documents)
                filled += 1
                if act == "upload" or f.get("type") == "file":
                    uploaded += 1
                records.append({**base, "action": act, "answer": value, "confidence": conf,
                                "source": source, "filled": True, "error": None})
            except Exception as exc:  # noqa: BLE001
                records.append({**base, "action": act, "answer": value, "confidence": conf,
                                "source": source, "filled": False,
                                "error": f"{type(exc).__name__}: {exc}"})
                log(f'Could not fill "{f.get("question")}": {exc}', "warn")
            await asyncio.sleep(0.08)

        return records, filled, uploaded

    async def _shot(self, page, app_id: str, touch) -> str | None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"{app_id}-{stamp}.png"
        target = settings.screenshot_dir / name
        try:
            await page.screenshot(path=str(target), full_page=True)
        except Exception:
            try:
                await page.screenshot(path=str(target))
            except Exception:
                return None
        touch(screenshot=name)
        return name

    async def _submit(self, page, app_id: str, touch, log) -> None:
        label = await find_and_click(page, SUBMIT_BUTTON_JS, "data-jaa-submit")
        if not label:
            touch(status="filled_not_submitted",
                  error="Could not find a submit button — finish this one by hand.")
            log("No submit button found.", "warn")
            return
        log(f'Clicked "{label}".')
        await asyncio.sleep(2.5)
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(1.5)

        body = ""
        try:
            body = await page.inner_text("body")
        except Exception:
            pass
        shot = await self._shot(page, app_id, touch)
        if SUCCESS_MARKERS.search(body or ""):
            touch(status="submitted", screenshot=shot, final_url=page.url, error=None)
            log("Application submitted — confirmation text detected.")
        else:
            touch(status="submitted_unconfirmed", screenshot=shot, final_url=page.url,
                  error="Submit clicked but no confirmation text was detected. Check the screenshot.")
            log("Submit clicked, but no confirmation text found. Verify the screenshot.", "warn")


orchestrator = Orchestrator()
