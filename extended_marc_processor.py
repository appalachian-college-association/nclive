"""
Extended MARC processor for handling manual review items from InfobaseLookup_updated.csv.
This script provides additional OCLC Discovery API searching and manual review workflow
for items marked with verifiedOCN = "X" and source = "MANUAL_REVIEW".

SIMPLIFIED VERSION - KBART processing moved to kbart_integration.py

Features:
1. Extended OCLC searches using title, series, and other fields
2. Series-level matching capability
3. Manual review file output (manual_review_searches.csv)
4. Process manual updates to create InfobaseLookup_final.csv
5. FOD/JFK breakdown statistics
"""
# pylint: disable=too-many-lines

import csv
import re
import sys
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
import pandas as pd
import requests
from dotenv import load_dotenv

# Import authentication from existing project
# Load environment variables from .env file
load_dotenv()

# Load configuration and set up authentication (matching main.py approach)
try:
    from config import Config, OCLC_DTYPES
    from auth import OCLCAuth

    config = Config()
    auth_handler = OCLCAuth()
    API_URL = f"{config.oclc_base_url}/search/brief-bibs"
    DEFAULT_LIBRARY = config.default_library
    RESTRICT_TO_LIBRARY = config.restrict_to_library # Default is false (all libraries)
    oclc_dtypes = OCLC_DTYPES

except ImportError:
    print("Warning: Could not import auth.py and config.py. Please ensure they exist.")
    API_URL = "https://discovery.api.oclc.org/worldcat-org-ci/search/brief-bibs"
    DEFAULT_LIBRARY = "ACACL" # Fallback to local holdings
    RESTRICT_TO_LIBRARY = False # Fallback to global search

