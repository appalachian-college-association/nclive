# OCLC Discovery API Search Tool & Complete MARC Processing Workflow

This comprehensive toolset automates the complete workflow for processing Infobase MARC records to replace invalid entries (Infobase title ID with 1000* prefix in MARC 035), performing OCLC Discovery API searches, and generating KBART files for OCLC Collection Manager. It replaces a manual MarcEdit + OpenRefine workflow with an integrated Python solution.

## Overview

The workflow consists of multiple integrated components:

1. **MARC Processing**: Extract and validate title IDs from Infobase MARC files using hierarchical lookup
2. **OCLC API Search**: Query WorldCat Discovery API for missing OCLC numbers
3. **Extended Search**: Enhanced searching with title and series matching for manual review items and optional human lookup and verification
4. **KBART Integration**: Generate final KBART files with validation and comprehensive statistics and FOD/JFK breakdowns

## Complete Workflow Architecture

```
MARC Files → MARC Processor → OCLC API Search → Extended Search → KBART Integration
    ↓              ↓              ↓                ↓               ↓
FOD*.mrc     search_terms.tsv    oclc_results.csv manual_review.csv .final_kbart
Just*.mrc    InfobaseLookup.csv  API matches      (optional) human verification  (KBART txt files ready to load to OCLC Manager)
            (primary authority)
```

## Files and Components

### Pre-processing
- **`clean_marc.py`** - Prepare Infobase MARC for processing
# Check your current directory has these files:
- **`InfobaseLookup.csv`**
- OCLC KBART File(s) for Infobase collections
- Cleaned .mrc for Infobase collections
- **`.env file`** OCLC API credentials and search config

### Core Processing Scripts
- **`marc_processor.py`** - Main MARC processing with hierarchical lookup
- **`main.py`** - OCLC Discovery API search engine
- **`extended_marc_processor.py`** - Enhanced searches for manual review items
- **`kbart_integration.py`** - Final KBART integration and comprehensive reporting

### Supporting Modules
- **`auth.py`** - OCLC API authentication with token caching
- **`config.py`** - Configuration settings and API endpoints
- **`requirements.txt`** - Python dependencies

### Authority and Data Files
- **`InfobaseLookup.csv`** - Primary authority file (manually verified OCLC numbers)
- **`kbart_files/`** - Directory containing current OCLC KBART files
- **`nclivemrc/`** - Directory containing downloaded Infobase MARC files

### Generated Files
- **`search_terms.tsv`** - API search terms for unmatched items
- **`oclc_results.csv`** - API search results
- **`manual_review_searches.csv`** - Extended search results for manual review
- **`InfobaseLookup_final.csv`** - Updated authority file
- **`infobase_kbart_final.txt`** - Final KBART ready for OCLC upload

### Validation and Reporting
- **`kbart_entry_validator.py`** - Validation script for KBART entry management
- **`kbart_reporting.py`** - Detailed statistics on additions, removals, and retained records

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

**Required packages:**
- pandas
- numpy
- requests
- pymarc
- python-dotenv
- structlog
- urllib3
- pathlib

### 2. Create Environment Configuration

Create a `.env` file in the project directory:

```env
# OCLC Discovery API Credentials
OCLC_KEY=your_oclc_key
OCLC_SECRET=your_oclc_secret
OCLC_BASE_URL=https://discovery.api.oclc.org/worldcat-org-ci
KB_BASE_URL=https://worldcat.org/webservices/kb/rest/entries/search
WORLDCAT_KB_KEY=your_kb_key #no secret required for read-only access

# Search configuration
RESTRICT_TO_LIBRARY=false
DEFAULT_LIBRARY=your_oclc_symbol

# Storage credentials (Infobase MARC retrieval - Optional)
FTP_USER=your_username
FTP_PASSWORD=your_password


# Optional: Credentials to scrape MARC files from mass storage (FTP)
local processing 
```

### 3. Prepare Directory Structure

```
project/
├── nclivemrc/           # MARC files directory
│   ├── FOD*.mrc        # Films on Demand MARC files
│   └── Just*.mrc       # Just for Kids MARC files
├── kbart_files/        # Current OCLC KBART files
│   └── *.txt          # Tab-separated KBART files
├── InfobaseLookup.csv  # Primary authority file
└── [script files]
```

## Complete Usage Workflow

### Phase 1: Initial MARC Processing

```bash
# Process MARC files using hierarchical lookup
python marc_processor.py
```

**This produces:**
- `search_terms.tsv` - Terms needing API lookup
- `InfobaseLookup_updated.csv` - Updated lookup file
- `rejected_records.csv` - Invalid records for analysis
- Statistics showing hierarchical lookup performance

