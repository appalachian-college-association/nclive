# test_query_formats.py
"""
Diagnostic script to test WorldCat Discovery API query formats. V2 replaces sn: with mn: index.

PURPOSE:
    Runs multiple query variations against the live API and prints the results
    so you can compare which format returns the most useful records.

KEY FINDINGS FROM API DOCUMENTATION (June 2026):
    The official Discovery API spec explicitly bans x4: from the q= parameter:
        "Following indexes are not allowed: [x0=, x0:, x4=, x4:]"
    If x4:digital has been working in our scripts, the API may be silently
    dropping the tag (treating "digital" as a keyword) rather than erroring.

    THE CORRECT WAY to filter for digital video format is via the separate
    itemType and itemSubType URL parameters:
        itemType=video
        itemSubType=video-digital
    These are passed as separate parameters, NOT inside the q= string.

INDEX TAGS THAT ARE VALID in the q= string:
    kw:   keyword (any field)  -- the default, so same as no tag
    ti:   title
    au:   author/creator
    pb:   publisher name
    # sn:   standard number (covers ISBN, ISSN, and publisher numbers / MARC 028)
    mn: publisher number *try replacing sn: tag in query 
    no:   OCLC number
    se:   series

HOW TO RUN:
    From the nclive root directory in your VS Code bash terminal:
        python test_query_formats.py
"""

import json
import requests
from dotenv import load_dotenv
from config import Config
from auth import OCLCAuth

# ---------------------------------------------------------------------------
# Setup
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

# The target standard number (xtid / publisher number) from your example
TARGET_MN = "10049"
# The target name (au:) from MARC 710a (take first (Firm), then clean)
TARGET_AU = "Digital Classics Distribution"
# The target series (se:) from MARC 830a (take first; skip if not exist)
# Include f" AND {TARGET_SE}" for series to avoid blank se: entry that breaks search
TARGET_SE = ""
# The target title (ti:) from MARC 245a
TARGET_TI = "David Malouf"
# The target subtitle (ti:) from MARC 245b
TARGET_SUB_TI = "an imaginary life"
# The general publisher (pb:) search with TARGET_MN
TARGET_PB = "Infobase OR Access OR Films"
# Cataloging Agency (only acceptable use with MN only)
TARGET_SA = "nyinf"


# ---------------------------------------------------------------------------
# Query variations
# Each entry is: (label, q_string, extra_params_dict)
#
# "extra_params_dict" lets us pass additional URL parameters like itemType
# and itemSubType that are separate from the q= string.
# If no extra params are needed, pass an empty dict: {}
# ---------------------------------------------------------------------------

QUERY_VARIATIONS = [
    (
        # Instead of x4:digital with TARGET_MN, try SpecificFormat with CatalogingAgency and mn:
        # Need another
        "1. Original approach: x4:digital in q= (should be blocked per API docs but digital processed as kw)",
        f"mn:{TARGET_MN}",
        {"catalogSource": "NYINF"},
    ),
    (
        # OK but picks up non-matching title (use title to choose correctly?)
        "2. New initial search format: pb: OR au: + mn:",
        f'(pb:({TARGET_PB}) OR au:{TARGET_AU})'
         f' AND mn:{TARGET_MN}',
        {"itemSubType": "video-digital"},
    ),
    (
        # Returned only relevant titles - need to be careful with TARGET_SE because null "" returns 0 results
        # Could use OR to extend title search results ({TARGET_TI} OR {TARGET_SUB_TI})
        "3. Extended search without mn:",
        f'ti:({TARGET_TI} {TARGET_SUB_TI}) AND au:{TARGET_AU}{TARGET_SE}',
        {"itemSubType": "video-digital"},
    ),
    (
        # Returned some non-related titles
        "4. Correct API format: mn: in q= + itemType/itemSubType as separate params",
        f"mn:{TARGET_MN} AND kw:{TARGET_PB}",
        {"itemSubType": "video-digital"},
    ),
    (
        # Returned zero titles
        "5. Publisher + mn: with itemSubType filter (most precise approach)",
        f'mn:{TARGET_MN}',
        {"itemSubType": "video-digital"},
    ),
    (
        # Same results as search 2 (pb: made no difference)
        "6. mn: without pb: (simpler, avoids publisher name variations); try au: from 710a",
        f"au:{TARGET_AU} AND mn:{TARGET_MN}",
        {"itemSubType": "video-digital"},
    ),
]

