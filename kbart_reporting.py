"""
KBART Change Reporting Script
Analyzes differences between old and new KBART files and generates comprehensive reports.

This script compares KBART files in kbart_files/originals/ (old) with final_kbart/ (new)
and provides detailed statistics on additions, removals, and retained records.
"""

import pandas as pd
import csv
import re
from pathlib import Path
import logging
from datetime import datetime
from typing import Dict, List, Tuple, Set

# Setup logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class KBARTChangeReporter:
    """
    Analyzes and reports on changes between old and new KBART files.
    """
    
    def __init__(self, 
                 old_kbart_dir: str = "kbart_files/originals",
                 new_kbart_dir: str = "final_kbart"):
        """
        Initialize the KBART change reporter.
        
        Args:
            old_kbart_dir: Directory containing original KBART files
            new_kbart_dir: Directory containing new KBART files
        """
        self.old_kbart_dir = Path(old_kbart_dir)
        self.new_kbart_dir = Path(new_kbart_dir)
        
        # Results storage
        self.comparison_results = {}
        self.collection_mappings = {
            'customer.5210.20': 'jfk',      # JFK maintenance
            'customer.54122.9': 'jfk',      # JFK NC Live
            'customer.5210.ncfod': 'fod',   # FOD maintenance  
            'customer.54122.8': 'fod'       # FOD NC Live
        }
    
    def _decode_url_encoding(self, text: str) -> str:
        """Decode URL percent-encoding in KBART title_id values."""
        if not text:
            return text
        return str(text).replace('%3D', '=').replace('%2D', '-')
    
    def _extract_title_info(self, df: pd.DataFrame) -> Dict[str, Dict]:
        """
        Extract title information from KBART DataFrame.
        
        Args:
            df: KBART DataFrame
            
        Returns:
            Dictionary mapping title_id to title info
        """
        title_info = {}
        
        # Find relevant columns
        title_id_col = None
        title_col = None
        entry_id_col = None
        oclc_num_col = None
        
        for col in df.columns:
            col_lower = col.lower()
            if 'title_id' in col_lower:
                title_id_col = col
            elif col_lower == 'publication_title':
                title_col = col
            elif 'oclc_entry_id' in col_lower or 'entry_id' in col_lower:
                entry_id_col = col
            elif 'oclc_number' in col_lower:
                oclc_num_col = col
        
        if not title_id_col:
            logger.warning("No title_id column found in KBART file")
            return title_info
        
        # Extract title information
        for _, row in df.iterrows():
            title_id_encoded = str(row.get(title_id_col, '')).strip()
            if title_id_encoded:
                # Decode title_id to get the lookup format
                title_id_decoded = self._decode_url_encoding(title_id_encoded)
                
                title_info[title_id_decoded] = {
                    'publication_title': row.get(title_col, '') if title_col else '',
                    'oclc_entry_id': row.get(entry_id_col, '') if entry_id_col else '',
                    'oclc_number': row.get(oclc_num_col, '') if oclc_num_col else '',
                    'title_id_encoded': title_id_encoded
                }
        
        return title_info
    
    def compare_kbart_files(self, old_file: Path, new_file: Path) -> Dict:
        """
        Compare two KBART files and return detailed statistics.
        
        Args:
            old_file: Path to original KBART file
            new_file: Path to new KBART file
            
        Returns:
            Dictionary with comparison results
        """
        logger.info(f"Comparing {old_file.name} vs {new_file.name}")
        
        # Load KBART files
        try:
            old_df = pd.read_csv(old_file, sep='\t', dtype=str, keep_default_na=False)
            new_df = pd.read_csv(new_file, sep='\t', dtype=str, keep_default_na=False)
        except Exception as e:
            logger.error(f"Error reading KBART files: {e}")
            return {}
        
        # Extract title information
        old_titles = self._extract_title_info(old_df)
        new_titles = self._extract_title_info(new_df)
        
        # Calculate sets for comparison
        old_title_ids = set(old_titles.keys())
        new_title_ids = set(new_titles.keys())
        
        # Find changes
        retained_ids = old_title_ids & new_title_ids
        new_ids = new_title_ids - old_title_ids
        removed_ids = old_title_ids - new_title_ids
        
        # Check for oclc_entry_id changes in retained titles
        entry_id_changes = []
        for title_id in retained_ids:
            old_entry_id = old_titles[title_id]['oclc_entry_id']
            new_entry_id = new_titles[title_id]['oclc_entry_id']
            
            if old_entry_id != new_entry_id:
                entry_id_changes.append({
                    'title_id': title_id,
                    'publication_title': old_titles[title_id]['publication_title'],
                    'old_entry_id': old_entry_id,
                    'new_entry_id': new_entry_id
                })
        
        # Create detailed lists for reporting
        new_titles_list = []
        for title_id in new_ids:
            new_titles_list.append({
                'title_id': title_id,
                'publication_title': new_titles[title_id]['publication_title'],
                'oclc_number': new_titles[title_id]['oclc_number'],
                'oclc_entry_id': new_titles[title_id]['oclc_entry_id']
            })
        
        removed_titles_list = []
        for title_id in removed_ids:
            removed_titles_list.append({
                'title_id': title_id,
                'publication_title': old_titles[title_id]['publication_title'],
                'oclc_number': old_titles[title_id]['oclc_number'],
                'oclc_entry_id': old_titles[title_id]['oclc_entry_id']
            })
        
        comparison_result = {
            'old_file': old_file.name,
            'new_file': new_file.name,
            'old_count': len(old_titles),
            'new_count': len(new_titles),
            'retained_count': len(retained_ids),
            'new_count_net': len(new_ids),
            'removed_count': len(removed_ids),
            'entry_id_changes_count': len(entry_id_changes),
            'new_titles': new_titles_list,
            'removed_titles': removed_titles_list,
            'entry_id_changes': entry_id_changes,
            'net_change': len(new_ids) - len(removed_ids)
        }
        
        return comparison_result
    
    def find_file_pairs(self) -> List[Tuple[Path, Path]]:
        """
        Find matching old and new KBART file pairs.
        
        Returns:
            List of (old_file, new_file) tuples
        """
        file_pairs = []
        
        if not self.old_kbart_dir.exists():
            logger.error(f"Old KBART directory not found: {self.old_kbart_dir}")
            return file_pairs
        
        if not self.new_kbart_dir.exists():
            logger.error(f"New KBART directory not found: {self.new_kbart_dir}")
            return file_pairs
        
        # Find all KBART files in new directory
        new_files = list(self.new_kbart_dir.glob("*.txt"))
        
        for new_file in new_files:
            # Look for corresponding old file
            # Try exact name match first
            old_file = self.old_kbart_dir / new_file.name
            
            if not old_file.exists():
                # Try alternative naming patterns
                # customer.5210.20_kbart.txt -> customer.5210.20
                if new_file.name.endswith('_kbart.txt'):
                    base_name = new_file.name.replace('_kbart.txt', '')
                    potential_old_files = [
                        self.old_kbart_dir / f"{base_name}_kbart.txt",
                        self.old_kbart_dir / f"{base_name}.txt",
                    ]
                    
                    for potential_file in potential_old_files:
                        if potential_file.exists():
                            old_file = potential_file
                            break
            
            if not old_file.exists():
                # Strip NC Live datestamp prefix: fod_reload_YYMMDD_ or jfk_reload_YYMMDD_
                stripped_name = re.sub(r'^(?:fod|jfk)_reload_\d{6}_', '', new_file.name)
                if stripped_name != new_file.name:
                    potential_old_files = [
                        self.old_kbart_dir / stripped_name,
                        self.old_kbart_dir / stripped_name.replace('_kbart.txt', '.txt'),
                    ]
                    for potential_file in potential_old_files:
                        if potential_file.exists():
                            old_file = potential_file
                            break

            if old_file.exists():
                file_pairs.append((old_file, new_file))
                logger.info(f"Found file pair: {old_file.name} -> {new_file.name}")
            else:
                logger.warning(f"No matching old file found for {new_file.name}")
        
        return file_pairs
    
    def analyze_all_changes(self) -> Dict:
        """
        Analyze changes across all KBART file pairs.
        
        Returns:
            Dictionary with all comparison results
        """
        logger.info("Starting comprehensive KBART change analysis...")
        
        file_pairs = self.find_file_pairs()
        
        if not file_pairs:
            logger.error("No matching KBART file pairs found")
            return {}
        
        # Analyze each file pair
        for old_file, new_file in file_pairs:
            collection_key = self._determine_collection_key(new_file.name)
            comparison_result = self.compare_kbart_files(old_file, new_file)
            
            if comparison_result:
                self.comparison_results[collection_key] = comparison_result
        
        return self.comparison_results
    
    def _determine_collection_key(self, filename: str) -> str:
        """
        Determine collection key from filename.
        
        Args:
            filename: KBART filename
            
        Returns:
            Collection key for organizing results
        """
        filename_lower = filename.lower()
        
        # Extract collection identifier
        for collection_id in self.collection_mappings.keys():
            if collection_id in filename_lower:
                collection_type = self.collection_mappings[collection_id]
                if 'customer.54122' in collection_id:
                    return f"{collection_type}_nclive"
                else:
                    return f"{collection_type}_maintenance"
        
        # Fallback based on content
        if 'fod' in filename_lower:
            if 'customer.54122' in filename_lower or 'nclive' in filename_lower:
                return 'fod_nclive'
            else:
                return 'fod_maintenance'
        elif 'jfk' in filename_lower or 'customer.5210.20' in filename_lower:
            if 'customer.54122' in filename_lower or 'nclive' in filename_lower:
                return 'jfk_nclive'
            else:
                return 'jfk_maintenance'
        
        return filename.replace('.txt', '').replace('_kbart', '')
    
    def generate_summary_report(self, output_file: str = "kbart_change_summary.txt") -> str:
        """
        Generate a summary report of all changes.
        
        Args:
            output_file: Output file path
            
        Returns:
            Path to generated report
        """
        logger.info(f"Generating summary report: {output_file}")
        
        if not self.comparison_results:
            logger.error("No comparison results available")
            return None
        
        report_lines = [
            "=" * 80,
            "KBART CHANGE ANALYSIS SUMMARY REPORT",
            "=" * 80,
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "OVERVIEW:",
            f"  Total KBART files analyzed: {len(self.comparison_results)}",
            ""
        ]
        
        # Summary statistics
        total_old = sum(result['old_count'] for result in self.comparison_results.values())
        total_new = sum(result['new_count'] for result in self.comparison_results.values())
        total_retained = sum(result['retained_count'] for result in self.comparison_results.values())
        total_added = sum(result['new_count_net'] for result in self.comparison_results.values())
        total_removed = sum(result['removed_count'] for result in self.comparison_results.values())
        total_entry_id_changes = sum(result['entry_id_changes_count'] for result in self.comparison_results.values())
        
        report_lines.extend([
            "OVERALL STATISTICS:",
            f"  Total records in old KBART files: {total_old:,}",
            f"  Total records in new KBART files: {total_new:,}",
            f"  Total records retained: {total_retained:,}",
            f"  Total new records: {total_added:,}",
            f"  Total removed records: {total_removed:,}",
            f"  Net change: {total_new - total_old:+,}",
            f"  OCLC entry_id changes: {total_entry_id_changes:,}",
            ""
        ])
        
        # Break down by collection
        fod_collections = {k: v for k, v in self.comparison_results.items() if 'fod' in k}
        jfk_collections = {k: v for k, v in self.comparison_results.items() if 'jfk' in k}
        
        if fod_collections:
            report_lines.append("FILMS ON DEMAND (FOD) COLLECTIONS:")
            for collection_key, result in fod_collections.items():
                report_lines.extend([
                    f"  {collection_key.upper()}:",
                    f"    Old: {result['old_count']:,} | New: {result['new_count']:,} | Net: {result['net_change']:+,}",
                    f"    Added: {result['new_count_net']:,} | Removed: {result['removed_count']:,} | Retained: {result['retained_count']:,}",
                    f"    Entry ID changes: {result['entry_id_changes_count']:,}",
                    ""
                ])
        
        if jfk_collections:
            report_lines.append("JUST FOR KIDS (JFK) COLLECTIONS:")
            for collection_key, result in jfk_collections.items():
                report_lines.extend([
                    f"  {collection_key.upper()}:",
                    f"    Old: {result['old_count']:,} | New: {result['new_count']:,} | Net: {result['net_change']:+,}",
                    f"    Added: {result['new_count_net']:,} | Removed: {result['removed_count']:,} | Retained: {result['retained_count']:,}",
                    f"    Entry ID changes: {result['entry_id_changes_count']:,}",
                    ""
                ])
        
        # Validation check - NC Live vs Maintenance should match
        report_lines.append("VALIDATION CHECK:")
        
        # Check FOD collections match
        fod_maintenance = next((v for k, v in self.comparison_results.items() if 'fod_maintenance' in k), None)
        fod_nclive = next((v for k, v in self.comparison_results.items() if 'fod_nclive' in k), None)
        
        if fod_maintenance and fod_nclive:
            fod_match = (fod_maintenance['new_count'] == fod_nclive['new_count'] and
                        fod_maintenance['new_count_net'] == fod_nclive['new_count_net'] and
                        fod_maintenance['removed_count'] == fod_nclive['removed_count'])
            
            status = "✅ MATCH" if fod_match else "❌ MISMATCH"
            report_lines.append(f"  FOD Maintenance vs NC Live: {status}")
        
        # Check JFK collections match
        jfk_maintenance = next((v for k, v in self.comparison_results.items() if 'jfk_maintenance' in k), None)
        jfk_nclive = next((v for k, v in self.comparison_results.items() if 'jfk_nclive' in k), None)
        
        if jfk_maintenance and jfk_nclive:
            jfk_match = (jfk_maintenance['new_count'] == jfk_nclive['new_count'] and
                        jfk_maintenance['new_count_net'] == jfk_nclive['new_count_net'] and
                        jfk_maintenance['removed_count'] == jfk_nclive['removed_count'])
            
            status = "✅ MATCH" if jfk_match else "❌ MISMATCH"
            report_lines.append(f"  JFK Maintenance vs NC Live: {status}")
        
        report_lines.extend([
            "",
            "DETAILED REPORTS GENERATED:",
            "  - kbart_change_summary.txt (this file)",
            "  - kbart_new_titles.csv (list of all new titles)",
            "  - kbart_removed_titles.csv (list of all removed titles)",
            "  - kbart_entry_id_changes.csv (OCLC entry_id changes)",
            "",
            "=" * 80
        ])
        
        # Write report
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))
        
        logger.info(f"Summary report saved: {output_file}")
        return output_file
    
    def generate_detailed_csv_reports(self):
        """
        Generate detailed CSV reports for new titles, removed titles, and entry ID changes.
        """
        if not self.comparison_results:
            logger.error("No comparison results available for detailed reports")
            return
        
        # Generate new titles report
        self._generate_new_titles_csv()
        
        # Generate removed titles report
        self._generate_removed_titles_csv()
        
        # Generate entry ID changes report (if any)
        self._generate_entry_id_changes_csv()
    
    def _generate_new_titles_csv(self, output_file: str = "kbart_new_titles.csv"):
        """Generate CSV report of all new titles."""
        new_titles = []
        
        for collection_key, result in self.comparison_results.items():
            for title in result['new_titles']:
                title_record = title.copy()
                title_record['collection'] = collection_key
                title_record['source_file'] = result['new_file']
                new_titles.append(title_record)
        
        if new_titles:
            df = pd.DataFrame(new_titles)
            df = df.sort_values(['collection', 'publication_title'])
            df.to_csv(output_file, index=False)
            logger.info(f"New titles report saved: {output_file} ({len(new_titles)} titles)")
        else:
            logger.info("No new titles to report")
    
    def _generate_removed_titles_csv(self, output_file: str = "kbart_removed_titles.csv"):
        """Generate CSV report of all removed titles."""
        removed_titles = []
        
        for collection_key, result in self.comparison_results.items():
            for title in result['removed_titles']:
                title_record = title.copy()
                title_record['collection'] = collection_key
                title_record['source_file'] = result['old_file']
                removed_titles.append(title_record)
        
        if removed_titles:
            df = pd.DataFrame(removed_titles)
            df = df.sort_values(['collection', 'publication_title'])
            df.to_csv(output_file, index=False)
            logger.info(f"Removed titles report saved: {output_file} ({len(removed_titles)} titles)")
        else:
            logger.info("No removed titles to report")
    
    def _generate_entry_id_changes_csv(self, output_file: str = "kbart_entry_id_changes.csv"):
        """Generate CSV report of OCLC entry_id changes."""
        entry_id_changes = []
        
        for collection_key, result in self.comparison_results.items():
            for change in result['entry_id_changes']:
                change_record = change.copy()
                change_record['collection'] = collection_key
                entry_id_changes.append(change_record)
        
        if entry_id_changes:
            df = pd.DataFrame(entry_id_changes)
            df = df.sort_values(['collection', 'publication_title'])
            df.to_csv(output_file, index=False)
            logger.info(f"Entry ID changes report saved: {output_file} ({len(entry_id_changes)} changes)")
            logger.warning("⚠️  OCLC entry_id changes detected - review for impact on existing links")
        else:
            logger.info("✅ No OCLC entry_id changes detected")
    
    def run_complete_analysis(self):
        """
        Run complete KBART change analysis and generate all reports.
        """
        logger.info("🚀 Starting complete KBART change analysis...")
        
        # Analyze all changes
        self.analyze_all_changes()
        
        if not self.comparison_results:
            logger.error("❌ No comparison results generated. Check file paths and formats.")
            return False
        
        # Generate summary report
        summary_file = self.generate_summary_report()
        
        # Generate detailed CSV reports
        self.generate_detailed_csv_reports()
        
        # Print summary to console
        self._print_console_summary()
        
        logger.info("🎉 KBART change analysis complete!")
        return True
    
    def _print_console_summary(self):
        """Print a brief summary to console."""
        if not self.comparison_results:
            return
        
        total_new = sum(result['new_count_net'] for result in self.comparison_results.values())
        total_removed = sum(result['removed_count'] for result in self.comparison_results.values())
        total_entry_changes = sum(result['entry_id_changes_count'] for result in self.comparison_results.values())
        
        print("\n" + "=" * 60)
        print("KBART CHANGE ANALYSIS SUMMARY")
        print("=" * 60)
        print(f"📊 Files analyzed: {len(self.comparison_results)}")
        print(f"➕ New titles: {total_new:,}")
        print(f"➖ Removed titles: {total_removed:,}")
        print(f"🔄 Entry ID changes: {total_entry_changes:,}")
        print(f"📈 Net change: {total_new - total_removed:+,}")
        print("=" * 60)
        print("📁 Reports generated:")
        print("   - kbart_change_summary.txt")
        print("   - kbart_new_titles.csv")
        print("   - kbart_removed_titles.csv")
        if total_entry_changes > 0:
            print("   - kbart_entry_id_changes.csv")
        print("=" * 60)

def main():
    """Main function for KBART change reporting."""
    print("KBART Change Analysis and Reporting")
    print("=" * 40)
    print("Comparing old KBART files (kbart_files/originals/)")
    print("with new KBART files (final_kbart/)")
    print()
    
    # Initialize reporter
    reporter = KBARTChangeReporter()
    
    # Run complete analysis
    success = reporter.run_complete_analysis()
    
    if not success:
        print("\n❌ Analysis failed. Check that:")
        print("  - kbart_files/originals/ directory exists with old KBART files")
        print("  - final_kbart/ directory exists with new KBART files")
        print("  - File names match between directories")
        return
    
    print("\n🎯 Next steps:")
    print("1. Review kbart_change_summary.txt for overview")
    print("2. Check new and removed titles in CSV files")
    print("3. Verify NC Live vs Maintenance counts match")
    print("4. Upload final KBART files to OCLC Collection Manager")

if __name__ == "__main__":
    main()