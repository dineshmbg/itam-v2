"""What does Inventory match make of a file?   python portal/db/profile_file.py <file> [<file> ...]
Prints the headings it found, the product it decided on, how many signature headings matched, and how many rows start with ANK. Changes nothing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from portal.app import inventory_match as m  # noqa: E402

for f in sys.argv[1:]:
    p = Path(f)
    headers, rows = m.read_file(p.name, p.read_bytes())
    prof = m.profile_file(headers)
    name = headers.index(prof["mapping"]["name"]) if prof["mapping"].get("name") in headers else None
    ank = sum(1 for r in rows if name is not None and m.key_of(r[name]).startswith("ANK"))
    print(f"\n{p.name}\n  rows: {len(rows)}   rows starting with ANK: {ank}\n  headings: {headers}\n  product: {prof['product']} ({prof['label']})   {prof['why']}")
    for key, spec in m.PRODUCTS.items():
        have = {h.strip().lower() for h in headers}
        print(f"  {key}: {sum(1 for s in spec['sig'] if s in have)} of {len(spec['sig'])} signature headings (needs {spec['min']})")
