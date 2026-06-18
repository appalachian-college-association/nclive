"""
recompute_stats_from_csv.py

Recomputes corrected FOD/JFK match statistics directly from an existing
manual_review_searches.csv, without re-running any OCLC API searches.

WHY THIS EXISTS:
generate_statistics_report() in extended_marc_processor.py had a bug where
records that matched via Search 2 (corporate name + title, match_type
'SEARCH_2_MATCH') were miscounted as "no match" in the FOD/JFK breakdown
lines and excluded from all three success-rate calculations. This script
reads match_type directly from the CSV (which was always correct) to
produce the numbers the report SHOULD have shown.

USAGE:
    python recompute_stats_from_csv.py
    python recompute_stats_from_csv.py path\\to\\manual_review_searches.csv

If no path is given, defaults to manual_review_searches.csv in the
current directory.
"""

import sys
from pathlib import Path
import pandas as pd


# match_type values that count as "no match" for this older-style file.
# Real files from the current pipeline will only ever show 'NO_MATCH' or
# 'SEARCH_1_MATCH' through 'SEARCH_6_MATCH', but this also recognizes the
# older label style (INFOBASE_ID_MATCH, TITLE_MATCH, etc.) in case this is
# ever run against a file produced before the sequential-search redesign.
NO_MATCH_VALUES = {'NO_MATCH', ''}


def categorize(match_type: str) -> str:
    """
    Map a match_type value to a stat bucket: 'infobase_id', 'corporate_name',
    'title', 'series', or 'no_match'.

    Recognizes both the current SEARCH_N_MATCH labels and the older
    pre-redesign labels (INFOBASE_ID_MATCH, CORPORATE_NAME_MATCH, etc.),
    so this script works on files produced by either version of
    extended_marc_processor.py.
    """
    match_type = str(match_type).strip()

    if match_type in NO_MATCH_VALUES or match_type == 'nan':
        return 'no_match'

    # Current-style labels: SEARCH_1_MATCH .. SEARCH_6_MATCH
    search_number_bucket = {
        'SEARCH_1_MATCH': 'infobase_id',
        'SEARCH_2_MATCH': 'corporate_name',
        'SEARCH_3_MATCH': 'title',
        'SEARCH_4_MATCH': 'series',
        'SEARCH_5_MATCH': 'title',
        'SEARCH_6_MATCH': 'infobase_id',
    }
    if match_type in search_number_bucket:
        return search_number_bucket[match_type]

    # Older-style labels, for backward compatibility
    older_bucket = {
        'INFOBASE_ID_MATCH': 'infobase_id',
        'CORPORATE_NAME_MATCH': 'corporate_name',
        'TITLE_MATCH': 'title',
        'SERIES_MATCH': 'series',
    }
    if match_type in older_bucket:
        return older_bucket[match_type]

    # Unrecognized match_type value — flagged separately rather than
    # silently lumped into "no match", so it's visible if it happens.
    return 'unrecognized'


def compute_bucket_counts(df: pd.DataFrame) -> dict:
    """
    Compute bucket counts for the full DataFrame and for the fod/jfk subsets.

    Returns:
        Dict with keys 'total', 'fod', 'jfk' (each a dict of bucket -> count),
        plus 'fod_total', 'jfk_total', 'total_rows', and 'unrecognized'
        (a DataFrame of any rows with an unrecognized match_type).
    """
    df = df.copy()
    df['bucket'] = df['match_type'].apply(categorize)

    unrecognized = df[df['bucket'] == 'unrecognized']

    buckets = ['infobase_id', 'corporate_name', 'title', 'series', 'no_match']
    fod_df = df[df['collection_type'] == 'fod']
    jfk_df = df[df['collection_type'] == 'jfk']

    return {
        'total': {b: int((df['bucket'] == b).sum()) for b in buckets},
        'fod': {b: int((fod_df['bucket'] == b).sum()) for b in buckets},
        'jfk': {b: int((jfk_df['bucket'] == b).sum()) for b in buckets},
        'total_rows': len(df),
        'fod_total': len(fod_df),
        'jfk_total': len(jfk_df),
        'unrecognized': unrecognized,
    }


