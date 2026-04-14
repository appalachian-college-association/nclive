# kbart_downloader_with_comparison.py V2 add validation to check for stale KBART
"""
Enhanced KBART downloader that:
1. Downloads KBART files for both BCLA (customer.5210.*) and NC Live (customer.54122.*) collections
2. Compares FOD and JFK collections between organizations
3. Reports any mismatches in title_id, title_url, oclc_number, and oclc_entry_id
4. Provides detailed statistics and reports
"""

import requests
import os
from dotenv import load_dotenv
from urllib.parse import urljoin
from datetime import datetime
from pathlib import Path
import pandas as pd

def load_config():
    """Load environment variables from .env file"""
    load_dotenv()
    api_key = os.getenv('WORLDCAT_KB_KEY')
    
    if not api_key:
        raise ValueError("WORLDCAT_KB_KEY not found in environment variables")
    
    return api_key

def get_collection_details(api_key, collection_uid):
    """Get detailed information about a specific collection including KBART link"""
    
    print(f"\n=== Getting details for collection: {collection_uid} ===")
    
    collection_url = f"http://worldcat.org/webservices/kb/rest/collections/{collection_uid}"
    
    params = {
        "wskey": api_key,
        "alt": "json"
    }
    
    try:
        response = requests.get(collection_url, params=params, timeout=15)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            
            print(f"Collection Title: {data.get('title', 'N/A')}")
            print(f"Owner Institution: {data.get('kb:owner_institution', 'N/A')}")
            print(f"Available Entries: {data.get('kb:available_entries', 0)}")
            print(f"Selected Entries: {data.get('kb:selected_entries', 0)}")
            
            # Construct KBART URL based on the working pattern
            owner_id = data.get('kb:owner_institution', '')
            kbart_url = None
            if owner_id:
                kbart_url = f"http://worldcat.org/webservices/kb/export/{owner_id}/{owner_id}_{collection_uid}_kbart.txt"
                print(f"Constructed KBART URL: {kbart_url}")
            
            return {
                'title': data.get('title', 'N/A'),
                'collection_uid': collection_uid,
                'owner_institution': data.get('kb:owner_institution', 'N/A'),
                'available_entries': data.get('kb:available_entries', 0),
                'selected_entries': data.get('kb:selected_entries', 0),
                'kbart_url': kbart_url
            }
            
        else:
            print(f"Error accessing collection details: {response.status_code}")
            print(f"Response: {response.text[:200]}")
            return None
            
    except Exception as e:
        print(f"Error getting collection details: {e}")
        return None

def download_kbart_file(api_key, kbart_url, collection_uid, metadata_count, output_dir="kbart_files"):
    """Download KBART file for a collection WITH VALIDATION
    Args:
       api_key: OCLC API key
       kbart_url: URL to KBART export
       collection_uid: Collection identifier
       metadata_count: Expected number of titles from metadata API
       output_dir: Directory to save files

    Returns:
       Tuple of (filepath: str, is_valid: bool) or (None, False) on error
    """
    
    print(f"\n=== Downloading KBART file for {collection_uid} ===")
    print(f"URL: {kbart_url}")
    
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    params = {
        "wskey": api_key
    }
    
    try:
        response = requests.get(kbart_url, params=params, timeout=30)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            # Save the KBART file
            filename = f"{collection_uid}_kbart.txt"
            filepath = os.path.join(output_dir, filename)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(response.text)
            
            print(f"SUCCESS! KBART file saved to: {filepath}")
            
            # Show preview of the file
            lines = response.text.split('\n')
            print(f"File contains {len(lines)} lines")
            print("First few lines:")
            for i, line in enumerate(lines[:3]):
                print(f"  {i+1}: {line[:100]}{'...' if len(line) > 100 else ''}")
            
            # *** NEW: VALIDATE KBART FRESHNESS ***
            is_valid, record_count, validation_message = validate_kbart_freshness(
                metadata_count,
                filepath,
                collection_uid
            )
            print(validation_message)

            return filepath, is_valid
            
        else:
            print(f"Error downloading KBART: {response.status_code}")
            print(f"Response: {response.text[:200]}")
            return None, False
            
    except Exception as e:
        print(f"Error downloading KBART file: {e}")
        return None, False

