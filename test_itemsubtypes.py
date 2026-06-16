# test_itemsubtypes.py
"""
Discover which itemSubType values appear in the Infobase collection
by querying the WorldCat Discovery API for all OCLC numbers in
InfobaseLookup.csv and aggregating the itemSubType facet counts.

STRATEGY:
    Rather than making 64,000+ individual API calls (one per OCLC number),
    this script batches multiple OCLC numbers into a single query using OR:
        no:123456 OR no:789012 OR no:345678 ...
    Each batch requests facets=itemSubType so the API returns a breakdown
    of format subtypes across that batch. We then add up the counts from
    all batches to get a complete picture.

    We use limit=1 because we only want the facet counts, not the full
    record data. This keeps each response small and fast.

WHY THIS MATTERS:
    The current scripts filter for itemSubType=video-digital, but the
    Infobase collection may include audiobooks, interactive media, or
    web resources. This script tells us exactly what subtypes are present
    so we can decide whether to expand the filter.

OUTPUT:
    Prints a table of itemSubType values and their counts, sorted by count
    descending. Also saves results to itemsubtype_counts.csv.

HOW TO RUN:
    From the nclive root directory in your VS Code bash terminal:
        python test_itemsubtypes.py
"""

import csv
import time
from collections import defaultdict
import requests
import pandas as pd
from dotenv import load_dotenv
from config import Config
from auth import OCLCAuth

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOOKUP_FILE = "InfobaseLookup.csv"   # Relative to nclive root directory
OUTPUT_FILE = "itemsubtype_counts.csv"
NON_DIGITAL_FILE = "non_digital_ocns.csv"  # OCNs where specificFormat != "Digital"
BATCH_SIZE = 10    # OCNs per API call; keep query string under ~3000 chars
MAX_BATCHES = 6510
SLEEP_BETWEEN_CALLS = 0.5  # Seconds to wait between API calls (be a good API citizen)

# ---------------------------------------------------------------------------
# Setup: authentication and API URL
# ---------------------------------------------------------------------------

load_dotenv()
config = Config()
auth_handler = OCLCAuth()
API_URL = f"{config.oclc_base_url}/search/brief-bibs"

token = auth_handler.get_valid_token()
if not token:
    print("ERROR: Could not obtain an API token. Check your .env credentials.")
    raise SystemExit(1)

HEADERS = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# Step 1: Load unique verified OCLC numbers from InfobaseLookup.csv
# ---------------------------------------------------------------------------

print(f"Loading OCLC numbers from {LOOKUP_FILE}...")

# dtype=str prevents pandas from converting large integers to floats,
# which can corrupt OCLC numbers (e.g. 1048832628 becoming 1048832628.0)
df = pd.read_csv(LOOKUP_FILE, dtype=str)

# Keep only rows where verifiedOCN is a non-empty numeric string
ocn_series = df["verifiedOCN"].dropna()
ocn_series = ocn_series[ocn_series.str.fullmatch(r"\d+")]  # digits only
unique_ocns = sorted(ocn_series.unique())  # sort for reproducibility

print(f"Found {len(unique_ocns):,} unique verified OCLC numbers.")
print(f"Batch size: {BATCH_SIZE} OCNs per API call.")

# Calculate and display estimated number of calls
num_batches = (len(unique_ocns) + BATCH_SIZE - 1) // BATCH_SIZE  # ceiling division
print(f"Estimated API calls: {num_batches}")
print(f"Estimated time at {SLEEP_BETWEEN_CALLS}s between calls: "
      f"~{num_batches * SLEEP_BETWEEN_CALLS / 60:.1f} minutes")
print()

# ---------------------------------------------------------------------------
# Step 2: Helper to build one batch query string
# ---------------------------------------------------------------------------

def build_batch_query(ocn_batch: list) -> str:
    """
    Build a q= string that searches for any of the given OCLC numbers.

    For example, with ocn_batch = ['123', '456', '789']:
        Returns: 'no:123 OR no:456 OR no:789'

    The no: index is the OCLC number field in WorldCat Discovery API.
    """
    return " OR ".join(f"no:{ocn}" for ocn in ocn_batch)

# ---------------------------------------------------------------------------
# Step 3: Query the API in batches, collecting facet counts
# ---------------------------------------------------------------------------

# defaultdict(int) means any new key starts at 0 automatically
# so we can just do: subtype_totals["video-digital"] += count
subtype_totals = defaultdict(int)

# Track stats
batches_succeeded = 0
batches_failed = 0
total_records_found = 0

# Collect any records where specificFormat is not "Digital" for separate review.
# Each entry will be a dict with oclcNumber, title, generalFormat, specificFormat.
non_digital_records = []

print("Querying API for itemSubType facets...")
print("-" * 60)

