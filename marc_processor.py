# marc_processor_v4.py
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

import csv
import pandas as pd
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional
import logging
from datetime import datetime
import json
from config import Config

# MARC processing library
try:
    from pymarc import MARCReader, Record, Field
except ImportError:
    print("Please install pymarc: pip install pymarc")
    exit(1)

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

        # Load OCLC dtypes configuration - handle missing credentials gracefully
        try:
            config = Config()
            self.oclc_dtypes = config.OCLC_DTYPES
        except ValueError as e:
            if "Missing required environment variables" in str(e):
                # Use default dtypes if credentials aren't available
                logger.warning("OCLC API credentials not loaded - using default dtypes for CSV reading")
                self.oclc_dtypes = {
                    'marcOCN': 'str',
                    'originalNCLiveOCN': 'str',
                    'verifiedOCN': 'str',
                    'oclcNumber': 'str',
                    'lookupID': 'str',
                    'lookupIDcollection': 'str',
                    'source': 'str',
                    'title': 'str',
                    'collection_type': 'str'
                }
            else:
                raise  # Re-raise if it's a different error
        
        # Create directories if they don't exist
        self.kbart_dir.mkdir(exist_ok=True)
        
        # Storage for processed data
        self.current_records = []  # Records from current MARC files
        self.infobase_lookup = {}  # PRIMARY: Manually verified OCLC numbers from InfobaseLookup.csv
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
    
    def load_existing_data(self):
        """
        Load existing lookup data with hierarchical priority:
        1. InfobaseLookup.csv (PRIMARY AUTHORITY)
        2. KBART files (SECONDARY)
        """
        logger.info("Loading existing lookup data with hierarchical priority...")
        
        # PRIMARY: Load InfobaseLookup.csv (manually verified matches)
        if self.lookup_file.exists():
            try:
                df = pd.read_csv(self.lookup_file, dtype=self.oclc_dtypes, keep_default_na=False)
                logger.info(f"InfobaseLookup columns found: {list(df.columns)}")
                
                # Based on your sample structure, use 'verifiedOCN' column
                verified_ocn_col = 'verifiedOCN'
                lookup_id_col = 'lookupID'
                
                if verified_ocn_col not in df.columns:
                    logger.warning("Could not find 'verifiedOCN' column in InfobaseLookup.csv")
                    logger.info("Available columns: " + ", ".join(df.columns))
                    # Fallback to automatic detection
                    for col in df.columns:
                        col_lower = col.lower()
                        if 'verified' in col_lower and 'ocn' in col_lower:
                            verified_ocn_col = col
                            break
                        elif 'oclc' in col_lower and ('number' in col_lower or 'num' in col_lower):
                            verified_ocn_col = col
                            break
                    logger.warning(f"Using fallback column: {verified_ocn_col}")
                else:
                    logger.info(f"Using expected column: {verified_ocn_col}")
                
                if verified_ocn_col in df.columns and lookup_id_col in df.columns:
                    # Create lookup dictionary: lookupID -> verified OCLC number
                    for _, row in df.iterrows():
                        lookup_id = str(row.get(lookup_id_col, '')).strip()
                        verified_ocn = str(row.get(verified_ocn_col, '')).strip()
                        
                        # Also capture original NC Live OCN if available for comparison
                        original_nclive_ocn = ""
                        if 'originalNCLiveOCN' in df.columns:
                            original_nclive_ocn = str(row.get('originalNCLiveOCN', '')).strip()
                        elif 'InfobaseMRCkey_original' in df.columns:
                            # Parse pipe-delimited format: "OCN|lookupID"
                            mrc_key = str(row.get('InfobaseMRCkey_original', '')).strip()
                            if '|' in mrc_key:
                                original_nclive_ocn = mrc_key.split('|')[0].strip()
                        
                        # Only store valid, verified OCLC numbers (not empty or NaN)
                        # Your data uses integers, so handle both int and string formats
                        if (lookup_id and verified_ocn and 
                            verified_ocn.lower() not in ['', 'X', 'x', 'nan', 'null'] and
                            str(verified_ocn) != 'nan'):
                            # Store both verified OCN and original for comparison
                            self.infobase_lookup[lookup_id] = {
                                'verified_ocn': verified_ocn,
                                'original_nclive_ocn': original_nclive_ocn
                            }
                    
                    logger.info(f"PRIMARY: Loaded {len(self.infobase_lookup)} manually verified entries from InfobaseLookup.csv")
                    
            except Exception as e:
                logger.warning(f"Could not load InfobaseLookup file: {e}")
        else:
            logger.warning(f"InfobaseLookup file not found: {self.lookup_file}")
        
        # SECONDARY: Load current KBART files
        kbart_records_loaded = 0
        for kbart_file in self.kbart_dir.glob("*.txt"):
            try:
                df = pd.read_csv(kbart_file, sep='\t', low_memory=False)
                
                # Handle different possible column names in KBART files
                title_id_col = None
                oclc_col = None
                
                for col in df.columns:
                    col_lower = col.lower()
                    if 'title_id' in col_lower or 'titleid' in col_lower:
                        title_id_col = col
                    elif 'oclc' in col_lower and ('number' in col_lower or 'num' in col_lower):
                        oclc_col = col
                
                if title_id_col and oclc_col:
                    for _, row in df.iterrows():
                        title_id_encoded = str(row.get(title_id_col, '')).strip()
                        oclc_num = str(row.get(oclc_col, '')).strip()
                        
                        if (title_id_encoded and oclc_num and 
                            oclc_num.lower() not in ['', 'nan', 'null']):
                            
                            # DECODE the percent-encoded title_id for matching
                            title_id_decoded = self._decode_url_encoding(title_id_encoded)
                            
                            # Create lookup ID format to match your InfobaseLookup format
                            # Convert from xtid%3D184316 to xtid=184316$
                            if title_id_decoded.startswith(('xtid=', 'customid=')):
                                lookup_id_format = f"{title_id_decoded}$"
                                # Extract numeric ID for storage key
                                numeric_id = title_id_decoded.split('=')[-1]
                            else:
                                # Handle cases where it might be just the numeric ID
                                lookup_id_format = f"customid={title_id_decoded}$"
                                numeric_id = title_id_decoded
                            
                            # Only store if not already in primary lookup
                            if lookup_id_format not in self.infobase_lookup:
                                self.kbart_lookup[numeric_id] = {
                                    'oclc_number': oclc_num,
                                    'encoded_title_id': title_id_encoded,  # Keep original encoded format
                                    'decoded_title_id': title_id_decoded   # Decoded for matching
                                }
                                kbart_records_loaded += 1
                                
                logger.info(f"SECONDARY: Loaded {len(df)} entries from {kbart_file.name}")
        
            except Exception as e:
                logger.warning(f"Could not load KBART file {kbart_file}: {e}")
				
        logger.info(f"SECONDARY: Total unique KBART records loaded: {kbart_records_loaded}")
        logger.info(f"TOTAL AUTHORITY RECORDS: {len(self.infobase_lookup)} (primary) + {len(self.kbart_lookup)} (secondary)")
    
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
                    
                    # Extract fields following your OpenRefine logic
                    record_data = self._extract_record_fields(record)
                    
                    if record_data:
                        records.append(record_data)
                    else:
                        # Track rejected records with detailed info
                        control_001 = record['001'].data if record['001'] else "Unknown"
                        field_245_a = self._get_subfield_values(record, '245', 'a')
                        title = field_245_a[0] if field_245_a else "No title found"
                        field_028_a = self._get_subfield_values(record, '028', 'a')
                        field_856_u = self._get_subfield_values(record, '856', 'u')
                        
                        # Extract numeric title ID for tracking
                        title_id_numeric = "Unknown"
                        if field_028_a:
                            first_title_id = field_028_a[0].strip()
                            if first_title_id.isdigit():
                                title_id_numeric = first_title_id
                            else:
                                numeric_match = re.search(r'\d+', first_title_id)
                                if numeric_match:
                                    title_id_numeric = numeric_match.group()
                        
                        rejection_info = {
                            'nc_live_title_id': title_id_numeric,
                            'control_001': control_001,
                            'title': title,
                            'reason': 'Failed title ID validation or URL matching',
                            'raw_title_ids': field_028_a,
                            'url': field_856_u[0] if field_856_u else "No URL"
                        }
                        
                        self.stats['rejected_records'].append(rejection_info)
                        
        except Exception as e:
            logger.error(f"Error reading MARC file {marc_file}: {e}")
        
        logger.info(f"Extracted {len(records)} valid records from {marc_file.name}")
        return records
    
    def _extract_record_fields(self, record: Record) -> Optional[Dict]:
        """Extract and process fields from a single MARC record."""
        try:
            # Extract key fields (following exportMARCsearch.txt specification)
            control_001 = record['001'].data if record['001'] else ""
            field_028_a = self._get_subfield_values(record, '028', 'a')  # Title IDs
            field_028_b = self._get_subfield_values(record, '028', 'b')  # Publisher/label
            field_035_a = self._get_subfield_values(record, '035', 'a')  # OCLC numbers
            field_245_a = self._get_subfield_values(record, '245', 'a')  # Title
            field_856_u = self._get_subfield_values(record, '856', 'u')  # URLs
            field_856_z = self._get_subfield_values(record, '856', 'z')  # URL descriptions
            
            # Process 028$a field (title IDs) - can contain multiple IDs separated by semicolons
            title_ids = []
            for value in field_028_a:
                if ';' in value:
                    title_ids.extend([id.strip() for id in value.split(';')])
                else:
                    title_ids.append(value.strip())
            
            # Find the correct title ID by matching with 856$u URL
            lookup_id = self._validate_title_id_with_url(title_ids, field_856_u, field_856_z)
            
            if not lookup_id:
                return None
            
            # Extract and clean OCLC number from 035$a (TERTIARY source)
            marc_035_ocn = self._extract_oclc_number(field_035_a, title_ids)
            
            # Determine collection type
            collection_type = 'fod' if any('Films on Demand' in z for z in field_856_z) else 'jfk'
            lookup_id_collection = f"{lookup_id}{collection_type}"
            
            record_data = {
                'control_001': control_001,
                'lookup_id': lookup_id,
                'lookup_id_collection': lookup_id_collection,
                'marc_035_ocn': marc_035_ocn,  # TERTIARY: OCN from MARC (unreliable)
                'title': field_245_a[0] if field_245_a else "",
                'url': field_856_u[0] if field_856_u else "",
                'collection_type': collection_type,
                'title_ids_raw': field_028_a,
                'url_description': field_856_z[0] if field_856_z else ""
            }
            
            return record_data
            
        except Exception as e:
            logger.warning(f"Error processing record: {e}")
            return None
    
    def _get_subfield_values(self, record: Record, field_tag: str, subfield_code: str) -> List[str]:
        """Extract all values for a specific subfield."""
        values = []
        fields = record.get_fields(field_tag)
        for field in fields:
            subfields = field.get_subfields(subfield_code)
            values.extend(subfields)
        return values
    
    def _validate_title_id_with_url(self, title_ids: List[str], urls: List[str], url_descriptions: List[str]) -> Optional[str]:
        """
        Validate title ID by checking if it appears in the 856$u URL.
        Only accepts title IDs that come from xtid= or customid= URL parameters.
        
        Fallback: If no title IDs found in MARC 028$a, extract from URL.
        """
        if not urls:
            return None
        
        url = urls[0].lower()  # Convert to lowercase for matching
        
        # First, try to match existing title IDs from MARC 028$a
        if title_ids:
            for title_id in title_ids:
                title_id_clean = title_id.strip()
                
                # Skip if this looks like it's already formatted
                if title_id_clean.startswith(('xtid=', 'customid=')):
                    continue
                    
                # Check if this title ID appears in the URL
                # Look for pattern like "id=<title_id>&" or "customid=<title_id>&"
                if f"id={title_id_clean}&" in url or f"customid={title_id_clean}&" in url:
                    # Determine the correct prefix based on URL content
                    if "xtid=" in url:
                        return f"xtid={title_id_clean}$"
                    elif "customid=" in url:
                        return f"customid={title_id_clean}$"
                    else:
                        # Skip this title ID if it doesn't match xtid= or customid= patterns
                        continue
        
        # FALLBACK: If no title IDs in MARC 028$a, extract directly from URL
        # This handles cases where MARC 028$a is missing
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
    
    def _determine_collection_type_with_fallback(self, record: Dict, original_lookup_data: Dict) -> str:
        """
        Determine collection type with fallback to original InfobaseLookup data.
        """
        lookup_id = record['lookup_id']

        # Method 1: Use MARC 856$z field if available (current MARC records)
        if 'url_description' in record and record['url_description']:
            url_descriptions = [record['url_description']]
            collection_type = 'fod' if any('Films on Demand' in z for z in url_descriptions) else 'jfk'
            return collection_type
        
        # Method 2: Fallback to original InfobaseLookup data (preserved records)
        if lookup_id in original_lookup_data:
            original_record = original_lookup_data[lookup_id]
            if isinstance(original_record, dict):
                lookup_id_collection = original_record.get('lookupIDcollection', '')
                if lookup_id_collection.endswith('fod'):
                    return 'fod'
                elif lookup_id_collection.endswith('jfk'):
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
        
        # Extract numeric title ID for KBART lookup
        title_id_numeric = None
        id_match = re.search(r'(?:xtid|customid)=(.+)\$', lookup_id)
        if id_match:
            title_id_numeric = id_match.group(1)
        
        # HIERARCHICAL LOOKUP:
        
        # 1. PRIMARY: Check InfobaseLookup.csv (manually verified matches)
        if lookup_id in self.infobase_lookup:
            lookup_data = self.infobase_lookup[lookup_id]
            oclc_num = lookup_data['verified_ocn'] if isinstance(lookup_data, dict) else lookup_data
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
        fod_files = [f for f in self.marc_dir.glob("FOD*.mrc") if f.parent == self.marc_dir]
        just_files = [f for f in self.marc_dir.glob("[Jj]ust*.mrc") if f.parent == self.marc_dir]
        
        marc_files = fod_files + just_files
        
        if not marc_files:
            logger.warning(f"No MARC files found in {self.marc_dir}")
            return []
        
        logger.info(f"Found {len(marc_files)} MARC files to process")
        
        for marc_file in marc_files:
            logger.info(f"Processing {marc_file.name}...")
            records = self.extract_marc_fields(marc_file)
            all_records.extend(records)
        
        self.current_records = all_records
        logger.info(f"Total records processed: {len(all_records)}")
        
        return all_records
    
    def generate_search_terms(self, output_file: str = "search_terms.tsv") -> str:
        """
        Generate search_terms.tsv file for main.py OCLC API searches.
        Only includes records that need OCLC number lookup after hierarchical matching.
        """
        logger.info("Generating search terms using hierarchical lookup...")
        
        search_terms = []
        
        for record in self.current_records:
            lookup_id_collection = record['lookup_id_collection']
            
            # Use hierarchical lookup to determine best OCLC number
            oclc_number, source = self._determine_best_oclc_number(record)
            
            # Only add to search terms if no reliable OCLC number was found
            if source == "NEEDS_SEARCH":
                # Extract numeric title ID for search
                id_match = re.search(r'(?:xtid|customid)=(.+)\$', record['lookup_id'])
                if id_match:
                    search_id = id_match.group(1)
                    search_term = f"sn:{search_id}"  # Serial number search
                    search_terms.append((lookup_id_collection, search_term))
        
        # Write search terms file
        output_path = Path(output_file)
        with open(output_path, 'w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file, delimiter='\t')
            writer.writerow(['lookupIDcollection', 'discovery-api-search'])
            writer.writerows(search_terms)
        
        logger.info(f"Generated {len(search_terms)} search terms in {output_file}")
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
        
        logger.info(f"KBART Analysis: {len(changes['keep'])} keep, "
                   f"{len(changes['remove'])} remove, {len(changes['new'])} new")
        
        return changes
    
    def create_updated_lookup_file(self, oclc_results_file: str = "oclc_results.csv",
                                   output_file: str = "InfobaseLookup_updated.csv"):
        """
        Create updated InfobaseLookup file by merging existing data with new OCLC search results.
        FIXED VERSION - Handles duplicate title_IDs in different collections correctly.
        """
        logger.info("Creating updated lookup file...")
        
        # Load OCLC search results and handle multiple entries per lookupID
        new_oclc_data = {}
        oclc_results_path = Path(oclc_results_file)
        
        if oclc_results_path.exists():
            try:
                logger.info(f"Loading OCLC search results from {oclc_results_file}")
                results_df = pd.read_csv(oclc_results_file, dtype={'oclcNumber': 'str', 'lookupID': 'str'})
                
                # Group by lookupID to handle multiple OCLC numbers per lookup
                lookup_groups = results_df.groupby('lookupID')
                
                for lookup_id_from_csv, group in lookup_groups:
                    # Extract base lookupID from OCLC results
                    # Convert "xtid=296591$fod" -> "xtid=296591$"
                    if lookup_id_from_csv.endswith(('$fod', '$jfk')):
                        base_lookup_id = lookup_id_from_csv[:-3]  # Remove collection suffix, keep $
                    else:
                        base_lookup_id = lookup_id_from_csv
                    
                    if len(group) == 1:
                        # Single OCLC number - use it directly
                        oclc_number = str(group.iloc[0]['oclcNumber']).strip()
                        new_oclc_data[base_lookup_id] = oclc_number
                        logger.debug(f"Single match for {base_lookup_id}: {oclc_number}")
                    else:
                        # Multiple OCLC numbers - select best match
                        best_oclc = self._select_best_oclc_from_multiple(base_lookup_id, group)
                        if best_oclc:
                            new_oclc_data[base_lookup_id] = best_oclc
                            logger.info(f"Multiple matches for {base_lookup_id}, selected: {best_oclc}")
                        else:
                            logger.warning(f"Could not select best OCLC for {base_lookup_id} from {len(group)} options")
                
                logger.info(f"Processed {len(new_oclc_data)} OCLC search results")
                logger.info(f"Sample lookup keys: {list(new_oclc_data.keys())[:5]}")  # Debug info
                
            except Exception as e:
                logger.error(f"Error loading OCLC results: {e}")
                new_oclc_data = {}
        else:
            logger.warning(f"OCLC results file not found: {oclc_results_file}")

        # Load original lookup file to preserve existing records
        try:
            original_df = pd.read_csv(self.lookup_file, dtype=self.oclc_dtypes, keep_default_na=False)
            logger.info(f"Loaded {len(original_df)} original records from {self.lookup_file}")
        except Exception as e:
            logger.error(f"Could not load original lookup file: {e}")
            return None
        
        # Start with ALL original records (CRITICAL: preserves existing data)
        updated_df = original_df.copy()

        # Create a set of lookupIDcollections from current MARC processing for updates
        current_lookup_id_collections = {record['lookup_id_collection'] for record in self.current_records}
        logger.info(f"Current MARC processing covers {len(current_lookup_id_collections)} lookupIDcollection entries")

        # Create a mapping of current MARC records by lookupIDcollection for easy access
        current_marc_records = {record['lookup_id_collection']: record for record in self.current_records}

        # FIXED: Only update records that match both lookupID AND collection type
        logger.info("Applying updates using lookupIDcollection as the primary key...")
        collection_type_fixes = 0
        
        # First, mark records that need updates (matching current MARC processing)
        updated_df['needs_update'] = updated_df['lookupIDcollection'].isin(current_lookup_id_collections)
        
        for index, row in updated_df.iterrows():
            lookup_id_collection = str(row.get('lookupIDcollection', '')).strip()
            
            # ONLY process records that are in the current MARC processing
            if lookup_id_collection in current_marc_records:
                current_collection_type = str(row.get('collection_type', '')).strip()
                marc_record = current_marc_records[lookup_id_collection]
                marc_collection_type = marc_record['collection_type']
                
                # Update collection type to match what's in the current MARC files
                if current_collection_type != marc_collection_type:
                    updated_df.at[index, 'collection_type'] = marc_collection_type
                    updated_df.at[index, 'last_updated'] = datetime.now().strftime('%Y-%m-%d')
                    collection_type_fixes += 1
                    logger.info(f"Updated collection_type for {lookup_id_collection}: '{current_collection_type}' -> '{marc_collection_type}' (from current MARC)")
        
        logger.info(f"Applied collection type fixes to {collection_type_fixes} records")
        
        # Process updates for records from current MARC processing
        updates_applied = 0
        new_records_added = 0
        api_matches_found = 0
        
        for record in self.current_records:
            lookup_id = record['lookup_id']  # This is in format "xtid=296591$"
            lookup_id_collection = record['lookup_id_collection']  # This is "xtid=296591$fod" or "xtid=296591$jfk"
            marc_035_ocn = record['marc_035_ocn']

            # Use hierarchical lookup FIRST, then check for new API results
            verified_ocn, source = self._determine_best_oclc_number(record)

            # Get original NC LIVE OCN for comparison
            original_nclive_ocn = ""
            if lookup_id in self.infobase_lookup and isinstance(self.infobase_lookup[lookup_id], dict):
                original_nclive_ocn = self.infobase_lookup[lookup_id].get('original_nclive_ocn', '')

            # If hierarchical lookup found nothing, check new API results
            if source == "NEEDS_SEARCH":
                # Use the base lookup_id (already in correct format)
                if lookup_id in new_oclc_data:
                    verified_ocn = new_oclc_data[lookup_id]
                    source = "API_SEARCH"
                    api_matches_found += 1
                    logger.info(f"Using API result for {lookup_id}: {verified_ocn}")
                else:
                    verified_ocn = "X"  # Mark for manual review
                    source = "MANUAL_REVIEW"
                    logger.warning(f"No API result found for {lookup_id}, marking for manual review")

            # FIXED: Use lookupIDcollection as the unique key instead of just lookupID
            mask = updated_df['lookupIDcollection'] == lookup_id_collection
            if mask.any():
                # Update existing record with data from current MARC processing
                updated_df.loc[mask, 'originalNCLiveOCN'] = original_nclive_ocn
                updated_df.loc[mask, 'marcOCN'] = marc_035_ocn
                updated_df.loc[mask, 'verifiedOCN'] = verified_ocn
                updated_df.loc[mask, 'source'] = source
                updated_df.loc[mask, 'title'] = record['title']
                updated_df.loc[mask, 'collection_type'] = record['collection_type']  # Use MARC-derived collection type
                updated_df.loc[mask, 'last_updated'] = datetime.now().strftime('%Y-%m-%d')
                updates_applied += 1
                logger.debug(f"Updated existing record: {lookup_id_collection}")
            else:
                # New record not in original lookup - add it
                new_record = {
                    'lookupID': lookup_id,
                    'lookupIDcollection': lookup_id_collection,  # This is the unique key
                    'originalNCLiveOCN': original_nclive_ocn,
                    'marcOCN': marc_035_ocn,
                    'verifiedOCN': verified_ocn,
                    'source': source,
                    'title': record['title'],
                    'collection_type': record['collection_type'],  # Use MARC-derived collection type
                    'last_updated': datetime.now().strftime('%Y-%m-%d')
                }

                # Add new record to DataFrame
                updated_df = pd.concat([updated_df, pd.DataFrame([new_record])], ignore_index=True)
                new_records_added += 1
                logger.debug(f"Added new record: {lookup_id_collection}")

        # Clean up the temporary column
        updated_df = updated_df.drop('needs_update', axis=1)

        logger.info(f"Applied updates to {updates_applied} existing records")
        logger.info(f"Added {new_records_added} new records")
        logger.info(f"Found {api_matches_found} API matches from OCLC results")
        logger.info(f"Updated lookup file has {len(updated_df)} records")

        # DIAGNOSTIC: Show collection type distribution and duplicate analysis
        collection_counts = updated_df['collection_type'].value_counts()
        logger.info(f"Current collection type distribution: {collection_counts.to_dict()}")
        
        # Check for duplicates by lookupID (should be expected for titles in both collections)
        lookup_id_counts = updated_df['lookupID'].value_counts()
        duplicates = lookup_id_counts[lookup_id_counts > 1]
        if len(duplicates) > 0:
            logger.info(f"Found {len(duplicates)} title IDs that appear in multiple collections (this is expected)")
            logger.info(f"Sample duplicates: {duplicates.head().to_dict()}")
        
        # Verify no duplicates by lookupIDcollection (this would be an error)
        collection_id_counts = updated_df['lookupIDcollection'].value_counts()
        collection_duplicates = collection_id_counts[collection_id_counts > 1]
        if len(collection_duplicates) > 0:
            logger.error(f"ERROR: Found {len(collection_duplicates)} duplicate lookupIDcollection entries!")
            logger.error(f"Duplicate entries: {collection_duplicates.to_dict()}")
        else:
            logger.info("✅ No duplicate lookupIDcollection entries found - data integrity maintained")

        # Save updated lookup file
        updated_df.to_csv(output_file, index=False)
        logger.info(f"Saved updated lookup file: {output_file}")
        return output_file
    
    def _select_best_oclc_from_multiple(self, lookup_id: str, oclc_group: pd.DataFrame) -> Optional[str]:
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
            logger.debug(f"Selected Video-Digital match for {lookup_id}")
            return str(video_digital.iloc[0]['oclcNumber']).strip()
        
        # Strategy 2: Prefer entries where isElectronicVideo == "Yes"
        electronic_videos = oclc_group[oclc_group['isElectronicVideo'] == 'Yes']
        
        if len(electronic_videos) == 1:
            logger.debug(f"Selected electronic video match for {lookup_id}")
            return str(electronic_videos.iloc[0]['oclcNumber']).strip()
        elif len(electronic_videos) > 1:
            # Multiple electronic videos - take the first one (they're likely duplicates)
            logger.debug(f"Multiple electronic videos for {lookup_id}, taking first")
            return str(electronic_videos.iloc[0]['oclcNumber']).strip()
        
        # Strategy 3: Take the first entry (fallback)
        logger.debug(f"Using fallback selection for {lookup_id}")
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
        print(f"KBART changes:")
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
                print(f"    ... and {len(self.stats['rejected_records']) - 10} more rejected records")
            
            print(f"Full rejected records list saved to: rejected_records.csv")
    
    def save_rejected_records(self, output_file: str = "rejected_records.csv"):
        """Save rejected records to CSV for detailed analysis."""
        if not self.stats['rejected_records']:
            logger.info("No rejected records to save.")
            return
        
        try:
            df = pd.DataFrame(self.stats['rejected_records'])
            df.to_csv(output_file, index=False)
            logger.info(f"Saved {len(self.stats['rejected_records'])} rejected records to {output_file}")
        except Exception as e:
            logger.error(f"Could not save rejected records: {e}")
            
            # Fallback to basic CSV writing
            try:
                with open(output_file, 'w', newline='', encoding='utf-8') as file:
                    writer = csv.writer(file)
                    writer.writerow(['nc_live_title_id', 'control_001', 'title', 'reason', 'raw_title_ids', 'url'])
                    for rejection in self.stats['rejected_records']:
                        writer.writerow([
                            rejection['nc_live_title_id'],
                            rejection['control_001'],
                            rejection['title'],
                            rejection['reason'],
                            str(rejection['raw_title_ids']),
                            rejection['url']
                        ])
                logger.info(f"Saved {len(self.stats['rejected_records'])} rejected records to {output_file} (fallback method)")
            except Exception as e2:
                logger.error(f"Could not save rejected records with fallback method: {e2}")
    
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
            id_match = re.search(r'(?:xtid|customid)=(.+)\$', record['lookupID'])
            if id_match:
                title_id_numeric = id_match.group(1)
                nc_live_pattern = f"1000{title_id_numeric}"
                nc_live_035_patterns.append(nc_live_pattern)
                print(f"  Title ID: {title_id_numeric}")
                print(f"  Expected NC Live 035: {nc_live_pattern}")
                
                # Check if current OCN matches NC Live pattern
                if record['marc_035_ocn'] == nc_live_pattern:
                    print(f"  Detected NC Live 035 pattern (would be rejected)")
                elif record['marc_035_ocn'] != "N/A":
                    print(f"  Valid OCLC number found: {record['marc_035_ocn']}")
        
        print(f"SUMMARY:")
        print(f"Records missing MARC 028$a: {missing_028_count}/{len(sample_records)}")
        print(f"NC Live 035 patterns to watch for: {nc_live_035_patterns[:3]}...")

    def debug_hierarchical_lookup(self, num_samples: int = 10):
        """Debug the hierarchical lookup process."""
        print("\n" + "="*80)
        print("DEBUG: HIERARCHICAL LOOKUP ANALYSIS")
        print("="*80)
        
        print(f"Authority data loaded:")
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
            
            print(f"  InfobaseLookup check: {self.infobase_lookup.get(record['lookup_id'], 'NOT_FOUND')}")
            print(f"  KBART check ({title_id_numeric}): {self.kbart_lookup.get(title_id_numeric, 'NOT_FOUND')}")
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
    
    # Analyze KBART changes  DELETE FOR FINAL VERSION
    # changes = processor.analyze_kbart_changes()
    
    # Print statistics
    processor.print_statistics()
    
    # Save rejected records for analysis
    processor.save_rejected_records()
    
    print(f"Next step:")
    if processor.stats['needs_api_search'] > 0:
        print(f"1. Run: python main.py")
        print(f"   This will search OCLC API for {processor.stats['needs_api_search']} records using {search_terms_file}")
        print(f"2. After main.py completes, run: python marc_processor.py with --update-lookup flag")
    else:
        print(f"All records have verified OCLC numbers! No API search needed.")
        print(f"   You can proceed directly to: python kbart_integration.py")
    
    print(f"Hierarchical Lookup Performance:")
    total_with_oclc = (processor.stats['matched_infobase_lookup'] + 
                      processor.stats['matched_kbart'] + 
                      processor.stats['matched_marc_035'])
    if len(records) > 0:
        success_rate = (total_with_oclc / len(records)) * 100
        print(f"   - Success rate: {success_rate:.1f}% ({total_with_oclc}/{len(records)})")
        print(f"   - InfobaseLookup coverage: {(processor.stats['matched_infobase_lookup']/len(records))*100:.1f}%")
        print(f"   - KBART coverage: {(processor.stats['matched_kbart']/len(records))*100:.1f}%")
        print(f"   - MARC 035 coverage: {(processor.stats['matched_marc_035']/len(records))*100:.1f}%")

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
    changes = processor.analyze_kbart_changes()
    
    # Print statistics
    processor.print_statistics()
    
    # Save rejected records for analysis
    processor.save_rejected_records()

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--debug":
        debug_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "--update-lookup":
        # Second phase: update lookup file with OCLC results
        processor = InfobaseMARCProcessor()
        processor.load_existing_data()
        processor.process_marc_files()
        
        updated_lookup = processor.create_updated_lookup_file()
        
        print(f"Updated lookup file: {updated_lookup}")
        print(f"Next steps:")
        print(f"1. Run: python extended_marc_processor.py (for manual review)")
        print(f"1. Run: python extended_marc_processor.py with --process-updates manual_review_searches.csv flag")
        print(f"2. Run: python kbart_integration.py (to create final KBART)")
        print(f"3. Run: python kbart_entry_validator.py (to validate)")
        
    else:
        # First phase: process MARC and generate search terms
        main()