def parse_kbart_file(filepath):
    """Parse KBART file and extract key fields for comparison"""
    
    print(f"\n=== Parsing KBART file: {filepath} ===")
    
    try:
        # Read the file
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Find header line and data start
        header_line = None
        data_start = 0
        
        for i, line in enumerate(lines):
            line = line.strip()
            if line and not line.startswith('#'):
                header_line = line
                data_start = i + 1
                break
        
        if not header_line:
            print("No header line found")
            return []
        
        # Parse header
        headers = [col.strip() for col in header_line.split('\t')]
        print(f"Headers found: {headers[:5]}...")  # Show first 5 headers
        
        # Find key column indices
        title_url_idx = None
        title_id_idx = None
        oclc_number_idx = None
        oclc_entry_id_idx = None
        publication_title_idx = None
        
        for i, header in enumerate(headers):
            header_lower = header.lower()
            if 'title_url' in header_lower or ('title' in header_lower and 'url' in header_lower):
                title_url_idx = i
            elif 'title_id' in header_lower:
                title_id_idx = i
            elif 'oclc_number' in header_lower:
                oclc_number_idx = i
            elif 'oclc_entry_id' in header_lower:
                oclc_entry_id_idx = i
            elif header_lower == 'publication_title':
                publication_title_idx = i
        
        print(f"Key column indices - title_url: {title_url_idx}, title_id: {title_id_idx}, "
              f"oclc_number: {oclc_number_idx}, oclc_entry_id: {oclc_entry_id_idx}")
        
        # Parse data rows
        parsed_data = []
        for line_num, line in enumerate(lines[data_start:], data_start + 1):
            line = line.strip()
            if not line:
                continue
                
            row_data = line.split('\t')
            
            # Extract key fields
            record = {
                'title_url': row_data[title_url_idx] if title_url_idx is not None and title_url_idx < len(row_data) else '',
                'title_id': row_data[title_id_idx] if title_id_idx is not None and title_id_idx < len(row_data) else '',
                'oclc_number': row_data[oclc_number_idx] if oclc_number_idx is not None and oclc_number_idx < len(row_data) else '',
                'oclc_entry_id': row_data[oclc_entry_id_idx] if oclc_entry_id_idx is not None and oclc_entry_id_idx < len(row_data) else '',
                'publication_title': row_data[publication_title_idx] if publication_title_idx is not None and publication_title_idx < len(row_data) else '',
                'line_number': line_num
            }
            
            # Extract FULL title_id (including prefix) for matching
            title_id_raw = record['title_id']
            if title_id_raw:
                # Handle URL-encoded format like "xtid%3D123456" -> "xtid=123456"
                if '%3D' in title_id_raw:
                    record['full_title_id'] = title_id_raw.replace('%3D', '=')
                else:
                    record['full_title_id'] = title_id_raw
            else:
                record['full_title_id'] = ''
            
            # Only include records with valid title_id
            if record['full_title_id']:
                parsed_data.append(record)
        
        print(f"Parsed {len(parsed_data)} valid records with title_id")
        return parsed_data
        
    except Exception as e:
        print(f"Error parsing KBART file: {e}")
        return []

