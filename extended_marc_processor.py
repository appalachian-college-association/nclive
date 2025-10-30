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

import csv
import pandas as pd
import re
import requests
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import json
from urllib.parse import quote
from dotenv import load_dotenv

# Import authentication from existing project
# Load environment variables from .env file
load_dotenv()

# Load configuration and set up authentication (matching main.py approach)
try:
    from config import Config
    from auth import OCLCAuth
    
    config = Config()
    auth_handler = OCLCAuth()
    API_URL = f"{config.OCLC_BASE_URL}/search/brief-bibs"
    DEFAULT_LIBRARY = config.DEFAULT_LIBRARY
    RESTRICT_TO_LIBRARY = config.RESTRICT_TO_LIBRARY # Default is false (all libraries)
    oclc_dtypes = config.OCLC_DTYPES
    
except ImportError:
    print("Warning: Could not import auth.py and config.py. Please ensure they exist.")
    API_URL = "https://discovery.api.oclc.org/worldcat-org-ci/search/brief-bibs"
    DEFAULT_LIBRARY = "ACACL" # Fallback to local holdings
    RESTRICT_TO_LIBRARY = False # Fallback to global search

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
        logger.info(f"Loading manual review items from {self.updated_lookup_file}")
        
        if not self.updated_lookup_file.exists():
            logger.error(f"File not found: {self.updated_lookup_file}")
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
            self.stats['fod_manual_review'] = len(manual_review_df[manual_review_df['collection_type'] == 'fod'])
            self.stats['jfk_manual_review'] = len(manual_review_df[manual_review_df['collection_type'] == 'jfk'])
            
            logger.info(f"Loaded {self.stats['total_manual_review']} manual review items")
            logger.info(f"  - FOD: {self.stats['fod_manual_review']}")
            logger.info(f"  - JFK: {self.stats['jfk_manual_review']}")
            
            return self.manual_review_records
            
        except Exception as e:
            logger.error(f"Error loading manual review items: {e}")
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
                series_candidate = re.sub(r'^(Series:\s*|The\s+)', '', series_candidate, flags=re.IGNORECASE)
                series_candidate = re.sub(r'\s+Series$', '', series_candidate, flags=re.IGNORECASE)
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
                auth_handler = OCLCAuth()
                self.access_token = auth_handler.get_valid_token()
                if not self.access_token:
                    raise ValueError("Failed to obtain valid token")
                logger.info("Successfully obtained OCLC API access token")
            except Exception as e:
                logger.error(f"Failed to get access token: {e}")
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
            token = self.get_token_cached()
            
            # Clean and format title for search
            clean_title = self.clean_title_for_search(title)
            if not clean_title:
                return []
            
            # Build query - search for electronic video format with title
            query = f'ti:"{clean_title}" AND x4:digital'
            
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json"
            }
            params = {
                "q": query, 
                "limit": max_results
            }
            
            if RESTRICT_TO_LIBRARY:
                params["heldByLibrary"] = DEFAULT_LIBRARY
                logger.debug(f"Restricting search to library {DEFAULT_LIBRARY}")
            else:
                logger.debug(f"Performing global search (all libraries)")

            logger.info(f"Searching OCLC by title: {query}")
            response = requests.get(API_URL, headers=headers, params=params)
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
            
        except Exception as e:
            logger.error(f"Error searching by title '{title}': {e}")
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
            token = self.get_token_cached()
            
            # Search both title field and series field for the series name
            queries = [
                f'ti:"{series}" AND x4:digital',  # Series as title
                f'se:"{series}" AND x4:digital',  # Series field (if available)
            ]
            
            all_results = []
            
            for query in queries:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json"
                }
                params = {
                    "q": query, 
                    "limit": max_results
                }

                if RESTRICT_TO_LIBRARY:
                    params["heldByLibrary"] = DEFAULT_LIBRARY
                    logger.debug(f"Restricting search to library {DEFAULT_LIBRARY}")
                else:
                    logger.debug(f"Performing global search (all libraries)")
                
                logger.info(f"Searching OCLC by series: {query}")
                response = requests.get(API_URL, headers=headers, params=params)
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
            
        except Exception as e:
            logger.error(f"Error searching by series '{series}': {e}")
            self.stats['api_errors'] += 1
            return []
    
    def search_oclc_by_infobase_id(self, lookup_id: str, max_results: int = 5) -> List[Dict]:
        """
        Search OCLC Discovery API using the main.py query format without kw:Infobase.
        This mimics the main.py search but removes the Infobase keyword constraint.

        Args:
            lookup_id: The lookup ID (e.g., "xtid=296504$")
            max_results: Maximum number of results to return

        Returns:
            List of matching records
        """

        if not lookup_id:
            return []
        
        try:
            token = self.get_token_cached()

            # Extract numeric ID from lookup_id format (e.g., "xtid=296504$" -> "296504")
            id_match = re.search(r'(?:xtid|customid)=(.+)\$', lookup_id)
            if not id_match:
                logger.warning(f"Could not extract numeric ID from lookup_id: {lookup_id}")
                return []
            
            numeric_id = id_match.group(1)

            # Build query similar to main.py but without kw:Infobase
            # Original main.py query: "x4:digital AND kw:Infobase AND (sn:296504)"
            # Modified query: "x4:digital AND (sn:296504)"
            query = f'x4:digital AND (sn:{numeric_id})'

            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json"
            }
            params = {
                "q": query, 
                "limit": max_results
            }

            if RESTRICT_TO_LIBRARY:
                params["heldByLibrary"] = DEFAULT_LIBRARY
                logger.debug(f"Restricting search to library {DEFAULT_LIBRARY}")
            else:
                logger.debug(f"Performing global search (all libraries)")

            logger.info(f"Searching OCLC by infobase query: {query}")
            response = requests.get(API_URL, headers=headers, params=params)
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
        
        except Exception as e:
            logger.error(f"Error searching by infobase query '{lookup_id}': {e}")
            self.stats['api_errors'] += 1
            return []
    
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
            logger.info(f"Processing record {i}/{len(self.manual_review_records)}: {record.get('title', '')[:50]}...")
            
            title = record.get('title', '')
            lookup_id = record.get('lookupID', '')
            collection_type = record.get('collection_type', '')
            
            # Extract series from title if present
            series_name = self.extract_series_from_title(title)
            
            search_result = {
                'original_lookup_id': lookup_id,
                'original_title': title,
                'collection_type': collection_type,
                'extracted_series': series_name,
                'title_matches': [],
                'series_matches': [],
                'infobase_id_matches': [],
                'recommended_action': 'NO_MATCH'
            }
            
            # 1. Search by infobase ID (main.py query without kw:Infobase)
            infobase_id_matches = self.search_oclc_by_infobase_id(lookup_id)
            search_result['infobase_id_matches'] = infobase_id_matches

            if infobase_id_matches:
                self.stats['infobase_id_matches_found'] += 1
                search_result['recommended_action'] = 'INFOBASE_ID_MATCH'
                logger.info(f"  Found {len(infobase_id_matches)} infobase id matches")           
            
            # 2. Search by title
            title_matches = self.search_oclc_by_title(title)
            search_result['title_matches'] = title_matches
            
            if title_matches:
                self.stats['title_matches_found'] += 1
                if not infobase_id_matches:    # Only recommend title if no ID match
                    search_result['recommended_action'] = 'TITLE_MATCH'
                logger.info(f"  Found {len(title_matches)} title matches")
            
            # 3. Search by series (if extracted)
            series_matches = []
            if series_name:
                series_matches = self.search_oclc_by_series(series_name)
                search_result['series_matches'] = series_matches
                
                if series_matches:
                    self.stats['series_matches_found'] += 1
                    if not infobase_id_matches and not title_matches:  # Only recommend series if no id or title match
                        search_result['recommended_action'] = 'SERIES_MATCH'
                    logger.info(f"  Found {len(series_matches)} series matches for '{series_name}'")
            
            
            # Track no matches
            if not infobase_id_matches and not title_matches and not series_matches:
                self.stats['no_matches_found'] += 1
                logger.info(f"  No matches found")
            
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
        
        logger.info(f"Creating manual review file: {output_file}")
        
        # Flatten results for CSV output
        csv_records = []
        
        for result in self.extended_search_results:
            # Extract lookup_id and build infobase link
            lookup_id = result['original_lookup_id']  # e.g., "xtid=296504$"
            collection_type = result['collection_type']  # e.g., "fod" or "jfk"
    
            # Build infobase link
            infobase_link = ""
            if lookup_id:
                # Remove the "$" from the end
                clean_lookup_id = lookup_id.rstrip('$')  # "xtid=296504$ becomes "xtid=296504"
        
                # Build URL based on collection type
                if collection_type == 'fod':
                    infobase_link = f"https://fod.infobase.com/PortalPlaylists.aspx?{clean_lookup_id}"
                elif collection_type == 'jfk':
                    infobase_link = f"https://jfk.infobase.com/PortalPlaylists.aspx?{clean_lookup_id}"
                else:
                    infobase_link = f"https://fod.infobase.com/PortalPlaylists.aspx?{clean_lookup_id}"  # default to FOD
    
            base_record = {
                'original_lookup_id': result['original_lookup_id'],
                'original_title': result['original_title'],
                'collection_type': result['collection_type'],
                'extracted_series': result.get('extracted_series', ''),
                'recommended_action': result['recommended_action'],
                'infobase_link': infobase_link  # Add the constructed link here
            }
            
            # Priority logic: only add the FIRST match from best match typle
            # Add Infobase ID matches
            if result['infobase_id_matches']:
                match = result['infobase_id_matches'][0]
                record = base_record.copy()
                record.update({
                    'match_type': 'INFOBASE_ID_MATCH',
                    'match_rank': 1,
                    'suggested_oclc': match['oclc_number'],
                    'suggested_title': self.clean_text_for_export(match['title']),
                    'match_format': f"{match['general_format']}-{match['specific_format']}",
                    'manual_review_notes': 'First infobase ID match',
                    'verifiedOCN': '',  # To be filled in manually
                    'accept_suggestion': ''  # To be filled in manually
                })
                csv_records.append(record)

            elif result['title_matches']:
                match = result['title_matches'][0]
                record = base_record.copy()
                record.update({
                    'match_type': 'TITLE_MATCH',
                    'match_rank': 1,
                    'suggested_oclc': match['oclc_number'],
                    'suggested_title': self.clean_text_for_export(match['title']),
                    'match_format': f"{match['general_format']}-{match['specific_format']}",
                    'manual_review_notes': 'First title match',
                    'verifiedOCN': '',  # To be filled in manually
                    'accept_suggestion': ''  # To be filled in manually
                })
                csv_records.append(record)
            
            # Add series matches
            elif result['series_matches']:
                match = result['series_matches'][0]
                record = base_record.copy()
                record.update({
                    'match_type': 'SERIES_MATCH',
                    'match_rank': 1,
                    'suggested_oclc': match['oclc_number'],
                    'suggested_title': match['title'],
                    'match_format': f"{match['general_format']}-{match['specific_format']}",
                    'manual_review_notes': f'Series-level match for: {result.get("extracted_series", "")}',
                    'verifiedOCN': '',  # To be filled in manually
                    'accept_suggestion': ''  # To be filled in manually
                })
                csv_records.append(record)
            
            # Add no-match record
            else:
                record = base_record.copy()
                record.update({
                    'match_type': 'NO_MATCH',
                    'match_rank': 0,
                    'suggested_oclc': '',
                    'suggested_title': '',
                    'match_format': '',
                    'manual_review_notes': 'No matches found',
                    'verifiedOCN': '',  # To be filled in manually
                    'accept_suggestion': ''  # To be filled in manually
                })
                csv_records.append(record)
        
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
        
        logger.info(f"Created manual review file with {len(csv_records)} records")
        return str(output_file)    
   
    def process_manual_review_updates(self, reviewed_file: str, 
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
        logger.info(f"Processing manual review updates from {reviewed_file}")
        
        # Load the reviewed file
        try:
            reviewed_df = pd.read_csv(reviewed_file, dtype=oclc_dtypes, keep_default_na=False)
        except Exception as e:
            logger.error(f"Could not load reviewed file: {e}")
            return None
        
        # Load original lookup file
        try:
            original_df = pd.read_csv(self.updated_lookup_file, dtype=oclc_dtypes, keep_default_na=False)
        except Exception as e:
            logger.error(f"Could not load original lookup file: {e}")
            return None
        
        # Process accepted suggestions
        accepted_updates = {}
        
        for _, row in reviewed_df.iterrows():
            accept_suggestion = str(row.get('accept_suggestion', '')).strip().lower()
            lookup_id = row.get('original_lookup_id', '')  # This is base lookupID like "xtid=93331$"
            collection_type = row.get('collection_type', '')  # This tells us fod or jfk
            match_type = row.get('match_type', '')

            # Get OCLC number from either suggested_oclc OR verifiedOCN
            oclc_number = None

            # First priority: Check for accept_suggestion is 'yes' and exist suggested_oclc
            if accept_suggestion in ['yes', 'y', '1', 'true']:
                suggested_oclc = str(row.get('suggested_oclc', '')).strip()
                if suggested_oclc and suggested_oclc.lower() not in ['', 'nan', 'null']:
                    oclc_number = suggested_oclc

            # Second priority: Check if we have a manually entered verifiedOCN (regardless of accept_suggestion)
            if not oclc_number:
                verified_ocn = str(row.get('verifiedOCN', '')).strip()
                if verified_ocn and verified_ocn.lower() not in ['', 'nan', 'null']:
                    oclc_number = verified_ocn
                    # Automatically set accept_suggestion to yes if we have a manual verifiedOCN
                    accept_suggestion = 'yes'

            # Only process if we have both lookup_id and a valid OCLC number
            if lookup_id and oclc_number and accept_suggestion in ['yes', 'y', '1', 'true']:
                # FIXED: Create the full lookupIDcollection for proper matching
                lookup_id_collection = f"{lookup_id}{collection_type}"
                
                # Determine source label
                if match_type == "INFOBASE_ID_MATCH":
                    source = 'API_INFOBASE_ID'
                elif match_type == 'TITLE_MATCH':
                    source = 'API_EXT_TITLE'
                elif match_type == 'SERIES_MATCH':
                    source = 'API_EXT_SERIES'
                elif match_type == 'NO_MATCH':
                    source = 'MANUAL_ENTRY'  # For manually entered OCLC numbers
                else:
                    source = 'API_EXT_SEARCH'
                
                # FIXED: Use lookupIDcollection as the key instead of just lookupID
                accepted_updates[lookup_id_collection] = {
                    'verifiedOCN': oclc_number,
                    'source': source,
                    'base_lookup_id': lookup_id  # Keep base ID for logging
                }
                
                logger.info(f"Accepted update for {lookup_id_collection}: {oclc_number} (source: {source})")
        
        logger.info(f"Processing {len(accepted_updates)} accepted updates")
        
        # Update original lookup data
        updated_df = original_df.copy()
        
        # FIXED: Use lookupIDcollection for matching instead of lookupID
        for lookup_id_collection, updates in accepted_updates.items():
            mask = updated_df['lookupIDcollection'] == lookup_id_collection
            if mask.any():
                updated_df.loc[mask, 'verifiedOCN'] = updates['verifiedOCN']
                updated_df.loc[mask, 'source'] = updates['source']
                updated_df.loc[mask, 'last_updated'] = datetime.now().strftime('%Y-%m-%d')
                logger.info(f"Updated {lookup_id_collection} with OCLC {updates['verifiedOCN']}")
            else:
                # Try fallback to base lookupID matching (for backwards compatibility)
                base_lookup_id = updates['base_lookup_id']
                mask_fallback = updated_df['lookupID'] == base_lookup_id
                if mask_fallback.any():
                    # If multiple matches (duplicate title IDs), warn user
                    if mask_fallback.sum() > 1:
                        logger.warning(f"Multiple matches found for {base_lookup_id}. Manual review may be needed.")
                        logger.warning(f"Matches found: {updated_df[mask_fallback]['lookupIDcollection'].tolist()}")
                        # Apply update to all matches (may not be ideal, but prevents data loss)
                        updated_df.loc[mask_fallback, 'verifiedOCN'] = updates['verifiedOCN']
                        updated_df.loc[mask_fallback, 'source'] = updates['source']
                        updated_df.loc[mask_fallback, 'last_updated'] = datetime.now().strftime('%Y-%m-%d')
                        logger.warning(f"Applied update to all {mask_fallback.sum()} matches for {base_lookup_id}")
                    else:
                        # Single match - safe to update
                        updated_df.loc[mask_fallback, 'verifiedOCN'] = updates['verifiedOCN']
                        updated_df.loc[mask_fallback, 'source'] = updates['source']
                        updated_df.loc[mask_fallback, 'last_updated'] = datetime.now().strftime('%Y-%m-%d')
                        logger.info(f"Updated {base_lookup_id} (fallback match) with OCLC {updates['verifiedOCN']}")
                else:
                    logger.warning(f"Could not find lookup_id {lookup_id_collection} or {base_lookup_id} in original data")
        
        # DIAGNOSTIC: Check for any remaining "X" values
        remaining_x_count = len(updated_df[updated_df['verifiedOCN'] == 'X'])
        if remaining_x_count > 0:
            logger.info(f"After manual review processing: {remaining_x_count} items still marked for manual review ('X')")
        else:
            logger.info("All manual review items have been processed - no 'X' values remaining")
        
        # Save updated lookup file
        updated_df.to_csv(updated_lookup_output, index=False)
        logger.info(f"Saved updated lookup file: {updated_lookup_output}")
        
        return str(updated_lookup_output)
    
    def generate_statistics_report(self, output_file: str = "extended_search_stats.txt") -> str:
        """
        Generate detailed statistics report with FOD/JFK breakdown.
        
        Args:
            output_file: Output file for statistics report
            
        Returns:
            Path to statistics report
        """
        logger.info(f"Generating statistics report: {output_file}")
        
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
                            not r['infobase_id_matches'] and not r['title_matches'] and not r['series_matches'])
        jfk_no_matches = sum(1 for r in self.extended_search_results 
                            if r['collection_type'] == 'jfk' and 
                            not r['infobase_id_matches'] and not r['title_matches'] and not r['series_matches'])
        
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
            overall_success_rate = ((self.stats['infobase_id_matches_found'] + self.stats['title_matches_found'] + self.stats['series_matches_found']) / 
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
    search_results = processor.perform_extended_searches()
    
    # Create manual review file
    manual_review_file = processor.create_manual_review_file()
    print(f"\n Created manual review file: {manual_review_file}")
    
    # Generate statistics report
    stats_report = processor.generate_statistics_report()
    print(f"Generated statistics report: {stats_report}")
    
    print(f"\n Next Steps:")
    print(f"1. Review and edit: {manual_review_file}")
    print(f"   - Update 'verifiedOCN' column with correct OCLC numbers")
    print(f"   - Set 'accept_suggestion' to 'yes' for items to accept")
    print(f"2. After manual review, run: python extended_marc_processor.py --process-updates {manual_review_file}")
    print(f"3. This will generate InfobaseLookup_final.csv")
    print(f"4. Then run: python kbart_integration.py for final KBART processing")

def process_updates_main():
    """Process manually reviewed updates."""
    import sys
    
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
        print(f"\n Processing complete!")
        print(f"Updated lookup file: {updated_lookup}")
        print(f"\n Final Steps:")
        print(f"1. Run: python kbart_integration.py")
        print(f"   This will create the final KBART files using {updated_lookup}")
        print(f"2. Run: python final_kbart_integration_reporting.py")
        print(f"   For comprehensive reporting and validation")
    else:
        print("\n Error processing updates. Check logs for details.")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--process-updates":
        process_updates_main()
    else:
        main()