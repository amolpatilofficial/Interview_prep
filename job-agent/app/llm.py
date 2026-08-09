"""The reasoning half of the agent.

Given a form schema, the candidate's profile, and what they answered on previous
applications, a model returns a structured fill plan — one action per field.

Two backends behind one interface:

* ``AnthropicPlanner``  — Claude, adaptive thinking, native structured outputs.
* ``OpenRouterPlanner`` — any OpenAI-compatible model on OpenRouter, including
  the free tier. Free models are smaller and looser, so this path asks for
  fewer fields per call, tolerates JSON wrapped in prose or code fences, and
  retries once with a blunter instruction before giving up.

Both validate every returned field id against the fields actually on the page,
so a hallucinated id can never reach the browser.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import anthropic
import httpx

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

# Appended for models that need reminding what "only JSON" means.
JSON_ONLY_SUFFIX = """

Reply with a single JSON object and nothing else. No markdown code fences, no
explanation before or after it. It must have exactly these top-level keys:
"job" (object with "company", "role", "location"), "actions" (array of objects
with "field_id", "action", "value", "confidence", "note"), and "blockers"
(array of strings).
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
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["job", "actions", "blockers"],
    "additionalProperties": False,
}

EMPTY_PLAN: dict[str, Any] = {
    "job": {"company": "", "role": "", "location": ""},
    "actions": [],
    "blockers": [],
}


class LLMError(RuntimeError):
    pass


# ----------------------------------------------------------------- prompt bits
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


def build_user_block(
    profile: Profile,
    page: dict[str, Any],
    fields: list[dict[str, Any]],
    precedents: list[dict],
) -> str:
    return "\n".join(
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
            json.dumps([_compact_field(f) for f in fields], indent=2, ensure_ascii=False),
            "",
            "Also extract the company, role title, and location for this posting "
            "from the page context. Return one action per field_id above.",
        ]
    )


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json(text: str) -> dict[str, Any]:
    """Parse a model's reply into a dict, tolerating the usual small-model mess.

    Handles: clean JSON, JSON in ``` fences, and JSON with prose either side.
    """
    if not text or not text.strip():
        raise LLMError("the model returned an empty response")

    candidate = _FENCE.sub("", text.strip())
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # Fall back to the outermost braced span.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(
                f"no JSON object found in the reply: {text[:200]!r}"
            ) from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"reply was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LLMError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _coerce_plan(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise a parsed reply so downstream code can trust its shape."""
    job = data.get("job")
    if not isinstance(job, dict):
        job = {}
    actions_raw = data.get("actions")
    actions: list[dict[str, Any]] = []
    if isinstance(actions_raw, list):
        for a in actions_raw:
            if not isinstance(a, dict) or not a.get("field_id"):
                continue
            try:
                confidence = float(a.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            actions.append(
                {
                    "field_id": str(a["field_id"]),
                    "action": str(a.get("action") or "skip"),
                    "value": "" if a.get("value") is None else str(a.get("value")),
                    "confidence": max(0.0, min(1.0, confidence)),
                    "note": str(a.get("note") or ""),
                }
            )
    blockers_raw = data.get("blockers")
    blockers = [str(b) for b in blockers_raw] if isinstance(blockers_raw, list) else []
    return {
        "job": {
            "company": str(job.get("company") or ""),
            "role": str(job.get("role") or ""),
            "location": str(job.get("location") or ""),
        },
        "actions": actions,
        "blockers": blockers,
    }


# --------------------------------------------------------------------- planners
class BasePlanner:
    """Batching, validation, and merging. Backends supply ``_complete``."""

    provider = "base"

    def describe(self) -> str:
        raise NotImplementedError

    async def _complete(self, user_block: str) -> dict[str, Any]:
        raise NotImplementedError

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
            return dict(EMPTY_PLAN, actions=[], blockers=[])

        size = settings.field_batch or len(pending)
        batches = [pending[i : i + size] for i in range(0, len(pending), size)]

        merged = {"job": {"company": "", "role": "", "location": ""},
                  "actions": [], "blockers": []}
        seen_ids: set[str] = set()

        for batch in batches:
            valid_ids = {f["jaa_id"] for f in batch}
            data = await self._complete(build_user_block(profile, page, batch, precedents))
            plan = _coerce_plan(data)

            for key in ("company", "role", "location"):
                if not merged["job"][key] and plan["job"][key]:
                    merged["job"][key] = plan["job"][key]
            for blocker in plan["blockers"]:
                if blocker not in merged["blockers"]:
                    merged["blockers"].append(blocker)
            for action in plan["actions"]:
                fid = action["field_id"]
                # A field id the page never had, or a duplicate, never reaches the browser.
                if fid not in valid_ids or fid in seen_ids:
                    continue
                seen_ids.add(fid)
                merged["actions"].append(action)

        return merged


class AnthropicPlanner(BasePlanner):
    provider = "anthropic"

    def __init__(self) -> None:
        if not settings.api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Either add it to .env, or switch to "
                "OpenRouter with JAA_PROVIDER=openrouter and OPENROUTER_API_KEY."
            )
        self._client = anthropic.AsyncAnthropic(api_key=settings.api_key)
        self._fallbacks_enabled = settings.server_fallbacks

    def describe(self) -> str:
        return f"Anthropic · {settings.model} · effort {settings.effort}"

    async def _complete(self, user_block: str, _retry_without_fallbacks: bool = False) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(
            model=settings.model,
            max_tokens=settings.max_output_tokens,
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
                return await self._complete(user_block, _retry_without_fallbacks=True)
            raise LLMError(f"Claude rejected the request: {exc}") from exc
        except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            raise LLMError(f"Claude API error: {exc}") from exc

        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"Claude declined this page (category: {category or 'unspecified'}).")

        text = next((b.text for b in message.content if b.type == "text"), "")
        return extract_json(text)