def validate_kbart_freshness(metadata_count, kbart_filepath, collection_name):
    """
    Validate that downloaded KBART matches collection metadata to detect stale cache.
    
    Args:
        metadata_count: Number of titles reported by collection metadata API
        kbart_filepath: Path to downloaded KBART file
        collection_name: Human-readable collection name for messages
        
    Returns:
        Tuple of (is_valid: bool, record_count: int, message: str)
    """
    print(f"\n=== Validating KBART freshness for {collection_name} ===")
    
    try:
        # Count records in downloaded file (excluding header)
        with open(kbart_filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            kbart_count = len(lines) - 1  # Subtract header row
        
        # Calculate difference
        difference = abs(metadata_count - kbart_count)
        tolerance = 100  # Allow small variance for processing differences
        
        print(f"Collection metadata reports: {metadata_count:,} titles")
        print(f"Downloaded KBART contains:   {kbart_count:,} records")
        print(f"Difference:                  {difference:,}")
        
        # Determine if data is fresh
        if difference > tolerance:
            # Stale data detected
            message = f"""
{"="*70}
⚠️  STALE KBART DATA DETECTED: {collection_name}
{"="*70}

The downloaded KBART file does not match current collection metadata.
This indicates the OCLC API is serving cached/outdated data.

Details:
  • Collection metadata (current):  {metadata_count:,} titles
  • Downloaded KBART file (cached): {kbart_count:,} records
  • Difference:                     {difference:,} titles
  • Tolerance threshold:            {tolerance} titles

Likely cause: OCLC's CDN cache has not refreshed yet after your last
              collection reload. This typically resolves within 24-48 hours.

RECOMMENDED ACTIONS:
1. ⏰ Wait 24 hours and re-run this script (OCLC cache refreshes nightly)
2. 📥 OR download manually from OCLC Collection Manager:
   - Log into Collection Manager
   - Open collection: {collection_name}
   - Click "Download it here" for KBART file
   - Use organize_manual_kbart.py to set up for pipeline

3. 🔄 OR continue processing (NOT recommended - may use outdated data)
{"="*70}
"""
            return False, kbart_count, message
            
        else:
            # Fresh data validated
            message = f"✅ KBART data validated - file is current ({kbart_count:,} records match metadata)"
            return True, kbart_count, message
            
    except Exception as e:
        error_message = f"❌ Error validating KBART file: {e}"
        print(error_message)
        return False, 0, error_message
    
def compare_collections(bcla_data, nclive_data, collection_type):
    """Compare BCLA and NC Live collections and identify mismatches"""
    
    print(f"\n=== Comparing {collection_type.upper()} Collections ===")
    
    # Create lookup dictionaries by FULL title ID (including prefix)
    bcla_lookup = {record['full_title_id']: record for record in bcla_data}
    nclive_lookup = {record['full_title_id']: record for record in nclive_data}
    
    bcla_ids = set(bcla_lookup.keys())
    nclive_ids = set(nclive_lookup.keys())
    
    print(f"BCLA {collection_type}: {len(bcla_ids)} titles")
    print(f"NC Live {collection_type}: {len(nclive_ids)} titles")
    
    # Find matches and mismatches
    common_ids = bcla_ids & nclive_ids
    bcla_only = bcla_ids - nclive_ids
    nclive_only = nclive_ids - bcla_ids
    
    # Find matches and mismatches by FULL title_id
    common_ids = bcla_ids & nclive_ids
    bcla_only = bcla_ids - nclive_ids
    nclive_only = nclive_ids - bcla_ids
    
    print(f"Common titles (exact match): {len(common_ids)}")
    print(f"BCLA only: {len(bcla_only)}")
    print(f"NC Live only: {len(nclive_only)}")
    
    # Check for field mismatches in common titles
    field_mismatches = []
    
    for title_id in common_ids:
        bcla_record = bcla_lookup[title_id]
        nclive_record = nclive_lookup[title_id]
        
        mismatches = {}
        
        # Compare key fields
        fields_to_compare = ['title_url', 'oclc_number', 'oclc_entry_id']
        
        for field in fields_to_compare:
            bcla_value = bcla_record.get(field, '').strip()
            nclive_value = nclive_record.get(field, '').strip()
            
            if bcla_value != nclive_value:
                mismatches[field] = {
                    'bcla': bcla_value,
                    'nclive': nclive_value
                }
        
        if mismatches:
            field_mismatches.append({
                'title_id': title_id,
                'publication_title': bcla_record.get('publication_title', ''),
                'mismatches': mismatches
            })
    
    print(f"Field mismatches in common titles: {len(field_mismatches)}")
    
    return {
        'collection_type': collection_type,
        'bcla_count': len(bcla_ids),
        'nclive_count': len(nclive_ids),
        'common_count': len(common_ids),
        'bcla_only_count': len(bcla_only),
        'nclive_only_count': len(nclive_only),
        'field_mismatches_count': len(field_mismatches),
        'bcla_only_ids': list(bcla_only),
        'nclive_only_ids': list(nclive_only),
        'field_mismatches': field_mismatches
    }

def save_comparison_report(fod_comparison, jfk_comparison, output_file="kbart_comparison_report.txt"):
    """Save detailed comparison report"""
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    report_lines = [
        "OCLC KNOWLEDGE BASE COLLECTION COMPARISON REPORT",
        "=" * 60,
        f"Generated: {timestamp}",
        "",
        "SUMMARY:",
        f"Films on Demand (FOD) Collections:",
        f"  BCLA: {fod_comparison['bcla_count']} titles",
        f"  NC Live: {fod_comparison['nclive_count']} titles",
        f"  Common (exact match): {fod_comparison['common_count']} titles",
        f"  BCLA only: {fod_comparison['bcla_only_count']} titles",
        f"  NC Live only: {fod_comparison['nclive_only_count']} titles",
        f"  Field mismatches: {fod_comparison['field_mismatches_count']} titles",
        "",
        f"Just for Kids (JFK) Collections:",
        f"  BCLA: {jfk_comparison['bcla_count']} titles",
        f"  NC Live: {jfk_comparison['nclive_count']} titles",
        f"  Common (exact match): {jfk_comparison['common_count']} titles",
        f"  BCLA only: {jfk_comparison['bcla_only_count']} titles",
        f"  NC Live only: {jfk_comparison['nclive_only_count']} titles",
        f"  Field mismatches: {jfk_comparison['field_mismatches_count']} titles",
        "",
        "DETAILED MISMATCHES:",
        ""
    ]
    
    # Add detailed mismatch information
    for comparison in [fod_comparison, jfk_comparison]:
        collection_type = comparison['collection_type'].upper()
        
        if comparison['field_mismatches']:
            report_lines.append(f"{collection_type} Field Mismatches:")
            report_lines.append("-" * 40)
            
            for mismatch in comparison['field_mismatches'][:20]:  # Show first 20
                report_lines.append(f"Title ID: {mismatch['title_id']}")
                report_lines.append(f"Title: {mismatch['publication_title'][:60]}...")
                
                for field, values in mismatch['mismatches'].items():
                    report_lines.append(f"  {field}:")
                    report_lines.append(f"    BCLA: {values['bcla']}")
                    report_lines.append(f"    NC Live: {values['nclive']}")
                
                report_lines.append("")
            
            if len(comparison['field_mismatches']) > 20:
                report_lines.append(f"... and {len(comparison['field_mismatches']) - 20} more field mismatches")
                report_lines.append("")
        
        if comparison['bcla_only_ids']:
            report_lines.append(f"{collection_type} - BCLA Only (first 10):")
            for title_id in comparison['bcla_only_ids'][:10]:
                report_lines.append(f"  {title_id}")
            if len(comparison['bcla_only_ids']) > 10:
                report_lines.append(f"  ... and {len(comparison['bcla_only_ids']) - 10} more")
            report_lines.append("")
        
        if comparison['nclive_only_ids']:
            report_lines.append(f"{collection_type} - NC Live Only (first 10):")
            for title_id in comparison['nclive_only_ids'][:10]:
                report_lines.append(f"  {title_id}")
            if len(comparison['nclive_only_ids']) > 10:
                report_lines.append(f"  ... and {len(comparison['nclive_only_ids']) - 10} more")
            report_lines.append("")
    
    # Save report
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines))
    
    print(f"Detailed comparison report saved: {output_file}")

