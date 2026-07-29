import sqlite3, json

DB = "data/predictx_memory.sqlite3"
MATCH_ID = "sr:match:72946560"

conn = sqlite3.connect(DB, timeout=10)
row = conn.execute(
    "select raw_enriched from match_buffer where match_id=?", (MATCH_ID,)
).fetchone()
conn.close()

if not row or not row[0]:
    print("NOT FOUND")
else:
    doc = json.loads(row[0])
    # Print top-level keys and sizes
    print("=== TOP-LEVEL KEYS ===")
    for k, v in doc.items():
        if isinstance(v, dict):
            print(f"  {k}: dict({len(v)} keys)")
        elif isinstance(v, list):
            print(f"  {k}: list({len(v)} items)")
        elif isinstance(v, str) and len(v) > 80:
            print(f"  {k}: str({len(v)} chars) = {v[:80]}...")
        else:
            print(f"  {k}: {repr(v)}")

    # Show sofascore_detail keys if present
    sd = doc.get("sofascore_detail") or {}
    if sd:
        print("\n=== sofascore_detail keys ===")
        for k, v in sd.items():
            if isinstance(v, dict):
                print(f"  {k}: dict({len(v)} keys)")
            elif isinstance(v, list):
                print(f"  {k}: list({len(v)} items)")
            else:
                print(f"  {k}: {repr(str(v)[:80])}")

    # Show odds structure
    odds = doc.get("odds") or doc.get("markets") or {}
    print(f"\n=== odds/markets ===")
    print(json.dumps(odds, default=str)[:600] if odds else "  NONE")

    # Sportybet markets
    sb = doc.get("sportybet_markets") or doc.get("sportybet") or {}
    print(f"\n=== sportybet_markets ===")
    print(json.dumps(sb, default=str)[:600] if sb else "  NONE")
