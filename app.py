"""
Futura Work — Engineering Enrichment Tool
Upload an Airscale/Clay CSV, get engineering hiring signal + tech stack enrichment back.

Search order for open engineering jobs:
  1. Apify — Jobo "ATS Jobs API" actor (queries 75+ ATS platforms: Greenhouse, Lever,
     Ashby, Workday, iCIMS, Taleo, SuccessFactors, and more, via one call, matched by
     company domain)
  2. Careers page read by Claude (fallback, only runs when classification is enabled,
     since it requires Claude to interpret unstructured text) — catches companies that
     post jobs on their own site without any ATS behind it (common for small biotech,
     academic/nonprofit orgs, etc.)

Adds new columns without touching any existing columns.
"""

import re
import json

import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

st.set_page_config(page_title="Futura — Engineering Enrichment", page_icon="🛠️", layout="wide")

REQUEST_TIMEOUT = 8
PAGE_FETCH_TIMEOUT = 10
CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_DESC_CHARS_PER_JOB = 700       # per-job description truncation fed to Claude
MAX_PAGE_CHARS_FOR_FALLBACK = 6000  # careers-page text truncation fed to Claude

# Apify — Jobo "ATS Jobs API" actor
APIFY_ACTOR_ID = "jobo.world~ats-jobs-api"
APIFY_RUN_SYNC_URL = f"https://api.apify.com/v2/actors/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
APIFY_TIMEOUT = 60  # this actor is typically fast (single-company lookups), but give it room

ENGINEERING_KEYWORDS = [
    "engineer", "engineering", "developer", "dev ", "swe", "software",
    "backend", "back-end", "frontend", "front-end", "full stack", "fullstack",
    "data scientist", "data engineer", "machine learning", "ml engineer",
    "devops", "sre", "site reliability", "infrastructure", "platform engineer",
    "qa engineer", "test engineer", "mobile engineer", "ios engineer", "android engineer",
    "security engineer", "cloud engineer", "systems engineer", "embedded",
]

COMPANY_NAME_CANDIDATES = ["company name", "company", "name", "organization", "org name", "account name"]
DOMAIN_CANDIDATES = ["company domain", "domain", "website", "company website", "url", "site"]

COMPANY_SUFFIXES = [
    "inc", "inc.", "llc", "ltd", "ltd.", "co", "co.", "corp", "corp.",
    "corporation", "gmbh", "sa", "s.a.", "srl", "bv", "ag", "plc",
    "technologies", "technology", "tech", "labs", "biosciences", "biotech",
    "holdings", "group",
]

CAREERS_PATH_CANDIDATES = [
    "/careers", "/jobs", "/careers/", "/jobs/", "/join-us", "/about/careers",
    "/company/careers", "/company/jobs", "/team/careers", "/en/careers",
]

WEBSITE_SIGNATURES = {
    "React": ['id="root"', "data-reactroot", "react-dom"],
    "Next.js": ["__next", "_next/static", 'name="next-head"'],
    "Vue": ["data-v-", "__vue__", "vue.js"],
    "Angular": ["ng-version", "ng-app"],
    "WordPress": ["wp-content", "wp-includes"],
    "Shopify": ["cdn.shopify.com", "shopify.com/s/"],
    "Webflow": ["webflow.io", "data-wf-page", "data-wf-site"],
    "Squarespace": ["squarespace.com", "static1.squarespace.com"],
    "Wix": ["wix.com", "wixstatic.com"],
    "HubSpot": ["hs-scripts.com", "hubspot.com/cs/"],
    "Google Tag Manager": ["googletagmanager.com"],
    "Cloudflare": ["cloudflare.com/cdn-cgi", "__cf_bm"],
    "Vercel": ["vercel.app", "_vercel"],
    "Netlify": ["netlify.app", "netlify.com"],
    "Django": ["csrfmiddlewaretoken"],
    "Ruby on Rails": ["csrf-param", "data-turbo"],
}


# ----------------------------------------------------------------------------
# Helpers — column auto-detection
# ----------------------------------------------------------------------------

def guess_column(columns, candidates):
    lower_map = {c.lower().strip(): c for c in columns}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
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


def clean_domain(domain):
    """Return a bare host, e.g. acme.com, suitable for building a URL."""
    if not isinstance(domain, str) or not domain.strip():
        return None
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0]
    return d if "." in d else None


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
    seen = set()
    out = []
    for s in slugs:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ----------------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------------

def html_to_text(html, limit=None):
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator=" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def looks_remote(location_text, remote_flag=None):
    if remote_flag is True:
        return True
    if remote_flag is False:
        return False
    if location_text and "remote" in location_text.lower():
        return True
    return None  # unknown


