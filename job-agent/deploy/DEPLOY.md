# Running the agent on a server

Goal: the agent lives on a small VM, you open a URL on your phone, paste job
links, and approve applications from wherever you are. Your laptop can be shut.

Two paths below. **Docker Compose** is the one to use unless you have a reason
not to — it pins the browser, the Python version, and the system libraries that
Chromium needs, none of which you want to debug by hand on a fresh Ubuntu box.

---

## Part 1 — Get the code onto GitHub

The branch is already pushed. Confirm from your laptop:

```bash
git fetch origin
git log --oneline origin/claude/ai-job-application-agent-vvfvx1 -1
```

You have three options for what to do with it:

**Deploy straight from the branch** (nothing else to do — skip to Part 2 and
clone that branch on the server).

**Merge it into your default branch** so the server can track `main`:

```bash
git checkout claude/data-engineer-interview-prep-gFxFa   # your default branch
git merge --no-ff claude/ai-job-application-agent-vvfvx1
git push origin HEAD
```

**Open a pull request** if you want to read the diff on github.com first:

```bash
gh pr create --base claude/data-engineer-interview-prep-gFxFa \
             --head claude/ai-job-application-agent-vvfvx1 \
             --title "AI job application agent"
```

> **Before you push anything from your own machine:** `git status` must not
> show `job-agent/.env`, `job-agent/config/profile.yaml`,
> `job-agent/deploy/.env`, or anything under `job-agent/data/`. They are all in
> `.gitignore`, but check — those files hold your API key, your address, and
> your session cookies.

---

## Part 2 — Pick a machine

| Provider | Instance | ~Cost/month |
|---|---|---|
| Hetzner Cloud | CPX21 — 3 vCPU, 4 GB | ~€8 |
| DigitalOcean | Basic — 2 vCPU, 4 GB | ~$24 |
| AWS Lightsail | 2 vCPU, 4 GB | ~$24 |
| Oracle Cloud | Ampere A1, 4 vCPU / 24 GB | free tier |

**4 GB RAM is the number that matters.** Each concurrent application is a real
Chromium browser; two at once plus the OS wants ~3 GB. On a 2 GB box set
`JAA_CONCURRENCY=1`. Ubuntu 24.04 LTS, 20 GB disk.

Open ports **22**, **80**, **443** in the provider's firewall.

---

## Part 3 — Deploy with Docker Compose

SSH in, then:

```bash
# Docker, from the official convenience script
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# The code
sudo mkdir -p /opt && cd /opt
sudo git clone https://github.com/amolpatilofficial/Interview_prep.git
sudo chown -R $USER:$USER Interview_prep
cd Interview_prep && git checkout claude/ai-job-application-agent-vvfvx1
cd job-agent
```

Configure secrets:

```bash
cp deploy/.env.example deploy/.env
openssl rand -base64 24        # copy this
nano deploy/.env
```

Fill in three things:

```ini
ANTHROPIC_API_KEY=sk-ant-...
JAA_AUTH_USER=amol
JAA_AUTH_PASSWORD=<the string you just generated>
JAA_SITE_ADDRESS=agent.yourdomain.com   # or :80 — see Part 5
```

The container **will not start** without `JAA_AUTH_PASSWORD`. That is deliberate:
it binds `0.0.0.0`, and it holds your resume.

Start it:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

First build takes 3-5 minutes (it downloads Chromium). Watch it:

```bash
docker compose -f deploy/docker-compose.yml logs -f agent
```

---

## Part 4 — Upload your profile and documents

The container keeps everything in a named volume at `/data`, so it survives
rebuilds. Push your files in from the server:

```bash
cd /opt/Interview_prep/job-agent

# 1. Your profile
cp config/profile.example.yaml /tmp/profile.yaml
nano /tmp/profile.yaml          # fill in identity, work_authorization, preferences
docker compose -f deploy/docker-compose.yml cp /tmp/profile.yaml agent:/data/profile.yaml
rm /tmp/profile.yaml
```

Your resume has to get from your laptop to the server. **From your laptop:**

```bash
scp ~/Documents/resume.pdf user@your-server-ip:/tmp/resume.pdf
```

**Back on the server:**

```bash
docker compose -f deploy/docker-compose.yml cp /tmp/resume.pdf agent:/data/documents/resume.pdf
rm /tmp/resume.pdf
```

Point `profile.yaml` at the container's path — note these are **absolute
`/data/...` paths**, not the relative ones you'd use locally:

```yaml
documents:
  resume: /data/documents/resume.pdf
  cover_letter: /data/documents/cover_letter.pdf
```

Restart and check:

```bash
docker compose -f deploy/docker-compose.yml restart agent
curl -s http://localhost/healthz
# {"ok":true,"profile_loaded":true,"api_key_present":true,"active_batches":0}
```

If `profile_loaded` is `false`, the path in `profile.yaml` is wrong or the YAML
doesn't parse. The container log says which.

---

## Part 5 — Reaching it safely

### Option A — a domain (best, works from anywhere)