class OpenRouterPlanner(BasePlanner):
    """OpenAI-compatible chat completions, aimed at OpenRouter's free tier."""

    provider = "openrouter"

    def __init__(self) -> None:
        if not settings.openrouter_api_key:
            raise LLMError(
                "OPENROUTER_API_KEY is not set. Create a key at "
                "https://openrouter.ai/keys and put it in .env."
            )
        self._client = httpx.AsyncClient(
            base_url=settings.openrouter_base_url,
            timeout=httpx.Timeout(settings.request_timeout_s, connect=20.0),
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "X-Title": settings.openrouter_app_title,
                "Content-Type": "application/json",
            },
        )

    def describe(self) -> str:
        return f"OpenRouter · {settings.openrouter_model}"

    def _body(self, user_block: str, strict_schema: bool) -> dict[str, Any]:
        system = SYSTEM_PROMPT if strict_schema else SYSTEM_PROMPT + JSON_ONLY_SUFFIX
        body: dict[str, Any] = {
            "model": settings.openrouter_model,
            "max_tokens": settings.max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_block},
            ],
        }
        if strict_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "fill_plan", "strict": True, "schema": PLAN_SCHEMA},
            }
        else:
            # Second attempt: drop the schema (some free models reject or ignore
            # it) and ask for a bare JSON object instead.
            body["response_format"] = {"type": "json_object"}
        return body

    async def _post(self, body: dict[str, Any]) -> str:
        try:
            response = await self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach OpenRouter: {exc}") from exc

        if response.status_code == 429:
            raise LLMError(
                "OpenRouter rate limit hit. The free tier allows 20 requests/minute "
                "and 50/day (1000/day once you have bought $10 of credits). Lower "
                "JAA_CONCURRENCY, raise JAA_FIELD_BATCH so each page needs fewer "
                "calls, or wait."
            )
        if response.status_code == 402:
            raise LLMError(
                f"OpenRouter says this request needs credits — {settings.openrouter_model} "
                "may no longer be free. Pick another from "
                "https://openrouter.ai/models?max_price=0 via JAA_OPENROUTER_MODEL."
            )
        if response.status_code >= 400:
            detail = response.text[:400]
            raise LLMError(f"OpenRouter returned {response.status_code}: {detail}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMError(f"OpenRouter returned non-JSON: {response.text[:200]!r}") from exc

        # OpenRouter reports upstream provider failures in a 200 body.
        if isinstance(payload.get("error"), dict):
            message = payload["error"].get("message", "unknown error")
            raise LLMError(f"OpenRouter upstream error: {message}")

        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"OpenRouter returned no choices: {json.dumps(payload)[:300]}")

        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise LLMError(
                "The model hit its output limit mid-answer. Lower JAA_FIELD_BATCH "
                "so it has fewer fields to answer per call."
            )
        return (choice.get("message") or {}).get("content") or ""

    # Conditions a second attempt cannot fix — surface them as-is instead of
    # burying them in a "did not return a usable plan" wrapper.
    _NO_RETRY = ("rate limit", "credits", "output limit", "could not reach openrouter")

    async def _complete(self, user_block: str) -> dict[str, Any]:
        try:
            return extract_json(await self._post(self._body(user_block, strict_schema=True)))
        except LLMError as first:
            if any(marker in str(first).lower() for marker in self._NO_RETRY):
                raise
            # Free models routinely ignore or choke on json_schema. Ask again
            # without it, spelling out the shape in the prompt instead.
            try:
                return extract_json(await self._post(self._body(user_block, strict_schema=False)))
            except LLMError as second:
                raise LLMError(
                    f"{settings.openrouter_model} did not return a usable fill plan. "
                    f"First attempt: {first}. Retry: {second}. Try a different model "
                    f"via JAA_OPENROUTER_MODEL."
                ) from second

    async def aclose(self) -> None:
        await self._client.aclose()


# Kept so existing imports and the Anthropic contract test keep working.
Planner = AnthropicPlanner

_planner: BasePlanner | None = None
_planner_lock = asyncio.Lock()


def credential_error() -> str | None:
    """Human-readable reason the configured provider can't be used yet."""
    if settings.provider == "openrouter":
        if not settings.openrouter_api_key:
            return (
                "OPENROUTER_API_KEY is not set. Create a free key at "
                "https://openrouter.ai/keys and add it to .env."
            )
        return None
    if not settings.api_key:
        return (
            "ANTHROPIC_API_KEY is not set. Add it to .env, or use a free model by "
            "setting JAA_PROVIDER=openrouter and OPENROUTER_API_KEY instead."
        )
    return None


def describe_provider() -> str:
    if settings.provider == "openrouter":
        return f"OpenRouter · {settings.openrouter_model}"
    return f"Anthropic · {settings.model} · effort {settings.effort}"


def build_planner() -> BasePlanner:
    if settings.provider == "openrouter":
        return OpenRouterPlanner()
    return AnthropicPlanner()


async def get_planner() -> BasePlanner:
    global _planner
    async with _planner_lock:
        if _planner is None:
            _planner = build_planner()
    return _planner


def reset_planner() -> None:
    """Drop the cached planner so a settings change takes effect."""
    global _planner
    _planner = None
