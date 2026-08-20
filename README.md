
# Futura — Engineering Enrichment (Streamlit app)

Web tool so Dylan and Janik can drop in a raw Airscale/Clay CSV and get engineering
hiring-signal enrichment back, without touching Python or the terminal.

## What it does

- Auto-detects the company name / domain columns (editable if the guess is wrong)
- Looks up each company's own careers page via the free, unauthenticated
  Greenhouse / Lever / Ashby APIs (this is what Airscale/LinkedIn Jobs misses)
- Optionally checks public GitHub org languages
- Optionally classifies `tech_stack`, `engineer_types`, `seniority_mix`,
  `confidence`, `notes` using the Claude API (toggle — off by default, so a
  free coverage test is always possible first)
- Appends new columns to the original data — never modifies or deletes existing ones
- One-click CSV download of the result

## Deploy it (one-time setup, ~10 minutes)

1. **Create a GitHub repo** (private is fine) and push these three files to it:
   - `app.py`
   - `requirements.txt`
   - `README.md` (optional, just for reference)

2. **Go to** [share.streamlit.io](https://share.streamlit.io) and sign in with
   GitHub (create a free Streamlit Community Cloud account if you don't have one).

3. **Click "New app"**, pick the repo, branch (`main`), and set the main file
   path to `app.py`. Click **Deploy**.

4. **Add your Claude API key as a secret** (do this BEFORE sharing the link with
   the team — never put the key in the code itself):
   - In the app dashboard, go to **Settings → Secrets**
   - Paste:
     ```toml
     ANTHROPIC_API_KEY = "sk-ant-...your-key-here..."
     ```
   - Save. The app will restart automatically and pick it up.

5. **Copy the app URL** (something like `futura-enrichment.streamlit.app`) and
   share it with Dylan and Janik. That's it — no installs, no CSV
   upload/download to Colab, no manual key handling on their end.

## Recommended first use

Have Dylan/Janik run with the **"Classify with Claude" toggle OFF** first, on
a small batch (10–20 rows), to sanity-check ATS coverage for free before
spending on classification. Once coverage looks reasonable, turn classification
on for the full run.

## Notes / limits

- Streamlit Community Cloud free tier: the app "sleeps" after inactivity and
  takes a few seconds to wake up on the next visit — normal, not a bug.
- GitHub's public API allows ~60 unauthenticated requests/hour. If several
  people run the GitHub check at the same time, some lookups may fail
  temporarily — this is harmless, just leave "Also check GitHub" off unless
  you need it.
- The app is public by URL by default. If you want to restrict who can open
  it, Streamlit Cloud has an option under app settings to require viewers to
  sign in / be on an allowed list — worth doing since the app can spend money
  on the Claude API if left open to anyone with the link.