Add a DNS **A record** pointing `agent.yourdomain.com` at the server's IP, set
`JAA_SITE_ADDRESS=agent.yourdomain.com` in `deploy/.env`, and restart Caddy:

```bash
docker compose -f deploy/docker-compose.yml up -d caddy
```

Caddy gets a Let's Encrypt certificate on first request. Open
`https://agent.yourdomain.com` — the browser prompts for the username and
password from `deploy/.env`.

### Option B — Tailscale (best if you don't own a domain)

A private network between your phone, your laptop, and the server. Nothing is
exposed to the internet at all.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Install the Tailscale app on your phone, sign in with the same account, and
visit `http://<server-tailscale-ip>`. Leave `JAA_SITE_ADDRESS=:80` and **close
ports 80 and 443** in your provider's firewall.

> **Do not** leave `JAA_SITE_ADDRESS=:80` on a public IP. Basic auth over plain
> HTTP sends your password in cleartext on every request. Domain + HTTPS, or
> Tailscale. Not neither.

---

## Part 6 — Sites that need a login

`cli.py --setup-login` opens a visible browser, which a headless server does not
have. Capture the session on your laptop and copy it up:

```bash
# On your laptop, in job-agent/ with the venv active
python cli.py --setup-login https://www.linkedin.com/login
scp data/storage_state.json user@your-server-ip:/tmp/

# On the server
docker compose -f deploy/docker-compose.yml cp /tmp/storage_state.json agent:/data/storage_state.json
docker compose -f deploy/docker-compose.yml restart agent
rm /tmp/storage_state.json
```

Sessions expire — LinkedIn's in a few weeks. Repeat when logged-in sites start
failing with "No form fields found".

---

## Part 7 — Living with it

```bash
cd /opt/Interview_prep/job-agent
C="docker compose -f deploy/docker-compose.yml"

$C logs -f agent            # follow the log
$C ps                       # is it up and healthy
$C restart agent            # after editing profile.yaml
$C down && $C up -d --build # after pulling new code
```

**Update:**

```bash
cd /opt/Interview_prep && git pull
cd job-agent && docker compose -f deploy/docker-compose.yml up -d --build
```

The `/data` volume is untouched by rebuilds — your applications, answer bank,
resume, and logins all survive.

**Back up** (the DuckDB file holds every application and your whole answer bank):

```bash
docker run --rm -v job-agent_agent-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/agent-backup-$(date +%F).tar.gz -C /data .
```

Then `scp` that tarball somewhere off the server. Restore by extracting it back
into the volume the same way.

**Query the database directly** any time:

```bash
docker compose -f deploy/docker-compose.yml exec agent python -c "
import duckdb; print(duckdb.connect('/data/job_agent.duckdb').sql('''
  SELECT company, role, status, created_at FROM applications
  ORDER BY created_at DESC LIMIT 20'''))"
```

---

## Part 8 — Using it from your phone

Open the URL, log in once (your phone keychain will offer to save it), and the
console works as it does on a laptop: paste links separated by `|||`, watch the
live log, and tap a row to review.

Keep it in **review** mode. You get a notification-free amber row when an
application is filled and waiting; open it, read the answers and the screenshot,
tap **Approve & submit** or **Reject**. Review waits one hour before giving up
and leaving the application unsubmitted, so approve within the hour or re-run it.

---

## Without Docker

If you'd rather run it directly on the host:

```bash
sudo adduser --system --group --home /opt/Interview_prep jobagent
cd /opt/Interview_prep/job-agent
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=$PWD/.playwright ./.venv/bin/playwright install --with-deps chromium

cp .env.example .env && nano .env      # API key + JAA_AUTH_PASSWORD + JAA_HEADLESS=true
cp config/profile.example.yaml config/profile.yaml && nano config/profile.yaml
sudo chown -R jobagent:jobagent /opt/Interview_prep

sudo cp deploy/jobagent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now jobagent
sudo journalctl -u jobagent -f
```

The unit binds `127.0.0.1:8000` only. Put Caddy or nginx in front of it for TLS —
the `Caddyfile` here works with `reverse_proxy 127.0.0.1:8000` instead of
`agent:8000`.

---

## Troubleshooting

**Container exits immediately** — read the log. `JAA_AUTH_PASSWORD` or
`ANTHROPIC_API_KEY` missing from `deploy/.env` is the usual cause; the entrypoint
says which.

**`Target page, context or browser has been closed`** — Chromium ran out of
memory. Set `JAA_CONCURRENCY=1`, and confirm `shm_size: "1gb"` is in the compose
file.

**503 "Refusing to serve a remote request with no password set"** — the app is
reachable but `JAA_AUTH_PASSWORD` is empty. Set it and restart.

**Live log doesn't stream** — something between you and Caddy is buffering. The
supplied `Caddyfile` sets `flush_interval -1` on `/api/events`; if you swapped in
nginx you need `proxy_buffering off;` on that path.

**A site works locally but not on the server** — it's probably detecting
headless Chromium. Nothing in the config fixes that; run those particular
applications from your laptop with `JAA_HEADLESS=false`.
