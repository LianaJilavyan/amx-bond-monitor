"""
Bond analytics. Pure functions, no dependencies, deterministic.

Everything here is testable and tested — run `python3 analytics.py` to verify.
Nothing in this module guesses. If an input is missing it returns None rather
than substituting a default, because a plausible-looking wrong yield is worse
than a blank cell.
"""

from datetime import date, datetime
from typing import Optional, List, Tuple

DAY_COUNTS = {"ACT/365": 365.0, "ACT/360": 360.0, "30/360": 360.0, "ACT/ACT": 365.25}


def parse_date(v) -> Optional[date]:
    """Accept the date formats an exchange feed plausibly emits."""
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()[:19]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def year_fraction(d1: date, d2: date, basis: str = "ACT/365") -> float:
    if basis == "30/360":
        dd1, dd2 = min(d1.day, 30), min(d2.day, 30)
        return ((d2.year - d1.year) * 360 + (d2.month - d1.month) * 30 + (dd2 - dd1)) / 360.0
    return (d2 - d1).days / DAY_COUNTS.get(basis, 365.0)


def _days_in_month(y: int, m: int) -> int:
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    return [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]


def step_back(d: date, months: int) -> date:
    """Move back whole months, clamping the day. Used by both the schedule and
    the accrual, so the two can never disagree about period boundaries."""
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(d.day, _days_in_month(y, m)))


def coupon_schedule(settle: date, maturity: date, freq: int) -> List[date]:
    """
    Remaining coupon dates, working backwards from maturity so the final
    period lands exactly on the redemption date. Only dates strictly after
    settlement are returned.
    """
    if freq <= 0 or maturity <= settle:
        return []
    step = 12 // freq
    dates, cursor = [], maturity
    while cursor > settle:
        dates.append(cursor)
        cursor = step_back(cursor, step)
    return sorted(dates)


def cashflows(settle: date, maturity: date, coupon_rate: float, freq: int,
              face: float = 100.0, basis: str = "ACT/365") -> List[Tuple[float, float]]:
    """[(years_from_settlement, amount)]. Zero-coupon handled as freq=0."""
    if maturity <= settle:
        return []
    if freq <= 0 or coupon_rate == 0:
        return [(year_fraction(settle, maturity, basis), face)]
    dates = coupon_schedule(settle, maturity, freq)
    per = face * coupon_rate / freq
    flows = [(year_fraction(settle, d, basis), per) for d in dates]
    flows[-1] = (flows[-1][0], flows[-1][1] + face)
    return flows


