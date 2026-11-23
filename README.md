# UK VAT Identifier Discovery

## What this project is about

The goal of this task is to figure out how to find and verify UK VAT numbers for active UK companies, starting only from the official **Companies House Basic Company Data** (`~5.17M` rows). That file gives us company numbers, names, registered addresses, and account categories, but **no VAT numbers and no website URLs**.

I split the work into three parts:
1. Checking public data sources and seeing which ones actually work.
2. Building a working Proof of Concept (PoC) in Python on a stratified sample of 300 active companies.
3. Looking at what breaks when scaling this to all 5M+ UK companies, plus how it would work outside the UK.

---

## 1. Source Research & Dead Ends

There is no public bulk file that maps Companies House numbers to HMRC VAT numbers. To get a reliable VAT number, you have to **find a candidate** somewhere on the web and then **verify it** against HMRC.

Here is what I tested:

| Source | What happened | Notes |
|---|---|---|
| **Companies House Bulk CSV** | Starting point only | Has `CompanyNumber`, `CompanyName`, `RegAddress`, `Accounts.AccountCategory`. No VAT numbers, no websites. |
| **HMRC VAT Check API (JSON)** | Blocked for quick PoC | Requires OAuth2 app registration through HMRC Developer Hub, so it can't be called anonymously. |
| **HMRC Public Web Checker** | Works, but rate-limited | `tax.service.gov.uk/check-vat-number/enter-vat-details` returns the registered entity name and address via form POST + CSRF token. Gives `HTTP 429` after ~6 fast requests, or after ~40–45 requests when spaced ~6–9s apart. |

---

## 2. Proof of Concept (PoC)

### Sampling (`src/sampler.py`)
A random sample from Companies House is full of dormant or tiny companies that sit below the UK VAT threshold (£90,000 turnover) and don't even have a website. To get a fair picture, I took a stratified sample of **300 active companies** (`data/sample_companies.csv`) based on `Accounts.AccountCategory`:

- **`LARGE_MEDIUM`** (`FULL`, `GROUP`, `MEDIUM`, `AUDIT EXEMPTION SUBSIDIARY`) — very likely to be VAT-registered.
- **`SMALL`** (`SMALL`, `TOTAL EXEMPTION FULL`, `TOTAL EXEMPTION SMALL`) — mixed.
- **`MICRO_CONTROL`** (`MICRO ENTITY`, `DORMANT`, `NO ACCOUNTS FILED`) — control group, low chance of having a VAT number.

### Pipeline Design
When I first ran HMRC checks directly inside the crawler, the script kept stalling on `HTTP 429`. Splitting discovery and verification into two separate scripts with resume support fixed that completely:

1. **Stage 1 — Discovery (`src/discover.py` → `data/candidates.csv`)**
   - Strips legal suffixes (`LIMITED`, `LTD`, punctuation) and searches using company name + town to find up to 3 candidate websites (ignoring registries, directories, and social media).
   - Crawls the homepage plus internal pages (`/about`, `/contact`, `/terms`, `/privacy`, and relevant links found on the homepage), checking both page text and `application/ld+json` blocks.
   - Checks if the company number or postcode appears on the site to confirm we're on the right website.
   - Pulls 9-digit VAT candidates using regex (`VAT ...` or `GB ...`, including trimming `000` off 12-digit EORI numbers) and filters them immediately with HMRC's **Mod-97** and **9755 (post-2010)** checksum rules.
   - Skips pages that contain more than 5 VAT numbers (`LIST_PAGE_SKIPPED`) so we don't scrape random directories.

2. **Stage 2 — Verification & Matching (`src/verify.py` + `src/hmrc.py` → `data/poc_results.csv`)**
   - Checks up to 2 candidates per company against HMRC's web checker with a ~6–9s delay between calls. If it hits `429`, it retries after 30s/60s or stops cleanly so it can resume later without losing progress.
   - Normalizes both Companies House and HMRC names (including weird HMRC spacing like `LIMI TED`) and compares name similarity (`SequenceMatcher`), postcode match, and shared brand words.
   - Outputs one of five verdicts: `HIGH_CONFIDENCE`, `POSSIBLE_VAT_GROUP`, `MEDIUM_REVIEW`, `NO_MATCH`, or `UNVERIFIED`.

