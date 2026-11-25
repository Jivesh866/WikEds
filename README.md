# wikeds

Live Wikipedia edit stats. A listener reads the Wikimedia stream into SQLite, a rollup
makes hourly summaries, and a Streamlit dashboard shows it all.

## Run locally (one command)

```bash
cp .env.example .env        # set WIKI_USER_AGENT to your email
uv run wikeds-run
```

This starts the listener and the dashboard together. Ctrl+C stops both.

Or run them separately in two terminals:

```bash
uv run wikeds-listener
uv run streamlit run src/wikeds/dashboard.py
```

Backfill history from existing data: `uv run python -m wikeds.rollup`

## Where data lives

- **Raw events**: local sqlite file (`data/wiki.db` locally, `/tmp/wikeds/raw.db` on cloud).
  Used for the Live tab. Fine if it gets wiped.
- **Hourly summaries + meta**: Supabase Postgres when `SUPABASE_DB_URL` is set, otherwise the
  same local sqlite file. Used for the History tab, and survives cloud restarts.

The dashboard reads both: raw for live, summaries for history.

### Supabase setup

1. Create a project, then Project Settings > Database > Connection string.
2. Use the **pooler** string (the direct one is IPv6 only and Cloud may not reach it).
3. Put it in `.env` as `SUPABASE_DB_URL=...` and in Cloud secrets (see `.streamlit/secrets.toml.example`).

Tables are created on first run, with row level security on. To push existing local raw
data into Supabase once, run `uv run python -m wikeds.rollup` with the URL set.

## Cloud vs local

`config.py` detects Streamlit Community Cloud on its own and picks defaults:

| setting | cloud | local |
|---|---|---|
| listener | runs inside the dashboard | separate process (`wikeds-run`) |
| raw db | `/tmp/wikeds/raw.db` | `data/wiki.db` |
| raw retention | 1 day | 7 days |

Any env var overrides these. If detection ever guesses wrong, set `RUN_ENV=cloud` or `RUN_ENV=local`.

## Streamlit Community Cloud

1. Push the repo to GitHub.
2. New app, main file: `streamlit_app.py`.
3. Dependencies come from `requirements.txt`.
4. Add the secrets from `.streamlit/secrets.toml.example`.

Idle apps sleep, and the listener sleeps with them, so expect gaps in History.
Hours that were only partly observed (restarts) keep their highest count, they never shrink.