def organize_kbart_files_for_pipeline():
    """
    Organize KBART files for pipeline use:
    1. Move all downloaded files to 'originals' subdirectory
    2. Copy one FOD and one JFK file back to main directory for processing
    """
    print(f"ORGANIZING FILES FOR PIPELINE")
    print("=" * 40)
    
    kbart_dir = Path("kbart_files")
    originals_dir = kbart_dir / "originals"
    originals_dir.mkdir(exist_ok=True)
    
    # Define which files to keep in main directory (prefer BCLA as working copies)
    working_files = {
        'customer.5210.ncfod_kbart.txt': 'fod_kbart.txt',  # FOD working copy
        'customer.5210.20_kbart.txt': 'jfk_kbart.txt'      # JFK working copy
    }
    
    # Move all files to originals directory first
    moved_files = []
    for kbart_file in kbart_dir.glob("*.txt"):
        if kbart_file.name != "kbart_comparison_report.txt":  # Don't move the report
            original_path = originals_dir / kbart_file.name
            kbart_file.replace(original_path)
            moved_files.append(kbart_file.name)
            print(f"Moved {kbart_file.name} to originals/")
    
    # Copy working files back to main directory with clean names
    for original_name, working_name in working_files.items():
        original_path = originals_dir / original_name
        working_path = kbart_dir / working_name
        
        if original_path.exists():
            import shutil
            shutil.copy2(original_path, working_path)
            print(f"Created working copy: {working_name}")
    
    print(f"FINAL STRUCTURE:")
    print(f"kbart_files/")
    print(f"   fod_kbart.txt (FOD working copy)")
    print(f"   jfk_kbart.txt (JFK working copy)")
    print(f"   kbart_comparison_report.txt")
    print(f"   originals/")
    for moved_file in moved_files:
        print(f"      {moved_file}")
    
    print(f"Files organized for pipeline use!")
    print(f"marc_processor_v4.py will now use only fod_kbart.txt and jfk_kbart.txt")

