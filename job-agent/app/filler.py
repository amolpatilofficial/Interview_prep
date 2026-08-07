"""The hands of the agent: turns a fill plan into real interactions."""
from __future__ import annotations

import asyncio
import difflib
from pathlib import Path
from typing import Callable

TEXTLIKE = {
    "text", "email", "tel", "url", "number", "date", "search", "month", "textarea",
}

FILL_TIMEOUT = 8000


def _match_option(value: str, options: list[dict]) -> dict | None:
    """Resolve a model-supplied answer to a real option, tolerantly."""
    if not options:
        return None
    target = (value or "").strip().lower()
    if not target:
        return None

    for opt in options:  # exact label or value
        if str(opt.get("label", "")).strip().lower() == target:
            return opt
        if str(opt.get("value", "")).strip().lower() == target:
            return opt
    for opt in options:  # containment either way
        label = str(opt.get("label", "")).strip().lower()
        if label and (label in target or target in label):
            return opt
    labels = [str(o.get("label", "")) for o in options]
    close = difflib.get_close_matches(value, labels, n=1, cutoff=0.72)
    if close:
        return next(o for o in options if str(o.get("label", "")) == close[0])
    return None


class Filler:
    def __init__(self, page, log: Callable[[str, str], None]):
        self.page = page
        self.log = log

    def _frame(self, index: int):
        frames = self.page.frames
        return frames[index] if 0 <= index < len(frames) else self.page.main_frame

    def _locator(self, field: dict, jaa_id: str | None = None):
        frame = self._frame(field.get("frame", 0))
        return frame.locator(f'[data-jaa-id="{jaa_id or field["jaa_id"]}"]').first

    # ------------------------------------------------------------------ actions
    async def apply(self, field: dict, action: str, value: str, documents: dict[str, Path]) -> None:
        ftype = field.get("type", "text")

        if action == "upload":
            await self._upload(field, value, documents)
        elif action in {"check", "uncheck"}:
            await self._toggle(field, action == "check", value)
        elif action == "select":
            await self._select(field, value)
        elif action == "fill":
            if ftype in {"select", "multiselect", "custom_select", "radio", "checkbox_group"}:
                await self._select(field, value)
            elif ftype == "checkbox":
                await self._toggle(field, str(value).lower() not in {"false", "no", "0", ""}, value)
            elif ftype == "file":
                await self._upload(field, value, documents)
            else:
                await self._fill_text(field, value)
        else:
            raise ValueError(f"unsupported action {action!r}")

    async def _fill_text(self, field: dict, value: str) -> None:
        loc = self._locator(field)
        await loc.scroll_into_view_if_needed(timeout=FILL_TIMEOUT)
        maxlen = field.get("maxlength")
        text = str(value)
        if isinstance(maxlen, int) and maxlen > 0 and len(text) > maxlen:
            text = text[:maxlen]
        if field.get("type") == "richtext":
            await loc.click(timeout=FILL_TIMEOUT)
            await loc.press("Control+a")
            await loc.press("Delete")
            await loc.type(text, delay=5, timeout=FILL_TIMEOUT * 2)
            return
        try:
            await loc.fill(text, timeout=FILL_TIMEOUT)
        except Exception:
            # Masked / controlled inputs sometimes refuse .fill()
            await loc.click(timeout=FILL_TIMEOUT)
            await loc.press("Control+a")
            await loc.type(text, delay=15, timeout=FILL_TIMEOUT * 2)

    async def _toggle(self, field: dict, want_checked: bool, value: str = "") -> None:
        if field.get("type") == "checkbox_group":
            await self._select(field, value)
            return
        loc = self._locator(field)
        await loc.scroll_into_view_if_needed(timeout=FILL_TIMEOUT)
        try:
            if want_checked:
                await loc.check(timeout=FILL_TIMEOUT)
            else:
                await loc.uncheck(timeout=FILL_TIMEOUT)
        except Exception:
            # Custom-styled checkboxes hide the real input behind a label.
            await loc.click(timeout=FILL_TIMEOUT, force=True)

    async def _select(self, field: dict, value: str) -> None:
        ftype = field.get("type")
        options = field.get("options") or []

        if ftype in {"radio", "checkbox_group"}:
            opt = _match_option(value, options)
            if not opt:
                raise ValueError(f"no option matching {value!r} among {[o.get('label') for o in options][:12]}")
            target = self._locator(field, opt["jaa_id"])
            await target.scroll_into_view_if_needed(timeout=FILL_TIMEOUT)
            try:
                await target.check(timeout=FILL_TIMEOUT)
            except Exception:
                await target.click(timeout=FILL_TIMEOUT, force=True)
            return

        if ftype in {"select", "multiselect"}:
            loc = self._locator(field)
            await loc.scroll_into_view_if_needed(timeout=FILL_TIMEOUT)
            opt = _match_option(value, options)
            if opt is not None:
                try:
                    await loc.select_option(label=str(opt["label"]), timeout=FILL_TIMEOUT)
                    return
                except Exception:
                    await loc.select_option(value=str(opt["value"]), timeout=FILL_TIMEOUT)
                    return
            await loc.select_option(label=str(value), timeout=FILL_TIMEOUT)
            return

        # custom_select: react-select, Workday, Ashby, headless-ui comboboxes
        await self._custom_select(field, value)

    async def _custom_select(self, field: dict, value: str) -> None:
        frame = self._frame(field.get("frame", 0))
        loc = self._locator(field)
        await loc.scroll_into_view_if_needed(timeout=FILL_TIMEOUT)
        await loc.click(timeout=FILL_TIMEOUT)
        await asyncio.sleep(0.25)
        try:
            await loc.type(str(value)[:60], delay=25, timeout=FILL_TIMEOUT)
        except Exception:
            pass
        await asyncio.sleep(0.45)

        option = frame.locator(
            '[role="option"]:visible, li[id*="option"]:visible, '
            '.select__option:visible, [class*="option"]:visible'
        )
        count = await option.count()
        for i in range(min(count, 40)):
            candidate = option.nth(i)
            text = (await candidate.inner_text()).strip()
            if not text:
                continue
            if text.lower() == str(value).strip().lower() or str(value).strip().lower() in text.lower():
                await candidate.click(timeout=FILL_TIMEOUT)
                return
        if count:
            await option.first.click(timeout=FILL_TIMEOUT)
            return
        await loc.press("Enter")

    async def _upload(self, field: dict, doc_key: str, documents: dict[str, Path]) -> None:
        path = documents.get(doc_key)
        if path is None:
            # Tolerate the model naming the file instead of the key.
            for key, p in documents.items():
                if doc_key and (doc_key.lower() in key.lower() or doc_key.lower() in p.name.lower()):
                    path = p
                    break
        if path is None:
            raise ValueError(f"no document configured for {doc_key!r}")
        if not path.exists():
            raise ValueError(f"document file is missing on disk: {path}")

        frame = self._frame(field.get("frame", 0))
        loc = self._locator(field)
        try:
            await loc.set_input_files(str(path), timeout=FILL_TIMEOUT)
            return
        except Exception:
            pass
        # Drag-and-drop widgets keep the real <input type=file> hidden nearby.
        hidden = frame.locator('input[type="file"]')
        n = await hidden.count()
        for i in range(n):
            try:
                await hidden.nth(i).set_input_files(str(path), timeout=FILL_TIMEOUT)
                return
            except Exception:
                continue
        raise ValueError("could not attach the file to any file input on the page")


async def find_and_click(page, marker_js: str, marker_attr: str, timeout: int = 8000) -> str | None:
    """Run a marker script in every frame; click the first element it tags."""
    for frame in page.frames:
        try:
            label = await frame.evaluate(marker_js)
        except Exception:
            continue
        if not label:
            continue
        loc = frame.locator(f"[{marker_attr}]").first
        try:
            await loc.scroll_into_view_if_needed(timeout=timeout)
            await loc.click(timeout=timeout)
            return label
        except Exception:
            try:
                await loc.click(timeout=timeout, force=True)
                return label
            except Exception:
                continue
    return None
