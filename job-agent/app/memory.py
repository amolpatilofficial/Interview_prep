"""The answer bank: what this human answered last time, reused this time.

Two lookup paths:
  * exact  — normalized question matches a stored key, reused verbatim, no LLM cost.
  * fuzzy  — token-overlap neighbours are handed to the model as precedent so it
             stays consistent across companies ("5 years", not "5" then "five").
"""
from __future__ import annotations

import json
from typing import Any

from .db import db
from .extractor import similarity

# Never reuse a stored answer for these — they are per-company by nature.
PER_COMPANY = (
    "why", "cover letter", "interest", "excites", "motivat", "tell us",
    "what do you know", "salary expectation", "how did you hear",
)

FUZZY_THRESHOLD = 0.55
MAX_PRECEDENTS = 25


def is_reusable_verbatim(question: str, field_type: str) -> bool:
    if field_type in {"file", "richtext"}:
        return False
    q = question.lower()
    return not any(marker in q for marker in PER_COMPANY)


def resolve(fields: list[dict[str, Any]]) -> tuple[dict[str, dict], list[dict]]:
    """Returns (exact_hits_by_field_id, precedents_for_the_prompt)."""
    normalized = [f.get("normalized_question", "") for f in fields]
    exact_rows = db.lookup_answers([n for n in normalized if n])

    exact: dict[str, dict] = {}
    for f in fields:
        key = f.get("normalized_question", "")
        row = exact_rows.get(key)
        if not row:
            continue
        if not is_reusable_verbatim(f.get("question", ""), f.get("type", "")):
            continue
        # A stored free-text answer must still be a legal option for choice fields.
        if f.get("type") in {"select", "radio", "checkbox_group", "multiselect"}:
            labels = {str(o.get("label", "")).strip().lower() for o in f.get("options", [])}
            values = {str(o.get("value", "")).strip().lower() for o in f.get("options", [])}
            if str(row["answer"]).strip().lower() not in (labels | values):
                continue
        exact[f["jaa_id"]] = {
            "answer": row["answer"],
            "question": row["question"],
            "pinned": bool(row.get("pinned")),
        }

    # Fuzzy precedents: everything remembered that resembles a field on this page.
    precedents: list[dict] = []
    bank = db.all_answers(limit=800)
    for f in fields:
        if f["jaa_id"] in exact:
            continue
        q = f.get("question", "")
        best = None
        best_score = 0.0
        for row in bank:
            score = similarity(q, row["question"])
            if score > best_score:
                best_score, best = score, row
        if best and best_score >= FUZZY_THRESHOLD:
            precedents.append(
                {
                    "similar_question": best["question"],
                    "previous_answer": best["answer"],
                    "for_field": f["jaa_id"],
                    "similarity": round(best_score, 2),
                }
            )
    # Plus the most-used answers overall, as general context.
    for row in bank[:15]:
        precedents.append(
            {"similar_question": row["question"], "previous_answer": row["answer"],
             "for_field": None, "similarity": None}
        )

    # Dedupe, keep the strongest.
    seen: set[str] = set()
    deduped: list[dict] = []
    for p in sorted(precedents, key=lambda x: -(x["similarity"] or 0)):
        k = p["similar_question"]
        if k in seen:
            continue
        seen.add(k)
        deduped.append(p)
    return exact, deduped[:MAX_PRECEDENTS]


def as_prompt_block(precedents: list[dict]) -> str:
    if not precedents:
        return "(no prior answers recorded yet)"
    return json.dumps(
        [
            {"question": p["similar_question"], "answer_given_before": p["previous_answer"]}
            for p in precedents
        ],
        indent=2,
        ensure_ascii=False,
    )


def learn(records: list[dict]) -> int:
    """Persist successfully filled answers so the next application is cheaper."""
    saved = 0
    for r in records:
        if not r.get("filled"):
            continue
        if r.get("action") not in {"fill", "select", "check"}:
            continue
        answer = r.get("answer")
        if answer in (None, ""):
            continue
        if not is_reusable_verbatim(r.get("question", ""), r.get("field_type", "")):
            continue
        if float(r.get("confidence") or 0) < 0.5:
            continue
        db.remember(
            r.get("normalized_question", ""),
            r.get("question", ""),
            r.get("field_type", "text"),
            str(answer),
        )
        saved += 1
    return saved
