#!/usr/bin/env python3
"""
Pulls the AMX corporate bond snapshot and appends it to data/history.csv.

    python3 fetch.py discover     inspect the live API and print its real schema
    python3 fetch.py              take today's snapshot
    python3 fetch.py --gov        government bonds (needed for the risk-free curve)
    python3 fetch.py backfill     pull per-ISIN history from getInstrument

Endpoints are undocumented. If AMX changes them this script fails loudly rather
than writing empty rows — a silent gap in the history is the expensive failure.
"""

import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles often default to cp1252, which cannot print Armenian issuer
# names. Without this, a long backfill can die partway through on a name rather
# than on anything that matters.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = "https://amx.am/api"
ROOT = Path(__file__).parent
DATA = ROOT / "data"
RAW = DATA / "raw"
PAUSE = 0.4

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://amx.am/en/market_data/corporate_bonds",
    "X-Requested-With": "XMLHttpRequest",
}


def get(url, retries=3):
    err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - want the message, whatever it is
            err = e
            if i < retries - 1:
                time.sleep(2 ** i)
    raise RuntimeError(f"{url} failed after {retries} tries: {type(err).__name__}: {err}")


def split(payload):
    """Bond rows and the embedded FX-rate object arrive in the same array."""
    bonds, rates = [], {"AMD": 1.0}
    for item in payload.get("data", []):
        if isinstance(item, dict) and "rate" in item:
            for cur, val in item["rate"].items():
                try:
                    rates[cur] = float(val)
                except (TypeError, ValueError):
                    pass
        elif isinstance(item, dict):
            bonds.append(item)
    return bonds, rates


def isin_of(row):
    for k in ("isin", "ISIN", "Isin", "isin_code", "symbol"):
        if row.get(k):
            return str(row[k]).strip()
    return None


def append_history(rows, stamp):
    """Append today's rows, skipping any ISIN already recorded for this date."""
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "history.csv"

    existing_cols, seen = [], set()
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            existing_cols = list(rd.fieldnames or [])
            for r in rd:
                seen.add((r.get("isin"), r.get("snapshot_date")))

    fresh = [r for r in rows if (r.get("isin"), stamp) not in seen]
    if not fresh:
        print("history: already recorded for this date, nothing appended")
        return 0

    cols = list(existing_cols)
    for r in fresh:
        for k in r:
            if k not in cols:
                cols.append(k)

    # If new fields appeared, rewrite the file so every row shares a header.
    if existing_cols and cols != existing_cols:
        old = []
        with path.open(encoding="utf-8-sig", newline="") as f:
            old = list(csv.DictReader(f))
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(old + fresh)
        print(f"history: schema changed, rewrote with {len(cols)} columns")
        return len(fresh)

    new_file = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(fresh)
    return len(fresh)


def flatten(row, prefix=""):
    """Nested objects become prefixed columns (price.close -> price_close).
    Lists are kept as JSON so coupon_date survives the round trip to CSV."""
    out = {}
    for k, v in (row or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, f"{key}_"))
        elif isinstance(v, list):
            out[key] = json.dumps(v, ensure_ascii=False)
        else:
            out[key] = v
    return out