### Results (`data/poc_results.csv`)

Out of the 300 stratified companies, **174 were verified** (`100 LARGE_MEDIUM`, `54 SMALL`, `20 MICRO_CONTROL`). I didn't get to run all 300 because HMRC's public web checker enforces strict rate limits (`HTTP 429`) and requires long cooldown pauses (~30–90s retries on top of the 6–9s delay per request), leading to frequent timeouts during full-batch runs. Because `src/verify.py` saves progress after every row, the remaining 126 companies can be resumed at any time.

| Stratum | Verified Sample | High Confidence | Possible VAT Group | Medium Review | No Match (Rejected) | Not Found | Verified Match Rate (`High + Group`) |
|---|---|---|---|---|---|---|---|
| `LARGE_MEDIUM` | 100 | 26 | 9 | 0 | 6 | 59 | **35.0%** (35/100) |
| `SMALL` | 54 | 15 | 2 | 1 | 3 | 33 | **31.5%** (17/54) |
| `MICRO_CONTROL` | 20 | 1 | 1 | 2 | 2 | 14 | **10.0%** (2/20) |
| **Total** | **174** | **42** | **12** | **3** | **11** | **106** | **31.0%** (54/174) |

#### Key Takeaways from the Numbers
- **Clear drop across company size strata:** Confirmed matches drop from **35.0%** in `LARGE_MEDIUM` and **31.5%** in `SMALL` down to **10.0%** in `MICRO_CONTROL` (where most entities are below the £90,000 VAT threshold or dormant).
- **Entity matching blocked 11 false positives (`NO_MATCH`):** Out of 68 candidates that were valid UK VAT numbers in HMRC, **11 (16.2%) belonged to a different company** (e.g., `RIGHTMOVE PLC` scraped from a property listing on `WELLINGTON REAL ESTATE LIMITED`, `DPA COMMUNICATIONS LTD` for `IP TELECOM LIMITED`, and `GBAF PUBLICATIONS LIMITED` for `EXITUS HOLDING 2 LIMITED`).
- **First-party websites vs. search snippets:** 9 confirmed matches came directly from the company's own website (all in `LARGE_MEDIUM`, such as `COSTAIN LIMITED`, `KANE INTERNATIONAL LIMITED`, `ARVILLE TEXTILES LIMITED`), while 45 came from search snippets (mostly VAT directory pages) and were then validated against HMRC's official registry.
- **HMRC legacy formatting quirks:** HMRC's mainframe data occasionally splits words mid-token (for example, `COSTAIN LIMITED` returned `COSTAIN ENGINEERING & CONSTR UCTION LIMI TED`), which required explicit normalization rules before scoring name similarity.

### Real Edge Cases Found During the Run
- **False positives caught by entity matching:** Without checking the HMRC name against Companies House, valid VAT numbers from unrelated companies would have slipped in. Examples caught as `NO_MATCH`:
  - `WELLINGTON REAL ESTATE LIMITED` → `918501823` (`RIGHTMOVE PLC`, picked up from a property listing)
  - `IP TELECOM LIMITED` → `274982162` (`DPA COMMUNICATIONS LTD`)
  - `ULSTER SUPPORTED EMPLOYMENT LIMITED` → `793399566` (`JLR SUPPLIES LTD`)
  - `OGM MOULDING LIMITED` → `282211332` (`OGM (SW) LIMITED`)
