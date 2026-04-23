# Ticket Tools (GCT)

Multi-site ticket watcher for Gold Coast Tickets. Runs on GitHub Actions — no server.
Every 15 minutes it checks configured sites for new events and emails you when any appear.

## Currently watching

Edit `sites.py` to add or remove. Current config:

- FrontGate Tickets (Webflow CMS) — festival ticketing, nationwide
- Metro Chicago (RHP CMS) — individual venue
- Rivers Casino Des Plaines (Playwright) — bot-protected venue

## Add a new site

1. Open `sites.py`.
2. Add an entry:
   ```python
   {
       "name": "Your Venue",
       "parser": "rhp",   # or "frontgate" / "playwright"
       "url": "https://yourvenue.com/events",
   },
   ```
3. Commit and push. The next run picks it up automatically (first run on a new site
   baselines silently — you won't get spammed with 200 "new" events).

## Parser types

| Parser | Use when | Speed |
|---|---|---|
| `frontgate` | FrontGate Tickets | fast |
| `rhp` | Rockhouse Partners CMS (Metro Chicago, many indie venues) | fast |
| `playwright` | Bot-protected sites (casinos, big arenas) | slow |

For `playwright` entries, you can optionally tighten matching with extra config:

```python
{
  "name": "...", "parser": "playwright", "url": "...",
  "event_selector": "a.event-card",          # CSS selector for event blocks
  "name_selector":  "h3",                     # inside each block
  "date_selector":  ".date",                  # inside each block
  "wait_for":       "a.event-card",           # wait for selector before parsing
}
```

If omitted, Playwright falls back to "any anchor whose href contains `/event/` or `/show/`"
which works for most venue sites.

## Setup

Repo secrets (Settings → Secrets and variables → Actions):

- `SENDGRID_API_KEY`
- `ALERT_EMAIL`
- `FROM_EMAIL`

## Local testing

```bash
pip install playwright
playwright install chromium
export SENDGRID_API_KEY=... ALERT_EMAIL=... FROM_EMAIL=...
python watcher.py
```

## State

Each site has its own `state/{slug}.json` file that tracks known events.
Committed back to the repo on change → free audit log of when each event first appeared.
