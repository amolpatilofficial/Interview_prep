"""Applicant-tracking-system detection plus the per-vendor quirks that matter."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class AtsProfile:
    key: str
    name: str
    # Extra settle time after navigation — SPA-heavy vendors need it.
    settle_ms: int = 1200
    # Some vendors mount the form only after an "Apply" click.
    needs_apply_click: bool = False
    notes: str = ""


PROFILES: dict[str, AtsProfile] = {
    "greenhouse": AtsProfile("greenhouse", "Greenhouse", 1500, False,
                             "Form often lives in an embedded iframe (#grnhse_iframe)."),
    "lever": AtsProfile("lever", "Lever", 1200, True,
                        "Job page links to /apply; react-select dropdowns."),
    "ashby": AtsProfile("ashby", "Ashby", 1800, True,
                        "Heavy SPA; fields render after hydration."),
    "workday": AtsProfile("workday", "Workday", 3000, True,
                          "Multi-step wizard behind a login wall; custom widgets."),
    "smartrecruiters": AtsProfile("smartrecruiters", "SmartRecruiters", 1500, True),
    "workable": AtsProfile("workable", "Workable", 1500, True),
    "icims": AtsProfile("icims", "iCIMS", 2500, True, "Nested iframes."),
    "taleo": AtsProfile("taleo", "Oracle Taleo", 2500, True),
    "successfactors": AtsProfile("successfactors", "SAP SuccessFactors", 2500, True),
    "bamboohr": AtsProfile("bamboohr", "BambooHR", 1200, False),
    "jobvite": AtsProfile("jobvite", "Jobvite", 1500, True),
    "recruitee": AtsProfile("recruitee", "Recruitee", 1200, True),
    "teamtailor": AtsProfile("teamtailor", "Teamtailor", 1200, True),
    "personio": AtsProfile("personio", "Personio", 1200, True),
    "linkedin": AtsProfile("linkedin", "LinkedIn", 2500, True,
                           "Requires a logged-in storage state; Easy Apply is a modal wizard."),
    "indeed": AtsProfile("indeed", "Indeed", 2500, True,
                         "Requires a logged-in storage state."),
    "naukri": AtsProfile("naukri", "Naukri", 2000, True),
    "generic": AtsProfile("generic", "Generic / custom careers page", 1200, False),
}

_HOST_HINTS: list[tuple[str, str]] = [
    ("greenhouse.io", "greenhouse"),
    ("grnh.se", "greenhouse"),
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("myworkdayjobs.com", "workday"),
    ("myworkday.com", "workday"),
    ("wd1.myworkdaysite.com", "workday"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("workable.com", "workable"),
    ("icims.com", "icims"),
    ("taleo.net", "taleo"),
    ("successfactors.com", "successfactors"),
    ("bamboohr.com", "bamboohr"),
    ("jobvite.com", "jobvite"),
    ("recruitee.com", "recruitee"),
    ("teamtailor.com", "teamtailor"),
    ("personio.de", "personio"),
    ("jobs.personio.com", "personio"),
    ("linkedin.com", "linkedin"),
    ("indeed.com", "indeed"),
    ("naukri.com", "naukri"),
]

_BODY_HINTS: list[tuple[str, str]] = [
    ("grnhse", "greenhouse"),
    ("greenhouse.io", "greenhouse"),
    ("lever-jobs", "lever"),
    ("jobs.lever.co", "lever"),
    ("_ashby", "ashby"),
    ("ashbyhq", "ashby"),
    ("workday", "workday"),
    ("smartrecruiters", "smartrecruiters"),
    ("workable", "workable"),
    ("icims", "icims"),
]


def detect(url: str, html: str = "") -> AtsProfile:
    host = (urlparse(url).hostname or "").lower()
    for needle, key in _HOST_HINTS:
        if needle in host:
            return PROFILES[key]
    lowered = (html or "").lower()[:200_000]
    for needle, key in _BODY_HINTS:
        if needle in lowered:
            return PROFILES[key]
    return PROFILES["generic"]


def company_from_url(url: str) -> str:
    """Best-effort company guess when the page gives us nothing better."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [p for p in parsed.path.split("/") if p]

    # boards.greenhouse.io/<company>/jobs/123, jobs.lever.co/<company>/<id>
    if any(h in host for h in ("greenhouse.io", "lever.co", "ashbyhq.com",
                               "recruitee.com", "teamtailor.com")) and parts:
        return parts[0].replace("-", " ").title()
    # <company>.myworkdayjobs.com, <company>.applytojob.com
    labels = host.split(".")
    if len(labels) > 2 and labels[0] not in {"jobs", "boards", "careers", "apply", "job"}:
        return labels[0].replace("-", " ").title()
    if labels:
        return labels[0].replace("-", " ").title()
    return ""