for batch_num, start_idx in enumerate(range(0, len(unique_ocns), BATCH_SIZE), start=1):
    if batch_num > MAX_BATCHES:
        print(f"  Stopping after {MAX_BATCHES} batches (diagnostic mode).")
        break

    # Slice out this batch of OCNs
    batch = unique_ocns[start_idx : start_idx + BATCH_SIZE]
    query = build_batch_query(batch)

    params = {
        "q": query,
        "limit": 10,             # We only need facets, not full records
    }

    try:
        response = requests.get(API_URL, headers=HEADERS, params=params, timeout=20)

        if response.status_code == 401:
            # Token may have expired mid-run; refresh and retry once
            print(f"  Batch {batch_num}: Token expired, refreshing...")
            token = auth_handler.get_valid_token()
            HEADERS["Authorization"] = f"Bearer {token}"
            response = requests.get(API_URL, headers=HEADERS, params=params, timeout=20)

        if response.status_code != 200:
            print(f"  Batch {batch_num}: HTTP {response.status_code} error. Skipping.")
            try:
                err = response.json()
                print(f"    Error type:   {err.get('type', 'N/A')}")
                print(f"    Error detail: {err.get('detail', 'N/A')}")
            except ValueError:
                print(f"    Raw response: {response.text[:300]}")
            batches_failed += 1
            time.sleep(SLEEP_BETWEEN_CALLS)
            continue

        data = response.json()
        total_records_found += data.get("numberOfRecords", 0)

        # Read generalFormat + specificFormat directly from returned records.
        # Combine them as "generalFormat/specificFormat" for the tally,
        # e.g. "Video/Digital", "Book/PrintBook". This is more reliable
        # than facets, which the API doesn't return for no: queries.
        for record in data.get("briefRecords", []):
            general = record.get("generalFormat", "Unknown")
            specific = record.get("specificFormat", "Unknown")
            subtype_totals[f"{general}/{specific}"] += 1

            # Flag any record where specificFormat is not "Digital" for
            # separate review. These are unexpected in the Infobase collection
            # and may indicate cataloging anomalies or non-streaming content.
            if specific != "Digital":
                non_digital_records.append({
                    "oclcNumber": record.get("oclcNumber", ""),
                    "title": record.get("title", ""),
                    "generalFormat": general,
                    "specificFormat": specific,
                })

        batches_succeeded += 1

        # Progress indicator every 25 batches
        if batch_num % 25 == 0 or batch_num == num_batches:
            print(f"  Processed batch {batch_num}/{num_batches} "
                  f"({batch_num * BATCH_SIZE:,} OCNs so far)...")

    except requests.exceptions.Timeout:
        print(f"  Batch {batch_num}: Timeout. Skipping.")
        batches_failed += 1

    except requests.exceptions.RequestException as e:
        print(f"  Batch {batch_num}: Request error: {e}. Skipping.")
        batches_failed += 1

    time.sleep(SLEEP_BETWEEN_CALLS)

# ---------------------------------------------------------------------------
# Step 4: Print and save the results
# ---------------------------------------------------------------------------

print()
print("=" * 60)
print("RESULTS: itemSubType distribution across InfobaseLookup.csv")
print("=" * 60)
print(f"Batches succeeded: {batches_succeeded}/{num_batches}")
print(f"Total records matched across all batches: {total_records_found:,}")
print()

if not subtype_totals:
    print("No itemSubType facet data was returned. The facet may not be")
    print("available for this query type, or all batches failed.")
else:
    # Sort by count descending so most common subtypes appear first
    sorted_subtypes = sorted(subtype_totals.items(), key=lambda x: x[1], reverse=True)

    print(f"{'itemSubType':<25} {'Count':>10}")
    print("-" * 37)
    for subtype, count in sorted_subtypes:
        print(f"{subtype:<25} {count:>10,}")

    print()
    print(f"Total records with a subtype: {sum(subtype_totals.values()):,}")

    # Save to CSV
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["itemSubType", "count"])
        writer.writerows(sorted_subtypes)

    print(f"Results saved to: {OUTPUT_FILE}")

    # Save non-digital records to a separate file if any were found
    if non_digital_records:
        with open(NON_DIGITAL_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["oclcNumber", "title", "generalFormat", "specificFormat"]
            )
            writer.writeheader()
            writer.writerows(non_digital_records)
        print(f"Non-digital records ({len(non_digital_records)}) saved to: {NON_DIGITAL_FILE}")
    else:
        print("No non-digital records found -- all records are specificFormat=Digital.")

print()
print("NOTE: Counts may exceed unique OCN count because:")
print("  - One OCN can match records with multiple subtypes (different editions)")
print("  - Facet counts reflect all matching bib records, not just one per OCN")
