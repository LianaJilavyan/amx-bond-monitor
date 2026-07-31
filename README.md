# AMX Corporate Bond Monitor

Pulls the AMX corporate bond board every weekday, keeps every snapshot forever, and
publishes a dashboard your colleagues open with a link. No logins, no daily upload,
no one's laptop involved.

```
fetch.py  ──►  data/history.csv  ──►  build_dashboard.py  ──►  docs/index.html
   │                  │                                              │
 AMX API      git history = your          analytics.py         GitHub Pages
              audit trail, forever         (23 tests)          (a URL to share)
```

## Setup, about fifteen minutes

**1. Create the repo.** Push these files to a new GitHub repository.

**2. Find out what the API actually returns.**

```bash
python3 fetch.py discover
```

This prints the real field names and saves the raw JSON under `data/raw/`. Do this
before anything else — the endpoints are undocumented and nothing here assumes it
knows the schema.

If it returns 403, the API is checking browser headers. Open
`https://amx.am/en/market_data/corporate_bonds` with DevTools on the Network tab,
find the `getMarketData` request, and copy its headers into `HEADERS` in `fetch.py`.

**3. Map the fields.** Copy `config.example.json` to `config.json` and set each
logical name to the actual key from step 2. Unmapped fields appear on the dashboard
as an explicit gap notice — never as a guessed number.

**4. Backfill the history.** Every instrument has a full price history behind
`getInstrument` — the same data as the "historical" tab on each ISIN's page at
amx.am. This pulls all of it and merges it straight into `data/history.csv`:

```bash
python3 fetch.py backfill
```

Roughly 208 instruments with about 250 days each, so expect tens of thousands of
rows and a few minutes. Safe to re-run: rows are keyed on ISIN plus date and
existing rows always win, so nothing is ever overwritten or duplicated.

**5. Turn on the automation.** Repo Settings → Pages → Source: *GitHub Actions*.
Then Actions → *Daily AMX snapshot* → *Run workflow* to test it immediately rather
than waiting for tomorrow. Your dashboard lands at
`https://<you>.github.io/<repo>/`.

**6. Share the link.** That's the whole collaboration story. Read-only, always
current, nothing to install.

## Daily commands

```bash
python3 fetch.py              # today's snapshot, appended to history
python3 fetch.py --gov        # government bonds, for the risk-free curve
python3 build_dashboard.py    # regenerate the page
python3 analytics.py          # run the test suite
```

## What the dashboard shows

Yield against maturity, with **filled dots for instruments that have actually moved
on price recently and hollow rings for stale ones**. Dot size is amount traded. The
table sorts on any column, filters by type, currency and maturity bucket, and hides
stale names entirely with one toggle. Click any bond for its yield history.

The headline number is deliberately not "average yield". It is how many of the
listed instruments actually printed a price. On a market with roughly 200 listings
and under 2,000 trades a month, most bonds are carrying an old price, and the
highest yields on any unfiltered screen are usually the least real. The design puts
that fact where you cannot miss it.

## Things to know before trading on these numbers

**The yields are annual-effective; AMX may publish nominal.** A 13.49% semi-annual
coupon is 13.95% effective. If your figure disagrees with the exchange's by roughly
this much, that is the reason, not an error.

**Accrued interest is inferred, not read.** Coupon dates are currently derived by
stepping back from maturity, because the feed's real coupon schedule has not been
mapped yet. Where `getInstrument` exposes a next-coupon-date field, map it in
`config.json` — this is the single biggest accuracy improvement available.

**Clean versus dirty.** `build_dashboard.py` assumes the close price is clean and
adds accrued. If AMX quotes dirty, flip `price_is_dirty` in the `analyse` call.

**Floating-rate and amortising bonds.** A single YTM is not meaningful for either.
They are computed anyway and should be treated as wrong until the type is mapped
and handled.

Analysis only, not investment advice.