# Montreat college AID/library ID used to build clickable AVOD/JFK
# links in the manual review CSV (infobase_link column). Sourced from .env via
# config.py — never hardcoded here, and never used in KBART title_id/title_url
# output (those must keep a blank ?aid= placeholder for Collection Manager).
TEST_AID = config.test_aid if 'config' in dir() else ''
TEST_LID = config.test_lid if 'config' in dir() else ''

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ExtendedMARCProcessor:  # pylint: disable=too-many-instance-attributes
    """
    Extended processor for handling manual review items with enhanced OCLC searches.
    Simplified version - KBART processing moved to separate files.

    too-many-instance-attributes is suppressed: the extra attribute beyond the
    threshold (self.search_terms_file / self._numeric_id_result_cache) supports
    the search_terms.tsv join and per-numeric-ID result caching added for the
    sequential search redesign — splitting the class further wouldn't reduce
    real complexity here.
    """

    def __init__(self,
                 updated_lookup_file: str = "InfobaseLookup_updated.csv",
                 manual_review_output: str = "manual_review_searches.csv",
                 search_terms_file: str = "search_terms.tsv"):
        """
        Initialize the extended processor.

        Args:
            updated_lookup_file: Path to InfobaseLookup_updated.csv
            manual_review_output: Output file for manual review searches
            search_terms_file: Path to search_terms.tsv (produced by
                marc_processor.py, consumed by main.py). Supplies the
                au-fragment/se-fragment query pieces and the mn/au review
                flags used to build extended search queries.
        """
        self.updated_lookup_file = Path(updated_lookup_file)
        self.manual_review_output = Path(manual_review_output)
        self.search_terms_file = Path(search_terms_file)

        # Storage for data
        self.manual_review_records = []
        self.extended_search_results = []

        # Caches search results per unique numeric xtid, so records sharing
        # the same numeric ID (e.g. the same title in both $fod and $jfk
        # collections) are searched only once and share the result.
        self._numeric_id_result_cache: Dict[str, Dict] = {}

        # Statistics tracking
        self.stats = {
            'total_manual_review': 0,
            'fod_manual_review': 0,
            'jfk_manual_review': 0,
            'title_matches_found': 0,
            'series_matches_found': 0,
            'infobase_id_matches_found': 0,
            'no_matches_found': 0,
            'corporate_name_matches_found': 0,
            'api_errors': 0
        }

        # Authentication token (will be set when needed)
        self.access_token = None

    def load_search_terms(self) -> Dict[str, Dict]:
        """
        Load search_terms.tsv (produced by marc_processor.py, consumed by
        main.py) and return a dict keyed by lookupIDcollection.

        Uses the same csv.reader settings as main.py's load_search_terms()
        (delimiter, quoting, escapechar) so the au-fragment/se-fragment
        text — which contains embedded escaped quotes — decodes the same way
        it was encoded by marc_processor.py's generate_search_terms().

        Returns:
            Dict mapping lookupIDcollection -> {
                'au_fragment': str,  # e.g. ' AND au:"BBC Worldwide Learning"' or ''
                'se_fragment': str,  # e.g. ' AND se:"Wild New World"' or ''
                'mn_flag': str,      # 'Y' or 'N'
                'au_flag': str,      # 'Y' or 'N'
            }
            Empty dict if the file doesn't exist or can't be read.
        """
        terms_by_id: Dict[str, Dict] = {}

        if not self.search_terms_file.exists():
            logger.warning(
                "search_terms.tsv not found at %s — au/se fragments and "
                "review flags will be unavailable", self.search_terms_file
            )
            return terms_by_id

        try:
            with open(self.search_terms_file, "r", newline='', encoding="utf-8") as file:
                reader = csv.reader(
                    file, delimiter='\t', quoting=csv.QUOTE_NONE, escapechar='\\'
                )
                next(reader, None)  # Skip header row
                for parts in reader:
                    if not parts:
                        continue
                    lookup_id_collection = parts[0].strip()
                    # NOTE: au_fragment/se_fragment use rstrip(), not strip().
                    # generate_search_terms() writes these with a deliberate
                    # leading space (e.g. ' AND au:"Name"') so they can be
                    # appended directly as an AND-clause suffix. A plain
                    # .strip() would remove that leading space and produce
                    # a malformed query like 'ti:"Title"AND au:"Name"'.
                    terms_by_id[lookup_id_collection] = {
                        'au_fragment': parts[2].rstrip('\r\n') if len(parts) > 2 else '',
                        'se_fragment': parts[3].rstrip('\r\n') if len(parts) > 3 else '',
                        'mn_flag': parts[4].strip() if len(parts) > 4 else 'N',
                        'au_flag': parts[5].strip() if len(parts) > 5 else 'N',
                    }
            logger.info("Loaded %s search terms from %s", len(terms_by_id), self.search_terms_file)
        except (OSError, csv.Error) as e:
            logger.error("Error loading search terms: %s", e)

        return terms_by_id

    def load_manual_review_items(self) -> List[Dict]:
        """
        Load items marked for manual review from InfobaseLookup_updated.csv,
        then join in au-fragment/se-fragment/mn_flag/au_flag from
        search_terms.tsv by lookupIDcollection.

        Manual review records are expected to be a subset of search_terms.tsv
        (they're the rows main.py's primary search skipped or didn't match),
        so every manual review row should normally find a match. Rows that
        don't are logged and proceed with empty fragments / 'N' flags, which
        falls back to the original title+mn+kw-only search behavior.

        Returns:
            List of records needing manual review, each with fragment/flag
            keys merged in.
        """
        logger.info("Loading manual review items from %s", self.updated_lookup_file)

        if not self.updated_lookup_file.exists():
            logger.error("File not found: %s", self.updated_lookup_file)
            return []

        try:
            df = pd.read_csv(self.updated_lookup_file, dtype=oclc_dtypes, keep_default_na=False)

            # Filter for manual review items
            manual_review_df = df[
                (df['verifiedOCN'] == 'X') |
                (df['source'] == 'MANUAL_REVIEW')
            ].copy()

            search_terms_by_id = self.load_search_terms()

            records = manual_review_df.to_dict('records')
            unmatched_count = 0
            for record in records:
                lookup_id_collection = record.get('lookupIDcollection', '')
                terms = search_terms_by_id.get(lookup_id_collection)
                if terms is None:
                    unmatched_count += 1
                    terms = {'au_fragment': '', 'se_fragment': '', 'mn_flag': 'N', 'au_flag': 'N'}
                record.update(terms)

            if unmatched_count:
                logger.warning(
                    "%s manual review record(s) had no matching row in search_terms.tsv "
                    "— proceeding with empty fragments for those", unmatched_count
                )

            self.manual_review_records = records

            # Calculate statistics
            self.stats['total_manual_review'] = len(self.manual_review_records)
            self.stats['fod_manual_review'] = (
                len(manual_review_df[manual_review_df['collection_type'] == 'fod'])
            )
            self.stats['jfk_manual_review'] = (
                len(manual_review_df[manual_review_df['collection_type'] == 'jfk'])
            )

            logger.info("Loaded %s manual review items", self.stats['total_manual_review'])
            logger.info("  - FOD: %s", self.stats['fod_manual_review'])
            logger.info("  - JFK: %s", self.stats['jfk_manual_review'])

            return self.manual_review_records

        except (
            pd.errors.ParserError, KeyError, ValueError) as e:
            logger.error("Error loading manual review items: %s", e)
            return []

    def clean_text_for_export(self, text: str) -> str:
        """Clean text for OpenRefine compatibility"""
        if not text:
            return text
        return str(text).replace('#', '[hashmark]')

    def extract_series_from_title(self, title: str) -> Optional[str]:
        """
        Extract series name from title with parenthetical information.

        Examples:
        "Assessment of the Newborn : 2025 Version" -> None (no parenthetical)
        "Title Name (Assessment of the Newborn Series)" -> "Assessment of the Newborn Series"
        "Title (Series: Assessment of the Newborn)" -> "Assessment of the Newborn"

        Args:
            title: Full title string

        Returns:
            Extracted series name or None
        """
        # Pattern to match content in parentheses at the end of title
        patterns = [
            r'\(([^)]+Series[^)]*)\)$',  # Matches "(Something Series)"
            r'\(Series:\s*([^)]+)\)$',   # Matches "(Series: Something)"
            r'\(([^)]{10,})\)$',         # Matches long parenthetical (likely series)
        ]

        for pattern in patterns:
            match = re.search(pattern, title.strip(), re.IGNORECASE)
            if match:
                series_candidate = match.group(1).strip()
                # Clean up common prefixes/suffixes
                series_candidate = re.sub(
                    r'^(Series:\s*|The\s+)', '', series_candidate, flags=re.IGNORECASE
                    )
                series_candidate = re.sub(
                    r'\s+Series$', '', series_candidate, flags=re.IGNORECASE
                    )
                return series_candidate

        return None

    def clean_title_for_search(self, title: str) -> str:
        """
        Clean title for OCLC search by removing problematic characters and formatting.

        Args:
            title: Original title

        Returns:
            Cleaned title suitable for API search
        """
        if not title:
            return ""

        # Remove parenthetical information (often contains series info)
        cleaned = re.sub(r'\([^)]+\)', '', title)

        # Remove subtitles after colon (but keep main title)
        cleaned = cleaned.split(':')[0].strip()

        # Remove common punctuation that can cause search issues
        cleaned = re.sub(r'[^\w\s\-]', ' ', cleaned)

        # Normalize whitespace
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        # Remove leading/trailing articles for better matching
        cleaned = re.sub(r'^(The|A|An)\s+', '', cleaned, flags=re.IGNORECASE)

        return cleaned

    def get_token_cached(self) -> str:
        """
        Get OCLC API access token with caching.

        Returns:
            Valid access token
        """
        if self.access_token is None:
            try:
                # Create OCLCAuth class instance and get token
                self.access_token = auth_handler.get_valid_token()
                if not self.access_token:
                    raise ValueError("Failed to obtain valid token")
                logger.info("Successfully obtained OCLC API access token")
            except Exception as e:
                logger.error("Failed to get access token: %s", e)
                raise

        return self.access_token

    def _extract_numeric_id(self, lookup_id: str) -> Optional[str]:
        """
        Extract the numeric xtid from a lookup_id string for use in mn: searches.

        IMPORTANT: Only returns a value for xtid= format. customid= values don't
        correspond to MARC 028 fields and would cause bad matches if used with mn:.

        Args:
            lookup_id: The lookup ID (e.g., "xtid=296504$")

        Returns:
            The numeric ID string, or None if not an xtid-format lookup_id.
        """
        if not lookup_id:
            return None

        xtid_match = re.search(r'xtid=(.+)\$', lookup_id)
        if xtid_match:
            return xtid_match.group(1)

        customid_match = re.search(r'customid=(.+)\$', lookup_id)
        if customid_match:
            logger.warning(
                "Skipping mn: search for customid (doesn't match MARC 028): %s", lookup_id
            )
        else:
            logger.warning("Could not extract xtid from lookup_id: %s", lookup_id)
        return None

    def _run_single_query(self, query: str, max_results: int = 5) -> List[Dict]:
        """
        Execute a single Discovery API query and return filtered video-digital records.

        Args:
            query: The fully-built q= query string
            max_results: Maximum number of results to return

        Returns:
            List of matching record dicts (empty list on no results or error)
        """
        try:
            headers = {
                "Authorization": f"Bearer {self.get_token_cached()}",
                "Accept": "application/json"
            }
            params = {
                "q": query,
                "limit": max_results,
                "itemSubType": "video-digital",
            }
            if RESTRICT_TO_LIBRARY:
                params["heldByLibrary"] = DEFAULT_LIBRARY
                logger.debug("Restricting search to library %s", DEFAULT_LIBRARY)
            else:
                logger.debug("Performing global search (all libraries)")

            logger.info("Searching OCLC: %s", query)
            response = requests.get(API_URL, headers=headers, params=params, timeout=(10, 30))
            response.raise_for_status()
            data = response.json()
            records = data.get("briefRecords", [])

            video_records = []
            for record in records:
                if (record.get("generalFormat") == "Video" and
                        record.get("specificFormat") == "Digital"):
                    video_records.append({
                        'oclc_number': record.get("oclcNumber", ""),
                        'title': record.get("title", ""),
                        'general_format': record.get("generalFormat", ""),
                        'specific_format': record.get("specificFormat", ""),
                    })
            return video_records

        except requests.exceptions.RequestException as e:
            logger.error("Error running query '%s': %s", query, e)
            self.stats['api_errors'] += 1
            return []

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def _build_sequential_queries(
        self, clean_title: str, numeric_id: Optional[str],
        au_fragment: str, se_fragment: str,
        mn_flag: str, au_flag: str
    ) -> List[tuple]:
        """
        Build the ordered list of (search_number, description, query) tuples to try.

        Searches run in this order and stop at the first match:
          1. ti: + mn:        (title + Infobase publisher number)
          2. ti: + au_fragment (title + corporate name, from search_terms.tsv)
          3. ti: + se_fragment (title + series, from search_terms.tsv)
          4. mn: + se_fragment (Infobase publisher number + series)
          5. ti: alone        (title only)
          6. mn: + kw:        (Infobase publisher number + broad keyword fallback)

        au_fragment and se_fragment come pre-formatted from search_terms.tsv
        (e.g. ' AND au:"BBC Worldwide Learning"') and are appended as suffixes,
        so search 2 reads as ti:"{title}" AND au:"{name}" rather than
        au:"{name}" AND ti:"{title}" — equivalent for the API since AND is
        order-independent, just reordered for reuse of the existing fragment text.

        mn_flag='Y' means there's no real MARC 028 publisher number for this
        record (main.py's search_term would be a placeholder), so any search
        anchored on mn: (1, 4, 6) is skipped. au_flag='Y' means au_fragment is
        empty (no corporate_name), so search 2 is skipped — checking the flag
        directly (rather than just the fragment) mirrors main.py's own
        skip-routing logic.

        Series searches (3, 4) are skipped entirely if se_fragment is blank,
        since an empty se: clause returns zero results from the Discovery API
        rather than being ignored. Series is never searched alone, since se:
        alone returns every title in the series.

        Args:
            clean_title: Cleaned title string (may be empty)
            numeric_id: Extracted xtid numeric value, or None if unavailable
            au_fragment: Pre-formatted ' AND au:"Name"' suffix, or ''
            se_fragment: Pre-formatted ' AND se:"Series"' suffix, or ''
            mn_flag: 'Y' if no real MARC 028 (mn:) value, else 'N'
            au_flag: 'Y' if no corporate_name (au_fragment empty), else 'N'

        Returns:
            List of (search_number, description, query) tuples to attempt in order
        """
        queries = []
        has_mn = numeric_id and mn_flag != 'Y'
        has_au = au_fragment and au_flag != 'Y'
        has_se = bool(se_fragment)

        if clean_title and has_mn:
            queries.append((
                1, 'ti+mn',
                f'ti:"{clean_title}" AND mn:{numeric_id}'
            ))

        if clean_title and has_au:
            queries.append((
                2, 'ti+au',
                f'ti:"{clean_title}"{au_fragment}'
            ))

        if clean_title and has_se:
            queries.append((
                3, 'ti+se',
                f'ti:"{clean_title}"{se_fragment}'
            ))

        if has_mn and has_se:
            queries.append((
                4, 'mn+se',
                f'mn:{numeric_id}{se_fragment}'
            ))

        if clean_title:
            queries.append((
                5, 'ti_only',
                f'ti:"{clean_title}"'
            ))

        # Search 6 (mn + broad kw fallback) runs last by design: the loose kw:
        # clause (which matches the generic word "Films") increases the chance
        # of a non-title match, so it's only tried after every more-specific
        # option has failed.
        if has_mn:
            queries.append((
                6, 'mn+kw',
                f'mn:{numeric_id} AND kw:(Infobase OR "Access Video" OR Films)'
            ))

        return queries

    def _extract_search_fields(self, record: Dict) -> tuple:
        """
        Pull the fields needed for sequential search out of a manual review
        record. Centralizes the .get() calls with their defaults in one place.

        Returns:
            (title, numeric_id, au_fragment, se_fragment, mn_flag, au_flag)
        """
        title = record.get('title', '')
        lookup_id = record.get('lookupID', '')
        numeric_id = self._extract_numeric_id(lookup_id)
        au_fragment = record.get('au_fragment', '')
        se_fragment = record.get('se_fragment', '')
        mn_flag = record.get('mn_flag', 'N')
        au_flag = record.get('au_flag', 'N')
        return title, numeric_id, au_fragment, se_fragment, mn_flag, au_flag

    def run_sequential_searches(self, record: Dict) -> Dict:
        """
        Run the ordered sequence of Discovery API searches for one manual review
        record, stopping at the first query that returns a match.

        Records sharing the same numeric xtid (e.g. the same title appearing
        in both the $fod and $jfk collection rows) are only searched once —
        the first call for a given numeric_id runs the queries and caches the
        result; subsequent calls for the same numeric_id reuse that cached
        result instead of repeating the API calls. Records with no extractable
        numeric_id (e.g. customid records) are never cached, since they have
        no shared key to dedup on.

        Args:
            record: A manual review record dict (from manual_review_records),
                expected to include au_fragment/se_fragment/mn_flag/au_flag
                merged in from search_terms.tsv by load_manual_review_items()

        Returns:
            Dict with keys:
                'matches': list of matching record dicts (empty if none found)
                'search_number': int (1-6) of the search that found a match, or None
                'search_description': short label for which fields were used, or ''
        """
        title, numeric_id, au_fragment, se_fragment, mn_flag, au_flag = (
            self._extract_search_fields(record)
        )

        if numeric_id and numeric_id in self._numeric_id_result_cache:
            logger.info(
                "  Reusing cached result for numeric_id %s (shared across collections)",
                numeric_id
            )
            return self._numeric_id_result_cache[numeric_id]

        clean_title = self.clean_title_for_search(title)

        queries = self._build_sequential_queries(
            clean_title, numeric_id, au_fragment, se_fragment, mn_flag, au_flag
        )

        result = self._run_queries_until_match(queries)

        if numeric_id:
            self._numeric_id_result_cache[numeric_id] = result

        return result

    def _run_queries_until_match(self, queries: List[tuple]) -> Dict:
        """
        Run a list of (search_number, description, query) tuples in order,
        stopping at the first one that returns matches.

        Returns:
            Dict with 'matches', 'search_number', 'search_description' keys
        """
        for search_number, description, query in queries:
            matches = self._run_single_query(query)
            # Brief pause between queries to stay within rate limits
            time.sleep(0.5)
            if matches:
                for match in matches:
                    match['search_type'] = f'SEARCH_{search_number}_MATCH'
                logger.info(
                    "  Search %s (%s) matched: %s", search_number, description, query
                )
                return {
                    'matches': matches,
                    'search_number': search_number,
                    'search_description': description,
                }

        logger.info("  No matches found across %s attempted searches", len(queries))
        return {'matches': [], 'search_number': None, 'search_description': ''}

    # --- Private helpers to reduce complexity in large methods ---

    # Maps each search number (1-6) to the closest existing stat bucket /
    # recommended_action category, so generate_statistics_report() and the
    # FOD/JFK breakdown keep working without changes. match_type in the CSV
    # output still shows the specific SEARCH_N_MATCH label (see _build_match_record).
    _SEARCH_NUMBER_TO_BUCKET = {
        1: 'INFOBASE_ID_MATCH',     # ti+mn
        2: 'CORPORATE_NAME_MATCH',  # au+ti
        3: 'TITLE_MATCH',           # ti+se
        4: 'SERIES_MATCH',          # mn+se
        5: 'TITLE_MATCH',           # ti alone
        6: 'INFOBASE_ID_MATCH',     # mn+kw
    }

    def _get_recommended_action(self, search_number: Optional[int]) -> str:
        """Return the recommended action bucket for the winning search number."""
        if search_number is None:
            return 'NO_MATCH'
        return self._SEARCH_NUMBER_TO_BUCKET.get(search_number, 'NO_MATCH')

    def _build_infobase_link(
        self, lookup_id: str, collection_type: str, avod_title_id: str = ''
    ) -> str:
        """Construct a browse link to the Infobase record.

        FOD is fully migrated to AVOD (access.infobase.com); JFK remains on the
        legacy platform until its own migration occurs.
        """
        if collection_type == 'fod' and avod_title_id:
            # avod_title_id is stored as e.g. "video/7384"
            return f"https://access.infobase.com/{avod_title_id}?aid={TEST_AID}"
        if not lookup_id:
            return ""
        clean_id = lookup_id.rstrip('$')
        if collection_type == 'jfk':
            return f"https://jfk.infobase.com/PortalPlaylists.aspx?{clean_id}&wID={TEST_LID}"
        # Default: legacy FOD URL (FOD records without avod_title_id yet, or unknown type)
        return f"https://fod.infobase.com/PortalPlaylists.aspx?{clean_id}&wID={TEST_LID}"

    def _build_match_record(self, base_record: dict, result: dict) -> dict:
        """Build a single flattened match record for CSV output."""
        record = base_record.copy()
        matches = result.get('matches', [])

        if matches:
            match = matches[0]
            search_number = result.get('search_number')
            search_description = result.get('search_description', '')
            notes = f'Search {search_number} ({search_description}) match'
            if search_number in (3, 4):
                notes += f' — series: {result.get("extracted_series", "")}'
            record.update({
                'match_type': match.get('search_type', 'NO_MATCH'),
                'match_rank': 1,
                'suggested_oclc': match['oclc_number'],
                'suggested_title': self.clean_text_for_export(match['title']),
                'match_format': f"{match['general_format']}-{match['specific_format']}",
                'manual_review_notes': notes,
                'verifiedOCN': '',
                'accept_suggestion': ''
            })
        else:
            record.update({
                'match_type': 'NO_MATCH',
                'match_rank': 0,
                'suggested_oclc': '',
                'suggested_title': '',
                'match_format': '',
                'manual_review_notes': 'No matches found',
                'verifiedOCN': '',
                'accept_suggestion': ''
            })
        return record

    def _get_update_source(self, match_type: str) -> str:
        """Map match_type to a source label for lookup updates.

        match_type values are now 'SEARCH_1_MATCH' through 'SEARCH_6_MATCH'
        (set in run_sequential_searches), or 'NO_MATCH'. Each search number maps
        to a source label reflecting which fields found the match.
        """
        search_number_source_map = {
            1: 'API_EXT_TITLE_MN',      # ti+mn
            2: 'API_EXT_CORPORATE',     # au+ti
            3: 'API_EXT_TITLE_SERIES',  # ti+se
            4: 'API_EXT_MN_SERIES',     # mn+se
            5: 'API_EXT_TITLE',         # ti alone
            6: 'API_EXT_MN_KW',         # mn+kw fallback
        }

        if match_type == 'NO_MATCH':
            return 'MANUAL_ENTRY'

        match = re.match(r'SEARCH_(\d)_MATCH', str(match_type))
        if match:
            search_number = int(match.group(1))
            return search_number_source_map.get(search_number, 'API_EXT_SEARCH')

        return 'API_EXT_SEARCH'

    def _collect_accepted_updates(self, reviewed_df: pd.DataFrame) -> dict:
        """Extract accepted OCN updates from a manually reviewed DataFrame."""
        accepted_updates = {}
        for _, row in reviewed_df.iterrows():
            accept_suggestion = str(row.get('accept_suggestion', '')).strip().lower()
            lookup_id = row.get('original_lookup_id', '')
            collection_type = row.get('collection_type', '')
            match_type = row.get('match_type', '')

            oclc_number = None
            if accept_suggestion in ['yes', 'y', '1', 'true']:
                suggested_oclc = str(row.get('suggested_oclc', '')).strip()
                if suggested_oclc and suggested_oclc.lower() not in ['', 'nan', 'null']:
                    oclc_number = suggested_oclc

            if not oclc_number:
                verified_ocn = str(row.get('verifiedOCN', '')).strip()
                if verified_ocn and verified_ocn.lower() not in ['', 'nan', 'null']:
                    oclc_number = verified_ocn
                    accept_suggestion = 'yes'

            if lookup_id and oclc_number and accept_suggestion in ['yes', 'y', '1', 'true']:
                lookup_id_collection = f"{lookup_id}{collection_type}"
                source = self._get_update_source(match_type)
                accepted_updates[lookup_id_collection] = {
                    'verifiedOCN': oclc_number,
                    'source': source,
                    'base_lookup_id': lookup_id
                }
                logger.info("Accepted update for %s: %s (source: %s)",
                            lookup_id_collection, oclc_number, source)
        return accepted_updates

    def _apply_updates_to_df(self, updated_df: pd.DataFrame, accepted_updates: dict) -> None:
        """Apply accepted OCN updates to the lookup DataFrame in place."""
        today = datetime.now().strftime('%Y-%m-%d')
        for lookup_id_collection, updates in accepted_updates.items():
            mask = updated_df['lookupIDcollection'] == lookup_id_collection
            if mask.any():
                updated_df.loc[mask, 'verifiedOCN'] = updates['verifiedOCN']
                updated_df.loc[mask, 'source'] = updates['source']
                updated_df.loc[mask, 'last_updated'] = today
                logger.info("Updated %s with OCLC %s",
                            lookup_id_collection, updates['verifiedOCN'])
                continue
            # Fallback: match on base lookupID alone
            base_lookup_id = updates['base_lookup_id']
            mask_fallback = updated_df['lookupID'] == base_lookup_id
            if not mask_fallback.any():
                logger.warning("Could not find lookup_id %s or %s in original data",
                               lookup_id_collection, base_lookup_id)
                continue
            if mask_fallback.sum() > 1:
                logger.warning(
                    "Multiple matches found for %s. Manual review may be needed.",
                    base_lookup_id
                )
                logger.warning("Matches found: %s",
                               updated_df[mask_fallback]['lookupIDcollection'].tolist())
                logger.warning("Applied update to all %s matches for %s",
                               mask_fallback.sum(), base_lookup_id)
            else:
                logger.info("Updated %s (fallback match) with OCLC %s",
                            base_lookup_id, updates['verifiedOCN'])
            updated_df.loc[mask_fallback, 'verifiedOCN'] = updates['verifiedOCN']
            updated_df.loc[mask_fallback, 'source'] = updates['source']
            updated_df.loc[mask_fallback, 'last_updated'] = today

    # Maps each recommended_action bucket to its stats counter key, used when
    # tallying a match found by _build_search_result_for_record().
    _BUCKET_TO_STAT_KEY = {
        'INFOBASE_ID_MATCH': 'infobase_id_matches_found',
        'CORPORATE_NAME_MATCH': 'corporate_name_matches_found',
        'TITLE_MATCH': 'title_matches_found',
        'SERIES_MATCH': 'series_matches_found',
    }

    def _extract_series_for_display(self, se_fragment: str, title: str) -> str:
        """
        Extract a bare series name for display/reporting purposes only.

        se_fragment from search_terms.tsv is pre-formatted as
        ' AND se:"Series Name"' for use directly in queries; this pulls out
        just "Series Name" for the extracted_series report column. Falls back
        to the regex-based title extraction (matching original behavior) if
        se_fragment is empty.

        Args:
            se_fragment: Pre-formatted se: fragment, or ''
            title: Original title, used as fallback source

        Returns:
            Bare series name, or '' if none available
        """
        if se_fragment:
            match = re.search(r'se:"([^"]*)"', se_fragment)
            if match:
                return match.group(1)
        return self.extract_series_from_title(title) or ''

    def _build_search_result_for_record(self, record: Dict) -> Dict:
        """
        Run the sequential searches for one record and assemble its search_result
        dict, including the back-compat match-list keys that
        generate_statistics_report() expects.

        Also updates self.stats as a side effect, matching the behavior of the
        original per-search-type loop.

        Args:
            record: A manual review record dict, expected to include
                au_fragment/se_fragment/mn_flag/au_flag merged in from
                search_terms.tsv by load_manual_review_items()

        Returns:
            The assembled search_result dict for this record
        """
        title = record.get('title', '')
        se_fragment = record.get('se_fragment', '')
        series_name = self._extract_series_for_display(se_fragment, title)

        search_outcome = self.run_sequential_searches(record)
        matches = search_outcome['matches']
        search_number = search_outcome['search_number']

        search_result = {
            'original_lookup_id': record.get('lookupID', ''),
            'original_title': title,
            'collection_type': record.get('collection_type', ''),
            'extracted_series': series_name,
            'avod_title_id': record.get('avod_title_id', ''),  # From MARC 856$u
            'matches': matches,
            'search_number': search_number,
            'search_description': search_outcome['search_description'],
            # Back-compat keys for generate_statistics_report(), populated
            # based on which search number produced the winning match.
            'title_matches': matches if search_number in (3, 5) else [],
            'series_matches': matches if search_number == 4 else [],
            'infobase_id_matches': matches if search_number in (1, 6) else [],
            'corporate_name_matches': matches if search_number == 2 else [],
            'recommended_action': self._get_recommended_action(search_number),
        }

        if matches:
            bucket = self._SEARCH_NUMBER_TO_BUCKET.get(search_number)
            stat_key = self._BUCKET_TO_STAT_KEY.get(bucket)
            if stat_key:
                self.stats[stat_key] += 1
            logger.info("  Found %s matches via search %s", len(matches), search_number)
        else:
            self.stats['no_matches_found'] += 1
            logger.info("  No matches found")

        return search_result

    def perform_extended_searches(self) -> List[Dict]:
        """
        Perform extended OCLC searches on all manual review items.

        Each record runs through run_sequential_searches() (via
        _build_search_result_for_record), which tries up to 6 queries in
        priority order and stops at the first match. The winning search number
        is then mapped back into the same four match-list keys
        (infobase_id_matches, corporate_name_matches, title_matches,
        series_matches) used by generate_statistics_report(), so the FOD/JFK
        breakdown reporting works unchanged.

        Returns:
            List of extended search results
        """
        logger.info("Performing extended OCLC searches...")

        if not self.manual_review_records:
            logger.warning("No manual review records to search")
            return []

        results = []

        for i, record in enumerate(self.manual_review_records, 1):
            logger.info(
                "Processing record %s/%s:%s...",
                i, len(self.manual_review_records), record.get('title', '')[:50]
                )

            results.append(self._build_search_result_for_record(record))

            # Rate limiting - pause between records
            time.sleep(1)

        self.extended_search_results = results
        return results

    def create_manual_review_file(self, output_file: str = None) -> str:
        """
        Create manual review file with extended search results.

        Args:
            output_file: Output file path (optional)

        Returns:
            Path to created file
        """
        if output_file is None:
            output_file = self.manual_review_output

        logger.info("Creating manual review file: %s", output_file)

        csv_records = []
        for result in self.extended_search_results:
            lookup_id = result['original_lookup_id']
            collection_type = result['collection_type']
            base_record = {
                'original_lookup_id': lookup_id,
                'original_title': result['original_title'],
                'collection_type': collection_type,
                'extracted_series': result.get('extracted_series', ''),
                'recommended_action': result['recommended_action'],
                'infobase_link': self._build_infobase_link(
                    lookup_id, collection_type, result.get('avod_title_id', '')
                )
            }
            csv_records.append(self._build_match_record(base_record, result))

        # Write CSV
        with open(output_file, 'w', newline='', encoding='utf-8') as file:
            if csv_records:
                writer = csv.DictWriter(file, fieldnames=csv_records[0].keys())
                writer.writeheader()
                writer.writerows(csv_records)
            else:
                # Write empty file with headers
                headers = [
                    'original_lookup_id', 'original_title', 'collection_type', 'extracted_series',
                    'recommended_action', 'match_type', 'match_rank', 'suggested_oclc',
                    'suggested_title', 'match_format', 'manual_review_notes',
                    'verifiedOCN', 'accept_suggestion'
                ]
                writer = csv.DictWriter(file, fieldnames=headers)
                writer.writeheader()

        logger.info("Created manual review file with %s records", len(csv_records))
        return str(output_file)

    def process_manual_review_updates(
            self, reviewed_file: str,
            updated_lookup_output: str = "InfobaseLookup_final.csv") -> str:
        """
        Process manually reviewed file and update lookup data.
        FIXED VERSION - Handles duplicate title IDs correctly using lookupIDcollection.

        Args:
            reviewed_file: Path to manually reviewed CSV file
            updated_lookup_output: Output path for final lookup file

        Returns:
            Path to updated lookup file
        """
        logger.info("Processing manual review updates from %s", reviewed_file)

        # Load the reviewed file
        try:
            reviewed_df = pd.read_csv(reviewed_file, dtype=oclc_dtypes, keep_default_na=False)
        except (pd.errors.ParserError, KeyError, ValueError) as e:
            logger.error("Could not load reviewed file: %s", e)
            return None

        # Load original lookup file
        try:
            original_df = pd.read_csv(
                self.updated_lookup_file, dtype=oclc_dtypes, keep_default_na=False
                )
        except (OSError, pd.errors.ParserError, KeyError, ValueError) as e:
            logger.error("Could not load original lookup file: %s", e)
            return None

        accepted_updates = self._collect_accepted_updates(reviewed_df)
        logger.info("Processing %s accepted updates", len(accepted_updates))

        updated_df = original_df.copy()
        self._apply_updates_to_df(updated_df, accepted_updates)

        # DIAGNOSTIC: Check for any remaining "X" values
        remaining_x_count = len(updated_df[updated_df['verifiedOCN'] == 'X'])
        if remaining_x_count > 0:
            logger.info(
                "After manual review processing: %s items still marked for manual review ('X')",
                remaining_x_count
            )
        else:
            logger.info("All manual review items have been processed - no 'X' values remaining")

        # Save updated lookup file
        updated_df.to_csv(updated_lookup_output, index=False)
        logger.info("Saved updated lookup file: %s", updated_lookup_output)

        return str(updated_lookup_output)

    def generate_statistics_report(self, output_file: str = "extended_search_stats.txt") -> str:
        """
        Generate detailed statistics report with FOD/JFK breakdown.

        Args:
            output_file: Output file for statistics report

        Returns:
            Path to statistics report
        """
        # pylint: disable=too-many-locals
        logger.info("Generating statistics report: %s", output_file)

        # Calculate additional statistics
        fod_id_matches = sum(1 for r in self.extended_search_results
                               if r['collection_type'] == 'fod' and r['infobase_id_matches'])
        jfk_id_matches = sum(1 for r in self.extended_search_results
                               if r['collection_type'] == 'jfk' and r['infobase_id_matches'])
        fod_title_matches = sum(1 for r in self.extended_search_results
                               if r['collection_type'] == 'fod' and r['title_matches'])
        jfk_title_matches = sum(1 for r in self.extended_search_results
                               if r['collection_type'] == 'jfk' and r['title_matches'])
        fod_series_matches = sum(1 for r in self.extended_search_results
                                if r['collection_type'] == 'fod' and r['series_matches'])
        jfk_series_matches = sum(1 for r in self.extended_search_results
                                if r['collection_type'] == 'jfk' and r['series_matches'])
        fod_no_matches = sum(1 for r in self.extended_search_results
                            if r['collection_type'] == 'fod' and
                            not r['infobase_id_matches'] and not
                            r['title_matches'] and not r['series_matches'])
        jfk_no_matches = sum(1 for r in self.extended_search_results
                            if r['collection_type'] == 'jfk' and not
                            r['infobase_id_matches'] and not
                            r['title_matches'] and not r['series_matches'])

        # Create report
        report_lines = [
            "="*80,
            "EXTENDED MARC PROCESSOR - MANUAL REVIEW STATISTICS",
            "="*80,
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "MANUAL REVIEW ITEMS BREAKDOWN:",
            f"  Total items for manual review: {self.stats['total_manual_review']}",
            f"  Films on Demand (FOD): {self.stats['fod_manual_review']}",
            f"  Just for Kids (JFK): {self.stats['jfk_manual_review']}",
            "",
            "EXTENDED SEARCH RESULTS:",
            f"  Items with infobase id matches found: {self.stats['infobase_id_matches_found']}",
            f"    - FOD title matches: {fod_id_matches}",
            f"    - JFK title matches: {jfk_id_matches}",
            f"  Items with title matches found: {self.stats['title_matches_found']}",
            f"    - FOD title matches: {fod_title_matches}",
            f"    - JFK title matches: {jfk_title_matches}",
            f"  Items with series matches found: {self.stats['series_matches_found']}",
            f"    - FOD series matches: {fod_series_matches}",
            f"    - JFK series matches: {jfk_series_matches}",
            f"  Items with no matches found: {self.stats['no_matches_found']}",
            f"    - FOD no matches: {fod_no_matches}",
            f"    - JFK no matches: {jfk_no_matches}",
            f"  API errors encountered: {self.stats['api_errors']}",
            "",
            "SUCCESS RATES:",
        ]

        if self.stats['total_manual_review'] > 0:
            overall_success_rate = ((
                self.stats['infobase_id_matches_found'] +
                self.stats['title_matches_found'] +
                self.stats['series_matches_found']) /
                                  self.stats['total_manual_review']) * 100
            report_lines.append(f"  Overall match rate: {overall_success_rate:.1f}%")

            if self.stats['fod_manual_review'] > 0:
                fod_success_rate = ((fod_title_matches + fod_series_matches) /
                                  self.stats['fod_manual_review']) * 100
                report_lines.append(f"  FOD match rate: {fod_success_rate:.1f}%")

            if self.stats['jfk_manual_review'] > 0:
                jfk_success_rate = ((jfk_title_matches + jfk_series_matches) /
                                  self.stats['jfk_manual_review']) * 100
                report_lines.append(f"  JFK match rate: {jfk_success_rate:.1f}%")

        # Write report
        with open(output_file, 'w', encoding='utf-8') as file:
            file.write('\n'.join(report_lines))

        # Also print to console
        for line in report_lines:
            print(line)

        return str(output_file)

