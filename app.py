"""
Futura Work — Engineering Enrichment Tool
Upload an Airscale/Clay CSV, get engineering hiring signal + tech stack enrichment back.

Adds new columns without touching any existing columns:
  - ats_source, open_engineering_jobs, engineering_job_titles
  - github_languages (optional)
  - tech_stack, engineer_types, seniority_mix, confidence, notes  (only if classification is on)
"""

import io
import re
import time
import json

import pandas as pd
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

st.set_page_config(page_title="Futura — Engineering Enrichment", page_icon="🛠️", layout="wide")

REQUEST_TIMEOUT = 8
CLAUDE_MODEL = "claude-sonnet-4-6"

ENGINEERING_KEYWORDS = [
    "engineer", "engineering", "developer", "dev ", "swe", "software",
    "backend", "back-end", "frontend", "front-end", "full stack", "fullstack",
    "data scientist", "data engineer", "machine learning", "ml engineer",
    "devops", "sre", "site reliability", "infrastructure", "platform engineer",
    "qa engineer", "test engineer", "mobile engineer", "ios engineer", "android engineer",
    "security engineer", "cloud engineer", "systems engineer", "embedded",
]

COMPANY_NAME_CANDIDATES = [
    "company name", "company", "name", "organization", "org name", "account name",
]
DOMAIN_CANDIDATES = [
    "company domain", "domain", "website", "company website", "url", "site",
]

COMPANY_SUFFIXES = [
    "inc", "inc.", "llc", "ltd", "ltd.", "co", "co.", "corp", "corp.",
    "corporation", "gmbh", "sa", "s.a.", "srl", "bv", "ag", "plc",
    "technologies", "technology", "tech", "labs", "biosciences", "biotech",
    "holdings", "group",
]


# ----------------------------------------------------------------------------
# Helpers — column auto-detection
# ----------------------------------------------------------------------------

def guess_column(columns, candidates):
    lower_map = {c.lower().strip(): c for c in columns}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    # fuzzy: contains
    for c in columns:
        cl = c.lower()
        if any(cand in cl for cand in candidates):
            return c
    return None


# ----------------------------------------------------------------------------
# Helpers — slug generation
# ----------------------------------------------------------------------------

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def domain_root(domain):
    if not isinstance(domain, str) or not domain.strip():
        return None
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0]
    d = d.split(".")[0]
    return d if d else None


def name_slug_variants(name):
    if not isinstance(name, str) or not name.strip():
        return []
    base = name.lower().strip()
    base = re.sub(r"[.,]", "", base)
    words = [w for w in base.split() if w not in COMPANY_SUFFIXES]
    joined_dash = "-".join(words)
    joined_nodash = "".join(words)
    return list({slugify(joined_dash), slugify(joined_nodash)})


def candidate_slugs(company_name, domain):
    slugs = []
    root = domain_root(domain) if domain else None
    if root:
        slugs.append(root)
    if company_name:
        slugs.extend(name_slug_variants(company_name))
    # dedupe, preserve order
    seen = set()
    out = []
    for s in slugs:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ----------------------------------------------------------------------------
# ATS lookups (free, no auth)
# ----------------------------------------------------------------------------

def try_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            jobs = data.get("jobs", [])
            if isinstance(jobs, list):
                return [j.get("title", "") for j in jobs]
    except requests.RequestException:
        pass
    return None


def try_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return [j.get("text", "") for j in data]
    except requests.RequestException:
        pass
    return None


def try_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            jobs = data.get("jobs", [])
            if isinstance(jobs, list):
                return [j.get("title", "") for j in jobs]
    except requests.RequestException:
        pass
    return None


def detect_ats_jobs(company_name, domain):
    """Try every candidate slug against every ATS. Return (ats_name, [job_titles]) or (None, [])."""
    slugs = candidate_slugs(company_name, domain)
    for slug in slugs:
        for ats_name, fn in [("Greenhouse", try_greenhouse), ("Lever", try_lever), ("Ashby", try_ashby)]:
            titles = fn(slug)
            if titles is not None:
                return ats_name, titles
    return None, []


def filter_engineering(titles):
    return [t for t in titles if any(kw in t.lower() for kw in ENGINEERING_KEYWORDS)]


# ----------------------------------------------------------------------------
# GitHub org languages (optional, free, unauthenticated — 60 req/hr limit)
# ----------------------------------------------------------------------------

