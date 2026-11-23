import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from hmrc import clean_vat, is_valid_checksum, normalize_name

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INPUT_FILE = DATA_DIR / "sample_companies.csv"
OUTPUT_FILE = DATA_DIR / "candidates.csv"

TEST_PER_STRATUM = None
SERPER_KEY = os.getenv("SERPER_API_KEY", "").strip()
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"}

KEY_PATHS = ["", "/about", "/about-us", "/contact", "/contact-us", "/terms", "/privacy"]
LINK_WORDS = ("contact", "terms", "legal", "privacy", "imprint", "about", "conditions", "policy", "delivery", "returns")

IGNORED_DOMAINS = {
    "gov.uk", "companieshouse.gov.uk", "endole.co.uk", "companycheck.co.uk",
    "checkcompany.co.uk", "pomanda.com", "opencorporates.com", "bizdb.co.uk",
    "dnb.com", "bloomberg.com", "yell.com", "trustpilot.com", "yelp.co.uk",
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "wikipedia.org", "vatsearch.co.uk", "vat-lookup.co.uk",
    "ukvat.co.uk", "globaldatabase.com", "crunchbase.com", "indeed.com",
    "corpium.co.uk", "vat-search.co.uk", "companieslist.co.uk", "opengovuk.com",
    "192.com", "kompass.com", "zoominfo.com", "glassdoor.co.uk",
    "companiesintheuk.co.uk", "find-and-update.company-information.service.gov.uk",
}

VAT_RE = re.compile(r"\bVAT[^0-9]{0,35}(?:GB[\s-]*)?((?:\d[\s-]?){8}\d)(?!\d)", re.IGNORECASE)
GB_RE = re.compile(r"\bGB[\s-]*((?:\d[\s-]?){8}\d)(?:000)?(?!\d)", re.IGNORECASE)


def short_name(name):
    s = re.sub(r"[().,]", " ", name)
    s = re.sub(r"\b(LIMITED|LTD|PLC|LLP)\b", "", s, flags=re.IGNORECASE)
    return " ".join(s.split())


def find_vats(text):
    if not text:
        return []
    out = []
    for raw in VAT_RE.findall(text) + GB_RE.findall(text):
        vat = clean_vat(raw)
        if vat and is_valid_checksum(vat) and vat not in out:
            out.append(vat)
    return out


def is_ignored(url):
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in IGNORED_DOMAINS)


def search_web(query, max_results=5):
    if SERPER_KEY:
        try:
            r = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
                json={"q": query, "gl": "gb", "num": max_results},
                timeout=10,
            )
            return [
                {"title": x.get("title", ""), "body": x.get("snippet", ""), "href": x.get("link", "")}
                for x in r.json().get("organic", [])
            ], True
        except Exception:
            return [], False

    for backend in ("yahoo", "bing", "brave", "duckduckgo"):
        try:
            with DDGS() as ddgs:
                res = list(ddgs.text(query, region="uk-en", backend=backend, max_results=max_results))
            if res:
                time.sleep(1.5)
                return res, True
        except Exception:
            continue

    time.sleep(1.5)
    return [], False


def pick_sites(results, limit=3):
    sites = []
    for r in results:
        url = r.get("href") or r.get("url") or ""
        if url.startswith("http") and not is_ignored(url):
            p = urlparse(url)
            base = f"{p.scheme}://{p.netloc}"
            if base not in sites:
                sites.append(base)
        if len(sites) >= limit:
            break
    return sites


def get_internal_links(soup, base_url):
    host = urlparse(base_url).netloc.lower().removeprefix("www.")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if urlparse(url).netloc.lower().removeprefix("www.") != host:
            continue
        if url.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".gif", ".zip")):
            continue
        label = (urlparse(url).path + " " + a.get_text(" ", strip=True)).lower()
        if any(w in label for w in LINK_WORDS) and url not in links:
            links.append(url)
    return links[:8]