def main():
    """Main function to run extended manual review processing."""
    print("Extended MARC Processor - Manual Review Handler (Simplified)")
    print("=" * 65)

    # Initialize processor
    processor = ExtendedMARCProcessor()

    # Load manual review items
    manual_review_items = processor.load_manual_review_items()

    if not manual_review_items:
        print("No manual review items found. Exiting.")
        return

    # Perform extended searches
    print(f"\nPerforming extended OCLC searches on {len(manual_review_items)} items...")
    processor.perform_extended_searches()

    # Create manual review file
    manual_review_file = processor.create_manual_review_file()
    print(f"\n Created manual review file: {manual_review_file}")

    # Generate statistics report
    stats_report = processor.generate_statistics_report()
    print(f"Generated statistics report: {stats_report}")

    print("\n Next Steps:")
    print(f"1. Review and edit: {manual_review_file}")
    print("   - Update 'verifiedOCN' column with correct OCLC numbers")
    print("   - Set 'accept_suggestion' to 'yes' for items to accept")
    print(
        "2. After manual review, run: python extended_marc_processor.py"
        f" --process-updates {manual_review_file}"
         )
    print("3. This will generate InfobaseLookup_final.csv")
    print("4. Then run: python kbart_integration.py for final KBART processing")

def process_updates_main():
    """Process manually reviewed updates."""

    if len(sys.argv) < 3:
        print("Usage: python extended_marc_processor.py --process-updates <reviewed_file>")
        return

    reviewed_file = sys.argv[2]

    print("Extended MARC Processor - Processing Manual Review Updates")
    print("=" * 60)

    processor = ExtendedMARCProcessor()

    # Process the manual review updates
    updated_lookup = processor.process_manual_review_updates(
        reviewed_file,
        "InfobaseLookup_final.csv"
    )

    if updated_lookup:
        print("\n Processing complete!")
        print(f"Updated lookup file: {updated_lookup}")
        print("\n Final Steps:")
        print("1. Run: python kbart_integration.py")
        print(f"   This will create the final KBART files using {updated_lookup}")
        print("2. Run: python final_kbart_integration_reporting.py")
        print("   For comprehensive reporting and validation")
    else:
        print("\n Error processing updates. Check logs for details.")

if __name__ == "__main__":

    if len(sys.argv) > 1 and sys.argv[1] == "--process-updates":
        process_updates_main()
    else:
        main()
