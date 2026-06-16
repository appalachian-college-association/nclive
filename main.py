# main.py
"""Searches OCLC Discovery API using search_terms.tsv

Routing logic:
  - Records with both mn: and au: fragment → primary search with itemSubType fallback cycling
  - Records with mn: but no au: fragment (au-review-flag = Y) → skipped; written to CSV
    with skip_reason so --update-lookup routes them to MANUAL_REVIEW in extended search
  - Records with no mn: (mn-review-flag = Y) → skipped; same routing as above
  - customid records (both flags = Y, empty search term) → skipped; same routing

itemSubType fallback cycling (primary search only):
  Tries each subtype in order and stops at the first that returns results.
  The subtype that produced the match is recorded in oclc_results.csv.
"""

import csv
import os
import sys
import logging
import time
from dotenv import load_dotenv
import requests
from auth import OCLCAuth
from config import Config

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# -------------------------------
# 1. Load environment and config
# -------------------------------
load_dotenv()
config = Config()
auth_handler = OCLCAuth()
API_URL = f"{config.oclc_base_url}/search/brief-bibs"
DEFAULT_LIBRARY = config.default_library      # Update .env if not ACACL
RESTRICT_TO_LIBRARY = config.restrict_to_library  # Update .env if not false

# itemSubType values to try in order for primary search fallback cycling.
# video-digital is tried first; if it returns 0 results, the next subtype
# is tried, and so on. The subtype that succeeds is recorded in the CSV.
ITEM_SUBTYPES_FALLBACK = [
    "video-digital",
    "intmm-digital",
    "audiobook-digital",
    "music-digital",
    "compfile-digital",
]

# -------------------------------
# 2. Load search terms from TSV file
# -------------------------------
def load_search_terms(filename):
    """
    Load search_terms.tsv produced by marc_processor.py.

    Reads all six columns from the updated TSV format:
      0: lookupIDcollection
      1: discovery-api-search  (mn: term, or '' for skipped records)
      2: au-fragment           (' AND au:"Name"' or '')
      3: se-fragment           (' AND se:"Series"' or '')
      4: mn-review-flag        ('Y' if 028 absent, 'N' otherwise)
      5: au-review-flag        ('Y' if no qualifying 710, 'N' otherwise)

    Returns a list of dicts, one per TSV row.
    """
    terms = []
    try:
        with open(filename, "r", encoding="utf-8") as file:
            next(file)  # Skip header row
            for line in file:
                line = line.strip()
                if not line:
                    continue
                parts = line.split('\t')
                if len(parts) >= 2:
                    terms.append({
                        'lookup_id':    parts[0].strip(),
                        'search_query': parts[1].strip(),
                        'au_fragment':  parts[2].strip() if len(parts) > 2 else '',
                        'se_fragment':  parts[3].strip() if len(parts) > 3 else '',
                        'mn_flag':      parts[4].strip() if len(parts) > 4 else 'N',
                        'au_flag':      parts[5].strip() if len(parts) > 5 else 'N',
                    })
        logger.info("Loaded %s search terms from %s", len(terms), filename)
    except (OSError, StopIteration) as e:
        logger.error("Error loading search terms: %s", e)
    return terms


