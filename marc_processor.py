# marc_processor.py
"""
Automates MARC data processing to replace MarcEdit + OpenRefine workflow.
Processes Films on Demand and Just for Kids MARC files to generate search terms
for OCLC API lookup.

This version implements hierarchical lookup with InfobaseLookup.csv as the
primary authority, followed by KBART files, then MARC data validation.

This replaces the manual workflow of:
1. Exporting MARC fields to text
2. Processing in OpenRefine with JSON transformations
3. Creating search_terms.tsv for main.py
"""
# pylint: disable=too-many-lines

import csv
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import logging
from datetime import datetime
import pandas as pd

# MARC processing library
try:
    from pymarc import MARCReader, Record, PymarcException
except ImportError:
    print("Please install pymarc: pip install pymarc")
    sys.exit(1)

# Local imports
from config import OCLC_DTYPES

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class InfobaseMARCProcessor:
    """
    Processes Infobase MARC records to extract and validate title IDs,
    generate OCLC search terms, and manage KBART updates.

    Uses hierarchical lookup strategy:
    1. InfobaseLookup.csv (manually verified matches) - PRIMARY AUTHORITY
    2. KBART files (current collection data) - SECONDARY
    3. MARC 035 field validation (filtered for title ID contamination) - TERTIARY
    """

    def __init__(self,
                 marc_files_dir: str = "nclivemrc",
                 kbart_dir: str = "kbart_files",
                 lookup_file: str = "InfobaseLookup.csv"):
        """
        Initialize the MARC processor.

        Args:
            marc_files_dir: Directory containing downloaded MARC files
            kbart_dir: Directory containing current KBART files from OCLC
            lookup_file: CSV file with manually verified OCLC numbers (PRIMARY AUTHORITY)
        """
        self.marc_dir = Path(marc_files_dir)
        self.kbart_dir = Path(kbart_dir)
        self.lookup_file = Path(lookup_file)

        # Create directories if they don't exist
        self.kbart_dir.mkdir(exist_ok=True)

        # Storage for processed data
        self.current_records = []  # Records from current MARC files
        self.infobase_lookup = {}  # PRIMARY: BCLA verified OCNs from InfobaseLookup.csv
        self.kbart_lookup = {}     # SECONDARY: Current KBART entries

        # Statistics tracking
        self.stats = {
            'total_processed': 0,
            'valid_title_ids': 0,
            'invalid_ocns': 0,
            'matched_infobase_lookup': 0,
            'matched_kbart': 0,
            'matched_marc_035': 0,
            'needs_api_search': 0,
            'removed_records': 0,
            'rejected_records': []  # Track records that get rejected
        }

    def _decode_url_encoding(self, text: str) -> str:
        """
        Decode URL percent-encoding in KBART title_id values.
        Converts %3D to = and %2D to - for matching.
        """
        return text.replace('%3D', '=').replace('%2D', '-')

    def _encode_url_encoding(self, text: str) -> str:
        """
        Encode to URL percent-encoding for KBART output.
        Converts = to %3D and - to %2D for KBART compatibility.
        """
        return text.replace('=', '%3D').replace('-', '%2D')

    def _resolve_verified_ocn_col(self, df: pd.DataFrame) -> str:
        """Return verified OCN column name, falling back to auto-detection."""
        col_name = 'verifiedOCN'
        if col_name in df.columns:
            logger.info("Using expected column: %s", col_name)
            return col_name
        logger.warning("Could not find 'verifiedOCN' column in InfobaseLookup.csv")
        logger.info("Available columns: %s", ", ".join(df.columns))
        for col in df.columns:
            col_lower = col.lower()
            if 'verified' in col_lower and 'ocn' in col_lower:
                col_name = col
                break
            if 'oclc' in col_lower and ('number' in col_lower or 'num' in col_lower):
                col_name = col
                break
        logger.warning("Using fallback column: %s", col_name)
        return col_name

    def _extract_original_nclive_ocn(self, row: pd.Series, df_columns) -> str:
        """Extract original NC Live OCN from a lookup row."""
        if 'originalNCLiveOCN' in df_columns:
            return str(row.get('originalNCLiveOCN', '')).strip()
        if 'InfobaseMRCkey_original' in df_columns:
            mrc_key = str(row.get('InfobaseMRCkey_original', '')).strip()
            if '|' in mrc_key:
                return mrc_key.split('|', maxsplit=1)[0].strip()
        return ""

    def _load_lookup_file(self) -> None:
        """Load primary authority data from InfobaseLookup.csv into infobase_lookup."""
        if not self.lookup_file.exists():
            logger.warning("InfobaseLookup file not found: %s", self.lookup_file)
            return
        try:
            df = pd.read_csv(self.lookup_file, dtype=OCLC_DTYPES, keep_default_na=False)
            logger.info("InfobaseLookup columns found: %s", list(df.columns))
            verified_ocn_col = self._resolve_verified_ocn_col(df)
            lookup_id_col = 'lookupID'
            if verified_ocn_col not in df.columns or lookup_id_col not in df.columns:
                return
            for _, row in df.iterrows():
                lookup_id = str(row.get(lookup_id_col, '')).strip()
                verified_ocn = str(row.get(verified_ocn_col, '')).strip()
                if not (lookup_id and verified_ocn and
                        verified_ocn.lower() not in ('', 'x', 'nan', 'null')):
                    continue
                self.infobase_lookup[lookup_id] = {
                    'verified_ocn': verified_ocn,
                    'original_nclive_ocn': self._extract_original_nclive_ocn(row, df.columns)
                }
            logger.info(
                "PRIMARY: Loaded %s manually verified entries from InfobaseLookup.csv",
                len(self.infobase_lookup)
            )
        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError, KeyError) as e:
            logger.warning("Could not load InfobaseLookup file: %s", e)

    def _find_kbart_columns(self, df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
        """Return (title_id_col, oclc_col) from a KBART DataFrame, or (None, None)."""
        title_id_col = None
        oclc_col = None
        for col in df.columns:
            col_lower = col.lower()
            if 'title_id' in col_lower or 'titleid' in col_lower:
                title_id_col = col
            elif 'oclc' in col_lower and ('number' in col_lower or 'num' in col_lower):
                oclc_col = col
        return title_id_col, oclc_col

    def _load_kbart_file(self, kbart_file: Path) -> int:
        """Load one KBART file into kbart_lookup; return count of new entries added."""
        try:
            df = pd.read_csv(kbart_file, sep='\t', low_memory=False)
            title_id_col, oclc_col = self._find_kbart_columns(df)
            count = 0
            if title_id_col and oclc_col:
                for _, row in df.iterrows():
                    title_id_encoded = str(row.get(title_id_col, '')).strip()
                    oclc_num = str(row.get(oclc_col, '')).strip()
                    if not (title_id_encoded and oclc_num and
                            oclc_num.lower() not in ('', 'nan', 'null')):
                        continue
                    title_id_decoded = self._decode_url_encoding(title_id_encoded)
                    title_id_normalized = title_id_decoded.lower()
                    if title_id_normalized.startswith(('xtid=', 'customid=')):
                        # Legacy FOD and JFK records: key by numeric ID
                        lookup_id_format = f"{title_id_normalized}$"
                        numeric_id = title_id_normalized.split('=', maxsplit=1)[-1]
                        if lookup_id_format not in self.infobase_lookup:
                            self.kbart_lookup[numeric_id] = {
                                'oclc_number': oclc_num,
                                'encoded_title_id': title_id_encoded,
                                'decoded_title_id': title_id_decoded
                            }
                            count += 1
                    elif re.match(r'^[a-z]+/\d+', title_id_decoded):
                        # AVOD records (e.g., 'video/7384?aid='): store under avod: prefix
                        # to prevent namespace collision with legacy xtid integers
                        avod_key = f"avod:{title_id_decoded}"
                        self.kbart_lookup[avod_key] = {
                            'oclc_number': oclc_num,
                            'encoded_title_id': title_id_encoded,
                            'decoded_title_id': title_id_decoded
                        }
                        count += 1
                    else:
                        # Unknown format: log a warning and skip rather than
                        # misclassifying as customid
                        logger.warning(
                            "Skipping unrecognized KBART title_id format: %s",
                            title_id_decoded
                        )
            logger.info("SECONDARY: Loaded %s entries from %s", count, kbart_file.name)
            return count
        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError, KeyError) as e:
            logger.warning("Could not load KBART file %s: %s", kbart_file, e)
            return 0

    def load_existing_data(self):
        """
        Load existing lookup data with hierarchical priority:
        1. InfobaseLookup.csv (PRIMARY AUTHORITY)
        2. KBART files (SECONDARY)
        """
        logger.info("Loading existing lookup data with hierarchical priority...")
        self._load_lookup_file()
        kbart_records_loaded = sum(
            self._load_kbart_file(f) for f in self.kbart_dir.glob("*.txt")
        )
        logger.info("SECONDARY: Total unique KBART records loaded: %s", kbart_records_loaded)
        logger.info("TOTAL AUTHORITY RECORDS: %s (primary) + %s (secondary)",
                    len(self.infobase_lookup), len(self.kbart_lookup))

    def _extract_numeric_title_id(self, field_028_a: List[str]) -> str:
        """Extract the first numeric title ID from 028$a values."""
        if not field_028_a:
            return "Unknown"
        first_title_id = field_028_a[0].strip()
        if first_title_id.isdigit():
            return first_title_id
        numeric_match = re.search(r'\d+', first_title_id)
        return numeric_match.group() if numeric_match else "Unknown"

    def _build_rejection_info(self, record: Record) -> dict:
        """Build a rejection-tracking entry for a record that failed title ID validation."""
        control_001 = record['001'].data if record['001'] else "Unknown"
        field_245_a = self._get_subfield_values(record, '245', 'a')
        field_028_a = self._get_subfield_values(record, '028', 'a')
        field_856_u = self._get_subfield_values(record, '856', 'u')
        return {
            'nc_live_title_id': self._extract_numeric_title_id(field_028_a),
            'control_001': control_001,
            'title': field_245_a[0] if field_245_a else "No title found",
            'reason': 'Failed title ID validation or URL matching',
            'raw_title_ids': field_028_a,
            'url': field_856_u[0] if field_856_u else "No URL"
        }

    def extract_marc_fields(self, marc_file: Path) -> List[Dict]:
        """
        Extract relevant fields from MARC file.

        Returns list of dictionaries with extracted field data.
        """
        records = []
        try:
            with open(marc_file, 'rb') as file:
                reader = MARCReader(file)
                for record in reader:
                    if record is None:
                        continue
                    self.stats['total_processed'] += 1
                    record_data = self._extract_record_fields(record)
                    if record_data:
                        records.append(record_data)
                    else:
                        self.stats['rejected_records'].append(
                            self._build_rejection_info(record)
                        )
        except (OSError, PymarcException) as e:
            logger.error("Error reading MARC file %s: %s", marc_file, e)
        logger.info("Extracted %s valid records from %s", len(records), marc_file.name)
        return records

    def _parse_title_ids(self, field_028_a: List[str]) -> List[str]:
        """Expand semicolon-separated 028$a values into a flat list of stripped IDs."""
        title_ids = []
        for value in field_028_a:
            if ';' in value:
                title_ids.extend(v.strip() for v in value.split(';'))
            else:
                title_ids.append(value.strip())
        return title_ids

    def _extract_record_fields(self, record: Record) -> Optional[Dict]:
        """Extract and process fields from a single MARC record."""
        # pylint: disable=too-many-locals
        try:
            control_001 = record['001'].data if record['001'] else ""
            field_028_a = self._get_subfield_values(record, '028', 'a')
            field_035_a = self._get_subfield_values(record, '035', 'a')
            field_245_a = self._get_subfield_values(record, '245', 'a')
            field_245_b = self._get_subfield_values(record, '245', 'b')
            field_710_corporate = self._extract_corporate_name(record)
            field_830_a = self._get_subfield_values(record, '830', 'a')
            field_856_u = self._get_subfield_values(record, '856', 'u')
            field_856_z = self._get_subfield_values(record, '856', 'z')

            title_ids = self._parse_title_ids(field_028_a)
            lookup_id = self._validate_title_id_with_url(title_ids, field_856_u)

            if not lookup_id:
                return None

            marc_035_ocn = self._extract_oclc_number(field_035_a, title_ids)
            collection_type = 'fod' if any(
                'Films on Demand' in z
                or 'FOD Collection' in z
                or 'AVOD Collection' in z
                for z in field_856_z
            ) else 'jfk'
            lookup_id_collection = f"{lookup_id}{collection_type}"
            avod_title_id = self._extract_avod_title_id(field_856_u[0]) if field_856_u else ""

            return {
                'control_001': control_001,
                'lookup_id': lookup_id,
                'lookup_id_collection': lookup_id_collection,
                'marc_035_ocn': marc_035_ocn,
                'title': field_245_a[0] if field_245_a else "",
                'subtitle': field_245_b[0] if field_245_b else "",
                'url': field_856_u[0] if field_856_u else "",
                'collection_type': collection_type,
                'title_ids_raw': field_028_a,
                'url_description': field_856_z[0] if field_856_z else "",
                'avod_title_id': avod_title_id,
                'corporate_name': field_710_corporate,
                'series_name': field_830_a[0].rstrip('., ') if field_830_a else "",
                'has_028': bool(field_028_a),
            }

        except (AttributeError, KeyError, TypeError, ValueError) as e:
            logger.warning("Error processing record: %s", e)
            return None

    def _get_subfield_values(
            self, record: Record, field_tag: str, subfield_code: str
    ) -> List[str]:
        """Extract all values for a specific subfield."""
        values = []
        fields = record.get_fields(field_tag)
        for field in fields:
            subfields = field.get_subfields(subfield_code)
            values.extend(subfields)
        return values

    def _validate_avod_title_id(
            self, title_ids: List[str], url: str
    ) -> Optional[str]:
        """Return a lookup ID for an access.infobase.com URL.

        Uses 028$a xtid directly (URL cross-validation not possible on the new platform).
        Falls back to the path segment when 028$a is absent.
        """
        for title_id in title_ids:
            title_id_clean = title_id.strip()
            if title_id_clean.isdigit():
                return f"xtid={title_id_clean}$"
        path_match = re.search(r'access\.infobase\.com/[^/?]+/([^/?]+)', url)
        if path_match:
            path_id = path_match.group(1)
            if path_id.isdigit():
                return f"xtid={path_id}$"
        return None

    def _match_title_id_in_url(
            self, title_ids: List[str], url: str
    ) -> Optional[str]:
        """Match a 028$a title ID against the 856$u URL for old-platform records.

        Returns a formatted lookup ID string, or None if no match found.
        """
        for title_id in title_ids:
            title_id_clean = title_id.strip()
            if title_id_clean.startswith(('xtid=', 'customid=')):
                continue
            if f"id={title_id_clean}&" in url or f"customid={title_id_clean}&" in url:
                if "xtid=" in url:
                    return f"xtid={title_id_clean}$"
                if "customid=" in url:
                    return f"customid={title_id_clean}$"
        return None

    def _validate_title_id_with_url(
            self, title_ids: List[str], urls: List[str]
    ) -> Optional[str]:
        """
        Validate title ID by checking if it appears in the 856$u URL.
        Only accepts title IDs that come from xtid= or customid= URL parameters.

        For new Access Video on Demand FOD URLs (access.infobase.com), the 028$a
        still contains the old xtid value but no longer appears in the URL.
        These records skip cross-validation and use the 028$a value directly.

        Fallback: If no title IDs found in MARC 028$a, extract from URL.
        """
        if not urls:
            return None
        url = urls[0].lower()
        if 'access.infobase.com' in url:
            return self._validate_avod_title_id(title_ids, url)
        result = self._match_title_id_in_url(title_ids, url)
        if result:
            return result
        return self._extract_id_from_url(url)

    def _extract_id_from_url(self, url: str) -> Optional[str]:
        """
        Extract title ID directly from URL when MARC 028$a is missing.
        Only extracts if URL contains xtid= or customid= patterns.
        """
        # Only extract if URL contains xtid= or customid= patterns
        patterns = [
            (r'[?&]customid=([^&]+)', 'customid='),
            (r'[?&]xtid=([^&]+)', 'xtid=')
        ]

        for pattern, prefix in patterns:
            match = re.search(pattern, url)
            if match:
                title_id = match.group(1)
                return f"{prefix}{title_id}$"

        # Don't extract from generic "id=" parameters
        return None

    def _extract_avod_title_id(self, url: str) -> str:
        """
        Extract the Access Video on Demand title ID from a new-format FOD URL.
        Returns format/path_id (e.g., 'video/7384') for use in KBART output.
        Returns empty string for JFK URLs or old-format FOD URLs.

        Example: 'https://access.infobase.com/video/7384?aid=256695' -> 'video/7384'
        Handles all format types: video, audiobook, series, speech, podcast-episode
        """
        match = re.search(r'access\.infobase\.com/([^/?]+)/([^/?]+)', url, re.IGNORECASE)
        if match:
            format_type = match.group(1)  # e.g., 'video', 'audiobook', 'series'
            path_id = match.group(2)       # e.g., '7384'
            return f"{format_type}/{path_id}"
        return ""

    # NEW helper methods for corporate name (au: search in query)
    def _extract_corporate_name(self, record: Record) -> str:
        """
        Extract the best corporate name from MARC 710$a for use in au: API searches.

        Priority order:
          1. First 710$a that is not Infobase and not Films for the Humanities & Sciences
          2. 'Films for the Humanities & Sciences' (if nothing better exists)
          3. A value starting with 'Infobase' (last resort)
          4. Empty string if no 710 fields exist at all

        The selected value is cleaned before return:
          - Trailing punctuation (periods, commas, spaces) is stripped
          - ' (Firm)' is stripped (case-insensitive)
          - Any remaining trailing whitespace is stripped

        Returns a clean name string, or '' if no 710 fields exist.
        """
        best_firm = ""         # Priority 1: any qualifying corporate name
        fhs_fallback = ""      # Priority 2: Films for the Humanities & Sciences
        infobase_fallback = "" # Priority 3: Infobase name

        for field in record.get_fields('710'):
            subfield_a = field.get_subfields('a')
            if not subfield_a:
                continue
            raw_name = subfield_a[0].strip()
            cleaned = self._clean_corporate_name(raw_name)

            if cleaned.lower().startswith('infobase'):
                if not infobase_fallback:
                    infobase_fallback = cleaned
            elif 'films for the humanities' in cleaned.lower():
                if not fhs_fallback:
                    fhs_fallback = cleaned
            else:
                if not best_firm:
                    best_firm = cleaned

        return best_firm or fhs_fallback or infobase_fallback

    def _clean_corporate_name(self, name: str) -> str:
        """
        Clean a MARC 710$a corporate name for use in an OCLC au: search.

        Strips trailing punctuation (periods, commas, spaces), removes the
        MARC relationship designator ' (Firm)' (case-insensitive), then
        strips any remaining trailing whitespace.
        """
        cleaned = name.rstrip('., ')
        # Remove ' (Firm)' suffix, case-insensitive
        if cleaned.lower().endswith(' (firm)'):
            cleaned = cleaned[:-len(' (firm)')]
        return cleaned.strip()

    def _determine_collection_type_with_fallback(
            self, record: Dict, original_lookup_data: Dict
    ) -> str:
        """
        Determine collection type with fallback to original InfobaseLookup data.
        """
        lookup_id = record['lookup_id']

        # Method 1: Use MARC 856$z field if available (current MARC records)
        if 'url_description' in record and record['url_description']:
            url_descriptions = [record['url_description']]
            collection_type = 'fod' if any(
                'Films on Demand' in z
                or 'FOD Collection' in z
                or 'AVOD Collection' in z
                for z in url_descriptions
            ) else 'jfk'
            return collection_type

        # Method 2: Fallback to original InfobaseLookup data (preserved records)
        if lookup_id in original_lookup_data:
            original_record = original_lookup_data[lookup_id]
            if isinstance(original_record, dict):
                lookup_id_collection = original_record.get('lookupIDcollection', '')
                if lookup_id_collection.endswith('fod'):
                    return 'fod'
                if lookup_id_collection.endswith('jfk'):
                    return 'jfk'

        # Method 3: Default fallback
        return 'id_error'

    def _extract_oclc_number(self, field_035_values: List[str], title_ids: List[str]) -> str:
        """
        Extract a valid OCLC number from the 035 field,
        rejecting values that match known title IDs.

        Uses anchored matching to prevent substring false matches.

        Key issues:
        1. Infobase puts title IDs in MARC 035 when they can't find valid OCLC number
        2. NC Live uses prefix "1000" + title ID in MARC 035 (e.g., 1000107886 for title ID 107886)
        """
        # Extract just the numeric IDs from title_ids for comparison
        numeric_title_ids = set()
        for title_id in title_ids:
            # Handle both raw numeric IDs and any that might be in the list
            title_id_clean = title_id.strip()
            if title_id_clean.isdigit():
                numeric_title_ids.add(title_id_clean)
                # Also add NC Live prefixed version (1000 + title_id)
                numeric_title_ids.add(f"1000{title_id_clean}")
            else:
                # Extract numeric part if it contains other formatting
                numeric_match = re.search(r'\d+', title_id_clean)
                if numeric_match:
                    clean_numeric = numeric_match.group()
                    numeric_title_ids.add(clean_numeric)
                    # Also add NC Live prefixed version
                    numeric_title_ids.add(f"1000{clean_numeric}")

        # Now check 035 field for OCLC numbers, rejecting any that match title IDs
        for value in field_035_values:
            # Use anchored regex to ensure we get complete OCLC number
            match = re.search(r'\((?:OCoLC|ocn|ocm|on)\)(\d+)', value.strip(), re.IGNORECASE)
            if match:
                ocn = match.group(1).strip()
                # Use exact matching to reject title IDs and NC Live prefixed IDs
                if self._validate_oclc_number(ocn, title_ids):
                    return ocn

        return "N/A"

    def _validate_oclc_number(self, oclc_number: str, title_ids: List[str]) -> bool:
        """
        Additional validation to ensure OCLC number is not a disguised title ID.
        Uses exact matching approach similar to OpenRefine ^ and $ anchoring.

        Handles NC Live's prefix pattern: "1000" + title_id (e.g., 1000107886 for title_id 107886)
        """
        if not oclc_number or oclc_number == "N/A":
            return False

        # Extract numeric title IDs for exact comparison
        numeric_title_ids = set()
        for title_id in title_ids:
            title_id_clean = title_id.strip()
            if title_id_clean.isdigit():
                numeric_title_ids.add(title_id_clean)
                # Add NC Live prefixed version (1000 + title_id)
                numeric_title_ids.add(f"1000{title_id_clean}")
            else:
                numeric_match = re.search(r'\d+', title_id_clean)
                if numeric_match:
                    clean_numeric = numeric_match.group()
                    numeric_title_ids.add(clean_numeric)
                    # Add NC Live prefixed version
                    numeric_title_ids.add(f"1000{clean_numeric}")

        # Exact match check (equivalent to ^OCN$ in OpenRefine)
        if oclc_number in numeric_title_ids:
            return False  # This "OCLC number" is actually a title ID or NC Live prefixed ID

        # Additional validation: reasonable OCLC number characteristics
        if len(oclc_number) < 4:  # OCLC numbers are typically longer
            return False

        if not oclc_number.isdigit():  # Should be pure numeric
            return False

        # Special check: if it starts with "1000" and the remainder matches a title ID, reject it
        if oclc_number.startswith("1000") and len(oclc_number) > 3:
            remainder = oclc_number[4:]  # Remove "1000" prefix
            if remainder in {tid.strip() for tid in title_ids if tid.strip().isdigit()}:
                return False  # This is NC Live's title ID with "1000" prefix

        return True

    def _determine_best_oclc_number(self, record: Dict) -> Tuple[str, str]:
        """
        Determine the best OCLC number using hierarchical lookup.

        Returns:
            Tuple of (oclc_number, source) where source indicates the authority level
        """
        lookup_id = record['lookup_id']
        marc_035_ocn = record['marc_035_ocn']

        # Extract lookup keys for KBART secondary lookup.
        # Legacy records use a numeric ID; AVOD records use the avod: prefix
        # to prevent namespace collision with legacy xtid integers.
        title_id_numeric = None
        avod_key = None
        id_match = re.search(r'(?:xtid|customid)=(.+)\$', lookup_id)
        if id_match:
            title_id_numeric = id_match.group(1)
        elif record.get('avod_title_id'):
            avod_key = f"avod:{record['avod_title_id']}"

        # HIERARCHICAL LOOKUP:

        # 1. PRIMARY: Check InfobaseLookup.csv (manually verified matches)
        if lookup_id in self.infobase_lookup:
            lookup_data = self.infobase_lookup[lookup_id]
            oclc_num = lookup_data['verified_ocn'] if isinstance(
                lookup_data, dict
            ) else lookup_data
            self.stats['matched_infobase_lookup'] += 1
            return oclc_num, "InfobaseLookup"

        # 2. SECONDARY: Check KBART files (current collection data)
        if title_id_numeric and title_id_numeric in self.kbart_lookup:
            kbart_data = self.kbart_lookup[title_id_numeric]
            if isinstance(kbart_data, dict):
                oclc_num = kbart_data['oclc_number']
            else:
                oclc_num = kbart_data  # Fallback for old format
            self.stats['matched_kbart'] += 1
            return oclc_num, "KBART"

        if avod_key and avod_key in self.kbart_lookup:
            kbart_data = self.kbart_lookup[avod_key]
            if isinstance(kbart_data, dict):
                oclc_num = kbart_data['oclc_number']
            else:
                oclc_num = kbart_data  # Fallback for old format
            self.stats['matched_kbart'] += 1
            return oclc_num, "KBART"

        # 3. TERTIARY: Use MARC 035 field (if it passed title ID filtering)
        if marc_035_ocn != "N/A":
            self.stats['matched_marc_035'] += 1
            return marc_035_ocn, "MARC_035"

        # 4. NO MATCH: Needs API search
        self.stats['needs_api_search'] += 1
        return "", "NEEDS_SEARCH"

    def process_marc_files(self) -> List[Dict]:
        """Process all MARC files in the directory."""
        logger.info("Processing MARC files...")

        all_records = []

        # Find FOD and Just for Kids files (only in main directory, not archived)
        fod_files = list(self.marc_dir.glob("FOD*.mrc")) + list(self.marc_dir.glob("[Ff]ilm*.mrc"))
        fod_files = [f for f in fod_files if f.parent == self.marc_dir]
        jfk_files = list(self.marc_dir.glob("JFK*.mrc")) + list(self.marc_dir.glob("[Jj]ust*.mrc"))
        jfk_files = [f for f in jfk_files if f.parent == self.marc_dir]

        marc_files = fod_files + jfk_files

        if not marc_files:
            logger.warning("No MARC files found in %s", self.marc_dir)
            return []

        logger.info("Found %s MARC files to process", len(marc_files))

        for marc_file in marc_files:
            logger.info("Processing %s...", marc_file.name)
            records = self.extract_marc_fields(marc_file)
            all_records.extend(records)

        self.current_records = all_records
        logger.info("Total records processed: %s", len(all_records))

        return all_records

    def generate_search_terms(self, output_file: str = "search_terms.tsv") -> str:
        """
        Generate search_terms.tsv file for main.py OCLC API searches.
        Only includes records that need OCLC number lookup after hierarchical matching.

        IMPORTANT: Only searches on xtid values, NOT customid values.
        customid values don't correspond to MARC 028 fields and cause bad matches.
        """
        # pylint: disable=too-many-locals
        logger.info("Generating search terms using hierarchical lookup...")

        search_terms = []
        customid_manual_review = []  # Track customid records that need manual review

        for record in self.current_records:
            lookup_id_collection = record['lookup_id_collection']
            lookup_id = record['lookup_id']

            # Hierarchical lookup for best OCLC number (_ placeholder is oclc_number)
            _, source = self._determine_best_oclc_number(record)

            # Only add to search terms if no reliable OCLC number was found
            if source == "NEEDS_SEARCH":
                # Check if this is an xtid or customid
                # ONLY search on xtid values - these correspond to MARC 028 fields
                xtid_match = re.search(r'xtid=(.+)\$', lookup_id)
                if xtid_match:
                    # This is an xtid - safe to search in API
                    search_id = xtid_match.group(1)
                    search_term = f"mn:{search_id}"  # Publisher number search (MARC 028$a)

                    # Build au: fragment from 710$a corporate name (empty string if absent)
                    corporate_name = record.get('corporate_name', '')
                    au_fragment = f' AND au:"{corporate_name}"' if corporate_name else ''

                    # Build se: fragment from 830$a series name (empty string if absent)
                    series_name = record.get('series_name', '')
                    se_fragment = f' AND se:"{series_name}"' if series_name else ''

                    # Set review flags
                    mn_flag = 'N' if record.get('has_028', True) else 'Y'
                    au_flag = 'Y' if not corporate_name else 'N'

                    search_terms.append((
                        lookup_id_collection,
                        search_term,
                        au_fragment,
                        se_fragment,
                        mn_flag,
                        au_flag,
                    ))
                    logger.debug("Added xtid for API search: %s", lookup_id_collection)

                else:
                    # Check if this is a customid - these should NOT be searched
                    customid_match = re.search(r'customid=(.+)\$', lookup_id)
                    if customid_match:
                        # customid records cannot be searched via mn: - write a
                        # placeholder row so --update-lookup routes them to MANUAL_REVIEW
                        customid_manual_review.append(lookup_id_collection)
                        search_terms.append((
                            lookup_id_collection,
                            '',   # no search term
                            '',   # no au: fragment
                            '',   # no se: fragment
                            'Y',  # mn-review-flag: mn: search not possible
                            'Y',  # au-review-flag: no au: anchor either
                        ))
                        logger.warning(
                            "Skipping API search for customid (will need manual review): %s",
                            lookup_id_collection
                        )
                    else:
                        # Unknown format - log a warning
                        logger.error(
                            "Unknown lookup_id format (neither xtid nor customid): %s", lookup_id
                        )

        # Write search terms file (only xtid records)
        output_path = Path(output_file)
        with open(output_path, 'w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file, delimiter='\t')
            writer.writerow([
                'lookupIDcollection',
                'discovery-api-search',
                'au-fragment',
                'se-fragment',
                'mn-review-flag',
                'au-review-flag',
            ])
            writer.writerows(search_terms)

        logger.info("Generated %s search terms (xtid only) in %s", len(search_terms), output_file)

        if customid_manual_review:
            logger.warning("Found %s customid records that will need manual review",
                           len(customid_manual_review))
            logger.warning(
                "These records will not be searched via API (customid doesn't match MARC 028)"
            )

        return str(output_path)

    def analyze_kbart_changes(self) -> Dict[str, List]:
        """
        Analyze what records need to be added/removed from KBART.
        """
        logger.info("Analyzing KBART changes...")

        # Get all current title IDs from MARC data
        current_title_ids = set()
        for record in self.current_records:
            # Extract title ID from lookup_id
            id_match = re.search(r'(?:xtid|customid)=(.+)\$', record['lookup_id'])
            if id_match:
                current_title_ids.add(id_match.group(1))

        # Get existing title IDs from KBART
        existing_title_ids = set(self.kbart_lookup.keys())

        # Analyze changes
        changes = {
            'keep': list(current_title_ids & existing_title_ids),  # In both
            'remove': list(existing_title_ids - current_title_ids),  # In KBART but not current
            'new': list(current_title_ids - existing_title_ids)  # In current but not KBART
        }

        self.stats['removed_records'] = len(changes['remove'])

        logger.info("KBART Analysis: %s keep, %s remove, %s new",
                    len(changes['keep']), len(changes['remove']), len(changes['new']))

        return changes

    def _get_original_nclive_ocn(self, lookup_id: str) -> str:
        """Return the original NC Live OCN for a lookup_id, or empty string."""
        lookup_data = self.infobase_lookup.get(lookup_id)
        if isinstance(lookup_data, dict):
            return lookup_data.get('original_nclive_ocn', '')
        return ""

    def _resolve_verified_ocn_and_source(
            self, record: Dict, new_oclc_data: dict
    ) -> Tuple[str, str]:
        """Determine the final verified OCN and its source label for a MARC record."""
        verified_ocn, source = self._determine_best_oclc_number(record)
        if source != "NEEDS_SEARCH":
            return verified_ocn, source
        lookup_id = record['lookup_id']
        if lookup_id in new_oclc_data:
            return new_oclc_data[lookup_id], "API_SEARCH"
        logger.warning("No API result found for %s, marking for manual review", lookup_id)
        return "X", "MANUAL_REVIEW"

    def _load_oclc_results(self, oclc_results_file: str) -> dict:
        """Load oclc_results.csv and return {base_lookup_id: oclc_number}."""
        oclc_results_path = Path(oclc_results_file)
        if not oclc_results_path.exists():
            logger.warning("OCLC results file not found: %s", oclc_results_file)
            return {}
        try:
            logger.info("Loading OCLC search results from %s", oclc_results_file)
            results_df = pd.read_csv(
                oclc_results_file, dtype={'oclcNumber': 'str', 'lookupID': 'str'}
            )
            # Drop placeholder rows written by main.py for skipped/unmatched records.
            # Those rows have an empty oclcNumber; storing them would override the
            # correct MANUAL_REVIEW routing in _resolve_verified_ocn_and_source.
            results_df = results_df[
                results_df['oclcNumber'].fillna('').str.strip() != ''
            ]
            new_oclc_data = {}
            for lookup_id_from_csv, group in results_df.groupby('lookupID'):
                base_lookup_id = (
                    lookup_id_from_csv[:-3]
                    if lookup_id_from_csv.endswith(('$fod', '$jfk'))
                    else lookup_id_from_csv
                )
                if len(group) == 1:
                    oclc_number = str(group.iloc[0]['oclcNumber']).strip()
                    new_oclc_data[base_lookup_id] = oclc_number
                    logger.debug("Single match for %s: %s", base_lookup_id, oclc_number)
                else:
                    best_oclc = self._select_best_oclc_from_multiple(base_lookup_id, group)
                    if best_oclc:
                        new_oclc_data[base_lookup_id] = best_oclc
                        logger.info(
                            "Multiple matches for %s, selected: %s", base_lookup_id, best_oclc
                        )
                    else:
                        logger.warning(
                            "Could not select best OCLC for %s from %s options",
                            base_lookup_id, len(group)
                        )
            logger.info("Processed %s OCLC search results", len(new_oclc_data))
            logger.info("Sample lookup keys: %s", list(new_oclc_data.keys())[:5])
            return new_oclc_data
        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError, KeyError) as e:
            logger.error("Error loading OCLC results: %s", e)
            return {}

    def _fix_collection_types(
            self, updated_df: pd.DataFrame, current_marc_records: dict
    ) -> Tuple[pd.DataFrame, int]:
        """Update collection_type to match current MARC files; return (df, fix_count)."""
        count = 0
        for index, row in updated_df.iterrows():
            lookup_id_collection = str(row.get('lookupIDcollection', '')).strip()
            if lookup_id_collection not in current_marc_records:
                continue
            current_collection_type = str(row.get('collection_type', '')).strip()
            marc_collection_type = current_marc_records[lookup_id_collection]['collection_type']
            if current_collection_type != marc_collection_type:
                updated_df.at[index, 'collection_type'] = marc_collection_type
                updated_df.at[index, 'last_updated'] = datetime.now().strftime('%Y-%m-%d')
                count += 1
                logger.info(
                    "Updated collection_type for %s: '%s' -> '%s' (from current MARC)",
                    lookup_id_collection, current_collection_type, marc_collection_type
                )
        return updated_df, count

    def _apply_marc_updates(
            self, updated_df: pd.DataFrame, new_oclc_data: dict
    ) -> Tuple[pd.DataFrame, dict]:
        """Apply current MARC records to updated_df; return (df, counts dict)."""
        updates_applied = 0
        new_records_added = 0
        api_matches_found = 0
        today = datetime.now().strftime('%Y-%m-%d')
        for record in self.current_records:
            lookup_id = record['lookup_id']
            lookup_id_collection = record['lookup_id_collection']
            verified_ocn, source = self._resolve_verified_ocn_and_source(record, new_oclc_data)
            if source == "API_SEARCH":
                api_matches_found += 1
                logger.info("Using API result for %s: %s", lookup_id, verified_ocn)
            original_nclive_ocn = self._get_original_nclive_ocn(lookup_id)
            mask = updated_df['lookupIDcollection'] == lookup_id_collection
            if mask.any():
                updated_df.loc[mask, 'originalNCLiveOCN'] = original_nclive_ocn
                updated_df.loc[mask, 'marcOCN'] = record['marc_035_ocn']
                updated_df.loc[mask, 'verifiedOCN'] = verified_ocn
                updated_df.loc[mask, 'source'] = source
                updated_df.loc[mask, 'title'] = record['title']
                updated_df.loc[mask, 'collection_type'] = record['collection_type']
                updated_df.loc[mask, 'last_updated'] = today
                updated_df.loc[mask, 'avod_title_id'] = record.get('avod_title_id', '')
                updates_applied += 1
                logger.debug("Updated existing record: %s", lookup_id_collection)
            else:
                new_record = {
                    'lookupID': lookup_id,
                    'lookupIDcollection': lookup_id_collection,
                    'originalNCLiveOCN': original_nclive_ocn,
                    'marcOCN': record['marc_035_ocn'],
                    'verifiedOCN': verified_ocn,
                    'source': source,
                    'title': record['title'],
                    'collection_type': record['collection_type'],
                    'last_updated': today,
                    'avod_title_id': record.get('avod_title_id', '')
                }
                updated_df = pd.concat(
                    [updated_df, pd.DataFrame([new_record])], ignore_index=True
                )
                new_records_added += 1
                logger.debug("Added new record: %s", lookup_id_collection)
        return updated_df, {
            'updates_applied': updates_applied,
            'new_records_added': new_records_added,
            'api_matches_found': api_matches_found
        }

    def _log_lookup_diagnostics(self, updated_df: pd.DataFrame) -> None:
        """Log collection type distribution and duplicate analysis for the lookup file."""
        collection_counts = updated_df['collection_type'].value_counts()
        logger.info("Current collection type distribution: %s", collection_counts.to_dict())
        lookup_id_counts = updated_df['lookupID'].value_counts()
        duplicates = lookup_id_counts[lookup_id_counts > 1]
        if len(duplicates) > 0:
            logger.info(
                "Found %s title IDs in multiple collections (this is expected)", len(duplicates)
            )
            logger.info("Sample duplicates: %s", duplicates.head().to_dict())
        collection_id_counts = updated_df['lookupIDcollection'].value_counts()
        collection_duplicates = collection_id_counts[collection_id_counts > 1]
        if len(collection_duplicates) > 0:
            logger.error("ERROR: %s lookupIDcollection duplicates!", len(collection_duplicates))
            logger.error("Duplicate entries: %s", collection_duplicates.to_dict())
        else:
            logger.info("✅ No duplicate lookupIDcollection entries - data integrity maintained")

    def create_updated_lookup_file(self, oclc_results_file: str = "oclc_results.csv",
                                   output_file: str = "InfobaseLookup_updated.csv"):
        """
        Create updated InfobaseLookup file by merging existing data with new OCLC search results.
        FIXED VERSION - Handles duplicate title_IDs in different collections correctly.
        """
        logger.info("Creating updated lookup file...")
        new_oclc_data = self._load_oclc_results(oclc_results_file)
        try:
            original_df = pd.read_csv(
                self.lookup_file, dtype=OCLC_DTYPES, keep_default_na=False
            )
            logger.info("Loaded %s original records from %s", len(original_df), self.lookup_file)
        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
            logger.error("Could not load original lookup file: %s", e)
            return None
        updated_df = original_df.copy()
        current_marc_records = {r['lookup_id_collection']: r for r in self.current_records}
        logger.info("Current MARC processing covers %s lookupIDcollection entries",
                    len(current_marc_records))
        logger.info("Applying updates using lookupIDcollection as the primary key...")
        updated_df, collection_type_fixes = self._fix_collection_types(
            updated_df, current_marc_records
        )
        logger.info("Applied collection type fixes to %s records", collection_type_fixes)
        updated_df, counts = self._apply_marc_updates(updated_df, new_oclc_data)
        logger.info("Applied updates to %s existing records", counts['updates_applied'])
        logger.info("Added %s new records", counts['new_records_added'])
        logger.info("Found %s API matches from OCLC results", counts['api_matches_found'])
        logger.info("Updated lookup file has %s records", len(updated_df))
        self._log_lookup_diagnostics(updated_df)
        updated_df.to_csv(output_file, index=False)
        logger.info("Saved updated lookup file: %s", output_file)
        return output_file

    def _select_best_oclc_from_multiple(
            self, lookup_id: str, oclc_group: pd.DataFrame
    ) -> Optional[str]:
        """
        Select the best OCLC number when multiple options exist for a lookup_id.

        Args:
            lookup_id: The lookup ID with multiple OCLC matches
            oclc_group: DataFrame group with multiple OCLC entries

        Returns:
            Best OCLC number or None if no good option found
        """

        # Strategy 1: Prefer videos with "Video-Digital" format
        video_digital = oclc_group[
            (oclc_group['generalFormat'] == 'Video') &
            (oclc_group['specificFormat'] == 'Digital')
        ]

        if len(video_digital) == 1:
            logger.debug("Selected Video-Digital match for %s", lookup_id)
            return str(video_digital.iloc[0]['oclcNumber']).strip()

        # Strategy 2: Prefer entries where isElectronicVideo == "Yes"
        electronic_videos = oclc_group[oclc_group['isElectronicVideo'] == 'True']

        if len(electronic_videos) == 1:
            logger.debug("Selected electronic video match for %s", lookup_id)
            return str(electronic_videos.iloc[0]['oclcNumber']).strip()
        if len(electronic_videos) > 1:
            # Multiple electronic videos - take the first one (they're likely duplicates)
            logger.debug("Multiple electronic videos for %s, taking first", lookup_id)
            return str(electronic_videos.iloc[0]['oclcNumber']).strip()

        # Strategy 3: Take the first entry (fallback)
        logger.debug("Using fallback selection for %s", lookup_id)
        return str(oclc_group.iloc[0]['oclcNumber']).strip()

    def print_statistics(self):
        """Print processing statistics with hierarchical lookup details."""
        print("\n" + "="*60)
        print("MARC PROCESSING STATISTICS - HIERARCHICAL LOOKUP")
        print("="*60)
        print(f"Total MARC records processed: {self.stats['total_processed']}")
        print(f"Valid title IDs extracted: {len(self.current_records)}")
        print(f"Rejected records: {len(self.stats['rejected_records'])}")
        print("HIERARCHICAL LOOKUP RESULTS:")
        print(f"  1. InfobaseLookup matches (PRIMARY): {self.stats['matched_infobase_lookup']}")
        print(f"  2. KBART matches (SECONDARY): {self.stats['matched_kbart']}")
        print(f"  3. MARC 035 matches (TERTIARY): {self.stats['matched_marc_035']}")
        print(f"  4. Need API search: {self.stats['needs_api_search']}")
        print("KBART changes:")
        print(f"  Records removed from collection: {self.stats['removed_records']}")
        print("="*60)

        # Print rejected records summary
        if self.stats['rejected_records']:
            print(f"REJECTED RECORDS SUMMARY ({len(self.stats['rejected_records'])} total)")
            print("-" * 60)
            for i, rejection in enumerate(self.stats['rejected_records'][:10], 1):  # Show first 10
                print(f"{i:2d}. Title ID: {rejection['nc_live_title_id']}")
                print(f"    Title: {rejection['title'][:60]}...")
                print(f"    Reason: {rejection['reason']}")
                print()

            if len(self.stats['rejected_records']) > 10:
                print(
                    f"    ... and {len(self.stats['rejected_records']) - 10} more rejected records"
                )

            print("Full rejected records list saved to: rejected_records.csv")

    def save_rejected_records(self, output_file: str = "rejected_records.csv"):
        """Save rejected records to CSV for detailed analysis."""
        if not self.stats['rejected_records']:
            logger.info("No rejected records to save.")
            return

        try:
            df = pd.DataFrame(self.stats['rejected_records'])
            df.to_csv(output_file, index=False)
            logger.info("Saved %s rejected records to %s",
                        len(self.stats['rejected_records']), output_file)
        except (OSError, ValueError) as e:
            logger.error("Could not save rejected records: %s", e)

            # Fallback to basic CSV writing
            try:
                with open(output_file, 'w', newline='', encoding='utf-8') as file:
                    writer = csv.writer(file)
                    writer.writerow([
                        'nc_live_title_id',
                         'control_001',
                         'title',
                         'reason',
                         'raw_title_ids',
                         'url'
                         ])
                    for rejection in self.stats['rejected_records']:
                        writer.writerow([
                            rejection['nc_live_title_id'],
                            rejection['control_001'],
                            rejection['title'],
                            rejection['reason'],
                            str(rejection['raw_title_ids']),
                            rejection['url']
                        ])
                logger.info("Saved %s rejected records to %s (fallback method)",
                            len(self.stats['rejected_records']), output_file)
            except (OSError, csv.Error) as e2:
                logger.error("Could not save rejected records with fallback method: %s", e2)

    def debug_marc_field_extraction(self, num_samples: int = 5):
        """Debug MARC field extraction to identify missing 028$a and NC Live 035 patterns."""
        print("\n" + "="*80)
        print("DEBUG: MARC FIELD EXTRACTION ANALYSIS")
        print("="*80)

        missing_028_count = 0
        nc_live_035_patterns = []

        sample_records = self.current_records[:num_samples]

        for i, record in enumerate(sample_records, 1):
            print(f"\n--- RECORD {i} ---")
            print(f"Lookup ID: {record['lookup_id']}")
            print(f"Raw Title IDs (028$a): {record.get('title_ids_raw', [])}")
            print(f"URL: {record.get('url', '')}")
            print(f"MARC 035 OCN: {record['marc_035_ocn']}")

            # Check for missing 028$a
            if not record.get('title_ids_raw'):
                missing_028_count += 1
                print("  MISSING MARC 028$a - using URL fallback")

            # Extract title ID for NC Live pattern analysis
            id_match = re.search(r'(?:xtid|customid)=(.+)\$', record['lookup_id'])
            if id_match:
                title_id_numeric = id_match.group(1)
                nc_live_pattern = f"1000{title_id_numeric}"
                nc_live_035_patterns.append(nc_live_pattern)
                print(f"  Title ID: {title_id_numeric}")
                print(f"  Expected NC Live 035: {nc_live_pattern}")

                # Check if current OCN matches NC Live pattern
                if record['marc_035_ocn'] == nc_live_pattern:
                    print("  Detected NC Live 035 pattern (would be rejected)")
                elif record['marc_035_ocn'] != "N/A":
                    print(f"  Valid OCLC number found: {record['marc_035_ocn']}")

        print("SUMMARY:")
        print(f"Records missing MARC 028$a: {missing_028_count}/{len(sample_records)}")
        print(f"NC Live 035 patterns to watch for: {nc_live_035_patterns[:3]}...")

    def debug_hierarchical_lookup(self, num_samples: int = 10):
        """Debug the hierarchical lookup process."""
        print("\n" + "="*80)
        print("DEBUG: HIERARCHICAL LOOKUP ANALYSIS")
        print("="*80)

        print("Authority data loaded:")
        print(f"  PRIMARY (InfobaseLookup): {len(self.infobase_lookup)} entries")
        print(f"  SECONDARY (KBART): {len(self.kbart_lookup)} entries")

        # Sample lookups
        sample_records = self.current_records[:num_samples]

        for i, record in enumerate(sample_records, 1):
            print(f"\n--- RECORD {i} ---")
            print(f"Lookup ID: {record['lookup_id']}")
            print(f"MARC 035 OCN: {record['marc_035_ocn']}")

            oclc_number, source = self._determine_best_oclc_number(record)
            print(f"BEST MATCH: {oclc_number} (source: {source})")

            # Show what each level found
            id_match = re.search(r'(?:xtid|customid)=(.+)\$', record['lookup_id'])
            title_id_numeric = id_match.group(1) if id_match else None

            print(
                f"  InfobaseLookup check: "
                f"{self.infobase_lookup.get(record['lookup_id'], 'NOT_FOUND')}"
            )
            print(
                f"  KBART check ({title_id_numeric}): "
                f"{self.kbart_lookup.get(title_id_numeric, 'NOT_FOUND')}"
            )
            print(f"  MARC 035 check: {record['marc_035_ocn']}")