# ---------------------------------------------------------------------------
# Helper: run one query and print a summary
# ---------------------------------------------------------------------------

def run_query(label: str, query: str, extra_params: dict, max_results: int = 5) -> None:
    """
    Send one query to the API and print a summary of results.

    Args:
        label:        Short description of this query variation
        query:        The q= string sent to the API
        extra_params: Additional URL parameters (e.g. itemType, itemSubType)
        max_results:  How many records to fetch (keep small for testing)
    """
    print("\n" + "=" * 70)
    print(f"TEST: {label}")
    print(f"q= string:  {query}")
    if extra_params:
        print(f"Extra params: {extra_params}")
    print("-" * 70)

    params = {"q": query, "limit": max_results}
    params.update(extra_params)  # Merge in any extra parameters

    try:
        response = requests.get(API_URL, headers=HEADERS, params=params, timeout=15)

        # Print the full URL the requests library built -- very useful for debugging
        print(f"Full URL:   {response.url}")

        if response.status_code == 400:
            # A 400 means the API rejected the query as invalid
            print("HTTP 400 BAD REQUEST -- API rejected this query format.")
            try:
                print(f"  Error type:   {response.json().get('type', 'N/A')}")
                print(f"  Error detail: {response.json().get('detail', 'N/A')}")
            except ValueError:
                print(f"  Raw response: {response.text[:300]}")
            return

        if response.status_code != 200:
            print(f"HTTP ERROR {response.status_code}: {response.text[:300]}")
            return

        records = response.json().get("briefRecords", [])
        total = response.json().get("numberOfRecords", "unknown")

        print(f"Total results reported by API: {total}")
        print(f"Records returned in this call: {len(records)}")

        if not records:
            print("  (no records returned)")
            return

        for i, record in enumerate(records, start=1):
            oclc_num = record.get("oclcNumber", "N/A")
            title = record.get("title", "N/A")
            general_fmt = record.get("generalFormat", "N/A")
            specific_fmt = record.get("specificFormat", "N/A")
            print(f"  [{i}] OCLC: {oclc_num}")
            print(f"       Title: {title}")
            print(f"       Format: {general_fmt} / {specific_fmt}")

    except requests.exceptions.Timeout:
        print("ERROR: Request timed out after 15 seconds.")
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Request failed: {e}")
    except json.JSONDecodeError:
        print("ERROR: Could not parse the API response as JSON.")
        print(f"Raw response: {response.text[:500]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    """Main function to run the query test."""
    print("WorldCat Discovery API -- Query Format Test")
    print(f"API endpoint: {API_URL}")
    print(f"Target publisher number (mn:): {TARGET_MN}")
    print()
    print("NOTE: x4: is listed as a banned index in the official API docs.")
    print("Test 1 checks whether the API errors or silently ignores it.")

    for label, query, extra_params in QUERY_VARIATIONS:
        run_query(label, query, extra_params)

    print("\n" + "=" * 70)
    print("INTERPRETING THE RESULTS:")
    print("  Test 1: If it gets a 400 error, x4: is truly blocked.")
    print("           If it returns results, the API is silently dropping the tag.")
    print("  Test 3: Should return the target record -- confirms mn: works.")
    print("  Test 4: The 'correct' API way to filter format (separate parameters).")
    print("  Test 5: Most precise -- adds publisher filter to Test 4.")
    print("  Best approach for extended_marc_processor.py: whichever of 4 or 5")
    print("  returns the right record with the fewest false matches.")

if __name__ == "__main__":
    main()