def accrued_interest(settle: date, maturity: date, coupon_rate: float, freq: int,
                     face: float = 100.0, basis: str = "ACT/365") -> float:
    if freq <= 0 or coupon_rate == 0:
        return 0.0
    dates = coupon_schedule(settle, maturity, freq)
    if not dates:
        return 0.0
    nxt = dates[0]
    prev = step_back(nxt, 12 // freq)
    total = year_fraction(prev, nxt, basis)
    if total <= 0:
        return 0.0
    elapsed = year_fraction(prev, settle, basis)
    return max(0.0, min(1.0, elapsed / total)) * face * coupon_rate / freq


def price_from_yield(flows: List[Tuple[float, float]], y: float) -> float:
    return sum(cf / (1.0 + y) ** t for t, cf in flows)


def solve_ytm(dirty_price: float, flows: List[Tuple[float, float]],
              lo: float = -0.95, hi: float = 5.0) -> Optional[float]:
    """
    Bisection. Slower than Newton-Raphson but cannot diverge, which matters
    more than speed on 200 bonds. Returns annual effective yield, or None if
    no root exists in the bracket.
    """
    if not flows or dirty_price <= 0:
        return None
    f_lo = price_from_yield(flows, lo) - dirty_price
    f_hi = price_from_yield(flows, hi) - dirty_price
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = price_from_yield(flows, mid) - dirty_price
        if abs(f_mid) < 1e-10:
            return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def durations(flows: List[Tuple[float, float]], y: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(Macaulay, modified, convexity). Annual compounding."""
    p = price_from_yield(flows, y)
    if p <= 0:
        return None, None, None
    mac = sum(t * cf / (1.0 + y) ** t for t, cf in flows) / p
    mod = mac / (1.0 + y)
    conv = sum(t * (t + 1) * cf / (1.0 + y) ** (t + 2) for t, cf in flows) / p
    return mac, mod, conv


def analyse(clean_price: Optional[float], settle: date, maturity: Optional[date],
            coupon_rate: Optional[float], freq: int = 2, face: float = 100.0,
            basis: str = "ACT/365", price_is_dirty: bool = False) -> dict:
    """
    Full analytic set for one bond. Every failure mode returns a `note`
    explaining what could not be computed and why.
    """
    out = {"ytm": None, "current_yield": None, "accrued": None, "dirty_price": None,
           "macaulay": None, "modified": None, "convexity": None,
           "years_to_maturity": None, "note": None}

    if maturity is None:
        out["note"] = "no maturity date"
        return out
    if maturity <= settle:
        out["note"] = "matured"
        return out

    out["years_to_maturity"] = round(year_fraction(settle, maturity, basis), 4)

    if clean_price is None or clean_price <= 0:
        out["note"] = "no price"
        return out
    if coupon_rate is None:
        out["note"] = "no coupon rate"
        return out

    ai = 0.0 if price_is_dirty else accrued_interest(settle, maturity, coupon_rate, freq, face, basis)
    dirty = clean_price if price_is_dirty else clean_price + ai
    out["accrued"] = round(ai, 6)
    out["dirty_price"] = round(dirty, 6)

    if coupon_rate > 0:
        out["current_yield"] = round(face * coupon_rate / clean_price, 6)

    flows = cashflows(settle, maturity, coupon_rate, freq, face, basis)
    y = solve_ytm(dirty, flows)
    if y is None:
        out["note"] = "yield did not converge — check price and cashflows"
        return out

    out["ytm"] = round(y, 6)
    mac, mod, conv = durations(flows, y)
    out["macaulay"] = round(mac, 4) if mac is not None else None
    out["modified"] = round(mod, 4) if mod is not None else None
    out["convexity"] = round(conv, 4) if conv is not None else None
    return out


# --- explicit schedules -----------------------------------------------------
# AMX supplies the real coupon dates per instrument. When we have them we use
# them, because inferring the schedule by stepping back from maturity is only
# ever an approximation of the actual terms.

def cashflows_from_dates(settle: date, maturity: date, coupon_dates: List[date],
                         coupon_rate: float, freq: int, face: float = 100.0,
                         basis: str = "ACT/365") -> List[Tuple[float, float]]:
    future = sorted(d for d in coupon_dates if d > settle)
    if not future:
        return [(year_fraction(settle, maturity, basis), face)] if maturity > settle else []
    per = face * coupon_rate / freq if freq else 0.0
    flows = [(year_fraction(settle, d, basis), per) for d in future]
    # redemption rides on the final coupon
    flows[-1] = (flows[-1][0], flows[-1][1] + face)
    return flows


def accrued_from_dates(settle: date, coupon_dates: List[date], coupon_rate: float,
                       freq: int, face: float = 100.0, basis: str = "ACT/365") -> float:
    if not freq or coupon_rate == 0:
        return 0.0
    past = sorted(d for d in coupon_dates if d <= settle)
    future = sorted(d for d in coupon_dates if d > settle)
    if not future:
        return 0.0
    nxt = future[0]
    prev = past[-1] if past else step_back(nxt, 12 // freq)
    total = year_fraction(prev, nxt, basis)
    if total <= 0:
        return 0.0
    frac = max(0.0, min(1.0, year_fraction(prev, settle, basis) / total))
    return frac * face * coupon_rate / freq


def analyse_full(clean_price, settle, maturity, coupon_rate, freq=2, face=100.0,
                 basis="ACT/365", coupon_dates=None, price_is_dirty=False) -> dict:
    """analyse(), but using the instrument's real coupon dates when supplied."""
    if not coupon_dates:
        return analyse(clean_price, settle, maturity, coupon_rate, freq, face,
                       basis, price_is_dirty)

    out = {"ytm": None, "current_yield": None, "accrued": None, "dirty_price": None,
           "macaulay": None, "modified": None, "convexity": None,
           "years_to_maturity": None, "note": None}
    if maturity is None or maturity <= settle:
        out["note"] = "matured" if maturity else "no maturity date"
        return out
    out["years_to_maturity"] = round(year_fraction(settle, maturity, basis), 4)
    if clean_price is None or clean_price <= 0:
        out["note"] = "no price"
        return out
    if coupon_rate is None:
        out["note"] = "no coupon rate"
        return out

    ai = 0.0 if price_is_dirty else accrued_from_dates(
        settle, coupon_dates, coupon_rate, freq, face, basis)
    dirty = clean_price if price_is_dirty else clean_price + ai
    out["accrued"] = round(ai, 6)
    out["dirty_price"] = round(dirty, 6)
    if coupon_rate > 0:
        out["current_yield"] = round(face * coupon_rate / clean_price, 6)

    flows = cashflows_from_dates(settle, maturity, coupon_dates, coupon_rate, freq, face, basis)
    y = solve_ytm(dirty, flows)
    if y is None:
        out["note"] = "yield did not converge"
        return out
    out["ytm"] = round(y, 6)
    mac, mod, conv = durations(flows, y)
    out["macaulay"] = round(mac, 4) if mac else None
    out["modified"] = round(mod, 4) if mod else None
    out["convexity"] = round(conv, 4) if conv else None
    return out


FREQ_WORDS = {
    "annual": 1, "annually": 1, "yearly": 1,
    "semi-annual": 2, "semi annual": 2, "semiannual": 2, "half-yearly": 2,
    "quarterly": 4, "monthly": 12, "bi-monthly": 6, "zero": 0, "zero coupon": 0,
}


def freq_from_text(v, default: int = 2) -> int:
    """AMX gives frequency as a word ('Quarterly'), not a number."""
    if v is None:
        return default
    if isinstance(v, (int, float)) and 0 < v <= 12:
        return int(v)
    s = str(v).strip().lower()
    if s in FREQ_WORDS:
        return FREQ_WORDS[s]
    for k, n in FREQ_WORDS.items():
        if k in s:
            return n
    try:
        n = int(float(s))
        if 0 < n <= 12:
            return n
    except (TypeError, ValueError):
        pass
    return default


# ----------------------------------------------------------------------------
# Tests. These are the contract — if they fail, the numbers cannot be trusted.
# ----------------------------------------------------------------------------

def _test():
    ok = 0
    fail = []

    def check(name, got, want, tol=1e-4):
        nonlocal ok
        if got is None:
            fail.append(f"{name}: got None, want {want}")
        elif abs(got - want) <= tol:
            ok += 1
        else:
            fail.append(f"{name}: got {got:.6f}, want {want:.6f}")

    s = date(2026, 1, 1)

    # A bond priced at par yields its coupon, whatever the frequency.
    for freq in (1, 2, 4):
        flows = cashflows(s, date(2036, 1, 1), 0.05, freq)
        y = solve_ytm(100.0, flows)
        # Annual effective equivalent of a 5% nominal paid `freq` times.
        want = (1 + 0.05 / freq) ** freq - 1
        check(f"par bond freq={freq}", y, want)

    # Zero-coupon: price 50, 10y -> (100/50)^(1/10) - 1
    flows = cashflows(s, date(2036, 1, 1), 0.0, 0)
    check("zero coupon ytm", solve_ytm(50.0, flows), 2 ** 0.1 - 1)

    # Macaulay duration of a zero equals its maturity.
    y0 = solve_ytm(50.0, flows)
    mac, mod, _ = durations(flows, y0)
    check("zero duration = maturity", mac, year_fraction(s, date(2036, 1, 1)), tol=0.01)
    check("modified = mac/(1+y)", mod, mac / (1 + y0))

    # Discount price implies yield above coupon; premium implies below.
    flows = cashflows(s, date(2031, 1, 1), 0.06, 2)
    y_disc = solve_ytm(95.0, flows)
    y_prem = solve_ytm(105.0, flows)
    y_par = solve_ytm(100.0, flows)
    if not (y_disc > y_par > y_prem):
        fail.append("price/yield inverse relationship violated")
    else:
        ok += 1

    # Round trip: price -> yield -> price.
    check("round trip", price_from_yield(flows, y_disc), 95.0, tol=1e-6)

    # Accrued interest is zero at issue-aligned settlement, and rises within a period.
    ai_mid = accrued_interest(date(2026, 4, 1), date(2031, 1, 1), 0.06, 2)
    if not (0 < ai_mid < 3.0):
        fail.append(f"accrued interest out of range: {ai_mid}")
    else:
        ok += 1

    # Settlement landing exactly on a coupon boundary must accrue nothing, and
    # the schedule's first date must be exactly one period ahead. This is the
    # case that breaks when the accrual and the schedule disagree about dates.
    for freq in (1, 2, 4):
        mat = date(2032, 3, 31)
        boundary = mat
        for _ in range(4):
            boundary = step_back(boundary, 12 // freq)
        ai = accrued_interest(boundary, mat, 0.08, freq)
        if abs(ai) > 1e-9:
            fail.append(f"accrual at boundary freq={freq}: {ai} (want 0)")
        else:
            ok += 1
        sched = coupon_schedule(boundary, mat, freq)
        if sched[0] != step_back(sched[1], 12 // freq) if len(sched) > 1 else False:
            fail.append(f"schedule spacing inconsistent freq={freq}")
        else:
            ok += 1
        # priced at par on a boundary -> yield equals the effective coupon
        r = analyse(100.0, boundary, mat, 0.08, freq)
        check(f"par on boundary freq={freq}", r["ytm"], (1 + 0.08 / freq) ** freq - 1, tol=1e-3)

    # Month-end maturities must not drift (31 Mar stepping back must clamp, not roll).
    if step_back(date(2032, 3, 31), 1) != date(2032, 2, 29):
        fail.append("step_back month-end clamp wrong")
    else:
        ok += 1

    # Schedule ends exactly on maturity and is strictly increasing.
    sched = coupon_schedule(s, date(2031, 7, 15), 2)
    if sched[-1] != date(2031, 7, 15) or sched != sorted(set(sched)):
        fail.append("coupon schedule malformed")
    else:
        ok += 1

    # Missing inputs degrade to None with a note, never to a fabricated number.
    r = analyse(None, s, date(2031, 1, 1), 0.05)
    if r["ytm"] is not None or r["note"] != "no price":
        fail.append("missing price should yield None + note")
    else:
        ok += 1
    r = analyse(100.0, s, None, 0.05)
    if r["ytm"] is not None or r["note"] != "no maturity date":
        fail.append("missing maturity should yield None + note")
    else:
        ok += 1

    # Convexity is positive for an ordinary bullet.
    _, _, conv = durations(flows, y_par)
    if conv is None or conv <= 0:
        fail.append("convexity should be positive")
    else:
        ok += 1

    # Frequency words, as AMX actually reports them.
    for text, want in [("Quarterly", 4), ("Semi-annual", 2), ("Annual", 1),
                       ("Monthly", 12), ("quarterly ", 4), (4, 4), ("4", 4)]:
        if freq_from_text(text) != want:
            fail.append(f"freq_from_text({text!r}) -> {freq_from_text(text)}, want {want}")
        else:
            ok += 1
    if freq_from_text(None) != 2 or freq_from_text("nonsense") != 2:
        fail.append("freq_from_text should fall back to 2")
    else:
        ok += 1

    # Explicit coupon dates must agree with the inferred schedule when the two
    # describe the same bond. If they diverge, one of them is wrong.
    mat = date(2031, 7, 15)
    inferred = coupon_schedule(s, mat, 2)
    r_inf = analyse(98.5, s, mat, 0.07, 2)
    r_exp = analyse_full(98.5, s, mat, 0.07, 2, coupon_dates=inferred)
    check("explicit dates match inferred (ytm)", r_exp["ytm"], r_inf["ytm"], tol=1e-9)
    check("explicit dates match inferred (accrued)", r_exp["accrued"], r_inf["accrued"], tol=1e-9)
    check("explicit dates match inferred (duration)", r_exp["modified"], r_inf["modified"], tol=1e-9)

    # A real AMX-shaped case: quarterly USD bond, ACT/ACT, par 100.
    cds = [date(2026, 10, 25), date(2027, 1, 25), date(2027, 4, 25), date(2027, 7, 25)]
    r = analyse_full(99.9433, date(2026, 7, 31), date(2027, 7, 25), 0.0525, 4,
                     basis="ACT/ACT", coupon_dates=cds)
    if r["ytm"] is None or not (0.03 < r["ytm"] < 0.09):
        fail.append(f"AMX-shaped quarterly bond gave implausible ytm: {r['ytm']}")
    else:
        ok += 1
    if r["accrued"] is None or not (0 <= r["accrued"] < 1.5):
        fail.append(f"AMX-shaped accrued out of range: {r['accrued']}")
    else:
        ok += 1

    print(f"{ok} passed, {len(fail)} failed")
    for f in fail:
        print("  FAIL", f)
    return not fail


if __name__ == "__main__":
    raise SystemExit(0 if _test() else 1)
