# main-simplified.py
import csv
import os
from dotenv import load_dotenv
from auth import OCLCAuth
from config import Config
import requests
import logging
import time

# Setup logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# -------------------------------
# 1. Load environment and config
# -------------------------------
load_dotenv()  # This loads variables from .env file
config = Config()
auth_handler = OCLCAuth()
API_URL = f"{config.OCLC_BASE_URL}/search/brief-bibs"
DEFAULT_LIBRARY = config.DEFAULT_LIBRARY # Update .env if not ACACL
RESTRICT_TO_LIBRARY = config.RESTRICT_TO_LIBRARY # Update .env if not false
oclc_dtypes = config.OCLC_DTYPES

# -------------------------------
# 2. Load search terms from TSV file
# -------------------------------
def load_search_terms(filename):
    terms = []
    try:
        with open(filename, "r", encoding="utf-8") as file:
            # Skip header row
            next(file)
            
            for line in file:
                line = line.strip()
                if not line:
                    continue
                    
                # Split by tab to get both columns
                parts = line.split('\t')
                if len(parts) >= 2:
                    lookup_id = parts[0].strip()   # First column (e.g., "xtid=296504$fod")
                    search_query = parts[1].strip()  # Second column (e.g., "sn:296504")
                    terms.append((lookup_id, search_query))
        
        logger.info(f"Loaded {len(terms)} search terms from {filename}")
    except Exception as e:
        logger.error(f"Error loading search terms: {e}")
        
    return terms

# -------------------------------
# 3. Submit query to the API and fetch results
# -------------------------------
def run_search(query, token, restrict_to_library=RESTRICT_TO_LIBRARY):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    params = {"q": query, "limit": 50}  # max 50 per API rules

    # Add library restriction as needed (restrict_to_library=True for run_search and call)
    if restrict_to_library:
        params["heldByLibrary"] = DEFAULT_LIBRARY
    
    logger.info(f"Sending request with query: {query}")
    if restrict_to_library:
        logger.info(f"Restricting search to library: {DEFAULT_LIBRARY}")
    else:
        logger.info("Performing global search (all libraries)")
    response = requests.get(API_URL, headers=headers, params=params)
    response.raise_for_status()
    return response.json()

# -------------------------------
# 4. Extract selected fields and write CSV, including lookup IDs
# -------------------------------
def clean_text_for_export(text:str) -> str:
    """Clean text for OpenRefine compatibility"""
    if not text:
        return text
    return str(text).replace("#", '[hash]')
def write_to_csv(data, output_file, lookup_id):
    """
    Write API results to CSV, assigning the original lookup_id to each result
    
    Args:
        data: API response data
        output_file: Path to output CSV file
        lookup_id: Original lookup ID from TSV
    """
    records = data.get("briefRecords", [])
    results_written = 0
    
    with open(output_file, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        
        for record in records:
            # Basic bibliographic info
            oclc = record.get("oclcNumber", "")
            title = record.get("title", "")
            isbns = [f"{isbn} (bn)" for isbn in record.get("isbns", [])]
            issns = [f"{issn} (sn)" for issn in record.get("issns",[])]
            isns = "; ".join(isbns + issns)
                        
            # Format information
            general_format = record.get("generalFormat", "")
            specific_format = record.get("specificFormat", "")
            
            # Determine if this is an electronic video
            is_electronic_video = (general_format == "Video" and specific_format == "Digital")
            
            # Combine format fields into a descriptive format label
            format_description = f"{general_format}-{specific_format}" if general_format and specific_format else ""
            
            # Add material type if available (for additional confirmation)
            material_types = record.get("format", {}).get("materialTypes", [])
            material_types_str = "; ".join(material_types) if material_types else ""
            
            # Write the record with the original lookup ID
            writer.writerow([
                lookup_id,           # Original lookup ID from your TSV
                oclc,                # OCLC number
                clean_text_for_export(title),               # Clean the title
                clean_text_for_export(isns),                # Clean ISBN(s)/ISSN(s)
                general_format,      # General format (Video, Book, etc.)
                specific_format,     # Specific format (Digital, DVD, etc.)
                format_description,  # Combined format description
                "Yes" if is_electronic_video else "No",  # Is electronic video flag
                material_types_str   # Material types for additional context
            ])
            results_written += 1
    
    return results_written

# -------------------------------
# 5. Run the script
# -------------------------------
if __name__ == "__main__":
    search_file = "search_terms.tsv"
    output_file = "oclc_results.csv"

    # Remove any existing output file to ensure we start fresh
    if os.path.exists(output_file):
        try:
            os.remove(output_file)
            logger.info(f"Removed existing file: {output_file}")
        except Exception as e:
            logger.error(f"Failed to remove existing file: {e}")

    print("Getting token from OCLCAuth...")
    token = auth_handler.get_valid_token()
    if not token:
        print("Failed to retrieve access token. Check your credentials.")
        exit(1)

    print("Loading search terms...")
    search_terms = load_search_terms(search_file)
    
    if not search_terms:
        print("No search terms found or could not parse the file.")
        exit(1)
    
    print(f"Found {len(search_terms)} search terms to process.")

    # Create new CSV file with headers
    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([
            "lookupID", 
            "oclcNumber", 
            "title", 
            "isns",
            "generalFormat", 
            "specificFormat", 
            "formatDescription", 
            "isElectronicVideo",
            "materialTypes"
        ])
    
    total_records = 0
    
    # Process each search term individually for guaranteed lookup ID association
    for i, (lookup_id, search_query) in enumerate(search_terms):
        print(f"Processing {i+1}/{len(search_terms)}: {lookup_id}")
        
        query = f"x4:digital AND kw:Infobase AND ({search_query})"
        
        try:
            data = run_search(query, token, restrict_to_library=RESTRICT_TO_LIBRARY)
            num_records = len(data.get("briefRecords", []))
            print(f"  Found {num_records} records")
            
            records_written = write_to_csv(data, output_file, lookup_id)
            total_records += records_written
            
            print(f"  Wrote {records_written} rows to CSV (Total: {total_records})")
            
            # Add a small delay to avoid rate limiting
            if i + 1 < len(search_terms):
                time.sleep(1)
                
        except requests.exceptions.HTTPError as e:
            print(f"Error for search {i+1}: {e}")

    print(f"Done! {total_records} results saved to {output_file}.")
    
    # Final verification
    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            row_count = sum(1 for row in reader) - 1  # Subtract 1 for header
            print(f"Verification: CSV contains {row_count} data rows")
            
            if row_count != total_records:
                print(f"Warning: Expected {total_records} rows but found {row_count} rows in the CSV.")
    except Exception as e:
        print(f"Error verifying CSV: {e}")