import json

import pandas as pd

INPUT_FILE = "../data/BasicCompanyDataAsOneFile-2026-11-01.csv"
OUTPUT_FILE = "../data/sample_companies.csv"
RANDOM_SEED = 42

USE_COLS = {
    "CompanyNumber",
    "CompanyName",
    "CompanyStatus",
    "Accounts.AccountCategory",
    "IncorporationDate",
    "RegAddress.AddressLine1",
    "RegAddress.PostTown",
    "RegAddress.PostCode",
    "SICCode.SicText_1",
}

# Target sample size per stratum
TARGET_COUNTS = {
    "LARGE_MEDIUM": 100,
    "SMALL": 120,
    "MICRO_CONTROL": 80,
}

# Map Companies House account categories into 3 representative strata
CATEGORY_TO_STRATUM = {
    "FULL": "LARGE_MEDIUM",
    "MEDIUM": "LARGE_MEDIUM",
    "GROUP": "LARGE_MEDIUM",
    "SMALL": "SMALL",
    "TOTAL EXEMPTION FULL": "SMALL",
    "TOTAL EXEMPTION SMALL": "SMALL",
    "UNAUDITED ABRIDGED": "SMALL",
    "MICRO ENTITY": "MICRO_CONTROL",
    "DORMANT": "MICRO_CONTROL",
}

sampled_chunks = []
population_counts = {stratum: 0 for stratum in TARGET_COUNTS}

# Read the ~2.5 GB file in chunks, loading only the required columns
reader = pd.read_csv(
    INPUT_FILE,
    usecols=lambda col: col.strip() in USE_COLS,
    dtype=str,
    chunksize=200_000,
)

for i, chunk in enumerate(reader):
    chunk.columns = chunk.columns.str.strip()

    # Keep only Active companies
    chunk = chunk[chunk["CompanyStatus"].str.strip() == "Active"].copy()

    # Assign each company to a stratum
    categories = chunk["Accounts.AccountCategory"].str.strip().str.upper()
    chunk["stratum"] = categories.map(CATEGORY_TO_STRATUM)
    chunk = chunk.dropna(subset=["stratum"])

    # Track total UK active population per stratum (useful for README reporting)
    for stratum, count in chunk["stratum"].value_counts().items():
        population_counts[stratum] += int(count)

    # Keep a 5% random pool from each chunk to stay memory-efficient
    sampled_chunks.append(chunk.sample(frac=0.05, random_state=RANDOM_SEED + i))

# Combine pools and draw the exact target count per stratum
pool_df = pd.concat(sampled_chunks, ignore_index=True)

selected_groups = []
for stratum, target_n in TARGET_COUNTS.items():
    group_df = pool_df[pool_df["stratum"] == stratum]
    selected_groups.append(
        group_df.sample(n=min(target_n, len(group_df)), random_state=RANDOM_SEED)
    )

sample_df = pd.concat(selected_groups, ignore_index=True)
sample_df = sample_df.drop(columns=["CompanyStatus"])

# Strip whitespace from all cell values
for col in sample_df.columns:
    sample_df[col] = sample_df[col].fillna("").str.strip()

sample_df.to_csv(OUTPUT_FILE, index=False)

# CHANGED: salvăm populația pe straturi. Fără ea nu poți pondera estimatul final.
with open("../data/population_counts.json", "w") as f:
    json.dump(population_counts, f, indent=2)
print("Populație pe straturi:", population_counts)