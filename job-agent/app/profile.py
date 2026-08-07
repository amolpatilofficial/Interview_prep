"""Loads the candidate profile: structured facts + resume text + uploadable docs."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .settings import settings


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception:
        return ""


def _read_docx(path: Path) -> str:
    try:
        import docx  # type: ignore
    except ImportError:  # pragma: no cover
        return ""
    try:
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs).strip()
    except Exception:
        return ""


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix in {".docx", ".doc"}:
        return _read_docx(path)
    if suffix in {".txt", ".md"}:
        try:
            return path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            return ""
    return ""


@dataclass
class Profile:
    raw: dict[str, Any] = field(default_factory=dict)
    resume_text: str = ""
    documents: dict[str, Path] = field(default_factory=dict)
    path: Path | None = None
    error: str | None = None

    # ------------------------------------------------------------------ views
    @property
    def identity(self) -> dict[str, Any]:
        return self.raw.get("identity", {}) or {}

    @property
    def display_name(self) -> str:
        ident = self.identity
        return (
            ident.get("full_name")
            or " ".join(x for x in (ident.get("first_name"), ident.get("last_name")) if x)
            or "(unnamed)"
        )

    def document_manifest(self) -> list[dict[str, str]]:
        """What the model is allowed to upload, described in words it can match on."""
        return [
            {
                "key": key,
                "filename": p.name,
                "path": str(p),
                "exists": p.exists(),
            }
            for key, p in self.documents.items()
        ]

    def standard_answers(self) -> dict[str, str]:
        raw = self.raw.get("standard_answers") or {}
        return {str(k): str(v) for k, v in raw.items() if v is not None}

    def as_prompt_block(self) -> str:
        """Everything the model needs about the human, as compact JSON + resume text."""
        facts = {k: v for k, v in self.raw.items() if k not in {"standard_answers"}}
        parts = [
            "### CANDIDATE FACTS (authoritative)",
            json.dumps(facts, indent=2, default=str, ensure_ascii=False),
        ]
        docs = self.document_manifest()
        if docs:
            parts += [
                "",
                "### UPLOADABLE DOCUMENTS",
                json.dumps(
                    [{"key": d["key"], "filename": d["filename"]} for d in docs if d["exists"]],
                    indent=2,
                ),
            ]
        std = self.standard_answers()
        if std:
            parts += [
                "",
                "### PRE-WRITTEN LONG ANSWERS (reuse verbatim when the question matches)",
                json.dumps(std, indent=2, ensure_ascii=False),
            ]
        if self.resume_text:
            trimmed = self.resume_text[:14000]
            parts += ["", "### RESUME TEXT", trimmed]
        return "\n".join(parts)

    def summary(self) -> dict[str, Any]:
        return {
            "loaded": self.error is None,
            "error": self.error,
            "path": str(self.path) if self.path else None,
            "name": self.display_name if self.error is None else None,
            "email": self.identity.get("email"),
            "resume_chars": len(self.resume_text),
            "documents": self.document_manifest(),
            "standard_answers": len(self.standard_answers()),
        }


def load_profile(path: Path | None = None) -> Profile:
    path = path or settings.profile_path
    if not path.exists():
        return Profile(
            path=path,
            error=(
                f"Profile not found at {path}. Copy config/profile.example.yaml to "
                f"{path.name} and fill it in."
            ),
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return Profile(path=path, error=f"Could not parse {path}: {exc}")
    if not isinstance(raw, dict):
        return Profile(path=path, error=f"{path} must be a YAML mapping.")

    # Resolve documents relative to the project root.
    documents: dict[str, Path] = {}
    for key, value in (raw.get("documents") or {}).items():
        if not value:
            continue
        p = Path(str(value))
        documents[str(key)] = p if p.is_absolute() else (settings.base_dir / p)

    # Resume text: explicit text file wins, else extract from the resume document.
    resume_text = ""
    text_file = raw.get("resume_text_file")
    if text_file:
        tp = Path(str(text_file))
        tp = tp if tp.is_absolute() else (settings.base_dir / tp)
        if tp.exists():
            resume_text = extract_text(tp)
    if not resume_text and "resume" in documents and documents["resume"].exists():
        resume_text = extract_text(documents["resume"])

    return Profile(raw=raw, resume_text=resume_text, documents=documents, path=path)


_cached: Profile | None = None


def get_profile(refresh: bool = False) -> Profile:
    global _cached
    if _cached is None or refresh:
        _cached = load_profile()
    return _cached
