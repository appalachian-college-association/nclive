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

# TESTING ONLY: institution AID/library ID used to build clickable AVOD/JFK
# links in the manual review CSV (infobase_link column). Sourced from .env via
# config.py — never hardcoded here, and never used in KBART title_id/title_url
# output (those must keep a blank ?aid= placeholder for Collection Manager).
TEST_AID = config.test_aid if 'config' in dir() else ''
TEST_LID = config.test_lid if 'config' in dir() else ''

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ExtendedMARCProcessor:
    """
    Extended processor for handling manual review items with enhanced OCLC searches.
    Simplified version - KBART processing moved to separate files.
    """

    def __init__(self,
                 updated_lookup_file: str = "InfobaseLookup_updated.csv",
                 manual_review_output: str = "manual_review_searches.csv"):
        """
        Initialize the extended processor.

        Args:
            updated_lookup_file: Path to InfobaseLookup_updated.csv
            manual_review_output: Output file for manual review searches
        """
        self.updated_lookup_file = Path(updated_lookup_file)
        self.manual_review_output = Path(manual_review_output)

        # Storage for data
        self.manual_review_records = []
        self.extended_search_results = []

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

    def load_manual_review_items(self) -> List[Dict]:
        """
        Load items marked for manual review from InfobaseLookup_updated.csv.

        Returns:
            List of records needing manual review
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

            self.manual_review_records = manual_review_df.to_dict('records')

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

    def search_oclc_by_title(self, title: str, max_results: int = 5) -> List[Dict]:
        """
        Search OCLC Discovery API by title.

        Args:
            title: Title to search for
            max_results: Maximum number of results to return

        Returns:
            List of matching records
        """
        if not title:
            return []
        try:
            clean_title = self.clean_title_for_search(title)
            if not clean_title:
                return []
            # Build query - search for electronic video format with title
            query = f'ti:"{clean_title}"'
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

            logger.info("Searching OCLC by title: %s", query)
            response = requests.get(API_URL, headers=headers, params=params, timeout=(10,30))
            response.raise_for_status()
            data = response.json()
            records = data.get("briefRecords", [])
            # Filter for electronic videos
            video_records = []
            for record in records:
                if (record.get("generalFormat") == "Video" and
                    record.get("specificFormat") == "Digital"):
                    video_records.append({
                        'oclc_number': record.get("oclcNumber", ""),
                        'title': record.get("title", ""),
                        'general_format': record.get("generalFormat", ""),
                        'specific_format': record.get("specificFormat", ""),
                        'search_type': 'title_search'
                    })
            return video_records

        except requests.exceptions.RequestException as e:
            logger.error("Error searching by title '%s': %s", title, e)
            self.stats['api_errors'] += 1
            return []

    def search_oclc_by_series(self, series: str, max_results: int = 3) -> List[Dict]:
        """
        Search OCLC Discovery API by series name.

        Args:
            series: Series name to search for
            max_results: Maximum number of results to return

        Returns:
            List of matching records
        """
        if not series:
            return []

        try:
            # Search both title field and series field for the series name
            queries = [
                f'se:"{series}"',   # Series field (MARC 830)
                f'ti:"{series}"',   # Series name searched as title
            ]

            all_results = []

            for query in queries:
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

                logger.info("Searching OCLC by series: %s", query)
                response = requests.get(API_URL, headers=headers, params=params, timeout=(10,30))
                response.raise_for_status()

                data = response.json()
                records = data.get("briefRecords", [])

                # Filter for electronic videos
                for record in records:
                    if (record.get("generalFormat") == "Video" and
                        record.get("specificFormat") == "Digital"):
                        result = {
                            'oclc_number': record.get("oclcNumber", ""),
                            'title': record.get("title", ""),
                            'general_format': record.get("generalFormat", ""),
                            'specific_format': record.get("specificFormat", ""),
                            'search_type': 'series_search'
                        }

                        # Avoid duplicates
                        if not any(r['oclc_number'] == result['oclc_number'] for r in all_results):
                            all_results.append(result)

                # Brief pause between queries
                time.sleep(0.5)

            return all_results[:max_results]  # Limit total results

        except requests.exceptions.RequestException as e:
            logger.error("Error searching by series '%s': %s", series, e)
            self.stats['api_errors'] += 1
            return []


    def search_oclc_by_infobase_id(self, lookup_id: str, max_results: int = 5) -> List[Dict]:
        """
        Search OCLC Discovery API using the main.py query format without kw:Infobase.
        This mimics the main.py search but removes the Infobase keyword constraint.

        IMPORTANT: Only searches on xtid values, NOT customid values.
        customid values don't correspond to MARC 028 fields and cause bad matches.

        Args:
            lookup_id: The lookup ID (e.g., "xtid=296504$")
            max_results: Maximum number of results to return

        Returns:
            List of matching records
        """

        if not lookup_id:
            return []

        try:
            # Extract numeric ID from lookup_id format
            # ONLY search on xtid values - these correspond to MARC 028 fields
            xtid_match = re.search(r'xtid=(.+)\$', lookup_id)

            if not xtid_match:
                # Check if this is a customid - these should NOT be searched via sn:
                customid_match = re.search(r'customid=(.+)\$', lookup_id)
                if customid_match:
                    logger.warning(
                        "Skipping infobase ID search for customid (doesn't match MARC 028): %s",
                        lookup_id
                        )
                    return []  # Return empty - customid can't be searched this way

                logger.warning("Could not extract xtid from lookup_id: %s", lookup_id)
                return []

            numeric_id = xtid_match.group(1)


            # itemSubType=video-digital is passed as a separate URL parameter (not in q=)
            query = f'mn:{numeric_id} AND pb:Infobase'

            # Build query using mn: (MARC 028$a / publisher number) — correct index
            # pb:Infobase tested against kw:(Infobase OR "Access Video" OR Films) on 8
            # manual-review records (see test_pb_vs_kw_anchor.py, 2026-06-17) — identical
            # results on every test case, no matches missed by the narrower form.
            # # itemSubType=video-digital is passed as a separate URL parameter (not in q=)
            query = f'mn:{numeric_id} AND pb:Infobase'

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

            logger.info("Searching OCLC by infobase query: %s", query)
            response = requests.get(API_URL, headers=headers, params=params, timeout=(10,30))
            response.raise_for_status()

            data = response.json()
            records = data.get("briefRecords", [])

            # Filter for digital videos (same as other search methods)
            video_records = []
            for record in records:
                if (record.get("generalFormat") == "Video" and
                record.get("specificFormat") == "Digital"):
                    video_records.append({
                        'oclc_number': record.get("oclcNumber", ""),
                        'title': record.get("title", ""),
                        'general_format': record.get("generalFormat", ""),
                        'specific_format': record.get("specificFormat", ""),
                        'search_type': 'infobase_id_search'
                    })

            return video_records

        except requests.exceptions.RequestException as e:
            logger.error("Error searching by infobase query '%s': %s", lookup_id, e)
            self.stats['api_errors'] += 1
            return []


    def search_oclc_by_corporate_name(
        self, corporate_name: str, title: str, max_results: int = 5
    ) -> List[Dict]:
        """
        Search OCLC by corporate name (au:) combined with title (ti:).

        Used when mn: search is unavailable or returned no results.
        Combines au: (from MARC 710$a) with ti: (from MARC 245$a) for precision.

        Args:
            corporate_name: Cleaned 710$a value (e.g. 'Digital Classics')
            title:          Title string from MARC 245$a for ti: search
            max_results:    Maximum number of results to return

        Returns:
            List of matching record dicts
        """
        if not corporate_name:
            return []

        try:
            clean_title = self.clean_title_for_search(title)

            # Build query: au: alone if no clean title; combined if both present
            if clean_title:
                query = f'au:"{corporate_name}" AND ti:"{clean_title}"'
            else:
                query = f'au:"{corporate_name}"'

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

            logger.info("Searching OCLC by corporate name: %s", query)
            response = requests.get(API_URL, headers=headers, params=params, timeout=(10,30))
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
                        'search_type': 'corporate_name_search',
                    })

            return video_records

        except requests.exceptions.RequestException as e:
            logger.error("Error searching by corporate name '%s': %s", corporate_name, e)
            self.stats['api_errors'] += 1
            return []

    # --- Private helpers to reduce complexity in large methods ---

    def _get_recommended_action(
        self,
        infobase_id_matches: list,
        corporate_name_matches: list,
        title_matches: list,
        series_matches: list
    ) -> str:
        """Return the highest-priority recommended action string."""
        if infobase_id_matches:
            return 'INFOBASE_ID_MATCH'
        if corporate_name_matches:
            return 'CORPORATE_NAME_MATCH'
        if title_matches:
            return 'TITLE_MATCH'
        if series_matches:
            return 'SERIES_MATCH'
        return 'NO_MATCH'

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
        if result['infobase_id_matches']:
            match = result['infobase_id_matches'][0]
            record.update({
                'match_type': 'INFOBASE_ID_MATCH',
                'match_rank': 1,
                'suggested_oclc': match['oclc_number'],
                'suggested_title': self.clean_text_for_export(match['title']),
                'match_format': f"{match['general_format']}-{match['specific_format']}",
                'manual_review_notes': 'First infobase ID match',
                'verifiedOCN': '',
                'accept_suggestion': ''
            })
        elif result.get('corporate_name_matches'):
            match = result['corporate_name_matches'][0]
            record.update({
                'match_type': 'CORPORATE_NAME_MATCH',
                'match_rank': 1,
                'suggested_oclc': match['oclc_number'],
                'suggested_title': self.clean_text_for_export(match['title']),
                'match_format': f"{match['general_format']}-{match['specific_format']}",
                'manual_review_notes': 'Corporate name + title match (710$a + 245$a)',
                'verifiedOCN': '',
                'accept_suggestion': ''
            })
        elif result['title_matches']:
            match = result['title_matches'][0]
            record.update({
                'match_type': 'TITLE_MATCH',
                'match_rank': 1,
                'suggested_oclc': match['oclc_number'],
                'suggested_title': self.clean_text_for_export(match['title']),
                'match_format': f"{match['general_format']}-{match['specific_format']}",
                'manual_review_notes': 'First title match',
                'verifiedOCN': '',
                'accept_suggestion': ''
            })
        elif result['series_matches']:
            match = result['series_matches'][0]
            record.update({
                'match_type': 'SERIES_MATCH',
                'match_rank': 1,
                'suggested_oclc': match['oclc_number'],
                'suggested_title': match['title'],
                'match_format': f"{match['general_format']}-{match['specific_format']}",
                'manual_review_notes': (
                    f'Series-level match for: {result.get("extracted_series", "")}'
                ),
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
        """Map match_type to a source label for lookup updates."""
        source_map = {
            'INFOBASE_ID_MATCH': 'API_INFOBASE_ID',
            'TITLE_MATCH': 'API_EXT_TITLE',
            'SERIES_MATCH': 'API_EXT_SERIES',
            'NO_MATCH': 'MANUAL_ENTRY',
        }
        return source_map.get(match_type, 'API_EXT_SEARCH')

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

    def perform_extended_searches(self) -> List[Dict]:
        """
        Perform extended OCLC searches on all manual review items.

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

            title = record.get('title', '')
            lookup_id = record.get('lookupID', '')
            collection_type = record.get('collection_type', '')
            corporate_name = record.get('corporate_name', '')   # From MARC 710$a
            series_name = record.get('series_name', '')         # From MARC 830$a
            avod_title_id = record.get('avod_title_id', '')     # From MARC 856$u (AVOD path)

            # Fall back to regex extraction only if series_name is empty
            if not series_name:
                series_name = self.extract_series_from_title(title)

            search_result = {
                'original_lookup_id': lookup_id,
                'original_title': title,
                'collection_type': collection_type,
                'extracted_series': series_name,
                'avod_title_id': avod_title_id,
                'title_matches': [],
                'series_matches': [],
                'infobase_id_matches': [],
                'corporate_name_matches': [],
                'recommended_action': 'NO_MATCH'
            }

            # 1. Search by infobase ID (mn: + pb: publisher anchor)
            infobase_id_matches = self.search_oclc_by_infobase_id(lookup_id)
            search_result['infobase_id_matches'] = infobase_id_matches
            if infobase_id_matches:
                self.stats['infobase_id_matches_found'] += 1
                logger.info("  Found %s infobase id matches", len(infobase_id_matches))

            # 2. Search by corporate name (au: + ti:) if corporate_name available
            corporate_name_matches = []
            if corporate_name:
                corporate_name_matches = self.search_oclc_by_corporate_name(corporate_name, title)
                search_result['corporate_name_matches'] = corporate_name_matches
                if corporate_name_matches:
                    self.stats['corporate_name_matches_found'] += 1
                    logger.info("  Found %s corporate name matches", len(corporate_name_matches))

            # 3. Search by title
            title_matches = self.search_oclc_by_title(title)
            search_result['title_matches'] = title_matches
            if title_matches:
                self.stats['title_matches_found'] += 1
                logger.info("  Found %s title matches", len(title_matches))

            # 4. Search by series (if available)
            series_matches = []
            if series_name:
                series_matches = self.search_oclc_by_series(series_name)
                search_result['series_matches'] = series_matches
                if series_matches:
                    self.stats['series_matches_found'] += 1
                    logger.info(
                        "  Found %s series matches for '%s'", len(series_matches), series_name
                        )

            if not any([infobase_id_matches, corporate_name_matches,
                        title_matches, series_matches]):
                self.stats['no_matches_found'] += 1
                logger.info("  No matches found")

            search_result['recommended_action'] = self._get_recommended_action(
                infobase_id_matches, corporate_name_matches, title_matches, series_matches
            )

            results.append(search_result)

            # Rate limiting - pause between searches
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
