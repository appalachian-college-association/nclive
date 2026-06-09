# kbart_integration.py
"""
KBART integration script with proper OCLC headers and entry management.
Uses lookupIDcollection as unique identifier and only processes records
that have corresponding NC LIVE MARC entries (verified by last_updated timestamp).
"""

import pandas as pd
import csv
from pathlib import Path
import logging
import re
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
    """Format KBART files for OCLC Collection Manager"""
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

        cleaned_text = str(text).replace('#', '')
        return cleaned_text

    def fix_customid_case(self, title_id_encoded):
        """Transform customid= to customID= for Infobase's case-sensitive platform."""
        if not title_id_encoded:
            return title_id_encoded

        if title_id_encoded.lower().startswith('customid'):
            return 'customID' + title_id_encoded[8:]

        return title_id_encoded  # Leave xtid unchanged

    def load_existing_kbart_entries(self, kbart_dir="kbart_files"):
        """Load existing oclc_entry_id values to preserve them using lookupIDcollection as key"""
        kbart_path = Path(kbart_dir)

        for kbart_file in kbart_path.glob("*.txt"):
            try:
                df = pd.read_csv(kbart_file, sep='\t', dtype=str, low_memory=False)
                self._load_entries_from_df(df, kbart_file.name)
                logger.info("Loaded %s existing entries from %s", len(df), kbart_file.name)
            except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
                logger.warning("Could not load existing KBART file %s: %s", kbart_file, e)

        logger.info("Total existing entry_ids loaded: %s", len(self.existing_entry_ids))

    def _load_entries_from_df(self, df, filename):
        """Load entry_id mappings from a single KBART DataFrame into self.existing_entry_ids."""
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

        if not (title_id_col and entry_id_col):
            return

        collection_suffix = self._determine_collection_from_filename(filename)
        if not collection_suffix:
            return

        for _, row in df.iterrows():
            title_id_encoded = str(row.get(title_id_col, '')).strip()
            entry_id = str(row.get(entry_id_col, '')).strip()
            oclc_num = str(row.get(oclc_num_col, '')).strip()
            if not title_id_encoded or not entry_id:
                continue
            decoded_title_id = (
                title_id_encoded.replace('%3D', '=').replace('%2D', '-').lower()
            )
            lookup_id_collection = f"{decoded_title_id}${collection_suffix}"
            self.existing_entry_ids[lookup_id_collection] = {
                'entry_id': entry_id,
                'oclc_number': oclc_num,
                'source_file': filename,
                'encoded_title_id': title_id_encoded
            }

    def _determine_collection_from_filename(self, filename):
        """Determine collection suffix from KBART filename"""
        filename_lower = filename.lower()
        if 'fod' in filename_lower or 'customer.5210.ncfod' in filename_lower:
            return 'fod'
        if 'jfk' in filename_lower or 'customer.5210.20' in filename_lower:
            return 'jfk'
        if 'customer.54122.9' in filename_lower:
            return 'jfk'
        if 'customer.54122.8' in filename_lower:
            return 'fod'
        return None

    # lookup_id_collection is not accessed (underscore placeholder)
    def generate_unique_entry_id(self, _, oclc_number, existing_ids_in_file):
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

            # Filter for valid entries (not 'X' or empty) AND have MARC entry
            valid_df = df[
                (df['verifiedOCN'] != 'X') &
                (df['verifiedOCN'].notna()) &
                (df['verifiedOCN'] != '') &
                (df['verifiedOCN'] != 'nan') &
                (df['last_updated'] == today)
            ].copy()

            self.stats['total_final_records'] = len(df)
            self.stats['records_with_marc'] = len(valid_df)
            self.stats['records_without_marc'] = len(df) - len(valid_df)

            logger.info("Loaded %s total entries from %s", len(df), lookup_file)
            logger.info(
                "Found %s valid entries with MARC correspondence (updated today)", len(valid_df)
            )
            logger.info(
                "Excluded %s entries without MARC files or invalid OCLC", len(df) - len(valid_df)
            )

            return valid_df

        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError, KeyError) as e:
            logger.error("Error loading lookup file: %s", e)
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
            logger.warning("Missing lookupID or lookupIDcollection: %s", row)
            return None

        # Extract title_id from lookupID for URL and encoding
        title_match = re.search(r'(xtid|customid)=(.+)\$', lookup_id)
        if not title_match:
            logger.warning("Could not parse title_id from lookupID: %s", lookup_id)
            return None

        prefix = title_match.group(1)
        numeric_id = title_match.group(2)
        url_prefix = "customID" if prefix.lower() == "customid" else prefix

        # Create encoded title_id for KBART
        title_id_encoded = f"{prefix}%3D{numeric_id}"

        # Create title_url
        if collection_info['type'] == 'fod':
            title_url = f"https://fod.infobase.com/portalplaylists.aspx?{url_prefix}={numeric_id}"
        else:  # jfk
            title_url = f"https://jfk.infobase.com/portalplaylists.aspx?{url_prefix}={numeric_id}"

        # Get OCLC number
        oclc_number = row.get('verifiedOCN', '')

        # FIXED: Determine oclc_entry_id using lookupIDcollection
        if lookup_id_collection in self.existing_entry_ids:
            # Use existing entry_id
            entry_id = self.existing_entry_ids[lookup_id_collection]['entry_id']
            existing_ids_in_file.add(entry_id)  # Register so new entries don't duplicate it
            self.stats['preserved_entry_ids'] += 1
            logger.debug(
                "Preserved entry_id %s for %s", entry_id, lookup_id_collection
            )
        else:
            # Generate new unique entry_id
            entry_id = self.generate_unique_entry_id(
                lookup_id_collection, oclc_number, existing_ids_in_file
            )
            existing_ids_in_file.add(entry_id)
            self.stats['new_entry_ids'] += 1
            logger.debug("Generated new entry_id %s for %s", entry_id, lookup_id_collection)

        return {
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

    def _write_kbart_file(self, path, records):
        """Write a list of KBART record dicts to a tab-separated file."""
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=OCLC_KBART_HEADERS, delimiter='\t')
            writer.writeheader()
            writer.writerows(records)

    def _build_nc_live_records(self, kbart_records, collection_info):
        """Return a copy of kbart_records updated for the NC Live collection."""
        nc_live_records = []
        for record in kbart_records:
            nc_live_record = record.copy()
            nc_live_record['oclc_collection_name'] = collection_info['nc_live_name']
            nc_live_record['oclc_collection_id'] = collection_info['nc_live_equivalent']
            nc_live_records.append(nc_live_record)
        return nc_live_records

    def create_collection_kbart(self, lookup_df, collection_id, collection_info):
        """Create KBART file for a specific collection."""
        collection_df = (
            lookup_df[lookup_df['lookupIDcollection'].str.endswith(
                f'${collection_info["type"]}'
            )].copy()
        )

        if collection_df.empty:
            logger.warning(
                "No data found for collection %s (type: %s)",
                collection_id, collection_info['type']
            )
            return None

        logger.info(
            "Creating KBART for %s with %s entries", collection_id, len(collection_df)
        )

        if collection_info['type'] == 'fod':
            self.stats['fod_records'] = len(collection_df)
        else:
            self.stats['jfk_records'] = len(collection_df)

        existing_ids_in_file = set()
        kbart_records = []

        for _, row in collection_df.iterrows():
            kbart_record = self.create_kbart_record(
                row, collection_id, collection_info, existing_ids_in_file
            )
            if kbart_record:
                kbart_records.append(kbart_record)
            else:
                logger.warning(
                    "Failed to create KBART record for %s",
                    row.get('lookupIDcollection', 'unknown')
                )

        if not kbart_records:
            logger.warning("No valid KBART records created for %s", collection_id)
            return None

        for record in kbart_records:
            record['title_id'] = self.fix_customid_case(record['title_id'])

        output_path = self.output_dir / f"{collection_id}_kbart.txt"
        self._write_kbart_file(output_path, kbart_records)
        logger.info("✅ Created %s with %s records", output_path.name, len(kbart_records))

        nc_live_id = collection_info['nc_live_equivalent']
        nc_live_filename = (
            f"{collection_info['type']}_reload_"
            f"{datetime.now().strftime('%y%m%d')}_{nc_live_id}_kbart.txt"
        )
        nc_live_path = self.output_dir / nc_live_filename
        nc_live_records = self._build_nc_live_records(kbart_records, collection_info)
        self._write_kbart_file(nc_live_path, nc_live_records)
        logger.info("✅ Created %s with %s records", nc_live_filename, len(nc_live_records))

        return [output_path, nc_live_path]

    def print_statistics(self):
        """Print detailed statistics about the KBART integration process."""
        print("\n" + "="*80)
        print("KBART INTEGRATION STATISTICS")
        print("="*80)
        print(f"Total records in InfobaseLookup_final.csv: {self.stats['total_final_records']:,}")
        print(f"Records with MARC entry (processed): {self.stats['records_with_marc']:,}")
        print(f"Records without MARC entry (skipped): {self.stats['records_without_marc']:,}")
        print("\n COLLECTION BREAKDOWN:")
        print(f"  Films on Demand (FOD) records: {self.stats['fod_records']:,}")
        print(f"  Just for Kids (JFK) records: {self.stats['jfk_records']:,}")
        print("\n")
        print("ENTRY ID MANAGEMENT:")
        print(f"  Preserved existing entry_ids: {self.stats['preserved_entry_ids']:,}")
        print(f"  Generated new entry_ids: {self.stats['new_entry_ids']:,}")
        print("\n DATA INTEGRITY:")
        print("  ✅ Using lookupIDcollection prevents duplicate title overwrites")
        print("  ✅ Only processing records with MARC file match")
        print("  ✅ Preserving existing OCLC entry_ids where possible")
        print("="*80)

    def run_final_integration(self):
        """Run the complete final KBART integration with improved data handling."""
        logger.info("🚀 Starting final KBART integration with lookupIDcollection support...")

        # Load existing entry_ids
        self.load_existing_kbart_entries()

        # Load final lookup data (only records with NC LIVE MARC match)
        lookup_df = self.load_final_lookup_data()

        if lookup_df.empty:
            logger.error("No valid lookup data found with MARC correspondence. Cannot proceed.")
            logger.error(
                "Make sure InfobaseLookup_final.csv exists and contains records updated today."
            )
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
        logger.info("Created %s KBART files:", len(created_files))
        for file_path in created_files:
            logger.info("  📄 %s", file_path.name)

        logger.info("\n📁 All files saved in: %s", self.output_dir)
        logger.info("Ready for OCLC Collection Manager upload!")

        return True

def main():
    """Output final integrated KBART files"""
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
        print("- Missing MARC file match")

if __name__ == "__main__":
    main()