def main():
    """Main function to run the MARC processor."""
    print("Infobase MARC Processor v4 - Hierarchical Lookup")
    print("=" * 60)

    # Initialize processor
    processor = InfobaseMARCProcessor()

    # Load existing data with hierarchical priority
    processor.load_existing_data()

    # Process MARC files
    records = processor.process_marc_files()

    if not records:
        print("No records to process. Exiting.")
        return

    # Generate search terms (only for records needing API lookup)
    search_terms_file = processor.generate_search_terms()
    print(f"Generated search terms file: {search_terms_file}")

    # Print statistics
    processor.print_statistics()

    # Save rejected records for analysis
    processor.save_rejected_records()

    print("Next step:")
    if processor.stats['needs_api_search'] > 0:
        print("1. Run: python main.py")
        print(f"   This will search OCLC API for {processor.stats['needs_api_search']} "
              f"records using {search_terms_file}"
              )
        print("2. After main.py, run: python marc_processor.py with --update-lookup flag")
    else:
        print("All records have verified OCLC numbers! No API search needed.")
        print("   You can proceed directly to: python kbart_integration.py")

    print("Hierarchical Lookup Performance:")
    total_with_oclc = (processor.stats['matched_infobase_lookup'] +
                      processor.stats['matched_kbart'] +
                      processor.stats['matched_marc_035'])
    if len(records) > 0:
        success_rate = (total_with_oclc / len(records)) * 100
        print(f"   - Success rate: {success_rate:.1f}% ({total_with_oclc}/{len(records)})")
        print(f"   - InfobaseLookup coverage: "
              f"{(processor.stats['matched_infobase_lookup']/len(records))*100:.1f}%")
        print(f"   - KBART coverage: "
              f"{(processor.stats['matched_kbart']/len(records))*100:.1f}%")
        print("   - MARC 035 coverage: "
              f"{(processor.stats['matched_marc_035']/len(records))*100:.1f}%")

