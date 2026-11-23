import re
import time
from difflib import SequenceMatcher
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HMRC_WEB_URL = "https://www.tax.service.gov.uk/check-vat-number/enter-vat-details"
LEGAL_SUFFIXES = {"LIMITED", "LTD", "PLC", "LLP", "UK", "THE", "CO", "COMPANY"}
GENERIC_WORDS = {
    "AND", "GROUP", "HOLDINGS", "SERVICES", "SOLUTIONS",
    "MANAGEMENT", "INTERNATIONAL", "GLOBAL", "TRADING",
}
UK_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE)


def clean_vat(raw_vat: str) -> str:
    """Keep only digits from a VAT string (e.g. 'GB 220 4302 31' -> '220430231')."""
    digits = re.sub(r"\D", "", str(raw_vat))
    return digits if len(digits) == 9 else ""


def is_valid_checksum(vat: str) -> bool:
    """Validate a 9-digit UK VAT number using HMRC's Mod-97 and 9755 rules."""
    vat = clean_vat(vat)
    if not vat or vat == "000000000":
        return False

    weights = [8, 7, 6, 5, 4, 3, 2]
    weighted_sum = sum(int(d) * w for d, w in zip(vat[:7], weights))
    check_digits = int(vat[7:])

    standard_ok = (weighted_sum + check_digits) % 97 == 0
    post_2010_ok = (weighted_sum + 55 + check_digits) % 97 == 0
    return standard_ok or post_2010_ok


def extract_postcode(text: str) -> str:
    """Pull a UK postcode from an address string."""
    match = UK_POSTCODE_RE.search(text or "")
    return match.group(0).upper() if match else ""

def lookup_hmrc(vat: str, delay: float = 6.0, max_retries: int = 3) -> dict:
    """Query HMRC's public web VAT checker with automatic retry on HTTP 429."""
    vat = clean_vat(vat)
    if not is_valid_checksum(vat):
        return {"status": "INVALID_CHECKSUM", "hmrc_name": "", "hmrc_postcode": ""}

    for attempt in range(max_retries): 
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; VATResearchPoC/1.0)"})

        try:
            get_resp = session.get(HMRC_WEB_URL, timeout=10)
            if get_resp.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"    [HMRC 429 on GET] Waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            if get_resp.status_code != 200:
                return {"status": f"GET_HTTP_{get_resp.status_code}", "hmrc_name": "", "hmrc_postcode": ""}

            soup = BeautifulSoup(get_resp.text, "html.parser")
            form = soup.find("form")
            if not form:
                return {"status": "FORM_NOT_FOUND", "hmrc_name": "", "hmrc_postcode": ""}

            payload = {}
            vat_filled = False
            for inp in form.find_all("input"):
                name = inp.get("name")
                if not name:
                    continue
                inp_type = (inp.get("type") or "text").lower()
                if inp_type == "hidden":
                    payload[name] = inp.get("value", "")
                elif inp_type == "radio":
                    if (inp.get("value") or "").lower() in ("false", "no"):
                        payload[name] = inp.get("value")
                elif inp_type in ("text", "search") and not vat_filled:
                    payload[name] = vat
                    vat_filled = True

            post_url = urljoin(HMRC_WEB_URL, form.get("action", ""))
            post_resp = session.post(post_url, data=payload, timeout=10)
            time.sleep(delay)

            if post_resp.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"    [HMRC 429 on POST] Waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            if post_resp.status_code != 200:
                return {"status": f"POST_HTTP_{post_resp.status_code}", "hmrc_name": "", "hmrc_postcode": ""}
        except requests.RequestException as err:
            return {"status": f"NETWORK_ERROR: {err}", "hmrc_name": "", "hmrc_postcode": ""}

        res_soup = BeautifulSoup(post_resp.text, "html.parser")
        page_text = res_soup.get_text(" ", strip=True)
        lower_text = page_text.lower()

        if "not a valid uk vat number" in lower_text or "does not match" in lower_text:
            return {"status": "NOT_FOUND", "hmrc_name": "", "hmrc_postcode": ""}

        hmrc_name = ""
        hmrc_address = ""

        for dt, dd in zip(res_soup.find_all("dt"), res_soup.find_all("dd")):
            label = dt.get_text(" ", strip=True).lower()
            value = dd.get_text(" ", strip=True)
            if "name" in label and not hmrc_name:
                hmrc_name = value
            elif "address" in label and not hmrc_address:
                hmrc_address = value

        if not hmrc_name:
            for heading in res_soup.find_all(["h2", "h3"]):
                label = heading.get_text(" ", strip=True).lower()
                sibling = heading.find_next_sibling(["p", "div"])
                if not sibling:
                    continue
                value = sibling.get_text(" ", strip=True)
                if "name" in label and not hmrc_name:
                    hmrc_name = value
                elif "address" in label and not hmrc_address:
                    hmrc_address = value

        if hmrc_name:
            return {
                "status": "VALID",
                "hmrc_name": hmrc_name,
                "hmrc_postcode": extract_postcode(hmrc_address),
            }

        return {
            "status": "PARSE_ERROR",
            "hmrc_name": "",
            "hmrc_postcode": "",
            "debug_snippet": page_text[:250],
        }

    return {"status": "RATE_LIMITED", "hmrc_name": "", "hmrc_postcode": ""}


