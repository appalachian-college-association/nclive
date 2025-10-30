import os
import re
import shutil
from pathlib import Path
from pymarc import MARCReader, MARCWriter

# === FILTER FUNCTIONS ===
def should_delete_856(field):
    return any(sub[0] == 'z' and sub[1] == 'Cover image' for sub in field.subfields)

def should_delete_035(field):
    return any(sub[0] == 'a' and not sub[1].startswith('(') for sub in field.subfields)

def should_delete_028(field):
    a_val = next((sub[1] for sub in field.subfields if sub[0] == 'a'), '')
    b_exists = any(sub[0] == 'b' for sub in field.subfields)
    return b_exists and re.search(r'[^0-9e]', a_val)

def clean_record_fields(record):
    return [
        field for field in record.fields
        if not (
            (field.tag == '856' and should_delete_856(field)) or
            (field.tag == '035' and should_delete_035(field)) or
            (field.tag == '028' and should_delete_028(field))
        )
    ]

# === FILE MANAGEMENT FUNCTIONS ===
def move_to_archived(file_path, archived_dir):
    """Move a file to the archived directory"""
    try:
        archived_dir.mkdir(exist_ok=True)  # Create archived dir if it doesn't exist
        archived_path = archived_dir / file_path.name
        
        # Handle duplicate names in archive by adding a timestamp
        if archived_path.exists():
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name_parts = file_path.stem, timestamp, file_path.suffix
            archived_path = archived_dir / f"{name_parts[0]}_{name_parts[1]}{name_parts[2]}"
        
        shutil.move(str(file_path), str(archived_path))
        print(f"Moved to archive: {file_path.name} → {archived_path.name}")
        return True
    except Exception as e:
        print(f"Error moving {file_path.name} to archive: {e}")
        return False

def verify_processed_files(input_dir):
    """Verify exactly two *_processed.mrc files exist in the directory"""
    input_path = Path(input_dir)
    processed_files = list(input_path.glob("*_processed.mrc"))
    
    if len(processed_files) == 2:
        print(f"Verification passed: Found exactly 2 processed files")
        for f in processed_files:
            print(f"   - {f.name}")
        return True
    else:
        print(f"ALERT: Expected exactly 2 processed files, but found {len(processed_files)}")
        if processed_files:
            print("   Found files:")
            for f in processed_files:
                print(f"   - {f.name}")
        else:
            print("   No processed files found!")
        return False

# === BATCH PROCESSING FUNCTION ===
def process_all_marc_files(input_dir):
    """Process all .mrc files and manage file locations"""
    input_path = Path(input_dir)
    archived_dir = input_path / "archived"
    
    print(f"Processing MARC files in: {input_path}")
    print(f"Archive directory: {archived_dir}")
    
    # Find all .mrc files that aren't already processed
    marc_files = [f for f in input_path.glob("*.mrc") if not f.name.endswith("_processed.mrc")]
    
    if not marc_files:
        print("No unprocessed .mrc files found in directory")
        verify_processed_files(input_dir)
        return
    
    print(f"Found {len(marc_files)} files to process: {[f.name for f in marc_files]}")
    
    processed_count = 0
    
    for marc_file in marc_files:
        filename = marc_file.name
        input_path_full = marc_file
        
        # Create output filename
        name, ext = os.path.splitext(filename)
        output_filename = f"{name}_processed{ext}"
        output_path = input_path / output_filename
        
        print(f"\nProcessing: {filename}")
        
        try:
            # Process the MARC file
            with open(input_path_full, 'rb') as infile, open(output_path, 'wb') as outfile:
                reader = MARCReader(infile)
                writer = MARCWriter(outfile)
                
                record_count = 0
                for record in reader:
                    if record is not None:  # Handle potential None records
                        record.fields = clean_record_fields(record)
                        writer.write(record)
                        record_count += 1
                
                writer.close()
            
            print(f"Created: {output_filename} ({record_count} records)")
            
            # Move original file to archived directory
            if move_to_archived(input_path_full, archived_dir):
                processed_count += 1
            
        except Exception as e:
            print(f"Error processing {filename}: {e}")
            # If processing failed, don't move the original file
            continue
    
    print(f"\nProcessing Summary:")
    print(f"   Files processed successfully: {processed_count}")
    print(f"   Files moved to archive: {processed_count}")
    
    # Verify we have exactly 2 processed files for the pipeline
    print(f"\nPipeline Verification:")
    verify_processed_files(input_dir)

# === MAIN EXECUTION ===
if __name__ == "__main__":
    import sys
    
    # Use relative paths - always relative to where script is run from
    if len(sys.argv) > 1:
        # Command line argument provided
        input_directory = Path(sys.argv[1])
    else:
        # Default: look for nclivemrc directory relative to current working directory
        input_directory = Path("nclivemrc")
    
    print("MARC File Processor with Pipeline Management")
    print("=" * 50)
    print(f"Working directory: {input_directory.absolute()}")
    print(f"Current working directory: {Path.cwd()}")
    
    # Verify the directory exists
    if not input_directory.exists():
        print(f"Error: Directory not found: {input_directory}")
        print("Please ensure you're running from the project root directory")
        print("Expected structure:")
        print("  project-root/")
        print("    ├── nclivemrc/")
        print("    ├── clean_marc.py (or nclivemrc/clean_marc.py)")
        print("    └── other files...")
        exit(1)
    
    process_all_marc_files(str(input_directory))