# ----------------------------------------------------------------------------
# ATS lookup via Apify — Jobo "ATS Jobs API"
# Replaces the previous direct Greenhouse/Lever/Ashby probing. Matches by
# company domain (more reliable than name, since indexed company names don't
# always match your CSV's spelling — e.g. "BOI" vs "BOI (Board of Innovation)").
# normalized job = {"title", "location", "remote" (True/False/None), "description"}
# ----------------------------------------------------------------------------

def call_apify_ats_jobs(apify_token, domain):
    host = clean_domain(domain)
    if not host:
        return None

    payload = {
        "companies_include": [host],
        "queries": [],       # empty = don't filter by title, get everything for this company
        "sources": [],       # empty = search across all supported ATS platforms
        "page_size": 100,
    }

    try:
        r = requests.post(
            APIFY_RUN_SYNC_URL,
            params={"token": apify_token},
            json=payload,
            timeout=APIFY_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        items = r.json()
        if not isinstance(items, list):
            return None
    except (requests.RequestException, ValueError):
        return None

    out = []
    for j in items:
        # Field names per Jobo's documented schema (Title, Company, Workplace,
        # Locations, Description, Role summary...). If Apify ever renames a
        # field, these .get() fallbacks keep this from silently breaking —
        # but double check against a real dataset export if results look thin.
        title = j.get("title", "") or ""
        workplace = j.get("workplace") or j.get("work_model")  # e.g. "remote"/"hybrid"/"onsite"
        locations = j.get("locations")
        if isinstance(locations, list):
            loc_text = ", ".join(str(l) for l in locations)
        else:
            loc_text = str(locations or "")
        remote_flag = True if workplace == "remote" else (False if workplace in ("hybrid", "onsite") else None)
        desc = j.get("description") or j.get("summary") or ""
        out.append({
            "title": title,
            "location": loc_text,
            "remote": looks_remote(loc_text, remote_flag),
            "description": html_to_text(desc, MAX_DESC_CHARS_PER_JOB) if "<" in desc else desc[:MAX_DESC_CHARS_PER_JOB],
        })
    return out


def detect_ats_jobs(apify_token, domain):
    """Single entry point for the ATS lookup step. Returns (source_name, [normalized jobs])."""
    jobs = call_apify_ats_jobs(apify_token, domain)
    if jobs:
        return "Apify (ATS Jobs API)", jobs
    return None, []


def filter_engineering(jobs):
    return [j for j in jobs if any(kw in j["title"].lower() for kw in ENGINEERING_KEYWORDS)]


def filter_remote(jobs, remote_only):
    if not remote_only:
        return jobs
    return [j for j in jobs if j.get("remote") is True]


# ----------------------------------------------------------------------------
# Careers-page fallback (only used when ATS found nothing, and only when
# Claude classification is enabled — reading unstructured HTML needs Claude)
# ----------------------------------------------------------------------------

def find_careers_url(domain):
    host = clean_domain(domain)
    if not host:
        return None
    for scheme in ("https://", "http://"):
        homepage = f"{scheme}{host}"
        try:
            r = requests.get(homepage, timeout=PAGE_FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = (a.get_text() or "").lower()
                if "career" in href.lower() or "career" in text or "jobs" in href.lower() or "jobs" in text:
                    if href.startswith("http"):
                        return href
                    return homepage.rstrip("/") + "/" + href.lstrip("/")
            # no link found on homepage — try common paths directly
            for path in CAREERS_PATH_CANDIDATES:
                candidate = homepage.rstrip("/") + path
                try:
                    rr = requests.get(candidate, timeout=PAGE_FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
                    if rr.status_code == 200:
                        return candidate
                except requests.RequestException:
                    continue
        except requests.RequestException:
            continue
    return None


def fetch_page_text(url):
    try:
        r = requests.get(url, timeout=PAGE_FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return html_to_text(r.text, MAX_PAGE_CHARS_FOR_FALLBACK)
    except requests.RequestException:
        pass
    return None


def claude_extract_jobs_from_page(api_key, company_name, page_text):
    """Ask Claude to pull engineering job listings out of raw careers-page text."""
    prompt = f"""This is text scraped from the careers/jobs page of "{company_name}".

Extract any ENGINEERING job openings mentioned (software, backend, frontend, data, ML, DevOps, SRE, QA, mobile, infra, security engineering roles). Ignore non-engineering roles.

For each one, note if the text suggests it's remote, hybrid, onsite, or unclear.

Page text:
---
{page_text}
---

Respond with ONLY a JSON array, no preamble, no markdown fences. Each item:
{{"title": "...", "remote_hint": "remote" | "hybrid" | "onsite" | "unclear"}}

If no engineering roles are found, respond with an empty array: []"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = text.strip().strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
        parsed = json.loads(text)
        out = []
        for item in parsed:
            hint = item.get("remote_hint", "unclear")
            remote_flag = True if hint == "remote" else (False if hint in ("hybrid", "onsite") else None)
            out.append({
                "title": item.get("title", ""),
                "location": hint,
                "remote": remote_flag,
                "description": "",  # no structured description available from this path
            })
        return out
    except Exception:
        return []


# ----------------------------------------------------------------------------
# Website tech fingerprint (free — homepage signature scan)
# ----------------------------------------------------------------------------

def website_signals(domain):
    host = clean_domain(domain)
    if not host:
        return ""
    for scheme in ("https://", "http://"):
        try:
            r = requests.get(f"{scheme}{host}", timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                haystack = r.text.lower() + " ".join(f"{k}:{v}" for k, v in r.headers.items()).lower()
                found = [name for name, markers in WEBSITE_SIGNATURES.items()
                         if any(m.lower() in haystack for m in markers)]
                return ", ".join(found) if found else "no signals detected"
        except requests.RequestException:
            continue
    return ""


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

def classify_with_claude(api_key, company_name, domain, eng_jobs, website_sig, github_langs):
    job_block = "\n".join(
        f"- {j['title']} ({j.get('location') or 'location unknown'}): {j.get('description') or 'no description available'}"
        for j in eng_jobs
    ) or "none found"

    prompt = f"""You are helping a technical recruiting company assess an engineering team.

Company: {company_name}
Domain: {domain}
Open engineering roles found:
{job_block}

Website frontend/infra signals detected (may be empty or 'unknown'): {website_sig or "unknown"}
GitHub languages detected (may be empty or 'unknown'): {github_langs or "unknown"}

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
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        if r.status_code != 200:
            return {"tech_stack": "", "engineer_types": "", "seniority_mix": "",
                     "confidence": "", "notes": f"API error {r.status_code}"}
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = text.strip().strip("`")
        if text.lower().startswith("json"):
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
    "Upload your Airscale/Clay CSV. Checks 75+ ATS platforms (via Apify/Jobo) for open "
    "engineering roles, falls back to reading the careers page with Claude if none of "
    "those match, and can classify tech stack, engineer types, and seniority mix."
)

api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
apify_token = st.secrets.get("APIFY_API_TOKEN", "")

with st.sidebar:
    st.header("Settings")
    if not apify_token:
        st.error("No APIFY_API_TOKEN found in app secrets. Ask an admin to add it before running.")
    do_classify = st.toggle(
        "Classify with Claude (costs ~$0.01–0.03/company)",
        value=False,
        help="Also enables the careers-page fallback for companies with no ATS match.",
    )
    if do_classify and not api_key:
        st.error("No ANTHROPIC_API_KEY found in app secrets. Ask an admin to add it before enabling this.")
    remote_only = st.toggle(
        "Only count remote roles",
        value=False,
        help="Filters to roles explicitly marked remote. Roles with unknown remote status are excluded when this is on.",
    )
    check_website = st.toggle("Fingerprint homepage (free)", value=True,
                               help="Scans the homepage HTML for frontend/infra signatures like React, WordPress, Shopify, etc.")
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

    run = st.button("Run enrichment", type="primary", disabled=not apify_token)

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

            ats_name, jobs = detect_ats_jobs(apify_token, domain)
            source = ats_name

            if not jobs and do_classify and api_key:
                careers_url = find_careers_url(domain)
                if careers_url:
                    page_text = fetch_page_text(careers_url)
                    if page_text:
                        jobs = claude_extract_jobs_from_page(api_key, company_name, page_text)
                        if jobs:
                            source = "Careers page (Claude)"

            eng_jobs = filter_engineering(jobs)
            eng_jobs = filter_remote(eng_jobs, remote_only)

            website_sig = website_signals(domain) if check_website else ""
            github_langs = try_github_languages(company_name, domain) if check_github else None

            row_result = {
                "ats_source": source or "",
                "open_engineering_jobs": len(eng_jobs),
                "engineering_job_titles": "; ".join(j["title"] for j in eng_jobs[:10]),
                "website_signals": website_sig,
                "github_languages": github_langs or "",
            }

            if do_classify and api_key:
                row_result.update(
                    classify_with_claude(api_key, company_name, domain, eng_jobs, website_sig, github_langs)
                )

            results.append(row_result)
            progress.progress((i + 1) / n)

        status.text("Done.")

        results_df = pd.DataFrame(results)
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