def print_unrecognized_warning(unrecognized: pd.DataFrame) -> None:
    """Print a warning listing any rows with an unrecognized match_type."""
    if unrecognized.empty:
        return
    print(f"WARNING: {len(unrecognized)} row(s) have an unrecognized match_type value:")
    print(unrecognized['match_type'].value_counts().to_string())
    print("These are excluded from the bucket counts below.\n")


def print_breakdown(stats: dict) -> None:
    """Print the MANUAL REVIEW ITEMS BREAKDOWN and EXTENDED SEARCH RESULTS sections."""
    total = stats['total']
    fod = stats['fod']
    jfk = stats['jfk']

    print("MANUAL REVIEW ITEMS BREAKDOWN:")
    print(f"  Total items for manual review: {stats['total_rows']}")
    print(f"  Films on Demand (FOD): {stats['fod_total']}")
    print(f"  Just for Kids (JFK): {stats['jfk_total']}")
    print()
    print("EXTENDED SEARCH RESULTS:")
    print(f"  Items with infobase id matches found: {total['infobase_id']}")
    print(f"    - FOD infobase id matches: {fod['infobase_id']}")
    print(f"    - JFK infobase id matches: {jfk['infobase_id']}")
    print(f"  Items with corporate name matches found: {total['corporate_name']}")
    print(f"    - FOD corporate name matches: {fod['corporate_name']}")
    print(f"    - JFK corporate name matches: {jfk['corporate_name']}")
    print(f"  Items with title matches found: {total['title']}")
    print(f"    - FOD title matches: {fod['title']}")
    print(f"    - JFK title matches: {jfk['title']}")
    print(f"  Items with series matches found: {total['series']}")
    print(f"    - FOD series matches: {fod['series']}")
    print(f"    - JFK series matches: {jfk['series']}")
    print(f"  Items with no matches found: {total['no_match']}")
    print(f"    - FOD no matches: {fod['no_match']}")
    print(f"    - JFK no matches: {jfk['no_match']}")


def print_success_rates(stats: dict) -> None:
    """Print the SUCCESS RATES section."""
    total_rows = stats['total_rows']
    fod_total = stats['fod_total']
    jfk_total = stats['jfk_total']

    overall_matched = total_rows - stats['total']['no_match']
    fod_matched = fod_total - stats['fod']['no_match']
    jfk_matched = jfk_total - stats['jfk']['no_match']

    print()
    print("SUCCESS RATES:")
    if total_rows > 0:
        print(f"  Overall match rate: {overall_matched / total_rows * 100:.1f}%")
    if fod_total > 0:
        print(f"  FOD match rate: {fod_matched / fod_total * 100:.1f}%")
    if jfk_total > 0:
        print(f"  JFK match rate: {jfk_matched / jfk_total * 100:.1f}%")


def print_reconciliation_check(stats: dict) -> None:
    """Print whether bucket counts sum to the total row count."""
    bucket_sum = sum(stats['total'].values()) + len(stats['unrecognized'])
    total_rows = stats['total_rows']
    print()
    if bucket_sum != total_rows:
        print(
            f"NOTE: bucket counts ({bucket_sum}) don't sum to total ({total_rows}). "
            "This shouldn't happen — check for unexpected match_type values above."
        )
    else:
        print(f"Reconciliation check passed: all {total_rows} rows accounted for.")


def recompute(csv_path: Path) -> None:
    """Read the CSV, recompute corrected stats, and print a report."""
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)

    if 'match_type' not in df.columns or 'collection_type' not in df.columns:
        print(f"Error: {csv_path} is missing 'match_type' or 'collection_type' column.")
        print(f"Columns found: {list(df.columns)}")
        sys.exit(1)

    stats = compute_bucket_counts(df)

    print("=" * 80)
    print("CORRECTED STATISTICS (recomputed from manual_review_searches.csv)")
    print("=" * 80)
    print(f"Source file: {csv_path}")
    print()

    print_unrecognized_warning(stats['unrecognized'])
    print_breakdown(stats)
    print_success_rates(stats)
    print_reconciliation_check(stats)


if __name__ == "__main__":
    path_arg = sys.argv[1] if len(sys.argv) > 1 else "manual_review_searches.csv"
    csv_file = Path(path_arg)

    if not csv_file.exists():
        print(f"Error: file not found: {csv_file}")
        print("Usage: python recompute_stats_from_csv.py [path_to_manual_review_searches.csv]")
        sys.exit(1)

    recompute(csv_file)
