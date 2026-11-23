import json
import time
from pathlib import Path

import pandas as pd

from hmrc import verify_and_match

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = PROJECT_ROOT / "data" / "candidates.csv"
OUTPUT_FILE = PROJECT_ROOT / "data" / "poc_results.csv"

MAX_CANDIDATES_PER_COMPANY = 2
EXTRA_HMRC_DELAY = 3.5 
VERDICT_RANK = {
    "HIGH_CONFIDENCE": 4,
    "POSSIBLE_VAT_GROUP": 3,
    "MEDIUM_REVIEW": 2,
    "NO_MATCH": 1,
    "UNVERIFIED": 0,
}


def verify_row(row: pd.Series) -> tuple:
    name = str(row["company_name"]).strip()
    postcode = str(row["postcode"]).strip()
    candidates = json.loads(row["candidates_json"]) if row["candidates_json"] else []

    best = None
    rejected_valid_vats = []

    for cand in candidates[:MAX_CANDIDATES_PER_COMPANY]:
        vat = cand["vat"]
        source_type = cand["source_type"]
        source_url = cand["source_url"]

        hmrc_res = verify_and_match(vat, name, postcode)
        if not hmrc_res.get("cached"): 
            time.sleep(EXTRA_HMRC_DELAY)

        if hmrc_res["status"] == "RATE_LIMITED":
            return {}, True

        if hmrc_res["status"] == "VALID" and hmrc_res["verdict"] == "NO_MATCH":
            rejected_valid_vats.append(f"{vat} ({hmrc_res['hmrc_name']})")

        rank = VERDICT_RANK.get(hmrc_res["verdict"], 0)
        is_first_party = 1 if source_type == "COMPANY_WEBSITE" else 0
        score_tuple = (rank, is_first_party, hmrc_res["name_score"])

        if best is None or score_tuple > best["score_tuple"]:
            best = {
                "score_tuple": score_tuple,
                "vat": vat,
                "source_type": source_type,
                "source_url": source_url,
                "hmrc": hmrc_res,
            }

        # Stop early if we already confirmed a high-confidence match
        if hmrc_res["verdict"] == "HIGH_CONFIDENCE":
            break

    record = {
        "stratum": row["stratum"],
        "company_number": row["company_number"],
        "company_name": name,
        "postcode": postcode,
        "website_found": row["website_found"],
        "company_num_on_site": row["company_num_on_site"],
        "vat_number": best["vat"] if best else "",
        "source_type": best["source_type"] if best else "NONE",
        "source_url": best["source_url"] if best else "",
        "hmrc_status": best["hmrc"]["status"] if best else "NO_CANDIDATE",
        "hmrc_name": best["hmrc"]["hmrc_name"] if best else "",
        "hmrc_postcode": best["hmrc"]["hmrc_postcode"] if best else "",
        "verdict": best["hmrc"]["verdict"] if best else "NOT_FOUND",
        "name_score": best["hmrc"]["name_score"] if best else 0.0,
        "postcode_match": best["hmrc"]["postcode_match"] if best else False,
        "rejected_valid_vats": " | ".join(rejected_valid_vats),
        "crawl_note": row["crawl_note"],
    }
    return record, False


def main():
    df = pd.read_csv(INPUT_FILE, dtype=str).fillna("")
    print(f"VERIFICATION MODE: Loaded {len(df)} companies from {INPUT_FILE}")

    results = []
    done = set()
    if OUTPUT_FILE.exists():
        existing = pd.read_csv(OUTPUT_FILE, dtype=str).fillna("")
        results = existing.to_dict("records")
        done = set(existing["company_number"])
        print(f"RESUME: Skipping {len(done)} already verified companies.")

    for idx, row in df.iterrows():
        comp_num = str(row["company_number"]).strip()
        if comp_num in done:
            continue

        cand_count = int(row["candidate_count"] or 0)
        if cand_count == 0:
            rec, _ = verify_row(row)
            results.append(rec)
            done.add(comp_num)
            pd.DataFrame(results).to_csv(OUTPUT_FILE, index=False)
            continue

        print(f"[{idx + 1}/{len(df)}] Verifying {row['company_name']} ({cand_count} cand.)...")
        rec, rate_limited = verify_row(row)

        if rate_limited:
            print("\n[STOPPED] HMRC rate limit still active. Progress saved.")
            print("Wait 2-3 minutes and re-run `python3 src/verify.py` to resume from here.")
            break

        results.append(rec)
        done.add(comp_num)
        print(f"    -> {rec['vat_number']} | {rec['verdict']} | HMRC: {rec['hmrc_name']}")
        pd.DataFrame(results).to_csv(OUTPUT_FILE, index=False)

    if results:
        out_df = pd.DataFrame(results)
        print(f"\nSaved {len(out_df)}/{len(df)} verified rows to {OUTPUT_FILE}\n")
        print("Verdict breakdown by stratum:")
        print(pd.crosstab(out_df["stratum"], out_df["verdict"]))


if __name__ == "__main__":
    main()