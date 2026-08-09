# AI Job Application Agent

A real agent — not a browser extension. It drives a headless-or-visible Chromium,
reads whatever form is on the page, works out what each question is actually
asking, answers it from your resume and your previous answers, uploads your
documents, and submits.

```
   URLs (||| separated)
        ↓
   Playwright opens each page  →  clicks "Apply" if needed
        ↓
   DOM extractor  →  every input/select/radio/checkbox/file, with its real label
        ↓
   Answer bank (DuckDB)  →  "you answered this exact question before"
        ↓
   Claude, or any OpenRouter model  →  one action per field + a confidence score
        ↓
   Playwright fills + uploads  →  screenshot
        ↓
   dry_run: stop  |  review: wait for your click  |  auto: submit
        ↓
   DuckDB: company, role, every field submitted, status
```

---

## Step 1 — Install

You need **Python 3.11+**. From this directory:

```bash
cd job-agent

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium         # downloads the browser (~150 MB, one time)
```

On Linux you may also need the system libraries Chromium links against:

```bash
playwright install-deps chromium    # or: sudo apt install libnss3 libnspr4 libasound2t64
```

## Step 2 — Configure

```bash
cp .env.example .env
```

Open `.env` and set **one API key** — either provider works:

**Anthropic** (best quality, paid — <https://console.anthropic.com/settings/keys>):

```ini
ANTHROPIC_API_KEY=sk-ant-...
```

**OpenRouter** (has a free tier — <https://openrouter.ai/keys>):

```ini
OPENROUTER_API_KEY=sk-or-v1-...
JAA_OPENROUTER_MODEL=nvidia/nemotron-3-super-120b-a12b:free
```

The provider is inferred from whichever key you set (Anthropic wins if you set
both; force it with `JAA_PROVIDER=openrouter`). See
[Using a free model](#using-a-free-model-openrouter) for what changes.

Everything else has a sane default. The ones you'll actually touch:

| Variable | Default | What it does |
|---|---|---|
| `JAA_HEADLESS` | `false` | `false` shows you the browser filling the form. Keep it visible until you trust it. |
| `JAA_DEFAULT_MODE` | `review` | `dry_run` \| `review` \| `auto` — see Step 6. |
| `JAA_CONCURRENCY` | `2` | How many applications run at once. |
| `JAA_MIN_CONFIDENCE` | `0.35` | Below this, the agent leaves the field blank rather than guessing. |
| `JAA_EFFORT` | `high` | Claude reasoning effort: `low`…`max`. `medium` is cheaper and usually fine. Anthropic only. |
| `JAA_FIELD_BATCH` | `0` / `18` | Fields per model call. `0` = all at once (Anthropic); OpenRouter defaults to 18. |

## Step 3 — Your profile

```bash
cp config/profile.example.yaml config/profile.yaml
```

Edit `config/profile.yaml`. This is the agent's only source of truth about you —
it is instructed never to invent a fact that isn't in here, in your resume, or in
your answer bank. Fill in at minimum: `identity`, `work_authorization`,
`preferences`, and `demographics`.

## Step 4 — Your documents

Drop your files in `data/documents/` and point at them from `profile.yaml`:

```
data/documents/resume.pdf
data/documents/cover_letter.pdf
```

```yaml
documents:
  resume: data/documents/resume.pdf
  cover_letter: data/documents/cover_letter.pdf
```

The resume text is extracted automatically from the PDF and given to the model
so it can answer free-text questions in your own words. If your PDF extracts
badly (scanned, heavy columns), paste plain text into a `.txt` and set
`resume_text_file:` to it.

Check it worked — the header line in the UI shows the extracted character count.

## Step 5 — Start it

```bash
uvicorn app.main:app --reload --port 8000
```

Open **<http://localhost:8000>**.

With no `JAA_AUTH_PASSWORD` set, the console is reachable from localhost only —
any request from another machine gets a 503 rather than your resume. Set a
password before exposing it anywhere. To run it on a server so you can use it
from your phone, see **[deploy/DEPLOY.md](deploy/DEPLOY.md)**.

## Step 6 — Run your first batch

1. Paste links into the box. **Multiple URLs are separated by `|||`** (newlines
   work too, and you can mix both):

   ```
   https://boards.greenhouse.io/acme/jobs/4012345 ||| https://jobs.lever.co/globex/8a7c ||| https://careers.initech.com/apply/992
   ```

2. Pick a mode:

   | Mode | Behaviour |
   |---|---|
   | **Dry run** | Fills everything, screenshots it, never clicks submit. **Start here.** |
   | **Review** | Fills everything, then pauses. The row turns amber; open it, read every answer and the screenshot, then hit **Approve & submit** or **Reject**. |
   | **Auto** | Fills and submits. Refuses to submit if a required field came out blank. |

3. Hit **Run agent**. Watch the live log on the right; each application appears
   in the table as it progresses.

4. Click any row for the detail drawer: every question, the exact answer
   submitted, where it came from (answer bank vs. model), the confidence score,
   and the full-page screenshot.

---

## Sites that need a login (LinkedIn, Workday, Indeed)

Log in once, in a real browser the agent controls, and it saves the cookies:

```bash
python cli.py --setup-login https://www.linkedin.com/login
```

Log in in the window that opens, then press Enter in the terminal. The session
is written to `data/storage_state.json` and reused by every later run.

## Using a free model (OpenRouter)

Set `OPENROUTER_API_KEY` instead of `ANTHROPIC_API_KEY` and you're on the free
tier. Four things change, and all four are worth knowing before you let it
submit anything.

**1. Quality drops, and it drops where it costs you.** Copying your email into
an email box is easy and any model does it. Reading *"Describe a time you had to
influence a team without authority"* and writing three grounded sentences from
your resume is not. That is exactly the kind of answer a 9B model gets bland or
subtly wrong — and it goes out under your name. **Stay in `review` mode and read
the answers.** If you only ever run free models, treat the agent as a very fast
form-filler that drafts your long answers, not as something to approve blind.

**2. Only four free models support strict JSON schema output**, and the agent
needs structured output to work at all. Best first:

| Model | Context |
|---|---|
| `nvidia/nemotron-3-super-120b-a12b:free` *(default)* | 1M |
| `google/gemma-4-26b-a4b-it:free` | 262k |
| `openai/gpt-oss-20b:free` | 131k |
| `nvidia/nemotron-nano-9b-v2:free` | 32k |

That list changes. Check the current one:

```bash
curl -s "https://openrouter.ai/api/v1/models?supported_parameters=structured_outputs" \
  | python3 -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data'] if m['id'].endswith(':free')]"
```

Models outside that list often still work — the agent retries without the schema
and salvages JSON out of code fences or surrounding prose — but it's a coin flip.

**3. Rate limits are the real ceiling: 20 requests/minute and 50/day**, rising
to 1000/day once you've ever bought $10 of credits. Each page costs
`ceil(fields ÷ JAA_FIELD_BATCH)` requests, so a 30-field form at the default
batch of 18 is 2 requests. Fifty a day is roughly 20-25 applications — genuinely
enough. If you hit the limit, raise `JAA_FIELD_BATCH` to trade accuracy for
quota, and keep `JAA_CONCURRENCY` at 1 or 2 so you don't burst past 20/min.

**4. Check the privacy toggle before you send your resume.** OpenRouter itself
does not store prompts unless you opt in. But `:free` endpoints are free because
the upstream provider gets something, and some of them train on inputs.
OpenRouter gates those behind a setting at
<https://openrouter.ai/settings/privacy>. Your prompt here contains your full
resume, home address, and phone number. Decide deliberately — and if you'd
rather not, `google/gemma-4-26b-a4b-it:free` and paid models both avoid it.

Tuning that helps weak models:

```ini
JAA_FIELD_BATCH=10        # smaller batches = more accurate, more requests
JAA_MIN_CONFIDENCE=0.5    # be stricter about leaving fields blank
JAA_CONCURRENCY=1         # stay under 20 requests/minute
```

## The answer bank

Every answer the agent commits with reasonable confidence is stored in DuckDB,
keyed by a normalized form of the question. The next time any site asks
"How many years of experience do you have with Python?" — however it's worded —
it reuses your previous answer verbatim instead of asking the model again.
That makes application #20 both cheaper and more consistent than application #1.

Open the **Answer bank** tab to see everything remembered. You can pin your own
answers there; pinned answers always win and are never overwritten.

Per-company questions ("why do you want to work here", cover letters, salary
expectations) are deliberately excluded from reuse.

## Command line

```bash
# Fill 3 applications, submit nothing
python cli.py --mode dry_run "https://a/apply ||| https://b/apply ||| https://c/apply"

# Fill and submit, 3 at a time, no visible browser
python cli.py --mode auto --concurrency 3 --headless "https://a/apply ||| https://b/apply"

python cli.py --stats
```

## Where the data lives

| Path | Contents |
|---|---|
| `data/job_agent.duckdb` | applications, every submitted field, answer bank, event log |
| `data/screenshots/` | full-page PNG of every form, before and after submit |
| `data/storage_state.json` | saved browser logins |

Query it directly whenever you want:

```bash
python -c "import duckdb; print(duckdb.connect('data/job_agent.duckdb').sql('''
  SELECT company, role, status, fields_filled, created_at
  FROM applications ORDER BY created_at DESC LIMIT 20'''))"
```

---

## Tests

Three suites, none of which need an API key or the network. Run them after any
change to the extractor or filler:

```bash
python tests/test_form_flow.py        # DOM extraction + every fill action, verified in a real browser
python tests/test_runner_flow.py      # full orchestrator, DuckDB, answer-bank reuse, review gate
python tests/test_planner_contract.py # the exact request sent to Claude, over a mock transport
python tests/test_openrouter.py       # OpenRouter backend, batching, and messy free-model output
python tests/test_auth.py             # nothing is reachable without the password
```

`tests/fixtures/sample_form.html` is a deliberately awkward form — legend-based
radio groups, a checkbox group, a react-select-style combobox, a file input
hidden behind a dropzone, plus a site-search box and a password field that must
be ignored. If a real ATS breaks the agent, add its shape to that fixture first.

## Statuses

| Status | Meaning |
|---|---|
| `running` | Browser is on the page |
| `awaiting_review` | Filled; waiting for you to approve or reject |
| `filled_not_submitted` | Dry run, or a required field was blank in auto mode |
| `submitted` | Submitted and a confirmation message was detected |
| `submitted_unconfirmed` | Submit was clicked but no confirmation text found — check the screenshot |
| `rejected` | You rejected it during review |
| `failed` | No form found, login wall, or an error (see the row's error text) |

## Troubleshooting

**"No form fields found"** — the page is a job *description*, not the form, and
the Apply button wasn't recognised; or it's behind a login. Open the URL that
lands directly on the form (Greenhouse/Lever apply URLs work well), or run
`--setup-login` for that site first.

**Everything is left blank** — check the header line in the UI. If it says the
profile failed to load, or resume is 0 characters, fix that first.

**Custom dropdowns don't get set** — some vendors (Workday especially) use
non-standard widgets. Run with `JAA_HEADLESS=false` and watch which field fails;
the detail drawer records the exact error per field.

**Rate limits / cost** — drop `JAA_EFFORT` to `medium` and `JAA_CONCURRENCY` to
`1`. The answer bank means repeat questions cost nothing after the first time.

## Please read this part

Some employers prohibit automated applications in their terms of service, and
mass-applying is a good way to get filtered out rather than hired. Use `review`
mode, read what the agent wrote before it goes out, and keep the volume
deliberate. Every answer it produces is attributed to you.
