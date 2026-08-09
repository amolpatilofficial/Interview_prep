#!/usr/bin/env python3
"""OpenRouter backend: request shape, and the mess free models return.

A mock transport stands in for openrouter.ai. Covers the failure modes small
models actually exhibit — JSON in code fences, prose around the JSON, a
schema-rejecting 400, truncation, rate limits — plus batching, which is what
keeps a weak model accurate.

    python tests/test_openrouter.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.llm import (  # noqa: E402
    PLAN_SCHEMA,
    LLMError,
    OpenRouterPlanner,
    extract_json,
)
from app.profile import Profile  # noqa: E402
from app.settings import settings  # noqa: E402

failures: list[str] = []
seen: list[dict] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


PLAN = {
    "job": {"company": "Acme Corp", "role": "Senior Data Engineer", "location": "Pune"},
    "actions": [
        {"field_id": "jaa-1", "action": "fill", "value": "Amol", "confidence": 0.95, "note": ""},
        {"field_id": "ghost", "action": "fill", "value": "x", "confidence": 0.9, "note": "made up"},
    ],
    "blockers": [],
}

FIELDS = [
    {"jaa_id": f"jaa-{i}", "question": f"Question {i}", "normalized_question": f"question {i}",
     "type": "text", "required": False, "options": [], "frame": 0}
    for i in range(1, 6)
]
PROFILE = Profile(raw={"identity": {"full_name": "Amol Patil"}}, resume_text="8 years.")


def reply(content: str, finish: str = "stop", status: int = 200) -> httpx.Response:
    if status != 200:
        return httpx.Response(status, json={"error": {"message": "nope"}})
    return httpx.Response(200, json={
        "id": "gen-1", "model": settings.openrouter_model,
        "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}],
    })


def planner_with(handler) -> OpenRouterPlanner:
    p = OpenRouterPlanner.__new__(OpenRouterPlanner)
    p._client = httpx.AsyncClient(
        base_url=settings.openrouter_base_url,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-key", "X-Title": "test"},
    )
    return p


async def run(planner, fields=None) -> dict:
    return await planner.plan(
        profile=PROFILE,
        page={"url": "https://example.com/apply", "title": "Apply", "body_text": "Acme"},
        fields=fields if fields is not None else FIELDS,
        precedents=[],
        already_answered={},
    )


async def main() -> int:
    object.__setattr__(settings, "field_batch", 0)

    # ------------------------------------------------------------ request shape
    print("\n[request shape]")

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url),
                     "headers": dict(request.headers),
                     "body": json.loads(request.content)})
        return reply(json.dumps(PLAN))

    plan = await run(planner_with(capture))
    body = seen[0]["body"]
    check("posts to /chat/completions", seen[0]["url"].endswith("/chat/completions"), seen[0]["url"])
    check("bearer auth sent", seen[0]["headers"].get("authorization") == "Bearer test-key")
    check("model is the configured free one", body["model"] == settings.openrouter_model,
          body["model"])
    check("system + user messages", [m["role"] for m in body["messages"]] == ["system", "user"])
    check("strict json_schema requested",
          body["response_format"]["json_schema"]["strict"] is True
          and body["response_format"]["json_schema"]["schema"] == PLAN_SCHEMA)
    check("max_tokens bounded", isinstance(body.get("max_tokens"), int) and body["max_tokens"] > 0)
    check("hallucinated field id dropped",
          {a["field_id"] for a in plan["actions"]} == {"jaa-1"},
          str([a["field_id"] for a in plan["actions"]]))
    check("job metadata parsed", plan["job"]["company"] == "Acme Corp")

    # ------------------------------------------------- the mess models return
    print("\n[tolerating small-model output]")
    fenced = "```json\n" + json.dumps(PLAN) + "\n```"
    check("fenced JSON parsed", (await run(planner_with(lambda r: reply(fenced))))["job"]["company"] == "Acme Corp")

    chatty = f"Sure! Here is the fill plan you asked for:\n\n{json.dumps(PLAN)}\n\nLet me know if you need changes."
    check("JSON surrounded by prose parsed",
          (await run(planner_with(lambda r: reply(chatty))))["job"]["company"] == "Acme Corp")

    check("bare object survives extract_json", extract_json('{"a": 1}') == {"a": 1})
    try:
        extract_json("I'm sorry, I can't help with that.")
        check("prose-only reply raises", False, "no exception")
    except LLMError as exc:
        check("prose-only reply raises", "no JSON object" in str(exc))

    # ------------------------------------------------------ schema-less retry
    print("\n[retry when the model rejects json_schema]")
    attempts: list[dict] = []

    def picky(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        attempts.append(body)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
        return reply(json.dumps(PLAN))

    plan = await run(planner_with(picky))
    check("falls back to json_object mode", len(attempts) == 2
          and attempts[1]["response_format"]["type"] == "json_object", str(len(attempts)))
    check("second attempt spells out the shape in the prompt",
          "single JSON object" in attempts[1]["messages"][0]["content"])
    check("plan still recovered", plan["job"]["company"] == "Acme Corp")

    # --------------------------------------------------------- error surfaces
    print("\n[error messages a user can act on]")
    try:
        await run(planner_with(lambda r: reply("", status=429)))
        check("rate limit explained", False, "no exception")
    except LLMError as exc:
        check("rate limit explained", "50/day" in str(exc) and "JAA_CONCURRENCY" in str(exc))

    try:
        await run(planner_with(lambda r: reply("", status=402)))
        check("payment-required explained", False, "no exception")
    except LLMError as exc:
        check("payment-required explained", "no longer be free" in str(exc))

    try:
        await run(planner_with(lambda r: reply('{"job":', finish="length")))
        check("truncation explained", False, "no exception")
    except LLMError as exc:
        check("truncation explained without a retry wrapper",
              "JAA_FIELD_BATCH" in str(exc) and "did not return" not in str(exc), str(exc)[:100])

    def upstream_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "provider offline", "code": 503}})

    try:
        await run(planner_with(upstream_error))
        check("200-with-error body raises", False, "no exception")
    except LLMError as exc:
        check("200-with-error body raises", "provider offline" in str(exc))

    # --------------------------------------------------------------- batching
    print("\n[batching for small models]")
    object.__setattr__(settings, "field_batch", 2)
    calls: list[list[str]] = []

    def batched(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked = [f["field_id"] for f in json.loads(
            body["messages"][1]["content"].split("### FIELDS TO ANSWER\n")[1].split("\n\nAlso extract")[0])]
        calls.append(asked)
        return reply(json.dumps({
            "job": {"company": "Acme Corp", "role": "SDE", "location": "Pune"},
            "actions": [{"field_id": fid, "action": "fill", "value": "v",
                         "confidence": 0.8, "note": ""} for fid in asked],
            "blockers": [f"blocker from {asked[0]}"],
        }))

    plan = await run(planner_with(batched))
    check("5 fields split into 3 calls of <=2", len(calls) == 3 and all(len(c) <= 2 for c in calls),
          str([len(c) for c in calls]))
    check("no field asked twice", sorted(sum(calls, [])) == sorted(f["jaa_id"] for f in FIELDS))
    check("actions merged across batches", len(plan["actions"]) == 5, str(len(plan["actions"])))
    check("blockers merged and deduped", len(plan["blockers"]) == 3, str(plan["blockers"]))
    check("job metadata taken from the first batch that had it",
          plan["job"]["company"] == "Acme Corp")

    object.__setattr__(settings, "field_batch", 0)
    print(f"\n{'ALL CHECKS PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
