#!/usr/bin/env python3
"""End-to-end check against a local form that mimics a real ATS page.

Exercises extraction, label resolution, noise filtering, every fill action
(text, select, radio, checkbox group, consent checkbox, custom combobox,
hidden file input behind a dropzone) and submit detection — with a stubbed
planner, so it needs no API key.

    python tests/test_form_flow.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.extractor import SUBMIT_BUTTON_JS, extract_page  # noqa: E402
from app.filler import Filler, find_and_click  # noqa: E402
from app.settings import settings  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "sample_form.html"

PLAN = {
    "first name": ("fill", "Amol"),
    "last name": ("fill", "Patil"),
    "email": ("fill", "amol@example.com"),
    "phone": ("fill", "+91 90000 00000"),
    "how many years of experience do you have with python": ("fill", "8"),
    "why do you want to work at acme": ("fill", "Because the data platform is the product here."),
    "notice period": ("select", "30 days"),
    "are you legally authorized to work in the united states": ("select", "No"),
    "which of these do you have production experience with": ("select", "Apache Spark"),
    "preferred work location": ("select", "Pune"),
    "resume cv": ("upload", "resume"),
    "i accept the privacy policy": ("check", "true"),
    "send me marketing emails about other roles": ("skip", ""),
}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


async def main() -> int:
    resume = Path(__file__).parent / "fixtures" / "fake_resume.txt"
    resume.write_text("Amol Patil — Senior Data Engineer\n", encoding="utf-8")

    async with async_playwright() as pw:
        launch: dict = {"headless": True}
        if settings.browser_executable:
            launch["executable_path"] = settings.browser_executable
        browser = await pw.chromium.launch(**launch)
        page = await browser.new_page()
        await page.goto(FIXTURE.as_uri())
        await page.wait_for_load_state("networkidle")

        snapshot = await extract_page(page)
        by_key = {f["normalized_question"]: f for f in snapshot["fields"]}

        print("\n[extraction]")
        check("page title read", "Senior Data Engineer" in snapshot["title"], snapshot["title"])
        check("site name read", snapshot["og_site"] == "Acme Corp", snapshot["og_site"])
        check("search box filtered out", "search jobs" not in by_key)
        check("password field filtered out", not any(f["type"] == "password" for f in snapshot["fields"]))
        check("label[for] resolved", by_key.get("first name", {}).get("question") == "First Name")
        check("required flag from asterisk", by_key.get("first name", {}).get("required") is True)
        check("email type detected", by_key.get("email", {}).get("type") == "email")
        check("maxlength captured", by_key.get("why do you want to work at acme", {}).get("maxlength") == 600)

        sel = by_key.get("notice period", {})
        check("select options captured", [o["label"] for o in sel.get("options", [])]
              == ["Select…", "Immediate", "30 days", "60 days"])

        radio = by_key.get("are you legally authorized to work in the united states", {})
        check("radio group collapsed via legend", radio.get("type") == "radio"
              and len(radio.get("options", [])) == 2,
              f"type={radio.get('type')} options={len(radio.get('options', []))}")

        cbg = by_key.get("which of these do you have production experience with", {})
        check("checkbox group collapsed", cbg.get("type") == "checkbox_group"
              and len(cbg.get("options", [])) == 3,
              f"type={cbg.get('type')} options={len(cbg.get('options', []))}")

        combo = by_key.get("preferred work location", {})
        check("custom combobox found", combo.get("type") == "custom_select", str(combo.get("type")))

        file_field = by_key.get("resume cv", {})
        check("hidden file input found behind dropzone", file_field.get("type") == "file")
        check("accept attribute captured", ".pdf" in (file_field.get("accept") or ""))

        consent = by_key.get("i accept the privacy policy", {})
        check("single consent checkbox found", consent.get("type") == "checkbox")

        # ---------------------------------------------------------- filling
        print("\n[filling]")
        filler = Filler(page, lambda lvl, msg: None)
        documents = {"resume": resume}
        errors: list[str] = []
        for key, (action, value) in PLAN.items():
            if action == "skip":
                continue
            field = by_key.get(key)
            if not field:
                errors.append(f"{key}: field not extracted")
                continue
            try:
                await filler.apply(field, action, value, documents)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{key}: {type(exc).__name__}: {exc}")
        check("every planned action applied cleanly", not errors, "; ".join(errors))

        # ------------------------------------------------------ verification
        print("\n[verification — values actually in the DOM]")
        check("text input set", await page.input_value("#fn") == "Amol")
        check("email input set", await page.input_value("#em") == "amol@example.com")
        check("number-ish input set", await page.input_value("#yrs") == "8")
        check("textarea set", (await page.input_value("#why")).startswith("Because the data platform"))
        check("select set by label", await page.input_value("#notice") == "30")
        check("correct radio checked", await page.is_checked('input[name="us_auth"][value="no"]'))
        check("wrong radio not checked", not await page.is_checked('input[name="us_auth"][value="yes"]'))
        check("checkbox group option checked", await page.is_checked('input[name="skills"][value="spark"]'))
        check("other group options untouched", not await page.is_checked('input[name="skills"][value="kafka"]'))
        check("consent checkbox checked", await page.is_checked('input[name="tos"]'))
        check("marketing checkbox left alone", not await page.is_checked('input[name="mkt"]'))
        check("custom combobox set", (await page.inner_text("#loc")).strip() == "Pune")
        check(
            "file attached to hidden input",
            await page.evaluate("document.querySelector('input[name=resume]').files.length") == 1,
        )

        # ----------------------------------------------------------- submit
        print("\n[submit]")
        label = await find_and_click(page, SUBMIT_BUTTON_JS, "data-jaa-submit")
        check("submit button located", label == "Submit Application", str(label))
        await page.wait_for_timeout(300)
        body = await page.inner_text("body")
        check("confirmation detected", "Thank you for your application" in body)

        await browser.close()

    resume.unlink(missing_ok=True)
    print(f"\n{'ALL CHECKS PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