def snapshot(market="corporate_bonds", detail=True):
    RAW.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    payload = get(f"{BASE}/getMarketData/{market}")
    bonds, rates = split(payload)
    if not bonds:
        raise RuntimeError("API returned zero bonds — refusing to write an empty snapshot")
    print(f"{len(bonds)} instruments")

    (RAW / f"{market}_{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    rows, details = [], []
    for i, b in enumerate(bonds, 1):
        row = flatten(b)
        row["isin"] = isin_of(b)
        row["snapshot_date"] = stamp
        row["market"] = market
        if detail and row["isin"]:
            try:
                d = get(f"{BASE}/getInstrument/{row['isin']}", retries=2).get("data", {}) or {}
                d.pop("market_data", None)
                flat = flatten(d)
                details.append({k: flat.get(k) for k in INSTRUMENT_FIELDS if k in flat}
                               | {"isin": row["isin"]})
            except RuntimeError:
                row["detail_error"] = "1"
            time.sleep(PAUSE)
        rows.append(slim(row))
        if i % 10 == 0 or i == len(bonds):
            print(f"\r  {i}/{len(bonds)}", end="", flush=True)
    print()

    if details:
        print(f"{save_instruments(details)} instruments in data/instruments.csv")
    n = merge_history(rows)
    (DATA / "fx.json").write_text(json.dumps({"date": stamp, "rates": rates}, indent=2),
                                  encoding="utf-8")
    print(f"appended {n} rows for {stamp}")
    return rows


# A bond's terms do not change day to day, so they belong in one row per ISIN
# rather than repeated on every historical row. Keeping coupon_date, documents
# and market_makers on all 250 daily rows for 208 bonds produced a 120 MB file
# that GitHub refuses to accept; split out, the same data is about 12 MB.
INSTRUMENT_FIELDS = [
    "isin", "ticker", "currency", "per_value", "cpn_rate", "cpn_frequency_en",
    "cpn_qquantity", "coupon_date", "day_count", "maturity_date", "issue_date",
    "first_payment_date", "isin_class", "instrument_type_en", "issuer_name_en",
    "issuer_id", "list_class", "isin_status_en", "outst_volume",
    "outst_volume_amd", "outst_quantity", "benchmark", "inflation_linked",
]

# Only price and quote data varies by day; everything else joins from instruments.csv.
HISTORY_FIELDS = [
    "isin", "snapshot_date", "cur", "price_bid", "price_ask", "price_close",
    "price_open", "price_high", "price_low", "price_avg", "price_change",
    "yield_bid", "yield_ask", "yield_close", "yield_open", "yield_high",
    "yield_low", "yield_avg", "yield_change", "vol", "trades_number",
    "last_date", "list", "market",
]


def save_instruments(details):
    """Upsert one row per ISIN into data/instruments.csv."""
    path = DATA / "instruments.csv"
    rows = {}
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                rows[r.get("isin")] = r
    for d in details:
        if d.get("isin"):
            rows[d["isin"]] = {**rows.get(d["isin"], {}), **d}
    cols = list(INSTRUMENT_FIELDS)
    for r in rows.values():
        for k in r:
            if k not in cols:
                cols.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])
    return len(rows)


def slim(row):
    """Drop anything that is not day-varying price data."""
    return {k: v for k, v in row.items() if k in HISTORY_FIELDS}


# Per-ISIN history uses different column names from the live board. Map them
# onto the board's names so one snapshot and one backfilled day are the same
# shape, and the dashboard cannot tell them apart.
HIST_MAP = {
    "order_date": "snapshot_date",
    "best_bid_price": "price_bid", "best_ask_price": "price_ask",
    "best_bid_yield": "yield_bid", "best_ask_yield": "yield_ask",
    "close_price": "price_close", "close_yield": "yield_close",
    "open_price": "price_open", "open_yield": "yield_open",
    "high_price": "price_high", "high_yield": "yield_high",
    "low_price": "price_low", "low_yield": "yield_low",
    "avg_price": "price_avg", "avg_yield": "yield_avg",
    "price_change": "price_change", "yield_change": "yield_change",
    "currency": "cur", "trades_volume": "vol",
}