def debug_main():
    """Debug version of main function with additional analysis."""
    print("Infobase MARC Processor v4 - DEBUG MODE")
    print("=" * 60)

    # Initialize processor
    processor = InfobaseMARCProcessor()

    # Load existing data
    processor.load_existing_data()

    # Process MARC files
    records = processor.process_marc_files()

    if not records:
        print("No records to process. Exiting.")
        return

    # Debug hierarchical lookup
    processor.debug_hierarchical_lookup()

    # Generate search terms
    search_terms_file = processor.generate_search_terms()
    print(f"Generated search terms file: {search_terms_file}")

    # Analyze KBART changes
    # changes = processor.analyze_kbart_changes()

    # Print statistics
    processor.print_statistics()

    # Save rejected records for analysis
    processor.save_rejected_records()

def update_lookup_main():
    """Second-phase entry point: update lookup file with OCLC search results."""
    processor = InfobaseMARCProcessor()
    processor.load_existing_data()
    processor.process_marc_files()

    updated_lookup = processor.create_updated_lookup_file()

    print(f"Updated lookup file: {updated_lookup}")
    print("Next steps:")
    print("1. Run: python extended_marc_processor.py (for manual review)")
    print("1. Run: python extended_marc_processor.py "
          "with --process-updates manual_review_searches.csv flag")
    print("2. Run: python kbart_integration.py (to create final KBART)")
    print("3. Run: python kbart_entry_validator.py (to validate)")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--debug":
        debug_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "--update-lookup":
        update_lookup_main()
    else:
        # First phase: process MARC and generate search terms
        main()