def crawl_site(base_url, comp_num, postcode=""):
    found, seen, visited = [], set(), set()
    confirmed = False
    note = ""
    num_clean = comp_num.lstrip("0")
    pc_clean = re.sub(r"\s+", "", postcode).upper()

    queue = [base_url + "/"]
    while queue and len(visited) < 9:
        url = queue.pop(0)
        key = url.rstrip("/")
        if key in visited:
            continue
        visited.add(key)
        first = len(visited) == 1

        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
        except requests.RequestException:
            if first:
                return [], False, "SITE_UNREACHABLE"
            continue

        if r.status_code in (403, 429, 503):
            if first:
                return [], False, f"BLOCKED_HTTP_{r.status_code}"
            continue
        if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", "").lower():
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        jsonld = " ".join(s.string or "" for s in soup.find_all("script", type="application/ld+json"))
        text = soup.get_text(" ", strip=True) + " " + jsonld

        if first:
            if len(text) < 300:
                note = "LOW_TEXT_JS_SITE"
            queue += get_internal_links(soup, url) + [urljoin(base_url, p) for p in KEY_PATHS[1:]]

        if comp_num and (comp_num in text or (len(num_clean) >= 6 and num_clean in text)):
            confirmed = True
        elif len(pc_clean) >= 5 and pc_clean in re.sub(r"\s+", "", text).upper():
            confirmed = True

        page_vats = find_vats(text)
        if len(page_vats) > 5:
            note = (note + " LIST_PAGE_SKIPPED").strip()
            continue

        for vat in page_vats:
            if vat not in seen:
                seen.add(vat)
                found.append((vat, url))

        if found:
            break

    return found, confirmed, note


def discover_company(row):
    name = row["CompanyName"].strip()
    comp_num = row["CompanyNumber"].strip()
    postcode = row["RegAddress.PostCode"].strip()
    town = row.get("RegAddress.PostTown", "").strip()
    q_name = short_name(name)

    site_res, ok1 = search_web(f'"{q_name}" {town}'.strip(), max_results=8)
    sites = pick_sites(site_res)
    ok = ok1
    if not sites:
        fb_res, ok2 = search_web(f"{q_name} UK", max_results=8)
        sites = pick_sites(fb_res)
        ok = ok1 or ok2

    vat_res, _ = search_web(f"{q_name} UK VAT", max_results=5)

    candidates, seen = [], set()
    website, confirmed, note = "", False, ""

    if sites:
        chosen = None
        for base in sites:
            vats, on_site, n = crawl_site(base, comp_num, postcode)
            if chosen is None or (on_site and not chosen[2]):
                chosen = (base, vats, on_site, n)
            if on_site:
                break
        website, site_vats, confirmed, note = chosen
        for vat, page_url in site_vats:
            candidates.append({"vat": vat, "source_type": "COMPANY_WEBSITE", "source_url": page_url})
            seen.add(vat)
        if not confirmed:
            note = (note + " SITE_NOT_CONFIRMED").strip()
    else:
        note = "NO_WEBSITE_FOUND" if ok else "SEARCH_FAILED"

    name_key = normalize_name(name)
    for r in vat_res:
        snippet = f"{r.get('title', '')} {r.get('body', '')}"
        if name_key and name_key not in normalize_name(snippet):
            continue
        url = r.get("href") or r.get("url") or ""
        for vat in find_vats(snippet):
            if vat not in seen:
                candidates.append({"vat": vat, "source_type": "SEARCH_SNIPPET", "source_url": url})
                seen.add(vat)

    return {
        "stratum": row["stratum"].strip(),
        "company_number": comp_num,
        "company_name": name,
        "postcode": postcode,
        "website_found": website,
        "company_num_on_site": confirmed,
        "candidate_count": len(candidates),
        "candidates_json": json.dumps(candidates),
        "crawl_note": note,
    }


def main():
    df = pd.read_csv(INPUT_FILE, dtype=str).fillna("")
    if TEST_PER_STRATUM:
        df = df.groupby("stratum", sort=False).head(TEST_PER_STRATUM).reset_index(drop=True)

    if OUTPUT_FILE.exists():
        records = pd.read_csv(OUTPUT_FILE, dtype=str).fillna("").to_dict("records")
        done = {r["company_number"] for r in records}
    else:
        records, done = [], set()

    for i, row in df.iterrows():
        comp_num = row["CompanyNumber"].strip()
        if comp_num in done:
            continue

        rec = discover_company(row)
        records.append(rec)
        done.add(comp_num)

        vats = [c["vat"] for c in json.loads(rec["candidates_json"])]
        status = f"candidates={vats}" if vats else (rec["crawl_note"] or "none")
        print(f"[{i + 1}/{len(df)}] {rec['company_name']} -> {status}")

        pd.DataFrame(records).to_csv(OUTPUT_FILE, index=False)

    out = pd.DataFrame(records)
    found = (out["candidate_count"].astype(int) > 0).sum()
    print(f"\nDone: {found}/{len(out)} with candidates.")


if __name__ == "__main__":
    main()