- **VAT groups and subsidiaries (`POSSIBLE_VAT_GROUP`):** In the UK, companies in the same corporate group often share one representative VAT number. Exact name matching would wrongly reject these. Good examples found:
  - `EVERLAST (GROUP) LIMITED` → `859241208` (`EVERLAST SCAFFOLDING LIMITED`)
  - `RENEWI EUROPEAN HOLDINGS LIMITED` → `259547713` (`RENEWI LIMITED`)
  - `HORNSEA THREE HOLDINGS LIMITED` → `408157307` (`HORNSEA 1 LIMITED`)
  - `APPLEBRIDGE CONSTRUCTION LIMITED` → `446744371` (`APPLEBRIDGE GEOENVIRONMENTAL LTD`)
- **Joint Venture trap:** For `COSTAIN LIMITED`, search snippets returned `934968081` (`COSTAIN SKANSKA A14 JV`), which is a separate tax entity for a highway project, while Costain's actual VAT (`235983531`) is on their `/about-us` page. That's why crawling the official site always takes priority over search snippets.
- **Generic holding-company words (audit finding):** During review, I noticed two false positives inside `POSSIBLE_VAT_GROUP`: `MAXTON TOPCO LIMITED` matched `MC TOPCO LIMITED`, and `BLACKWOOD NEWCO LIMITED` matched `CD NEWCO LIMITED`. That happened because `TOPCO` and `NEWCO` weren't in the generic stop-words list. Adding UK SPV terms (`TOPCO`, `NEWCO`, `HOLDCO`, `BIDCO`) to `GENERIC` fixes this.

---

## 3. Scaling to 5M+ UK Companies

### What Breaks First
1. **HMRC Rate Limits (`HTTP 429`):** The public web form is the first thing that breaks. At 5M+ scale, you need the **official HMRC Check a UK VAT Number API (OAuth2)**, deduplication before querying (many subsidiaries share one VAT number), and a cache (e.g., 90-day TTL).
2. **Search API Costs:** Running live SERP queries for 5.17M companies is expensive and mostly wasted, since a huge chunk of Companies House is dormant or micro-entities without websites.
3. **Live Crawling Issues:** Bot blocks (`403`/`503`), JS-heavy sites (`LOW_TEXT_JS_SITE`), and slow servers make live scraping fragile at scale.

### How to Build It in Production
- **Filter first:** Skip dormant/dissolved companies and prioritize `LARGE_MEDIUM` and active `SMALL` companies.
- **Use Common Crawl before live search:** Scan Common Crawl pages (`.co.uk`, `.uk`, `.com`) that contain a UK company number or postcode alongside a checksum-valid VAT number. Only spend money on SERP APIs for high-value companies that weren't found in Common Crawl.
- **Model VAT as many-to-one:** Don't assume one VAT = one company. Use Companies House PSC (Persons with Significant Control) and group filings to confirm when multiple entities belong to the same VAT group.
- **Keep it fresh:** Re-check active VAT numbers periodically via the HMRC API to catch deregistrations or acquisitions.

---

## 4. Trade-offs & Beyond the UK

### Why Brute-Forcing HMRC Doesn't Work
Even though the Mod-97/9755 checksum drops ~98% of invalid 9-digit numbers, there are still roughly **10 million** checksum-valid combinations. Probing HMRC for all of them is impossible because of strict rate limits (`HTTP 429` on the web form, quotas on OAuth2) and violates HMRC's Terms of Service. The checksum is only useful as a fast local filter on scraped text.

### Outside the UK
- **EU (VIES):** Works similarly to HMRC — you can only verify a VAT number you already have, and some countries (like Germany) restrict returning the company name.
- **Countries with open registries:** Several countries actually publish direct, free company-ID-to-VAT mappings (for example, **Romania via ANAF**, **France via INSEE Sirene**, **Poland's White List**, **Belgium's KBO/BCE**). A multi-country pipeline should use those official APIs directly where they exist, and only fall back to web discovery + verification for closed systems like the UK.

---

## How to Run

```bash
pip install -r requirements.txt

# 1. Create the stratified sample from Companies House CSV
python3 src/sampler.py

# 2. Run Stage 1 (web discovery + checksum filtering)
python3 -u src/discover.py

# 3. Run Stage 2 (HMRC verification + entity matching)
python3 -u src/verify.py