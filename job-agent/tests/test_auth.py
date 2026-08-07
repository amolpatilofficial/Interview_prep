#!/usr/bin/env python3
"""Auth middleware: nothing reachable without the password, once one is set.

    python tests/test_auth.py
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth as auth_mod  # noqa: E402
from app.main import app  # noqa: E402
from app.settings import settings  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


PROTECTED = ["/", "/api/stats", "/api/applications", "/api/profile", "/api/answers", "/api/log"]


def main() -> int:
    client = TestClient(app)
    # TestClient's client host is "testclient", which app.auth treats as loopback.
    remote = {"X-Forwarded-For": "203.0.113.9"}

    print("\n[no password configured]")
    object.__setattr__(settings, "auth_password", None)
    check("loopback still works for local dev", client.get("/api/stats").status_code == 200)

    # Simulate a genuinely remote peer by taking 'testclient' out of the loopback set.
    original = set(auth_mod.LOOPBACK)
    auth_mod.LOOPBACK.discard("testclient")
    r = client.get("/api/stats", headers=remote)
    check("remote request refused outright", r.status_code == 503, str(r.status_code))
    check("refusal explains the fix", "JAA_AUTH_PASSWORD" in r.text)
    check("health probe stays open", client.get("/healthz").status_code == 200)

    print("\n[password configured]")
    object.__setattr__(settings, "auth_password", "s3cret-pw")
    object.__setattr__(settings, "auth_user", "amol")

    for path in PROTECTED:
        r = client.get(path)
        if r.status_code != 401:
            check(f"{path} requires auth", False, str(r.status_code))
            break
    else:
        check("every protected route returns 401 unauthenticated", True)

    r = client.get("/api/stats")
    check("401 sends a Basic challenge", "Basic" in r.headers.get("www-authenticate", ""),
          r.headers.get("www-authenticate", ""))

    check("wrong password rejected",
          client.get("/api/stats", headers=basic("amol", "wrong")).status_code == 401)
    check("wrong username rejected",
          client.get("/api/stats", headers=basic("nope", "s3cret-pw")).status_code == 401)
    check("malformed header rejected",
          client.get("/api/stats", headers={"Authorization": "Basic !!!not-b64"}).status_code == 401)
    check("bearer token rejected",
          client.get("/api/stats", headers={"Authorization": "Bearer s3cret-pw"}).status_code == 401)

    ok = basic("amol", "s3cret-pw")
    check("correct credentials pass", client.get("/api/stats", headers=ok).status_code == 200)
    check("UI is served when authenticated", client.get("/", headers=ok).status_code == 200)
    check("run endpoint is protected too",
          client.post("/api/runs", json={"urls": "https://x/apply"}).status_code == 401)
    check("health probe still open with auth on", client.get("/healthz").status_code == 200)

    auth_mod.LOOPBACK.clear()
    auth_mod.LOOPBACK.update(original)
    object.__setattr__(settings, "auth_password", None)

    print(f"\n{'ALL CHECKS PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
