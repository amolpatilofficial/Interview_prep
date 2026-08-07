"""FastAPI backend + the UI it serves."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import db
from .events import hub, sse
from .extractor import normalize_question
from .profile import get_profile
from .runner import orchestrator, parse_urls
from .settings import VALID_MODES, settings

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="AI Job Application Agent", version="1.0.0")


# ------------------------------------------------------------------ schemas
class RunRequest(BaseModel):
    urls: str = Field(..., description="URLs separated by ||| or newlines")
    mode: str | None = None
    concurrency: int | None = None
    headless: bool | None = None


class DecisionRequest(BaseModel):
    approved: bool


class AnswerRequest(BaseModel):
    question: str
    answer: str


# --------------------------------------------------------------------- runs
@app.post("/api/runs")
async def create_run(req: RunRequest) -> dict[str, Any]:
    urls = parse_urls(req.urls)
    if not urls:
        raise HTTPException(400, "No usable URLs found. Separate multiple links with |||")

    profile = get_profile(refresh=True)
    if profile.error:
        raise HTTPException(400, profile.error)
    if not settings.api_key:
        raise HTTPException(400, "ANTHROPIC_API_KEY is not set — copy .env.example to .env.")

    mode = req.mode or settings.default_mode
    if mode not in VALID_MODES:
        raise HTTPException(400, f"mode must be one of {', '.join(VALID_MODES)}")
    try:
        return orchestrator.start_batch(urls, mode, req.concurrency, req.headless)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ------------------------------------------------------------- applications
@app.get("/api/applications")
async def list_applications(limit: int = 200, batch_id: str | None = None) -> dict[str, Any]:
    return {"applications": db.list_applications(limit=limit, batch_id=batch_id)}


@app.get("/api/applications/{app_id}")
async def get_application(app_id: str) -> dict[str, Any]:
    row = db.get_application(app_id)
    if not row:
        raise HTTPException(404, "Unknown application")
    return {"application": row, "fields": db.application_fields(app_id)}


@app.post("/api/applications/{app_id}/decision")
async def decide(app_id: str, req: DecisionRequest) -> dict[str, Any]:
    if not db.get_application(app_id):
        raise HTTPException(404, "Unknown application")
    if not orchestrator.decide(app_id, req.approved):
        raise HTTPException(409, "This application is not waiting for a review decision.")
    return {"ok": True, "approved": req.approved}


# --------------------------------------------------------------- answer bank
@app.get("/api/answers")
async def list_answers(limit: int = 500) -> dict[str, Any]:
    return {"answers": db.all_answers(limit=limit)}


@app.put("/api/answers")
async def upsert_answer(req: AnswerRequest) -> dict[str, Any]:
    key = normalize_question(req.question)
    if not key:
        raise HTTPException(400, "Question cannot be empty")
    db.upsert_answer(key, req.question.strip(), req.answer, pinned=True)
    return {"ok": True, "normalized_question": key}


@app.delete("/api/answers/{normalized}")
async def delete_answer(normalized: str) -> dict[str, Any]:
    db.delete_answer(normalized)
    return {"ok": True}


# --------------------------------------------------------------------- misc
@app.get("/api/stats")
async def stats() -> dict[str, Any]:
    return {
        "stats": db.stats(),
        "active_batches": orchestrator.active_batches,
        "awaiting_review": orchestrator.awaiting_review(),
    }


@app.get("/api/profile")
async def profile_summary(refresh: bool = False) -> dict[str, Any]:
    return {
        "profile": get_profile(refresh=refresh).summary(),
        "settings": {
            "model": settings.model,
            "effort": settings.effort,
            "default_mode": settings.default_mode,
            "concurrency": settings.concurrency,
            "headless": settings.headless,
            "min_confidence": settings.min_confidence,
            "api_key_present": bool(settings.api_key),
        },
    }


@app.get("/api/screenshots/{name}")
async def screenshot(name: str) -> FileResponse:
    safe = Path(name).name
    target = settings.screenshot_dir / safe
    if not target.exists():
        raise HTTPException(404, "No screenshot")
    return FileResponse(target, media_type="image/png")


@app.get("/api/events")
async def events() -> StreamingResponse:
    queue = hub.subscribe()

    async def stream():
        try:
            for past in hub.replay()[-100:]:
                yield sse(past)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                    yield sse(event)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/log")
async def recent_log(limit: int = 200) -> dict[str, Any]:
    return {"events": db.recent_events(limit)}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
