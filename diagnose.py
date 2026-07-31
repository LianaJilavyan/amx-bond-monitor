#!/usr/bin/env python3
"""
diagnose.py — reads your data/history.csv and reports exactly why a field is
not being read. Run it when the dashboard shows blanks.

    python3 diagnose.py
"""

import csv
import sys
from pathlib import Path

import analytics as an
import amx_schema as ax

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).parent
HIST = ROOT / "data" / "history.csv"


def main():
    if not HIST.exists():
        print("data/history.csv does not exist. Run: python3 fetch.py")
        return 1

    with HIST.open(encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        cols = list(rd.fieldnames or [])
        rows = list(rd)

    inst_path = ROOT / "data" / "instruments.csv"
    inst = {}
    if inst_path.exists():
        with inst_path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("isin"):
                    inst[r["isin"]] = {f"i_{k}": v for k, v in r.items() if v not in (None, "")}
    for r in rows:
        terms = inst.get(r.get("isin"))
        if terms:
            for k, v in terms.items():
                r.setdefault(k, v)
            if not r.get("maturity_date"):
                r["maturity_date"] = terms.get("i_maturity_date")
    for r in rows[:1]:
        for k in r:
            if k not in cols:
                cols.append(k)

    print(f"history.csv: {len(rows)} rows, {len(cols)} columns after joining "
          f"{len(inst)} instruments\n")

    dates = sorted({r.get("snapshot_date") for r in rows if r.get("snapshot_date")})
    print(f"snapshot dates: {len(dates)}  {dates[:3]}{' ...' if len(dates) > 3 else ''}")
    latest = [r for r in rows if r.get("snapshot_date") == dates[-1]] if dates else rows
    print(f"rows on the latest date: {len(latest)}\n")

    # ---- every logical field, and whether it resolves ----------------------
    print("=" * 74)
    print(f"{'field':<16}{'resolved from':<26}{'filled':<12}example")
    print("=" * 74)
    problems = []
    for logical, candidates in sorted(ax.FIELDS.items()):
        hit, n, sample = None, 0, ""
        for cand in candidates:
            if cand not in cols:
                continue
            vals = [r.get(cand) for r in latest]
            good = [v for v in vals if v not in (None, "", "-", "null", "None")]
            if good:
                hit, n, sample = cand, len(good), str(good[0])[:26]
                break
        if hit:
            print(f"{logical:<16}{hit:<26}{n}/{len(latest):<8}{sample}")
        else:
            present = [c for c in candidates if c in cols]
            why = (f"present but all empty: {present}" if present
                   else f"no column named any of {candidates}")
            print(f"{logical:<16}{'NOT FOUND':<26}{'0':<12}{why}")
            problems.append((logical, candidates, present))

    # ---- columns that look like they might be the missing ones -------------
    if problems:
        print("\n" + "=" * 74)
        print("COLUMNS IN YOUR FILE THAT MIGHT BE WHAT'S MISSING")
        print("=" * 74)
        for logical, candidates, _ in problems:
            key = logical.split("_")[0][:4]
            near = [c for c in cols if key.lower() in c.lower()]
            print(f"\n{logical}: looked for {candidates}")
            print(f"  columns containing '{key}': {near or 'none'}")
            if logical == "maturity":
                dateish = [c for c in cols if "date" in c.lower()]
                print(f"  all date-like columns: {dateish}")

    # ---- date parsing, the other common failure ---------------------------
    mat_col = next((c for c in ax.FIELDS["maturity"] if c in cols), None)
    if mat_col:
        print("\n" + "=" * 74)
        print(f"DATE PARSING CHECK on '{mat_col}'")
        print("=" * 74)
        raw = [r.get(mat_col) for r in latest]
        empty = sum(1 for v in raw if v in (None, "", "-", "null", "None"))
        parsed = sum(1 for v in raw if an.parse_date(v) is not None)
        print(f"  empty or dash : {empty}/{len(latest)}")
        print(f"  parsed as date: {parsed}/{len(latest)}")
        bad = [v for v in raw if v not in (None, "", "-", "null", "None")
               and an.parse_date(v) is None]
        if bad:
            print(f"  UNPARSEABLE   : {len(bad)}, e.g. {bad[:5]}")
            print("  -> the format is not one analytics.parse_date handles; send me these")

    # ---- what normalise actually produces ---------------------------------
    print("\n" + "=" * 74)
    print("RESULT AFTER NORMALISATION")
    print("=" * 74)
    today = an.parse_date(dates[-1]) if dates else None
    out = [ax.normalise(r, today) for r in latest]
    notes = {}
    for b in out:
        notes[b["note"] or "computed a yield"] = notes.get(b["note"] or "computed a yield", 0) + 1
    for k, v in sorted(notes.items(), key=lambda x: -x[1]):
        print(f"  {v:>5}  {k}")

    ccy = {}
    for b in out:
        ccy[b["ccy"]] = ccy.get(b["ccy"], 0) + 1
    print("\n  by currency: " + ", ".join(f"{k} {v}" for k, v in sorted(ccy.items())))

    ok = [b for b in out if b["ytm_bid"] is not None]
    if ok:
        ys = sorted(b["ytm_bid"] for b in ok)
        print(f"\n  bid yields computed: {len(ok)}")
        print(f"  range {ys[0]:.2f}% to {ys[-1]:.2f}%, median {ys[len(ys)//2]:.2f}%")
        s = ok[0]
        print(f"\n  sample: {s['isin']} {str(s['issuer'])[:28]}")
        print(f"    maturity {s['maturity']}  coupon {s['coupon']}%  freq {s['freq']}  "
              f"basis {s['basis']}  coupon dates {s['n_coupon_dates']}")
        print(f"    bid {s['bid']} ask {s['ask']} -> bid ytm {s['ytm_bid']}%  "
              f"AMX bid {s['amx_ytm_bid']}%")
    else:
        print("\n  NO yields computed. The note counts above say why.")

    # ---- one full raw row, so nothing is hidden ---------------------------
    print("\n" + "=" * 74)
    print("FIRST RAW ROW (every non-empty column)")
    print("=" * 74)
    if latest:
        for k in cols:
            v = latest[0].get(k)
            if v not in (None, "", "-"):
                print(f"  {k:<28} {str(v)[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