def main():
    """Main function to download KBART files and perform comparisons"""
    
    print("OCLC Knowledge Base KBART Downloader with Collection Comparison")
    print("=" * 70)
    
    try:
        api_key = load_config()
        print(f"API key loaded")
        
        # Define target collections
        target_collections = {
            'bcla_fod': 'customer.5210.ncfod',
            'bcla_jfk': 'customer.5210.20',
            'nclive_fod': 'customer.54122.8',
            'nclive_jfk': 'customer.54122.9'
        }
        
        downloaded_files = {}
        validation_failures = []  # Track collections with stale data
        
        # Download all KBART files
        print(f"DOWNLOADING KBART FILES")
        print("=" * 40)
        
        for collection_name, collection_uid in target_collections.items():
            print(f"Processing {collection_name} ({collection_uid})")
            
            details = get_collection_details(api_key, collection_uid)
            
            if details and details['kbart_url']:
                # Get metadata count for validation
                metadata_count = int(details.get('available_entries', 0))

                # Download with validation
                filepath, is_valid = download_kbart_file(
                    api_key,
                    details['kbart_url'],
                    collection_uid,
                    metadata_count  # Pass metadata count for validation
                )

                if filepath:
                    downloaded_files[collection_name] = {
                        'filepath': filepath,
                        'details': details,
                        'is_valid': is_valid
                    }

                    if is_valid:
                        print(f"✅ {collection_name} downloaded and validated successfully")
                    else:
                        print(f"{collection_name} downloaded but VALIDATION FAILED")
                        validation_failures.append(collection_name)
                else:
                    print(f"Failed to download {collection_name}")
            else:
                print(f"Could not get details for {collection_name}")

        # *** NEW: CHECK FOR VALIDATION FAILURES BEFORE PROCEEDING ***
        if validation_failures:
            print(f"\n{'='*70}")
            print(f"⚠️  VALIDATION FAILURES DETECTED")
            print(f"{'='*70}")
            print(f"\nThe following collections have stale KBART data:")
            for collection in validation_failures:
                print(f"  • {collection}")

            print(f"\nRECOMMENDED ACTIONS:")
            print(f"1. Wait 24 hours and re-run this script")
            print(f"2. Use manual downloads with organize_manual_kbart.py")
            print(f"3. Review validation messages above for details")

            # Ask user if they want to continue anyway
            print(f"\n{'='*70}")
            user_input = input("Continue with stale data? (yes/no): ").strip().lower()

            if user_input not in ['yes', 'y']:
                print("\n⏸️  Processing stopped by user")
                print("Re-run this script after 24 hours or use manual downloads")
                return
            else:
                print("\n⚠️  WARNING: Continuing with potentially stale data")
        # Continue with comparison and organization if user chose to proceed
        # OR if all validations passed

        if len(downloaded_files) == 4:
            print(f"\n{'='*70}")
            print(f"PERFORMING COLLECTION COMPARISONS")
            print(f"{'='*70}")

            # Parse KBART files
            kbart_data = {}
            for collection_name, file_info in downloaded_files.items():
                kbart_data[collection_name] = parse_kbart_file(file_info['filepath'])
            
            # Compare FOD collections
            fod_comparison = compare_collections(
                kbart_data['bcla_fod'],
                kbart_data['nclive_fod'],
                'FOD'
            )
            
            # Compare JFK collections
            jfk_comparison = compare_collections(
                kbart_data['bcla_jfk'],
                kbart_data['nclive_jfk'],
                'JFK'
            )
            
            # Save detailed report
            save_comparison_report(fod_comparison, jfk_comparison)
            
            # Print summary
            print(f"COMPARISON SUMMARY")
            print("=" * 30)
            print(f"FOD Collections:")
            print(f"  Exact matches: {fod_comparison['common_count']}")
            print(f"  Field mismatches: {fod_comparison['field_mismatches_count']}")
            print(f"JFK Collections:")
            print(f"  Exact matches: {jfk_comparison['common_count']}")
            print(f"  Field mismatches: {jfk_comparison['field_mismatches_count']}")
            
        else:
            print(f"Could not download all required KBART files. Downloaded: {len(downloaded_files)}/4")
        
        print(f"\nFILES IN kbart_files DIRECTORY:")
        kbart_dir = Path("kbart_files")
        if kbart_dir.exists():
            for file in kbart_dir.glob("*.txt"):
                print(f"  {file.name}")
        
        print(f"Process complete!")
        
        # Organize files for pipeline use
        organize_kbart_files_for_pipeline()
    
    except Exception as e:
        print(f"Script failed: {e}")

if __name__ == "__main__":
    main()