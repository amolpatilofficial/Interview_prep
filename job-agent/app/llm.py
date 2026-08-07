"""The reasoning half of the agent.

Given a form schema, the candidate's profile, and what they answered on previous
applications, Claude returns a structured fill plan — one action per field.
Structured outputs guarantee the JSON parses; we still validate every id.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import anthropic

from .memory import as_prompt_block
from .profile import Profile
from .settings import settings

SYSTEM_PROMPT = """\
You fill in online job-application forms on behalf of one specific candidate.

You are given: the candidate's authoritative facts and resume, the answers they
gave on previous applications, and the exact list of form fields on the page.
Return one action per field.

Rules:
1. Never invent a fact about the candidate. If a field asks for something not in
   the profile, resume, or prior answers, use action "skip" with a low
   confidence and say what is missing in `note`.
2. For select / radio / checkbox_group fields, `value` MUST be one of the
   supplied option labels, copied character for character. Never paraphrase an
   option.
3. For file fields, `value` is the document key from UPLOADABLE DOCUMENTS
   (e.g. "resume", "cover_letter"). Match the field's wording to the right
   document; skip if no document fits.
4. Prefer a previous answer verbatim when the question is the same question in
   different words. Consistency across applications matters more than variety.
5. Free-text questions ("why this company", "describe a project") should be
   answered in the candidate's own voice, grounded only in their resume, and
   sized to the field: 2-4 sentences unless a longer answer is clearly wanted.
   Respect `maxlength`.
6. Demographic / EEO / veteran / disability questions: use the candidate's
   stated preference. If they have not stated one, choose the decline-to-answer
   option when it exists, otherwise skip.
7. Consent, terms, and privacy checkboxes required to submit: check them.
   Marketing, newsletter, and "contact me about other roles" checkboxes: leave
   them alone unless the profile opts in.
8. `confidence` is your genuine calibration: 0.9+ means the value is copied
   from an authoritative fact, ~0.5 means a reasonable inference, below 0.4
   means a guess. Guesses on required fields are worse than blanks.

Answer only through the required JSON schema.
"""

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job": {
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "role": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["company", "role", "location"],
            "additionalProperties": False,
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["fill", "select", "check", "uncheck", "upload", "skip"],
                    },
                    "value": {"type": "string"},
                    "confidence": {"type": "number"},
                    "note": {"type": "string"},
                },
                "required": ["field_id", "action", "value", "confidence", "note"],
                "additionalProperties": False,
            },
        },
        "blockers": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["job", "actions", "blockers"],
    "additionalProperties": False,
}


class LLMError(RuntimeError):
    pass


def _compact_field(f: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "field_id": f["jaa_id"],
        "question": f.get("question"),
        "type": f.get("type"),
        "required": bool(f.get("required")),
    }
    if f.get("placeholder"):
        out["placeholder"] = f["placeholder"]
    if f.get("maxlength"):
        out["maxlength"] = f["maxlength"]
    if f.get("current_value"):
        out["current_value"] = str(f["current_value"])[:200]
    if f.get("accept"):
        out["accepted_file_types"] = f["accept"]
    if f.get("options"):
        out["options"] = [str(o.get("label", "")) for o in f["options"]][:80]
    return out


class Planner:
    def __init__(self) -> None:
        if not settings.api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        self._client = anthropic.AsyncAnthropic(api_key=settings.api_key)
        self._fallbacks_enabled = settings.server_fallbacks

    async def plan(
        self,
        *,
        profile: Profile,
        page: dict[str, Any],
        fields: list[dict[str, Any]],
        precedents: list[dict],
        already_answered: dict[str, dict],
    ) -> dict[str, Any]:
        pending = [f for f in fields if f["jaa_id"] not in already_answered]
        if not pending:
            return {"job": {"company": "", "role": "", "location": ""}, "actions": [], "blockers": []}

        user_block = "\n".join(
            [
                profile.as_prompt_block(),
                "",
                "### PREVIOUS ANSWERS THIS CANDIDATE HAS GIVEN",
                as_prompt_block(precedents),
                "",
                "### PAGE",
                json.dumps(
                    {
                        "url": page.get("url"),
                        "title": page.get("title"),
                        "heading": page.get("h1"),
                        "site": page.get("og_site"),
                        "visible_text_excerpt": (page.get("body_text") or "")[:3500],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                "",
                "### FIELDS TO ANSWER",
                json.dumps([_compact_field(f) for f in pending], indent=2, ensure_ascii=False),
                "",
                "Also extract the company, role title, and location for this posting "
                "from the page context. Return one action per field_id above.",
            ]
        )

        data = await self._call(user_block)
        valid_ids = {f["jaa_id"] for f in pending}
        data["actions"] = [a for a in data.get("actions", []) if a.get("field_id") in valid_ids]
        return data

    # ------------------------------------------------------------------ plumbing
    async def _call(self, user_block: str, _retry_without_fallbacks: bool = False) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(
            model=settings.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_block}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": settings.effort,
                "format": {"type": "json_schema", "schema": PLAN_SCHEMA},
            },
        )
        use_fallbacks = self._fallbacks_enabled and not _retry_without_fallbacks
        if use_fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"

        try:
            if use_fallbacks:
                async with self._client.beta.messages.stream(**kwargs) as stream:
                    message = await stream.get_final_message()
            else:
                async with self._client.messages.stream(**kwargs) as stream:
                    message = await stream.get_final_message()
        except anthropic.BadRequestError as exc:
            text = str(exc).lower()
            if use_fallbacks and ("fallback" in text or "beta" in text):
                # This account/SDK build doesn't have the fallback beta; carry on without it.
                self._fallbacks_enabled = False
                return await self._call(user_block, _retry_without_fallbacks=True)
            raise LLMError(f"Claude rejected the request: {exc}") from exc
        except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            raise LLMError(f"Claude API error: {exc}") from exc

        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"Claude declined this page (category: {category or 'unspecified'}).")

        text = next((b.text for b in message.content if b.type == "text"), "")
        if not text:
            raise LLMError("Claude returned no content for the fill plan.")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Fill plan was not valid JSON: {exc}") from exc


_planner: Planner | None = None
_planner_lock = asyncio.Lock()


async def get_planner() -> Planner:
    global _planner
    async with _planner_lock:
        if _planner is None:
            _planner = Planner()
    return _planner
