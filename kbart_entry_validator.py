# kbart_entry_validator.py
"""
Validation script for KBART entry management
Ensures proper oclc_entry_id handling and data retention rules
"""

import pandas as pd
from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def validate_kbart_entries(file_path):
    """Validate KBART entry management rules"""
    try:
        if file_path.suffix == '.txt':
            df = pd.read_csv(file_path, sep='\t', dtype=str)
        else:
            df = pd.read_csv(file_path, dtype=str)
        
        issues = []
        
        # Find key columns
        oclc_num_col = None
        entry_id_col = None
        for col in df.columns:
            if 'oclc_number' in col.lower():
                oclc_num_col = col
            elif 'oclc_entry_id' in col.lower() or 'entry_id' in col.lower():
                entry_id_col = col
        
        if not oclc_num_col:
            issues.append("No OCLC number column found")
            return issues
        
        if not entry_id_col:
            issues.append("No oclc_entry_id column found")
            return issues
        
        # Rule 5: Same OCLC number in one file must have unique entry_ids
        oclc_groups = df.groupby(oclc_num_col)[entry_id_col].apply(list)
        for oclc_num, entry_ids in oclc_groups.items():
            if len(entry_ids) > 1:  # Multiple entries for same OCLC number
                unique_entry_ids = set(entry_ids)
                if len(unique_entry_ids) != len(entry_ids):
                    issues.append(f"OCLC {oclc_num} has duplicate entry_ids: {entry_ids}")
                else:
                    logger.info(f"✅ OCLC {oclc_num} has {len(entry_ids)} entries with unique entry_ids")
        
        # Check entry_id format (alphanumeric only)
        invalid_entry_ids = df[entry_id_col].dropna()
        for entry_id in invalid_entry_ids:
            cleaned_entry_id = str(entry_id).replace('_', '').replace('-', '').replace('.', '')
            if not cleaned_entry_id.isalnum():
                issues.append(f"Invalid entry_id format: {entry_id} (should be alphanumeric with optional . _ -)")
                       
        return issues
        
    except Exception as e:
        return [f"Error validating {file_path}: {e}"]

def validate_infobase_lookup_retention(original_file, updated_file):
    """Validate that all original InfobaseLookup entries are retained"""
    try:
        orig_df = pd.read_csv(original_file, dtype=str)
        updated_df = pd.read_csv(updated_file, dtype=str)
        
        issues = []
        
        # Check lookupIDcollection retention (must be unique)
        orig_lookup_ids = set(orig_df['lookupIDcollection'].dropna())
        updated_lookup_ids = set(updated_df['lookupIDcollection'].dropna())
        
        missing_ids = orig_lookup_ids - updated_lookup_ids
        if missing_ids:
            issues.append(f"Missing lookupIDcollection entries: {list(missing_ids)[:10]}...")  # Show first 10
        
        # Check for duplicate lookupIDcollection (should be unique)
        duplicate_lookup_ids = updated_df['lookupIDcollection'].duplicated()
        if duplicate_lookup_ids.any():
            issues.append(f"Duplicate lookupIDcollection found: {updated_df[duplicate_lookup_ids]['lookupIDcollection'].tolist()}")
        
        logger.info(f"Original entries: {len(orig_lookup_ids)}")
        logger.info(f"Updated entries: {len(updated_lookup_ids)}")
        logger.info(f"Retained: {len(orig_lookup_ids & updated_lookup_ids)}")
        logger.info(f"New: {len(updated_lookup_ids - orig_lookup_ids)}")
        
        return issues
        
    except Exception as e:
        return [f"Error validating retention: {e}"]

def validate_all_files(kbart_directory="final_kbart"):
    """Validate all KBART files and InfobaseLookup retention"""
    logger.info("🔍 Starting comprehensive validation...")
    
    all_issues = []
    
    # Validate KBART files
    kbart_dir = Path(kbart_directory)
    if kbart_dir.exists():
        kbart_files = list(kbart_dir.glob("*.txt")) + list(kbart_dir.glob("*.csv"))
        
        for file_path in kbart_files:
            logger.info(f"Validating {file_path.name}...")
            issues = validate_kbart_entries(file_path)
            if issues:
                logger.error(f"❌ Issues in {file_path.name}: {issues}")
                all_issues.extend(issues)
            else:
                logger.info(f"✅ {file_path.name} validation passed")
    
    # Validate InfobaseLookup retention if files exist
    if Path("InfobaseLookup.csv").exists() and Path("InfobaseLookup_updated.csv").exists():
        logger.info("Validating InfobaseLookup retention...")
        retention_issues = validate_infobase_lookup_retention("InfobaseLookup.csv", "InfobaseLookup_updated.csv")
        if retention_issues:
            logger.error(f"❌ InfobaseLookup retention issues: {retention_issues}")
            all_issues.extend(retention_issues)
        else:
            logger.info("✅ InfobaseLookup retention validation passed")
    
    if Path("InfobaseLookup_updated.csv").exists() and Path("InfobaseLookup_final.csv").exists():
        logger.info("Validating InfobaseLookup final retention...")
        final_retention_issues = validate_infobase_lookup_retention("InfobaseLookup_updated.csv", "InfobaseLookup_final.csv")
        if final_retention_issues:
            logger.error(f"❌ InfobaseLookup final retention issues: {final_retention_issues}")
            all_issues.extend(final_retention_issues)
        else:
            logger.info("✅ InfobaseLookup final retention validation passed")
    
    # Summary
    if all_issues:
        logger.error(f"❌ VALIDATION FAILED - {len(all_issues)} issues found")
        return False
    else:
        logger.info("🎉 ALL VALIDATIONS PASSED!")
        return True

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        directory = sys.argv[1]
    else:
        directory = "final_kbart"
    
    validate_all_files(directory)