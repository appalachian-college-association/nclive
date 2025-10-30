# kbart_integration_fixed.py
"""
KBART integration script with proper OCLC headers and entry management.
FIXED VERSION: Uses lookupIDcollection as unique identifier and only processes
records that have corresponding MARC entries (verified by last_updated timestamp).
"""

import pandas as pd
import csv
from pathlib import Path
import logging
import re
import hashlib
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OCLC KBART Headers from KBARTvalidateHeaders.txt
OCLC_KBART_HEADERS = [
    'publication_title', 'print_identifier', 'online_identifier', 
    'date_first_issue_online', 'num_first_vol_online', 'num_first_issue_online',
    'date_last_issue_online', 'num_last_vol_online', 'num_last_issue_online',
    'title_url', 'first_author', 'title_id', 'embargo_info', 'coverage_depth',
    'coverage_notes', 'publisher_name', 'location', 'title_notes', 'staff_notes',
    'vendor_id', 'oclc_collection_name', 'oclc_collection_id', 'oclc_entry_id',
    'oclc_linkscheme', 'oclc_number', 'ACTION'
]

class KBARTFinalIntegrator:
    def __init__(self):
        self.output_dir = Path("final_kbart")
        self.output_dir.mkdir(exist_ok=True)
        
        # Collection mappings
        self.collections = {
            'customer.5210.20': {
                'type': 'jfk',
                'name': 'maintenance nc jfk',
                'nc_live_equivalent': 'customer.54122.9',
                'nc_live_name': 'NC LIVE Just For Kids Collection'
            },
            'customer.5210.ncfod': {
                'type': 'fod', 
                'name': 'maintenance nc fod',
                'nc_live_equivalent': 'customer.54122.8',
                'nc_live_name': 'NC LIVE Films on Demand Collection'
            }
        }
        
        # Store existing entry_ids to preserve them
        # FIXED: Using lookupIDcollection as key instead of decoded title_id
        self.existing_entry_ids = {}
        
        # Track statistics
        self.stats = {
            'total_final_records': 0,
            'records_with_marc': 0,
            'records_without_marc': 0,
            'fod_records': 0,
            'jfk_records': 0,
            'preserved_entry_ids': 0,
            'new_entry_ids': 0
        }

    def clean_text_for_kbart(self, text):
        """Clean text for KBART compatibility by removing problematic characters"""
        if not text:
                return text
        # Remove hashmarks that cause loading issues
        cleaned_text = str(text).replace('#', '')
        return cleaned_text
            
    
    def load_existing_kbart_entries(self, kbart_dir="kbart_files"):
        """Load existing oclc_entry_id values to preserve them using lookupIDcollection as key"""
        kbart_path = Path(kbart_dir)
        
        for kbart_file in kbart_path.glob("*.txt"):
            try:
                df = pd.read_csv(kbart_file, sep='\t', dtype=str, low_memory=False)
                
                # Find relevant columns
                title_id_col = None
                entry_id_col = None
                oclc_num_col = None
                
                for col in df.columns:
                    if 'title_id' in col.lower():
                        title_id_col = col
                    elif 'entry_id' in col.lower():
                        entry_id_col = col
                    elif 'oclc_number' in col.lower():
                        oclc_num_col = col
                
                if title_id_col and entry_id_col:
                    for _, row in df.iterrows():
                        title_id_encoded = str(row.get(title_id_col, '')).strip()
                        entry_id = str(row.get(entry_id_col, '')).strip()
                        oclc_num = str(row.get(oclc_num_col, '')).strip()
                        
                        if title_id_encoded and entry_id:
                            # Decode title_id for conversion to lookupIDcollection format
                            decoded_title_id = title_id_encoded.replace('%3D', '=').replace('%2D', '-')
                            
                            # Convert to lookupIDcollection format
                            # From "xtid=123456" -> "xtid=123456$fod" or "xtid=123456$jfk"
                            # Determine collection type from file name or collection context
                            collection_suffix = self._determine_collection_from_filename(kbart_file.name)
                            if collection_suffix:
                                lookup_id_collection = f"{decoded_title_id}${collection_suffix}"
                                
                                # FIXED: Store by lookupIDcollection instead of decoded_title_id
                                self.existing_entry_ids[lookup_id_collection] = {
                                    'entry_id': entry_id,
                                    'oclc_number': oclc_num,
                                    'source_file': kbart_file.name,
                                    'encoded_title_id': title_id_encoded
                                }
                                
                logger.info(f"Loaded {len(df)} existing entries from {kbart_file.name}")
                
            except Exception as e:
                logger.warning(f"Could not load existing KBART file {kbart_file}: {e}")
        
        logger.info(f"Total existing entry_ids loaded: {len(self.existing_entry_ids)}")
    
    def _determine_collection_from_filename(self, filename):
        """Determine collection suffix from KBART filename"""
        filename_lower = filename.lower()
        if 'fod' in filename_lower or 'customer.5210.ncfod' in filename_lower:
            return 'fod'
        elif 'jfk' in filename_lower or 'customer.5210.20' in filename_lower:
            return 'jfk'
        else:
            # Try to parse from customer ID patterns
            if 'customer.5210.20' in filename or 'customer.54122.9' in filename:
                return 'jfk'
            elif 'customer.5210.ncfod' in filename or 'customer.54122.8' in filename:
                return 'fod'
        return None
    
    def generate_unique_entry_id(self, lookup_id_collection, oclc_number, existing_ids_in_file):
        """Generate unique alphanumeric entry_id for new entries"""
        # Base entry_id using OCLC number
        base_id = f"{oclc_number}"
        
        # If this OCLC number appears multiple times, add suffix
        counter = 1
        entry_id = base_id
        
        while entry_id in existing_ids_in_file:
            entry_id = f"{base_id}{chr(97 + counter - 1)}"  # a, b, c, etc.
            counter += 1
            if counter > 26:  # fallback to numbers
                entry_id = f"{base_id}{counter - 26}"
        
        return entry_id
    
    def load_final_lookup_data(self, lookup_file="InfobaseLookup_final.csv"):
        """
        Load the final lookup data with verified OCLC numbers.
        FIXED: Only includes records that have corresponding MARC entries (last_updated = today).
        """
        try:
            df = pd.read_csv(lookup_file, dtype=str, keep_default_na=False)
            
            # Get today's date for filtering
            today = datetime.now().strftime('%Y-%m-%d')
            
            # Filter for valid entries (not 'X' or empty) AND have MARC correspondence
            valid_df = df[
                (df['verifiedOCN'] != 'X') & 
                (df['verifiedOCN'].notna()) & 
                (df['verifiedOCN'] != '') &
                (df['verifiedOCN'] != 'nan') &
                (df['last_updated'] == today)  # FIXED: Only records updated today (have MARC entries)
            ].copy()
            
            self.stats['total_final_records'] = len(df)
            self.stats['records_with_marc'] = len(valid_df)
            self.stats['records_without_marc'] = len(df) - len(valid_df)
            
            logger.info(f"Loaded {len(df)} total entries from {lookup_file}")
            logger.info(f"Found {len(valid_df)} valid entries with MARC correspondence (updated today)")
            logger.info(f"Excluded {len(df) - len(valid_df)} entries without MARC files or invalid OCLC")
            
            return valid_df
            
        except Exception as e:
            logger.error(f"Error loading lookup file: {e}")
            return pd.DataFrame()
    
    def create_kbart_record(self, row, collection_id, collection_info, existing_ids_in_file):
        """
        Create a single KBART record with proper formatting.
        FIXED: Uses lookupIDcollection for entry_id management.
        """
        
        # FIXED: Get lookupIDcollection directly from the row
        lookup_id_collection = row.get('lookupIDcollection', '')
        lookup_id = row.get('lookupID', '')
        
        if not lookup_id_collection or not lookup_id:
            logger.warning(f"Missing lookupID or lookupIDcollection: {row}")
            return None
        
        # Extract title_id from lookupID for URL and encoding
        title_match = re.search(r'(xtid|customid)=(.+)\$', lookup_id)
        if not title_match:
            logger.warning(f"Could not parse title_id from lookupID: {lookup_id}")
            return None
        
        prefix = title_match.group(1)
        numeric_id = title_match.group(2)
        
        # Create encoded title_id for KBART
        title_id_encoded = f"{prefix}%3D{numeric_id}"
        
        # Create title_url
        collection_type = collection_info['type']
        if collection_type == 'fod':
            title_url = f"https://fod.infobase.com/portalplaylists.aspx?{prefix}={numeric_id}"
        else:  # jfk
            title_url = f"https://jfk.infobase.com/portalplaylists.aspx?{prefix}={numeric_id}"
        
        # Get OCLC number
        oclc_number = row.get('verifiedOCN', '')
        
        # FIXED: Determine oclc_entry_id using lookupIDcollection
        if lookup_id_collection in self.existing_entry_ids:
            # Use existing entry_id
            entry_id = self.existing_entry_ids[lookup_id_collection]['entry_id']
            self.stats['preserved_entry_ids'] += 1
            logger.debug(f"Preserved entry_id {entry_id} for {lookup_id_collection}")
        else:
            # Generate new unique entry_id
            entry_id = self.generate_unique_entry_id(lookup_id_collection, oclc_number, existing_ids_in_file)
            existing_ids_in_file.add(entry_id)
            self.stats['new_entry_ids'] += 1
            logger.debug(f"Generated new entry_id {entry_id} for {lookup_id_collection}")
        
        # Create KBART record
        kbart_record = {
            'publication_title': self.clean_text_for_kbart(row.get('title', '')),
            'print_identifier': '',
            'online_identifier': '',
            'date_first_issue_online': '',
            'num_first_vol_online': '',
            'num_first_issue_online': '',
            'date_last_issue_online': '',
            'num_last_vol_online': '',
            'num_last_issue_online': '',
            'title_url': title_url,
            'first_author': '',
            'title_id': title_id_encoded,
            'embargo_info': '',
            'coverage_depth': 'video',
            'coverage_notes': '',
            'publisher_name': '',
            'location': '',
            'title_notes': '',
            'staff_notes': '',
            'vendor_id': '',
            'oclc_collection_name': collection_info['name'],
            'oclc_collection_id': collection_id,
            'oclc_entry_id': entry_id,
            'oclc_linkscheme': '',
            'oclc_number': oclc_number,
            'ACTION': 'raw'
        }
        
        return kbart_record
    
    def create_collection_kbart(self, lookup_df, collection_id, collection_info):
        """
        Create KBART file for a specific collection.
        FIXED: Uses lookupIDcollection for proper filtering and processing.
        """
        
        # FIXED: Filter data for this collection type using lookupIDcollection
        collection_type = collection_info['type']
        collection_df = lookup_df[lookup_df['lookupIDcollection'].str.endswith(f'${collection_type}')].copy()
        
        if collection_df.empty:
            logger.warning(f"No data found for collection {collection_id} (type: {collection_type})")
            return None
        
        logger.info(f"Creating KBART for {collection_id} with {len(collection_df)} entries")
        
        # Update collection statistics
        if collection_type == 'fod':
            self.stats['fod_records'] = len(collection_df)
        else:
            self.stats['jfk_records'] = len(collection_df)
        
        # Track entry_ids used in this file to ensure uniqueness
        existing_ids_in_file = set()
        
        # Create KBART records
        kbart_records = []
        
        for _, row in collection_df.iterrows():
            kbart_record = self.create_kbart_record(row, collection_id, collection_info, existing_ids_in_file)
            if kbart_record:
                kbart_records.append(kbart_record)
            else:
                logger.warning(f"Failed to create KBART record for {row.get('lookupIDcollection', 'unknown')}")
        
        if not kbart_records:
            logger.warning(f"No valid KBART records created for {collection_id}")
            return None
        
        # Create output filename
        output_filename = f"{collection_id}_kbart.txt"
        output_path = self.output_dir / output_filename
        
        # Write KBART file with proper headers
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=OCLC_KBART_HEADERS, delimiter='\t')
            writer.writeheader()
            writer.writerows(kbart_records)
        
        logger.info(f"✅ Created {output_filename} with {len(kbart_records)} records")
        
        # Also create NC Live equivalent
        nc_live_id = collection_info['nc_live_equivalent']
        nc_live_filename = f"{nc_live_id}_kbart.txt"
        nc_live_path = self.output_dir / nc_live_filename
        
        # Modify records for NC Live (only collection name and ID differ)
        nc_live_records = []
        for record in kbart_records:
            nc_live_record = record.copy()
            nc_live_record['oclc_collection_name'] = collection_info['nc_live_name']
            nc_live_record['oclc_collection_id'] = nc_live_id
            nc_live_records.append(nc_live_record)
        
        with open(nc_live_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=OCLC_KBART_HEADERS, delimiter='\t')
            writer.writeheader()
            writer.writerows(nc_live_records)
        
        logger.info(f"✅ Created {nc_live_filename} with {len(nc_live_records)} records")
        
        return [output_path, nc_live_path]
    
    def print_statistics(self):
        """Print detailed statistics about the KBART integration process."""
        print("\n" + "="*80)
        print("KBART INTEGRATION STATISTICS")
        print("="*80)
        print(f"Total records in InfobaseLookup_final.csv: {self.stats['total_final_records']:,}")
        print(f"Records with MARC correspondence (processed): {self.stats['records_with_marc']:,}")
        print(f"Records without MARC correspondence (skipped): {self.stats['records_without_marc']:,}")
        print(f"")
        print(f"COLLECTION BREAKDOWN:")
        print(f"  Films on Demand (FOD) records: {self.stats['fod_records']:,}")
        print(f"  Just for Kids (JFK) records: {self.stats['jfk_records']:,}")
        print(f"")
        print(f"ENTRY ID MANAGEMENT:")
        print(f"  Preserved existing entry_ids: {self.stats['preserved_entry_ids']:,}")
        print(f"  Generated new entry_ids: {self.stats['new_entry_ids']:,}")
        print(f"")
        print(f"DATA INTEGRITY:")
        print(f"  ✅ Using lookupIDcollection prevents duplicate title overwrites")
        print(f"  ✅ Only processing records with MARC file correspondence")
        print(f"  ✅ Preserving existing OCLC entry_ids where possible")
        print("="*80)
    
    def run_final_integration(self):
        """Run the complete final KBART integration with improved data handling."""
        logger.info("🚀 Starting final KBART integration with lookupIDcollection support...")
        
        # Load existing entry_ids
        self.load_existing_kbart_entries()
        
        # Load final lookup data (only records with MARC correspondence)
        lookup_df = self.load_final_lookup_data()
        
        if lookup_df.empty:
            logger.error("No valid lookup data found with MARC correspondence. Cannot proceed.")
            logger.error("Make sure InfobaseLookup_final.csv exists and contains records updated today.")
            return False
        
        created_files = []
        
        # Create KBART files for each collection
        for collection_id, collection_info in self.collections.items():
            files = self.create_collection_kbart(lookup_df, collection_id, collection_info)
            if files:
                created_files.extend(files)
        
        # Print detailed statistics
        self.print_statistics()
        
        # Summary
        logger.info("🎉 Final KBART integration complete!")
        logger.info(f"Created {len(created_files)} KBART files:")
        for file_path in created_files:
            logger.info(f"  📄 {file_path.name}")
        
        logger.info(f"\n📁 All files saved in: {self.output_dir}")
        logger.info("Ready for OCLC Collection Manager upload!")
        
        return True

def main():
    print("KBART Final Integration - Fixed Version")
    print("Using lookupIDcollection and MARC correspondence filtering")
    print("="*60)
    
    integrator = KBARTFinalIntegrator()
    success = integrator.run_final_integration()
    
    if success:
        print("\n🎯 NEXT STEPS:")
        print("1. Run validation: python kbart_entry_validator.py")
        print("2. Review files in final_kbart/ directory")
        print("3. Run reports: python kbart_reporting.py")
        print("\n💡 KEY IMPROVEMENTS:")
        print("- Uses lookupIDcollection to prevent duplicate title overwrites")
        print("- Only processes records with corresponding MARC files")
        print("- Preserves existing OCLC entry_ids where possible")
        print("- Provides detailed statistics for verification")
    else:
        print("\n❌ Integration failed. Check logs for details.")
        print("Common issues:")
        print("- InfobaseLookup_final.csv not found or empty")
        print("- No records with today's last_updated timestamp")
        print("- Missing MARC file correspondence")

if __name__ == "__main__":
    main()