def normalize_name(name: str) -> str:
    """Uppercase, fix HMRC split suffixes, remove punctuation and legal suffixes."""
    text = str(name).upper().replace("&", " AND ")
    # Fix broken mainframe spacing occasionally returned by HMRC (e.g. 'LIMI TED')
    text = re.sub(r"\bLIMI\s+TED\b", "LIMITED", text)
    text = re.sub(r"[^A-Z0-9\s]", " ", text)
    words = [w for w in text.split() if w not in LEGAL_SUFFIXES]
    return " ".join(words)

def core_tokens(normalized_name: str) -> set:
    """Keep only meaningful brand words (ignore generic words and tiny tokens)."""
    return {w for w in normalized_name.split() if w not in GENERIC_WORDS and len(w) >= 3}

def normalize_postcode(postcode: str) -> str:
    """Remove spaces and uppercase (e.g. 'al7 1ga' -> 'AL71GA')."""
    return re.sub(r"\s+", "", str(postcode).upper())

def match_company(ch_name: str, ch_postcode: str, hmrc_name: str, hmrc_postcode: str) -> dict:
    """Compare Companies House record with HMRC response."""
    norm_ch = normalize_name(ch_name)
    norm_hmrc = normalize_name(hmrc_name)

    name_score = SequenceMatcher(None, norm_ch, norm_hmrc).ratio() if (norm_ch and norm_hmrc) else 0.0
    pc_ch = normalize_postcode(ch_postcode)
    pc_hmrc = normalize_postcode(hmrc_postcode)
    postcode_match = bool(pc_ch and pc_hmrc and pc_ch == pc_hmrc)

    tokens_ch = core_tokens(norm_ch)
    tokens_hmrc = core_tokens(norm_hmrc)
    shared_tokens = tokens_ch & tokens_hmrc

    # Check if one company's brand words are fully contained in the other's
    # (e.g. 'WESTGATE SECURITY' inside 'WESTGATE ROTHERHAM SECURITY')
    is_subset = False
    if tokens_ch and tokens_hmrc:
        smaller = tokens_ch if len(tokens_ch) <= len(tokens_hmrc) else tokens_hmrc
        larger = tokens_hmrc if len(tokens_ch) <= len(tokens_hmrc) else tokens_ch
        if smaller.issubset(larger) and (len(smaller) >= 2 or max(len(w) for w in smaller) >= 4):
            is_subset = True

    if name_score >= 0.90 or (name_score >= 0.75 and postcode_match):
        verdict = "HIGH_CONFIDENCE"
    elif is_subset or (postcode_match and len(shared_tokens) >= 1):
        verdict = "POSSIBLE_VAT_GROUP"
    elif name_score >= 0.60 or postcode_match:
        verdict = "MEDIUM_REVIEW"
    else:
        verdict = "NO_MATCH"

    return {
        "verdict": verdict,
        "name_score": round(name_score, 2),
        "postcode_match": postcode_match,
    }


def verify_and_match(vat: str, ch_name: str, ch_postcode: str) -> dict:
    """One-stop function: checksum -> HMRC check -> entity matching."""
    hmrc = lookup_hmrc(vat)
    if hmrc["status"] != "VALID":
        return {**hmrc, "verdict": "UNVERIFIED", "name_score": 0.0, "postcode_match": False}

    match = match_company(ch_name, ch_postcode, hmrc["hmrc_name"], hmrc["hmrc_postcode"])
    return {**hmrc, **match}
