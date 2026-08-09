#!/usr/bin/env python3
"""Checks the Claude request we build and the response we parse, without a key.

A mock HTTP transport stands in for api.anthropic.com: it asserts the outgoing
request body, then replays a real SSE stream back. This catches parameter drift
in the Anthropic SDK that a type checker would not.

    python tests/test_planner_contract.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402
import httpx  # noqa: E402

from app.llm import PLAN_SCHEMA, AnthropicPlanner, LLMError  # noqa: E402
from app.profile import Profile  # noqa: E402
from app.settings import settings  # noqa: E402

failures: list[str] = []
captured: dict = {}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


PLAN_JSON = {
    "job": {"company": "Acme Corp", "role": "Senior Data Engineer", "location": "Pune"},
    "actions": [
        {"field_id": "jaa-1", "action": "fill", "value": "Amol", "confidence": 0.98, "note": ""},
        {"field_id": "jaa-9", "action": "upload", "value": "resume", "confidence": 0.95, "note": ""},
        {"field_id": "nope", "action": "fill", "value": "x", "confidence": 0.9, "note": "hallucinated id"},
    ],
    "blockers": ["No US work authorization on file."],
}


def sse_body(payload: dict, stop_reason: str = "end_turn") -> bytes:
    text = json.dumps(payload)
    frames = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_test", "type": "message", "role": "assistant", "model": settings.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 50}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in frames).encode()


def make_transport(stop_reason: str = "end_turn") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, content=sse_body(PLAN_JSON, stop_reason),
            headers={"content-type": "text/event-stream"},
        )
    return httpx.MockTransport(handler)


FIELDS = [
    {"jaa_id": "jaa-1", "question": "First Name", "normalized_question": "first name",
     "type": "text", "required": True, "options": [], "frame": 0},
    {"jaa_id": "jaa-9", "question": "Resume", "normalized_question": "resume",
     "type": "file", "required": True, "options": [], "accept": ".pdf", "frame": 0},
    {"jaa_id": "jaa-4", "question": "Notice period", "normalized_question": "notice period",
     "type": "select", "required": False, "frame": 0,
     "options": [{"label": "30 days", "value": "30"}, {"label": "60 days", "value": "60"}]},
]


async def main() -> int:
    profile = Profile(
        raw={"identity": {"full_name": "Amol Patil", "email": "a@example.com"}},
        resume_text="Senior data engineer, 8 years.",
        documents={"resume": Path("data/documents/resume.pdf")},
    )

    planner = AnthropicPlanner.__new__(AnthropicPlanner)  # skip the API-key requirement
    planner._client = anthropic.AsyncAnthropic(
        api_key="test-key", http_client=httpx.AsyncClient(transport=make_transport())
    )
    planner._fallbacks_enabled = True

    print("\n[request we send]")
    plan = await planner.plan(
        profile=profile,
        page={"url": "https://example.com/apply", "title": "Apply", "h1": "Apply",
              "og_site": "Acme", "body_text": "Senior Data Engineer at Acme"},
        fields=FIELDS,
        precedents=[{"similar_question": "Years of Python", "previous_answer": "8"}],
        already_answered={"jaa-4": {"answer": "30 days", "question": "Notice", "pinned": False}},
    )
    body = captured["body"]
    check("hits the beta messages endpoint", captured["url"].endswith("/v1/messages?beta=true"),
          captured["url"])
    check("model is the configured one", body["model"] == settings.model, body["model"])
    check("streaming enabled", body.get("stream") is True)
    check("adaptive thinking requested", body["thinking"] == {"type": "adaptive"},
          json.dumps(body.get("thinking")))
    check("effort set inside output_config", body["output_config"]["effort"] == settings.effort)
    check("structured output schema attached",
          body["output_config"]["format"]["schema"] == PLAN_SCHEMA)
    check("no sampling params (400 on opus-5)",
          not any(k in body for k in ("temperature", "top_p", "top_k")))
    check("no budget_tokens (400 on opus-5)", "budget_tokens" not in json.dumps(body.get("thinking")))
    check("server-side fallbacks opted in", body.get("fallbacks") == "default", str(body.get("fallbacks")))
    check("fallback beta header sent",
          "server-side-fallback-2026-07-01" in captured["headers"].get("anthropic-beta", ""),
          captured["headers"].get("anthropic-beta", ""))
    check("no assistant prefill in messages",
          all(m["role"] != "assistant" for m in body["messages"]))
    check("already-answered field excluded from the prompt",
          "jaa-4" not in json.dumps(body["messages"]))
    check("pending fields included", "jaa-1" in json.dumps(body["messages"])
          and "jaa-9" in json.dumps(body["messages"]))
    check("profile facts reach the model", "Amol Patil" in json.dumps(body["messages"]))
    check("prior answers reach the model", "Years of Python" in json.dumps(body["messages"]))

    print("\n[response we parse]")
    check("job metadata parsed", plan["job"]["company"] == "Acme Corp")
    check("blockers parsed", plan["blockers"] == ["No US work authorization on file."])
    ids = {a["field_id"] for a in plan["actions"]}
    check("hallucinated field id dropped", "nope" not in ids, str(ids))
    check("real actions kept", ids == {"jaa-1", "jaa-9"}, str(ids))

    print("\n[refusal handling]")
    planner._client = anthropic.AsyncAnthropic(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=make_transport(stop_reason="refusal")),
    )
    try:
        await planner.plan(profile=profile, page={}, fields=FIELDS,
                           precedents=[], already_answered={})
        check("refusal raises instead of returning junk", False, "no exception")
    except LLMError as exc:
        check("refusal raises instead of returning junk", "declined" in str(exc).lower(), str(exc))

    print("\n[fallback downgrade path]")
    calls = {"n": 0}

    def picky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        if "fallbacks" in body:
            return httpx.Response(400, json={"type": "error", "error": {
                "type": "invalid_request_error", "message": "fallbacks: unsupported beta"}})
        captured["body"] = body
        return httpx.Response(200, content=sse_body(PLAN_JSON),
                              headers={"content-type": "text/event-stream"})

    planner._client = anthropic.AsyncAnthropic(
        api_key="test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(picky)),
        max_retries=0,
    )
    planner._fallbacks_enabled = True
    plan2 = await planner.plan(profile=profile, page={}, fields=FIELDS,
                               precedents=[], already_answered={})
    check("retries without fallbacks when the beta is rejected", plan2["job"]["company"] == "Acme Corp")
    check("fallbacks dropped on the retry", "fallbacks" not in captured["body"])
    check("stays off for later calls", planner._fallbacks_enabled is False)

    print(f"\n{'ALL CHECKS PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