def try_github_languages(company_name, domain):
    slugs = candidate_slugs(company_name, domain)
    for slug in slugs:
        try:
            r = requests.get(f"https://api.github.com/orgs/{slug}/repos?per_page=20", timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                repos = r.json()
                if isinstance(repos, list) and repos:
                    langs = {}
                    for repo in repos:
                        lang = repo.get("language")
                        if lang:
                            langs[lang] = langs.get(lang, 0) + 1
                    if langs:
                        ranked = sorted(langs.items(), key=lambda x: -x[1])
                        return ", ".join(l for l, _ in ranked[:6])
            elif r.status_code == 403:
                return "RATE_LIMITED"
        except requests.RequestException:
            pass
    return None


# ----------------------------------------------------------------------------
# Claude classification
# ----------------------------------------------------------------------------

def classify_with_claude(api_key, company_name, domain, eng_titles, github_langs):
    prompt = f"""You are helping a technical recruiting company assess an engineering team.

Company: {company_name}
Domain: {domain}
Open engineering job titles found (may be empty): {json.dumps(eng_titles)}
GitHub languages detected (may be empty or "unknown"): {github_langs or "unknown"}

Based ONLY on this evidence, respond with ONLY a JSON object, no preamble, no markdown fences:
{{
  "tech_stack": "comma-separated likely technologies, or 'insufficient data'",
  "engineer_types": "comma-separated roles they appear to be hiring/using, e.g. backend, data, ML, or 'insufficient data'",
  "seniority_mix": "short phrase, e.g. 'mostly senior', 'mixed', 'unclear', or 'insufficient data'",
  "confidence": "low, medium, or high",
  "notes": "one short sentence caveat or observation"
}}"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if r.status_code != 200:
            return {"tech_stack": "", "engineer_types": "", "seniority_mix": "",
                     "confidence": "", "notes": f"API error {r.status_code}"}
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = text.strip().strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
        parsed = json.loads(text)
        return {
            "tech_stack": parsed.get("tech_stack", ""),
            "engineer_types": parsed.get("engineer_types", ""),
            "seniority_mix": parsed.get("seniority_mix", ""),
            "confidence": parsed.get("confidence", ""),
            "notes": parsed.get("notes", ""),
        }
    except Exception as e:
        return {"tech_stack": "", "engineer_types": "", "seniority_mix": "",
                 "confidence": "", "notes": f"parse error: {e}"}


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

st.title("🛠️ Futura — Engineering Enrichment")
st.caption(
    "Upload your Airscale/Clay CSV. This tool checks each company's own careers page "
    "(Greenhouse / Lever / Ashby — not LinkedIn Jobs) for open engineering roles, "
    "optionally checks public GitHub, and can classify tech stack, engineer types, "
    "and seniority mix with Claude."
)

api_key = st.secrets.get("ANTHROPIC_API_KEY", "")

with st.sidebar:
    st.header("Settings")
    do_classify = st.toggle(
        "Classify with Claude (costs ~$0.01–0.02/company)",
        value=False,
        help="Turn off to run a free coverage test (just ATS + GitHub lookups).",
    )
    if do_classify and not api_key:
        st.error("No ANTHROPIC_API_KEY found in app secrets. Ask an admin to add it before enabling classification.")
    check_github = st.toggle("Also check public GitHub languages", value=False,
                              help="Free, but GitHub allows only ~60 unauthenticated requests/hour.")
    max_companies = st.number_input("Max companies to process (0 = all)", min_value=0, value=10, step=5)

uploaded = st.file_uploader("Upload CSV", type=["csv"])

if uploaded is not None:
    try:
        df = pd.read_csv(uploaded)
    except Exception as e:
        st.error(f"Couldn't read that CSV: {e}")
        st.stop()

    st.write(f"Loaded **{len(df)}** rows, **{len(df.columns)}** columns.")

    cols = list(df.columns)
    guessed_name_col = guess_column(cols, COMPANY_NAME_CANDIDATES)
    guessed_domain_col = guess_column(cols, DOMAIN_CANDIDATES)

    col1, col2 = st.columns(2)
    with col1:
        name_col = st.selectbox("Company name column", options=cols,
                                 index=cols.index(guessed_name_col) if guessed_name_col in cols else 0)
    with col2:
        domain_col = st.selectbox("Company domain column", options=cols,
                                   index=cols.index(guessed_domain_col) if guessed_domain_col in cols else 0)

    run = st.button("Run enrichment", type="primary")

    if run:
        work_df = df.copy()
        n = len(work_df) if max_companies == 0 else min(max_companies, len(work_df))

        results = []
        progress = st.progress(0.0)
        status = st.empty()

        for i in range(n):
            row = work_df.iloc[i]
            company_name = str(row.get(name_col, "") or "")
            domain = str(row.get(domain_col, "") or "")
            status.text(f"Processing {i + 1}/{n}: {company_name or domain}")

            ats_name, titles = detect_ats_jobs(company_name, domain)
            eng_titles = filter_engineering(titles)

            github_langs = None
            if check_github:
                github_langs = try_github_languages(company_name, domain)

            row_result = {
                "ats_source": ats_name or "",
                "open_engineering_jobs": len(eng_titles),
                "engineering_job_titles": "; ".join(eng_titles[:10]),
                "github_languages": github_langs or "",
            }

            if do_classify and api_key:
                row_result.update(
                    classify_with_claude(api_key, company_name, domain, eng_titles, github_langs)
                )
                time.sleep(0.2)  # gentle pacing

            results.append(row_result)
            progress.progress((i + 1) / n)

        status.text("Done.")

        results_df = pd.DataFrame(results)
        # pad remaining rows (if max_companies < len(df)) with blanks so columns still align
        if n < len(work_df):
            pad = pd.DataFrame([{} for _ in range(len(work_df) - n)])
            results_df = pd.concat([results_df, pad], ignore_index=True)

        final_df = pd.concat([work_df.reset_index(drop=True), results_df.reset_index(drop=True)], axis=1)

        st.success(f"Enriched {n} of {len(work_df)} companies.")
        st.dataframe(final_df, use_container_width=True)

        csv_bytes = final_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download enriched CSV",
            data=csv_bytes,
            file_name="enriched_" + uploaded.name,
            mime="text/csv",
        )
else:
    st.info("Upload a CSV to get started.")
