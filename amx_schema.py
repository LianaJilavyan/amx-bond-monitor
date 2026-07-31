"""
amx_schema.py — turns the real AMX response into something analysable.

Written against the live schema. The three things that matter:

1. `price` and `yield` on the market board are nested objects; they get flattened
   to price_* and yield_* columns.

2. `close_price` exists only on days a bond actually traded — around one day in
   ten. `best_bid_price` and `best_ask_price` exist nearly every day. So the
   daily mark is the mid of the two quotes, and close price is treated as what
   it is: the last real print, with a date attached.

3. AMX publishes its own yields. We keep them alongside ours and report the
   difference rather than silently preferring either.
"""

from datetime import date
from typing import Optional, List

import analytics as an

# Where the daily mark comes from, in order of preference.
# Live board nests price/yield as objects with these nine keys:
#   change, change_percent, bid, ask, avg, open, close, high, low
# Flattening prefixes them, so price.bid -> price_bid. On a typical day only
# bid and ask carry values; everything else is "-". The per-ISIN history uses
# different names (close_price, best_bid_price), so both are listed.
PRICE_CLOSE = ["price_close", "close_price", "price_last", "last_price"]
PRICE_BID = ["price_bid", "best_bid_price", "price_best_bid", "bid_price"]
PRICE_ASK = ["price_ask", "best_ask_price", "price_best_ask", "ask_price"]
PRICE_AVG = ["price_avg", "avg_price", "price_average"]
PRICE_OPEN = ["price_open", "open_price"]

YIELD_CLOSE = ["yield_close", "close_yield", "yield_last"]
YIELD_BID = ["yield_bid", "best_bid_yield", "yield_best_bid"]
YIELD_ASK = ["yield_ask", "best_ask_yield", "yield_best_ask"]

FIELDS = {
    "isin": ["isin"],
    "ticker": ["ticker"],
    "currency": ["cur", "currency", "i_currency"],
    "issuer": ["short_name_en", "i_issuer_name_en", "issuer_name_en",
               "short_name", "i_issuer_name"],
    "maturity": ["maturity_date", "i_maturity_date"],
    "issue_date": ["i_issue_date", "issue_date"],
    "coupon": ["i_cpn_rate", "cpn_rate", "coupon"],
    "coupon_freq_text": ["i_cpn_frequency_en", "cpn_frequency_en", "i_cpn_frequency"],
    "coupon_dates": ["i_coupon_date", "coupon_date"],
    "day_count": ["i_day_count", "day_count"],
    "par": ["i_per_value", "per_value"],
    "bond_class": ["i_isin_class", "isin_class"],
    "instrument_type": ["i_instrument_type_en", "instrument_type_en"],
    "listing": ["list", "i_list_class", "list_class"],
    "status": ["i_isin_status_en", "isin_status_en"],
    "outstanding_amd": ["i_outst_volume_amd", "outst_volume_amd"],
    "outstanding": ["i_outst_volume", "outst_volume"],
    "last_trade_date": ["last_date", "i_last_date"],
    "volume": ["vol", "trades_volume", "volume"],
    "trades_count": ["trades_number", "trades_instr_qty"],
    "inflation_linked": ["i_inflation_linked", "inflation_linked"],
    "benchmark": ["i_benchmark", "benchmark"],
}