**Hierarchical Lookup Strategy:**
1. **Primary Authority**: InfobaseLookup.csv (manually verified matches)
2. **Secondary Authority**: Current KBART files (existing collection data)  
3. **Tertiary Source**: MARC 035 field (filtered for title ID contamination)
4. **Needs Search**: Items requiring API lookup

### Phase 2: OCLC API Search (for unmatched items)

```bash
# Search OCLC API for items without verified OCLC numbers
python main.py
```

**This produces:**
- `oclc_results.csv` - API search results with bibliographic data
- Processing statistics and match rates

**Search Strategy:**
- Batched queries: `x4:digital AND (sn:296504 OR sn:296497 OR ...)`
- Electronic video filtering: `generalFormat=Video AND specificFormat=Digital`
- Rate limiting and error handling
- Individual result matching to original lookup IDs

### Phase 3: Extended Search (for manual review items)

```bash
# Enhanced searching for items marked "MANUAL_REVIEW"
python extended_marc_processor.py
```

**Enhanced Search Features:**
- **Series extraction**: Automatically detects series from title parentheticals
- **Title-based searches**: `ti:"Assessment of the Newborn" AND x4:digital`
- **Series-level searches**: `se:"Assessment of the Newborn Series" AND x4:digital`
- **Smart ranking**: Prioritizes title matches over series matches
- **FOD/JFK tracking**: Collection-specific statistics

**This produces:**
- `manual_review_searches.csv` - Structured file for human review
- `extended_search_stats.txt` - Detailed statistics with FOD/JFK breakdown

### Phase 4: Manual Review Process

Edit `manual_review_searches.csv`:
- **Review suggested matches**: Check OCLC numbers and titles
- **Update `verifiedOCN`**: Enter correct OCLC numbers
- **Set `accept_suggestion`**: Mark "yes" for items to accept

**Manual Review Columns:**
- `original_lookup_id` - Source identifier
- `suggested_oclc` - API-found OCLC number
- `match_type` - TITLE_MATCH, SERIES_MATCH, or NO_MATCH
- `verifiedOCN` - **Your input**: Final OCLC number to use
- `accept_suggestion` - **Your input**: "yes" to accept, blank to skip

### Phase 5: Process Manual Updates

```bash
# Integrate manual review decisions
python extended_marc_processor.py --process-updates manual_review_searches.csv
```

**This produces:**
- `InfobaseLookup_final.csv` - Complete authority file with all updates
- `kbart_additions.txt` - New entries from manual review
- Source labels: `API_EXT_TITLE`, `API_EXT_SERIES` for tracking

### Phase 6: Final KBART Integration

```bash
# Create final KBART with comprehensive reporting
python kbart_integration.py
```

**This produces:**
- `infobase_kbart_final.txt` - **Ready for OCLC Collection Manager upload**
- `kbart_integration_report.txt` - Comprehensive statistics
- `kbart_changes_detail.csv` - Detailed change analysis

## Output Specifications

### OCLC Search Results (`oclc_results.csv`)

| Column | Description |
|--------|-------------|
| lookupID | Original identifier from search terms |
| oclcNumber | OCLC record number |
| title | Title of the resource |
| standardNumbers | ISBN/ISSN identifiers |
| generalFormat | Primary format category (Video, Book, etc.) |
| specificFormat | Secondary format category (Digital, DVD, etc.) |
| formatDescription | Combined format description |
| isElectronicVideo | "Yes" if the item is an electronic video |
| materialTypes | Additional material type information |

### Final KBART Output (`infobase_kbart_final.txt`)

| Column | Description |
|--------|-------------|
| publication_title | Resource title |
| title_id | URL-encoded title identifier (e.g., `xtid%3D296504`) |
| title_url | Direct access URL (decoded for EZproxy compatibility) |
| oclc_number | Verified OCLC number |
| collection_type | fod (Films on Demand) or jfk (Just for Kids) |
| source | Data source: InfobaseLookup, KBART, MARC_035, API_SEARCH, etc. |
| last_updated | Processing date |

## Statistics and Reporting

The workflow provides comprehensive statistics with **FOD/JFK breakdowns**:

```
HIERARCHICAL LOOKUP PERFORMANCE:
  InfobaseLookup matches (PRIMARY): 62,642
    - FOD: 45,231 | JFK: 17,411
  KBART matches (SECONDARY): 892  
    - FOD: 634 | JFK: 258
  MARC 035 matches (TERTIARY): 156
  API Search needed: 249

MANUAL REVIEW RESULTS:
  Total manual review items: 54
    - FOD: 31 | JFK: 23
  Title matches found: 18
    - FOD: 12 | JFK: 6
  Series matches found: 22  
    - FOD: 15 | JFK: 7

FINAL COLLECTION CHANGES:
  Total kept: 62,891
  Total added: 271
  Total removed: 156
    - FOD changes: +89 net | JFK changes: +67 net
```