def backfill(market="corporate_bonds"):
    """
    Pulls every instrument's full price history and merges it into
    data/history.csv. One getInstrument call per ISIN returns both the static
    terms and the whole history, so this is one pass, not two.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    bonds, _ = split(get(f"{BASE}/getMarketData/{market}"))
    if not bonds:
        raise RuntimeError("no instruments returned")
    print(f"{len(bonds)} instruments; pulling full history for each")

    rows, details, no_history, failed = [], [], 0, 0
    for i, b in enumerate(bonds, 1):
        isin = isin_of(b)
        if not isin:
            continue
        try:
            data = (get(f"{BASE}/getInstrument/{isin}", retries=2).get("data") or {})
        except RuntimeError:
            failed += 1
            print(f"\r  {i}/{len(bonds)}  {len(rows)} rows  ({failed} failed)", end="", flush=True)
            time.sleep(PAUSE)
            continue

        hist = data.pop("market_data", None) or []
        flat = flatten(data)
        details.append({k: flat.get(k) for k in INSTRUMENT_FIELDS if k in flat}
                       | {"isin": isin})
        if not hist:
            no_history += 1

        for h in hist:
            row = {}
            for k, v in flatten(h).items():
                row[HIST_MAP.get(k, k)] = v
            if not row.get("snapshot_date"):
                continue
            row["snapshot_date"] = str(row["snapshot_date"])[:10]
            row["isin"] = isin
            row["market"] = market
            rows.append(slim(row))

        print(f"\r  {i}/{len(bonds)}  {len(rows)} rows", end="", flush=True)
        time.sleep(PAUSE)
    print()

    if not rows:
        print("the API returned no history at all")
        return

    n_inst = save_instruments(details)
    added = merge_history(rows)
    dates = sorted({r["snapshot_date"] for r in rows})
    print(f"{n_inst} instruments written to data/instruments.csv")
    print(f"{len(rows)} historical rows covering {dates[0]} to {dates[-1]}")
    print(f"{added} added to data/history.csv ({len(rows) - added} already present)")
    if no_history:
        print(f"{no_history} instruments have no history published")
    if failed:
        print(f"{failed} instruments could not be fetched — re-run to pick them up")


def merge_history(rows):
    """Merge rows into history.csv, keyed on (isin, snapshot_date). Existing
    rows always win, so re-running can never overwrite what you already have."""
    path = DATA / "history.csv"
    existing, cols = [], []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            cols = list(rd.fieldnames or [])
            existing = list(rd)

    seen = {(r.get("isin"), r.get("snapshot_date")) for r in existing}
    fresh = []
    for r in rows:
        key = (r.get("isin"), r.get("snapshot_date"))
        if key in seen:
            continue
        seen.add(key)
        fresh.append(r)
    if not fresh:
        return 0

    for r in fresh:
        for k in r:
            if k not in cols:
                cols.append(k)

    merged = existing + fresh
    merged.sort(key=lambda r: (r.get("snapshot_date") or "", r.get("isin") or ""))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(merged)
    return len(fresh)


def discover(market="corporate_bonds"):
    RAW.mkdir(parents=True, exist_ok=True)
    payload = get(f"{BASE}/getMarketData/{market}")
    bonds, rates = split(payload)
    (RAW / "discover_market.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def show(rows, label):
        print(f"\n{'=' * 68}\n{label}  ({len(rows)} records)\n{'=' * 68}")
        keys = {}
        for r in rows:
            if isinstance(r, dict):
                for k, v in r.items():
                    keys.setdefault(k, []).append(v)
        for k in sorted(keys):
            vals = [v for v in keys[k] if v not in (None, "")]
            s = vals[0] if vals else None
            if isinstance(s, (dict, list)):
                s = f"<{type(s).__name__} len={len(s)}>"
            print(f"  {k:<30} {len(vals)}/{len(rows):<6} e.g. {str(s)[:52]}")

    show(bonds, f"getMarketData/{market}")
    print(f"\nFX: {rates}")

    isin = isin_of(bonds[0]) if bonds else None
    if isin:
        inst = get(f"{BASE}/getInstrument/{isin}")
        (RAW / "discover_instrument.json").write_text(
            json.dumps(inst, ensure_ascii=False, indent=2), encoding="utf-8")
        d = dict(inst.get("data", {}) or {})
        hist = d.pop("market_data", None)
        show([d], f"getInstrument/{isin}")
        if hist:
            print(f"\n>>> market_data holds {len(hist)} historical rows for this ISIN")
            show(hist[:100], "history row shape")

    print(f"\nRaw responses in {RAW}/ — map these names into config.json")


if __name__ == "__main__":
    args = sys.argv[1:]
    mkt = "government_bonds" if "--gov" in args else "corporate_bonds"
    cmd = next((a for a in args if not a.startswith("-")), "snapshot")
    try:
        if cmd == "discover":
            discover(mkt)
        elif cmd == "backfill":
            backfill()
        else:
            snapshot(mkt, detail="--fast" not in args)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