def flatten(obj, prefix="", out=None):
    """Nested dicts become prefixed columns; lists are kept as JSON."""
    import json
    out = {} if out is None else out
    for k, v in (obj or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            flatten(v, f"{key}_", out)
        elif isinstance(v, list):
            out[key] = json.dumps(v, ensure_ascii=False)
        else:
            out[key] = v
    return out


def pick(row, names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", "-", "null", "None"):
            return v
    return None


def num(v):
    if v in (None, "", "-", "null", "None"):
        return None
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and v != v else float(v)
    try:
        return float(str(v).replace(",", "").replace("%", "").replace("\u00a0", "").strip())
    except (TypeError, ValueError):
        return None


# Armenian issuers carry their business and legal form in the name. Credit risk
# differs sharply across them — a regulated deposit-funded bank is not the same
# proposition as a universal credit organisation — so business type is resolved
# before legal form, and a bank that happens to be an OJSC classifies as a bank.
ISSUER_CLASSES = [
    ("Bank", ("BANK", "ԲԱՆԿ")),
    ("UCO", ("UCO", "U.C.O", "UNIVERSAL CREDIT", "CREDIT ORGANIZATION",
             "CREDIT ORGANISATION", "ՈՒՎԿ")),
    ("Insurance", ("INSURANCE", "ԱՊԱՀՈՎԱԳՐ")),
    ("Leasing", ("LEASING", "ԼԻԶԻՆԳ")),
    ("LLC", ("LLC", "L.L.C", "ՍՊԸ")),
    ("CJSC", ("CJSC", "C.J.S.C", "ՓԲԸ")),
    ("OJSC", ("OJSC", "O.J.S.C", "ԲԲԸ")),
]


def issuer_class(name: Optional[str]) -> str:
    """Bank / UCO / Insurance / Leasing, else the legal form, else Other."""
    if not name:
        return "Other"
    up = str(name).upper()
    for label, needles in ISSUER_CLASSES:
        if any(n in up for n in needles):
            return label
    return "Other"


def amd_equivalent(row) -> Optional[float]:
    """Each row carries a `rates` list holding the same quote expressed in
    another currency. Note this is NOT the FX table — that arrives as a separate
    item keyed `rate` (singular) and is handled in fetch.py."""
    import json
    raw = row.get("rates")
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    for item in raw or []:
        if not isinstance(item, dict) or item.get("currency") != "AMD":
            continue
        bid, ask = num(item.get("best_bid_price")), num(item.get("best_ask_price"))
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return num(item.get("close_price")) or bid or ask
    return None


def coupon_dates_of(row) -> List[date]:
    """`coupon_date` arrives as a JSON list; entries may be strings or objects."""
    import json
    raw = pick(row, FIELDS["coupon_dates"])
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            item = (item.get("date") or item.get("coupon_date")
                    or item.get("payment_date") or next(iter(item.values()), None))
        d = an.parse_date(item)
        if d:
            out.append(d)
    return sorted(set(out))


def normalise(row: dict, settle: Optional[date] = None) -> dict:
    """One AMX row -> one analysed bond."""
    settle = settle or date.today()

    ccy = pick(row, FIELDS["currency"]) or "AMD"

    # Quoted prices are a PERCENTAGE of par, not an amount: a bond with a
    # 100,000 AMD face still quotes around 99.95. Yield is independent of
    # notional, so the cash flows are always built on a base of 100. Using the
    # instrument's real per_value here silently breaks every bond that isn't
    # par-100 — the solver is handed a price of 99.95 against a 100,000
    # redemption and cannot converge.
    PRICE_BASE = 100.0
    par = num(pick(row, FIELDS["par"])) or 100.0
    maturity = an.parse_date(pick(row, FIELDS["maturity"]))

    coupon = num(pick(row, FIELDS["coupon"]))
    if coupon is not None and coupon > 1:
        coupon /= 100.0          # AMX quotes 5.25, the maths wants 0.0525
    freq = an.freq_from_text(pick(row, FIELDS["coupon_freq_text"]))
    basis = str(pick(row, FIELDS["day_count"]) or "ACT/365").upper().strip()
    if basis not in an.DAY_COUNTS:
        basis = "ACT/365"
    cdates = coupon_dates_of(row)

    close = num(pick(row, PRICE_CLOSE))
    bid = num(pick(row, PRICE_BID))
    ask = num(pick(row, PRICE_ASK))
    avg = num(pick(row, PRICE_AVG))

    # The daily mark: mid of the two-sided quote where one exists, since close
    # price only appears on days the bond actually traded.
    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
    mark = mid if mid is not None else (close if close is not None else (avg or bid or ask))
    mark_source = ("mid quote" if mid is not None else
                   "last trade" if close is not None else
                   "average" if avg is not None else
                   "one-sided quote" if (bid or ask) else None)

    spread_bp = None
    if bid and ask and mid:
        spread_bp = round((ask - bid) / mid * 10000, 1)

    def _yield_at(px):
        if px is None:
            return None, None
        rr = an.analyse_full(px, settle, maturity, coupon, freq, PRICE_BASE, basis, cdates)
        if rr["ytm"] is None:
            return None, rr
        nominal = ((1 + rr["ytm"]) ** (1.0 / freq) - 1) * freq if freq else rr["ytm"]
        return round(nominal * 100, 4), rr

    # Bid and ask are not interchangeable: you sell into the bid and buy at the
    # ask, so the ask yield is what a buyer actually earns and the bid yield
    # flatters it. Both are computed; the dashboard chooses which to show.
    ytm_bid, _ = _yield_at(bid)
    ytm_ask, _ = _yield_at(ask)
    ytm_mid, a_mid = _yield_at(mid)
    ytm_last, _ = _yield_at(close)

    a = a_mid if a_mid else an.analyse_full(mark, settle, maturity, coupon, freq,
                                            PRICE_BASE, basis, cdates)

    # AMX publishes nominal yields compounded at the coupon frequency, not
    # annual-effective. Report nominal as the headline so the numbers reconcile
    # with the exchange, and keep effective alongside for comparing across
    # instruments with different coupon frequencies.
    ytm_nom = None
    if a["ytm"] is not None and freq:
        ytm_nom = ((1 + a["ytm"]) ** (1.0 / freq) - 1) * freq

    amx_bid = num(pick(row, YIELD_BID))
    amx_ask = num(pick(row, YIELD_ASK))
    amx_yield = num(pick(row, YIELD_CLOSE)) or amx_bid
    diff_bp = None
    if ytm_nom is not None and amx_yield is not None:
        diff_bp = round((ytm_nom * 100 - amx_yield) * 100, 1)

    ltd = an.parse_date(pick(row, FIELDS["last_trade_date"]))
    days_since_trade = (settle - ltd).days if ltd else None

    return {
        "isin": pick(row, FIELDS["isin"]),
        "ticker": pick(row, FIELDS["ticker"]),
        "issuer": pick(row, FIELDS["issuer"]) or "—",
        "issuer_class": issuer_class(pick(row, FIELDS["issuer"])),
        "ccy": ccy,
        "type": (pick(row, FIELDS["bond_class"])
                 or pick(row, FIELDS["instrument_type"]) or "Unclassified"),
        "listing": pick(row, FIELDS["listing"]),
        "status": pick(row, FIELDS["status"]),
        "price": mark,
        "price_source": mark_source,
        "close": close,
        "bid": bid,
        "ask": ask,
        "spread_bp": spread_bp,
        "coupon": round(coupon * 100, 4) if coupon is not None else None,
        "freq": freq,
        "basis": basis,
        "n_coupon_dates": len(cdates),
        "maturity": maturity.isoformat() if maturity else None,
        "yrs": a["years_to_maturity"],
        "ytm": round(ytm_nom * 100, 4) if ytm_nom is not None else None,
        "ytm_effective": round(a["ytm"] * 100, 4) if a["ytm"] is not None else None,
        "ytm_bid": ytm_bid,
        "ytm_ask": ytm_ask,
        "ytm_mid": ytm_mid,
        "ytm_last": ytm_last,
        "amx_ytm_bid": amx_bid,
        "amx_ytm_ask": amx_ask,
        "amx_ytm": amx_yield,
        "ytm_diff_bp": diff_bp,
        "mod": a["modified"],
        "conv": a["convexity"],
        "accrued": a["accrued"],
        "cy": round(a["current_yield"] * 100, 4) if a["current_yield"] else None,
        "par": par,
        "amd_price": amd_equivalent(row),
        "outstanding_amd": num(pick(row, FIELDS["outstanding_amd"])),
        "volume": num(pick(row, FIELDS["volume"])),
        "trades": num(pick(row, FIELDS["trades_count"])),
        "last_trade": ltd.isoformat() if ltd else None,
        "days_since_trade": days_since_trade,
        "note": a["note"],
    }


def _selftest():
    """Exercises the real shapes seen in the AMX response."""
    import json
    ok, fail = 0, []
    settle = date(2026, 7, 31)

    row = {
        "isin": "AMPROMB2CER0", "cur": "USD", "ticker": "PROMBC",
        "short_name_en": "EVOCABANK OJSC", "maturity_date": "2027-07-25",
        "list": "BBOND", "last_date": "2026-07-30", "vol": 0,
        "price_best_bid": 99.9433, "price_best_ask": 100.4149,
        "yield_best_bid": 5.2800, "yield_best_ask": 5.0287,
        "i_cpn_rate": "5.250000", "i_cpn_frequency_en": "Quarterly",
        "i_day_count": "ACT/ACT", "i_per_value": "100.000000",
        "i_isin_class": "Coupon bond", "i_issuer_name_en": "EVOCABANK OJSC",
        "i_coupon_date": json.dumps(["2026-10-25", "2027-01-25", "2027-04-25", "2027-07-25"]),
        "i_outst_volume_amd": "5499450000",
    }
    b = normalise(row, settle)

    def want(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            fail.append(name)

    want("mid used as mark", abs(b["price"] - 100.1791) < 1e-3)
    want("mark source labelled", b["price_source"] == "mid quote")
    want("spread computed", b["spread_bp"] and 40 < b["spread_bp"] < 50)
    want("coupon scaled to percent", abs(b["coupon"] - 5.25) < 1e-6)
    want("quarterly parsed", b["freq"] == 4)
    want("ACT/ACT honoured", b["basis"] == "ACT/ACT")
    want("real coupon dates used", b["n_coupon_dates"] == 4)
    want("ytm plausible", b["ytm"] and 3 < b["ytm"] < 9)
    want("nominal below effective", b["ytm"] < b["ytm_effective"])
    want("amx yield captured", b["amx_ytm"] == 5.28)
    want("difference reported", b["ytm_diff_bp"] is not None)
    # Bid price is lower than ask, so bid yield must exceed ask yield.
    want("bid yield above ask yield", b["ytm_bid"] > b["ytm_ask"])
    want("mid sits between the two", b["ytm_ask"] < b["ytm_mid"] < b["ytm_bid"])
    want("amx bid yield kept", b["amx_ytm_bid"] == 5.28)
    want("duration positive", b["mod"] and 0.5 < b["mod"] < 1.2)
    want("currency preserved", b["ccy"] == "USD")
    want("type from isin_class", b["type"] == "Coupon bond")
    want("days since trade", b["days_since_trade"] == 1)

    # Issuer classification, including the Armenian-script forms in the feed.
    for nm, cls in [
        ("EVOCABANK OJSC", "Bank"), ("ARDSHINBANK CJSC", "Bank"),
        ("ԷՎՈԿԱԲԱՆԿ ԲԲԸ", "Bank"),
        ("GLOBAL CREDIT UCO CJSC", "UCO"),
        ("FAST CREDIT CAPITAL UNIVERSAL CREDIT ORGANIZATION", "UCO"),
        ("SOME TRADING LLC", "LLC"), ("ALPHA CJSC", "CJSC"), ("BETA OJSC", "OJSC"),
        ("INGO ARMENIA INSURANCE CJSC", "Insurance"),
        ("", "Other"), (None, "Other"), ("MYSTERY HOLDINGS", "Other"),
    ]:
        want(f"issuer_class({nm!r}) == {cls}", issuer_class(nm) == cls)

    # A bond that has never traded must still normalise, with a note not a crash.
    quiet = dict(row)
    for k in ("price_best_bid", "price_best_ask"):
        quiet.pop(k)
    q = normalise(quiet, settle)
    want("no price -> note", q["ytm"] is None and q["note"] == "no price")

    # Missing maturity (5 of 208 rows) must not blow up.
    nomat = dict(row)
    nomat.pop("maturity_date")
    n = normalise(nomat, settle)
    want("no maturity -> note", n["ytm"] is None and n["note"] == "no maturity date")

    # Live regression: EVOCABANK PROMBC on 2026-07-30. AMX published 5.3024 at
    # the bid and 5.0499 at the ask. Our engine must stay within 2bp of both.
    cds = []
    d = date(2024, 10, 25)
    while d <= date(2027, 7, 25):
        cds.append(d)
        mth = d.month + 3
        d = date(d.year + (mth - 1) // 12, (mth - 1) % 12 + 1, 25)
    for side, px, amx in [("bid", 99.9495, 5.3024), ("ask", 100.1909, 5.0499)]:
        rr = an.analyse_full(px, date(2026, 7, 30), date(2027, 7, 25),
                             0.0525, 4, 100.0, "ACT/ACT", cds)
        nom = ((1 + rr["ytm"]) ** 0.25 - 1) * 4 * 100
        want(f"live {side} within 2bp of AMX", abs(nom - amx) < 0.02)

    # Nested board shape: everything "-" except a two-sided quote.
    live = {"isin": "AMPROMB2CER0", "cur": "USD", "maturity_date": "2027-07-25",
            "price_change": "-", "price_bid": "99.9495", "price_ask": "100.1909",
            "price_avg": "-", "price_open": "-", "price_close": "-",
            "price_high": "-", "price_low": "-",
            "yield_bid": "5.3024", "yield_ask": "5.0499", "yield_close": "-",
            "i_cpn_rate": "5.250000", "i_cpn_frequency_en": "Quarterly",
            "i_day_count": "ACT/ACT", "i_per_value": "100.000000",
            "rates": json.dumps([{"currency": "AMD", "best_bid_price": "36602.000000",
                                  "best_ask_price": "36690.000000"}])}
    lb = normalise(live, date(2026, 7, 30))
    want("dash strings ignored", lb["close"] is None and lb["price"] is not None)
    want("mid from live quote", abs(lb["price"] - 100.0702) < 1e-3)
    want("amx yield read from bid", lb["amx_ytm"] == 5.3024)
    want("amd equivalent extracted", lb["amd_price"] and abs(lb["amd_price"] - 36646) < 1)

    # Regression: a quoted price is a percentage of par, so the same quote must
    # produce the same yield whatever the instrument's face value. Getting this
    # wrong made 113 of 208 real bonds fail to converge.
    base = None
    for pv in ("1.000000", "100.000000", "1000.000000", "10000.000000", "100000.000000"):
        v = dict(live); v["i_per_value"] = pv
        y = normalise(v, date(2026, 7, 30))["ytm_bid"]
        if base is None:
            base = y
        want(f"par {pv.split('.')[0]} yields the same", y is not None and abs(y - base) < 1e-9)
    want("par is still reported", normalise(live, date(2026, 7, 30))["par"] == 100.0)

    print(f"schema: {ok} passed, {len(fail)} failed")
    for f in fail:
        print("  FAIL", f)
    if not fail:
        print(f"  sample -> mark {b['price']:.4f} ({b['price_source']}), "
              f"our YTM {b['ytm']}% nominal ({b['ytm_effective']}% eff), "
              f"AMX {b['amx_ytm']}%, diff {b['ytm_diff_bp']}bp, "
              f"spread {b['spread_bp']}bp, mod dur {b['mod']}")
    return not fail


if __name__ == "__main__":
    raise SystemExit(0 if _selftest() else 1)