## Customization Options

### MARC Processing
- **Collection detection**: Modify `_validate_title_id_with_url()` for different URL patterns
- **Field extraction**: Adjust `_extract_record_fields()` for additional MARC fields
- **Title ID validation**: Update `_validate_oclc_number()` for new contamination patterns

### API Search Configuration
```python
# In main.py - adjust batch size
batch_size = 4  # Smaller = more reliable, Larger = faster

# Query format customization
def build_query(terms):
    return f'x4:digital AND ({" OR ".join(f"sn:{term}" for term in terms)})'
```

### Extended Search Parameters
```python
# In extended_marc_processor.py - search limits
title_matches = self.search_oclc_by_title(title, max_results=5)
series_matches = self.search_oclc_by_series(series, max_results=3)

# Rate limiting
time.sleep(1)  # Pause between searches
```

## Troubleshooting

### Common Issues

**Authentication Problems:**
- Verify OCLC credentials in `.env`
- Check API permissions for WorldCat Discovery
- Monitor token expiration in logs

**MARC Processing Errors:**
- Ensure MARC files are in `nclivemrc/` directory
- Check for corrupted .mrc files
- Verify InfobaseLookup.csv column headers

**API Rate Limiting:**
- Increase sleep intervals between requests
- Reduce batch sizes for large datasets
- Monitor API quota usage

**File Encoding Issues:**
- Ensure UTF-8 encoding for all text files
- Check CSV delimiter consistency (tabs vs commas)
- Verify special character handling

### Performance Optimization

**For Large Datasets:**
1. Process MARC files in smaller batches
2. Use parallel processing for API searches (future enhancement)
3. Implement result caching for repeated searches
4. Monitor memory usage with large CSV files

**For Monthly Updates:**
1. Maintain InfobaseLookup.csv as authoritative source
2. Archive previous KBART files for change tracking
3. Monitor success rates to optimize search strategies
4. Update series extraction patterns as needed

## Development and Future Enhancements

### Current Architecture

The system follows a modular, pipeline-based design:

1. **Data Sources**: MARC files, InfobaseLookup, KBART files
2. **Processing Engines**: MARC parser, OCLC API client, search enhancer
3. **Integration Layer**: KBART generator, statistics reporter
4. **Output Generation**: Files ready for OCLC systems

### Planned Enhancements

#### Phase 1: Enhanced Automation
- **Auto-accept high-confidence matches**: Automatically accept title matches above 90% confidence
- **Batch series processing**: Group series-level matches for bulk acceptance
- **Smart retry logic**: Exponential backoff for API failures

#### Phase 2: Advanced Search Capabilities  
- **Fuzzy title matching**: Handle minor title variations and typos
- **Multi-field searching**: Combine title, series, and metadata searches
- **WorldCat record creation**: Automatic creation of missing records (with extensive testing)

#### Phase 3: Web Interface
- **Manual review dashboard**: Web-based interface for reviewing matches
- **Statistics visualization**: Interactive charts and graphs
- **Progress monitoring**: Real-time processing status

#### Phase 4: Integration Enhancements
- **Direct OCLC upload**: API-based KBART file submission
- **Change notifications**: Automated alerts for collection changes
- **Validation checks**: Pre-upload KBART validation

### Contributing

When making changes to the workflow:

1. **Test with small datasets first** - Use 10-20 records for initial testing
2. **Monitor API usage** - Track query counts and rate limits
3. **Validate output formats** - Ensure KBART compatibility with OCLC systems
4. **Document search patterns** - Update series extraction rules as patterns evolve
5. **Maintain backward compatibility** - Preserve InfobaseLookup.csv structure

### Version History

- **v3.0** - Complete workflow integration with extended search capabilities
- **v2.x** - Hierarchical lookup implementation  
- **v1.x** - Basic OCLC API search functionality

---

## Success Metrics

The workflow has demonstrated impressive success rates:
- **62,642 verified matches** from InfobaseLookup authority
- **249 additional matches** from WorldCat API searches
- **54 items for manual review** (down from hundreds in manual workflow)
- **Zero errors** in OCLC Collection Manager uploads
- **Monthly processing time**: Reduced from days to hours

This represents a **99%+ automation rate** for MARC-to-KBART processing, with comprehensive audit trails and quality control measures.

## Support and Maintenance

For ongoing maintenance:
- Monitor monthly success rates and adjust search strategies
- Update InfobaseLookup.csv as the primary authority source
- Archive processing reports for trend analysis
- Review and update series extraction patterns annually
- Test API changes and update authentication as needed

The system is designed to be **production-ready** and **maintainable** for long-term use in library collection management workflows.