# -------------------------------
# 3. Submit query to the API and fetch results
# -------------------------------
def run_search(query, token, item_subtype, restrict_to_library=RESTRICT_TO_LIBRARY):
    """
    Run one search against the OCLC Discovery API.

    Args:
        query:               The q= string (e.g. 'mn:10032 AND au:"Digital Classics"')
        token:               Valid Bearer token
        item_subtype:        itemSubType URL parameter value (e.g. 'video-digital')
        restrict_to_library: If True, adds heldByLibrary filter

    Returns:
        Parsed JSON response dict
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    params = {
        "q": query,
        "limit": 50,           # max 50 per API rules
        "itemSubType": item_subtype,
    }

    if restrict_to_library:
        params["heldByLibrary"] = DEFAULT_LIBRARY
        logger.info("Restricting search to library: %s", DEFAULT_LIBRARY)
    else:
        logger.info("Performing global search (all libraries)")

    logger.info("Query: %s | itemSubType: %s", query, item_subtype)
    response = requests.get(API_URL, headers=headers, params=params, timeout=(10, 30))
    response.raise_for_status()
    return response.json()


# -------------------------------
# 4. Extract selected fields and write CSV
# -------------------------------
def clean_text_for_export(text: str) -> str:
    """Clean text for OpenRefine compatibility."""
    if not text:
        return text
    return str(text).replace("#", '[hash]')


def _extract_record_fields(record):
    """Extract fields from one briefRecord returned by the API."""
    oclc = record.get("oclcNumber", "")
    title = record.get("title", "")
    isbns = [f"{isbn} (bn)" for isbn in record.get("isbns", [])]
    issns = [f"{issn} (sn)" for issn in record.get("issns", [])]
    isns = "; ".join(isbns + issns)
    general_format = record.get("generalFormat", "")
    specific_format = record.get("specificFormat", "")
    is_electronic_video = (general_format == "Video" and specific_format == "Digital")
    format_description = (
        f"{general_format}-{specific_format}" if general_format and specific_format else ""
    )
    material_types = record.get("format", {}).get("materialTypes", [])
    material_types_str = "; ".join(material_types) if material_types else ""
    return (
        oclc,
        clean_text_for_export(title),
        clean_text_for_export(isns),
        general_format,
        specific_format,
        format_description,
        is_electronic_video,
        material_types_str,
    )


def write_results_to_csv(data, output_file, lookup_id, item_subtype):
    """
    Append API results to the output CSV.

    Args:
        data:         Parsed API response dict
        output_file:  Path to oclc_results.csv
        lookup_id:    lookupIDcollection value from TSV
        item_subtype: itemSubType that produced these results (for auditing)

    Returns:
        Number of rows written
    """
    records = data.get("briefRecords", [])
    results_written = 0
    with open(output_file, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        for record in records:
            fields = _extract_record_fields(record)
            writer.writerow([lookup_id, *fields, item_subtype, ''])
            results_written += 1
    return results_written


def write_skipped_to_csv(output_file, lookup_id, skip_reason):
    """
    Write a placeholder row for a record that was not searched.

    Records skipped here will be routed to MANUAL_REVIEW by --update-lookup
    and picked up by extended_marc_processor.py.

    Args:
        output_file:  Path to oclc_results.csv
        lookup_id:    lookupIDcollection value from TSV
        skip_reason:  'NO_AU', 'NO_MN', or 'NO_AU_NO_MN'
    """
    with open(output_file, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        # All result columns are empty; skip_reason is in the final column
        writer.writerow([
            lookup_id, '', '', '', '', '', '', '', '', '', skip_reason
        ])


# -------------------------------
# 5. Run the script
# -------------------------------
def _remove_existing_output(output_file: str) -> None:
    """Delete a stale oclc_results.csv so each run starts from a clean slate."""
    if os.path.exists(output_file):
        try:
            os.remove(output_file)
            logger.info("Removed existing file: %s", output_file)
        except OSError as e:
            logger.error("Failed to remove existing file: %s", e)


def _determine_skip_reason(mn_flag: str, au_flag: str) -> str:
    """Return the skip-reason code for a record that should not be searched."""
    if mn_flag == 'Y' and au_flag == 'Y':
        return 'NO_AU_NO_MN'
    if mn_flag == 'Y':
        return 'NO_MN'
    if au_flag == 'Y':
        return 'NO_AU'
    return ''


def _write_csv_header(output_file: str) -> None:
    """Create a new oclc_results.csv and write the column header row."""
    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        csv.writer(csvfile).writerow([
            "lookupID",
            "oclcNumber",
            "title",
            "isns",
            "generalFormat",
            "specificFormat",
            "formatDescription",
            "isElectronicVideo",
            "materialTypes",
            "itemSubType",    # which subtype produced the match (empty if skipped)
            "skip_reason",    # why a record was not searched (empty if searched)
        ])

def _search_with_subtypes(term: dict, token: str, output_file: str) -> tuple[int, str | None]:
    """
    Try each itemSubType in ITEM_SUBTYPES_FALLBACK until results are found.

    Builds the query from term['search_query'] and term['au_fragment'].
    Returns (records_written, matched_subtype); matched_subtype is None if
    all subtypes returned 0 results.
    """
    lookup_id = term['lookup_id']
    query = f"{term['search_query']}{term['au_fragment']}"
    matched_subtype = None
    records_written = 0

    for subtype in ITEM_SUBTYPES_FALLBACK:
        data = run_search(query, token, subtype, restrict_to_library=RESTRICT_TO_LIBRARY)
        result_count = len(data.get("briefRecords", []))
        print(f"  itemSubType={subtype}: {result_count} result(s)")

        if result_count > 0:
            records_written = write_results_to_csv(data, output_file, lookup_id, subtype)
            matched_subtype = subtype
            break

        time.sleep(0.5)

    return records_written, matched_subtype


def _verify_row_count(output_file: str, expected_count: int) -> None:
    """Check that the CSV row count matches expected_count (header not included)."""
    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            row_count = sum(1 for _ in csv.reader(f)) - 1
            print(f"  Verified:  {row_count} data rows in CSV")
            if row_count != expected_count:
                print(f"  Warning: expected {expected_count} rows but found {row_count}")
    except OSError as e:
        print(f"Error verifying CSV: {e}")


def main():
    """
    Main search loop.

    For each record in search_terms.tsv:
      - If either review flag is Y → write a skipped placeholder row and continue
      - Otherwise → run primary search with itemSubType fallback cycling:
          Try each subtype in ITEM_SUBTYPES_FALLBACK until results are found.
          If all subtypes return 0 results → write a skipped row with skip_reason='NO_MATCH'
    """
    output_file = "oclc_results.csv"
    _remove_existing_output(output_file)

    print("Getting token from OCLCAuth...")
    token = auth_handler.get_valid_token()
    if not token:
        print("Failed to retrieve access token. Check your credentials.")
        sys.exit(1)

    print("Loading search terms...")
    search_terms = load_search_terms("search_terms.tsv")

    if not search_terms:
        print("No search terms found or could not parse the file.")
        sys.exit(1)

    print(f"Found {len(search_terms)} search terms to process.")
    _write_csv_header(output_file)

    total_records = 0
    skipped_count = 0

    for i, term in enumerate(search_terms):
        lookup_id = term['lookup_id']
        mn_flag   = term['mn_flag']
        au_flag   = term['au_flag']

        print(f"Processing {i+1}/{len(search_terms)}: {lookup_id}")

        skip_reason = _determine_skip_reason(mn_flag, au_flag)
        if skip_reason:
            write_skipped_to_csv(output_file, lookup_id, skip_reason)
            skipped_count += 1
            print(f"  Skipped ({skip_reason}) → routed to extended search")
            continue

        try:
            records_written, matched_subtype = _search_with_subtypes(term, token, output_file)
            if matched_subtype:
                total_records += records_written
                print(f"  Matched on itemSubType={matched_subtype} "
                      f"→ wrote {records_written} row(s)")
            else:
                write_skipped_to_csv(output_file, lookup_id, 'NO_MATCH')
                skipped_count += 1
                print("  No results from any itemSubType → routed to extended search")

        except requests.exceptions.HTTPError as e:
            logger.error("HTTP error for %s: %s", lookup_id, e)
            write_skipped_to_csv(output_file, lookup_id, 'API_ERROR')
            skipped_count += 1

        if i + 1 < len(search_terms):
            time.sleep(1)

    print("\nDone!")
    print(f"  Searched:  {len(search_terms) - skipped_count} records")
    print(f"  Skipped:   {skipped_count} records (→ extended search queue)")
    print(f"  API rows:  {total_records} results written to {output_file}")
    _verify_row_count(output_file, total_records + skipped_count)


if __name__ == "__main__":
    main()
