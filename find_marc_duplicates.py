"""
find_marc_duplicates.py

Identifies MARC records sharing the same lookup_id_collection value,
which causes silent overwrites in marc_processor.py line 1035:
    current_marc_records = {r['lookup_id_collection']: r for r in self.current_records}

Run from the same directory as your MARC files.
Usage: python find_marc_duplicates.py
"""

from pathlib import Path
from pymarc import MARCReader
import re
import sys

MARC_FILES = [
    'nclivemrc/FOD-05-11-2026_processed.mrc',
    'nclivemrc/Just_For_Kids_05-11-2026_processed.mrc',
]

def extract_avod_title_id(url: str) -> str:
    match = re.search(r'access\.infobase\.com/([^/?]+)/([^/?]+)', url, re.IGNORECASE)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return ""

def extract_lookup_id_collection(record) -> tuple:
    """Extract (lookup_id_collection, title, url) from a pymarc record."""
    field_028 = record.get_fields('028')
    field_856 = record.get_fields('856')
    field_245 = record.get_fields('245')
    field_856_z = [f['z'] for f in field_856 if f['z']] if field_856 else []

    title = field_245[0]['a'].strip() if field_245 and field_245[0]['a'] else ''
    url = field_856[0]['u'].strip() if field_856 and field_856[0]['u'] else ''

    title_ids = []
    for f in field_028:
        val = f['a'] or ''
        if ';' in val:
            title_ids.extend(v.strip() for v in val.split(';'))
        else:
            title_ids.append(val.strip())

    # Determine collection type from 856$z
    collection_type = 'fod' if any(
        'Films on Demand' in z or 'FOD Collection' in z or 'AVOD Collection' in z
        for z in field_856_z
    ) else 'jfk'

    # Determine lookup_id
    lookup_id = None
    url_lower = url.lower()
    if 'access.infobase.com' in url_lower:
        for tid in title_ids:
            if tid.strip().isdigit():
                lookup_id = f"xtid={tid.strip()}$"
                break
        if not lookup_id:
            m = re.search(r'access\.infobase\.com/[^/?]+/([^/?]+)', url_lower)
            if m and m.group(1).isdigit():
                lookup_id = f"xtid={m.group(1)}$"
    else:
        for tid in title_ids:
            if f"id={tid}&" in url_lower or f"customid={tid}&" in url_lower:
                prefix = 'xtid' if 'xtid=' in url_lower else 'customid'
                lookup_id = f"{prefix}={tid}$"
                break

    if not lookup_id:
        return None, title, url

    return f"{lookup_id}{collection_type}", title, url


def main():
    all_records = []  # list of (lookup_id_collection, title, url, filename)

    for marc_filename in MARC_FILES:
        path = Path(marc_filename)
        if not path.exists():
            print(f"WARNING: {marc_filename} not found, skipping.")
            continue
        with open(path, 'rb') as f:
            reader = MARCReader(f)
            for record in reader:
                if record is None:
                    continue
                lic, title, url = extract_lookup_id_collection(record)
                if lic:
                    all_records.append((lic, title, url, marc_filename))

    print(f"Total records parsed: {len(all_records)}")

    # Find duplicates
    seen = {}
    for lic, title, url, filename in all_records:
        seen.setdefault(lic, []).append((title, url, filename))

    duplicates = {k: v for k, v in seen.items() if len(v) > 1}

    if not duplicates:
        print("No duplicate lookup_id_collection values found.")
        print("The 3 missing records may be dropped for a different reason.")
        sys.exit(0)

    print(f"\nFound {len(duplicates)} lookup_id_collection value(s) with duplicates:\n")
    for lic, entries in duplicates.items():
        print(f"  lookup_id_collection: {lic}  ({len(entries)} records)")
        for i, (title, url, filename) in enumerate(entries, 1):
            print(f"    [{i}] title: {title}")
            print(f"         url:   {url}")
            print(f"         file:  {filename}")
        print()

    print(f"In dict comprehension, only the LAST entry per key is kept.")
    print(f"Total records lost to silent overwrite: "
          f"{sum(len(v)-1 for v in duplicates.values())}")


if __name__ == '__main__':
    main()
