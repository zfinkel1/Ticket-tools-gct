# Ticket Tools (GCT)

Personal tools for Gold Coast Tickets. Runs on GitHub Actions — no server needed.

## Tools

### 1. FrontGate Watcher (`frontgate_watcher.py`)
Scrapes `frontgatetickets.com/events` every hour. Emails when new events appear.

- **Schedule:** hourly (`.github/workflows/frontgate.yml`)
- **State:** `state/events.json` — committed back to the repo on change (free audit log)
- **Email:** SendGrid HTTP API (reuses SCN's SendGrid account)
- **First run:** baselines the current ~200 events without sending an email

## Setup

1. Add three repo secrets in **Settings → Secrets and variables → Actions → New repository secret**:
   - `SENDGRID_API_KEY` — your SendGrid API key
   - `ALERT_EMAIL` — where alerts go (e.g. `zfinkel1@gmail.com`)
   - `FROM_EMAIL` — a SendGrid-verified sender (e.g. `alerts@sportscardnetwork.ai`)
2. Push this repo to GitHub (main branch).
3. The workflow runs automatically every hour. To test immediately: **Actions → FrontGate Event Watcher → Run workflow**.

## Running locally

```bash
export SENDGRID_API_KEY=...
export ALERT_EMAIL=zfinkel1@gmail.com
export FROM_EMAIL=alerts@sportscardnetwork.ai
python frontgate_watcher.py
```

No pip install needed — stdlib only.
