## ARR Interest Calculator — SONIA / SOFR (Compounded-in-Arrears, Business-Day Lookback)

This project computes loan interest using risk-free rates (SONIA or SOFR) with a compounded-in-arrears convention and business-day lookback (without observation shift). Margin and Credit Adjustment Spread (CAS) are applied as simple interest after compounding.

### Core concepts
- Accrual period: from `start_date` (inclusive) to `end_date` (exclusive). Let `dc = end_date - start_date` in calendar days.
- Business days: the set of dates present in the input rate series. Non-listed calendar days are treated as non-business days.
- Lookback: an integer `L >= 1` in business days. For each business day `b` in the accrual period, the observed rate is the rate on the business day that is `L` positions earlier than `b` in the business-day sequence.
- Day-count basis `N`: SONIA uses 365 (ACT/365F), SOFR uses 360 (ACT/360).
- Margin and CAS: annualized percentages applied as non-compounded add-ons after the compounded RFR component.
- Optional single margin step: at `margin_change_date` (inclusive), the per-annum margin changes to `margin_after` for the remaining days in the period.

### Inputs
- principal: notional amount (currency-agnostic).
- start_date, end_date: ISO dates; accrual is `[start_date, end_date)`.
- pricing_option: `SONIA` or `SOFR` (sets `N = 365` or `N = 360`).
- lookback: integer business days `L >= 1`.
- rates: time series of daily RFR values as either:
  - Array of `{ date: YYYY-MM-DD, rate: number }`, or
  - CSV (header optional): first column date, second column daily rate.
    - Rate may be percent (e.g., `5.12`) or decimal (`0.0512`). Values > 1 are treated as percent and divided by 100.
- margin: per-annum percentage; applied post-compounding as simple interest.
- cas: per-annum percentage; applied post-compounding as simple interest.
- Optional margin step:
  - `margin_change_date`: ISO date; if present and within the accrual period, the new margin applies from that date (inclusive).
  - `margin_after`: per-annum percentage used on/after `margin_change_date`.

### Data requirements and validations
- The rate series must contain sufficient history to support the lookback for the first business day in the accrual period:
  - Let `first_bd = previous_business_day(bdays, start_date)`.
  - The observed rate on that first business day reads from `shift_back_business_days(first_bd, L)`; this must exist.
- `end_date` must be after `start_date`.
- `lookback >= 1`.

### Compounded-in-arrears logic (business-day product)
Let `C` be the compounded factor for the RFR component, initialized to `1`. Iterate forward from `start_date` to `end_date` in blocks delimited by business days:

- For the current calendar date `d`:
  - If `d` is a business day, set `business_day = d` and `next_bd = next business day after d`.
  - Otherwise, set `business_day = previous business day on/before d` and `next_bd = next business day on/after d`.
  - Let `n = min(next_bd - d, end_date - d)` (calendar days) be the number of days this business day’s observed rate applies to.
  - Let `obs = shift_back_business_days(business_day, L)` and `r = rate[obs]` (decimal, e.g., 0.0512 for 5.12%).
  - Update the compounded factor using the business-day product step:

```
C <- C * (1 + (r * n) / N)
```

  - Advance `d += n`.

Notes:
- This is the standard “compounded in arrears” convention without observation shift; the observed rate for each business day is taken from a prior business day determined by the lookback.
- For SONIA, the implementation quantizes `C` to 18 decimal places after each step to match market precision.

### Interest components
Let `dc = (end_date - start_date)` and `DCF_total = dc / N`.
Handle the single margin step (if any):
- If `margin_change_date <= start_date`: all days use `margin_after`.
- If `margin_change_date >= end_date`: the change is ignored.
- Otherwise, split the period at `margin_change_date` (inclusive) into `pre_days` and `post_days` with day-count fractions `DCF_pre = pre_days / N` and `DCF_post = post_days / N`.

Compute:

```
I_RFR    = (C - 1) * principal
I_margin = (m1 * DCF_pre + m2 * DCF_post) * principal
I_CAS    = cas * DCF_total * principal
I_total  = I_RFR + I_margin + I_CAS
```

where `m1` is the initial margin (decimal) and `m2` is the post-change margin (or `m1` if no change applies). Margin and CAS are not compounded.

### Annualized rates for display
```
r_RFR_annualized   = (C - 1) * (N / dc)                # if dc > 0
m_weighted         = (m1 * DCF_pre + m2 * DCF_post) / DCF_total   # if DCF_total > 0
r_applicable       = r_RFR_annualized + m_weighted + cas
```

### Daily detail semantics (for auditability)
The implementation can emit a per-calendar-day stream with, for each day:
- `date`, `business_day` (the business day controlling this block), `observation_date`, `daily_rate` (decimal), `cumulative_factor` after the block’s compounding step, `is_business_day` flag.
- The display-layer “daily ARR interest” is computed as the change in `C` since the previous row times `principal`, and set to 0 on non-business days. This mirrors the GUI behavior, attributing the compounding to business days while still listing non-business calendar days.

### Pseudocode
```python
C = 1
N = 365 if SONIA else 360
bdays = sorted(rate_series.keys())
# Validate coverage for lookback at the start boundary
first_bd = previous_business_day(bdays, start)
_ = shift_back_business_days(index(bdays), bdays, first_bd, L)

d = start
while d < end:
    if d in bdays:
        business_day = d
        next_bd = next_business_day(bdays, d + 1 day)
    else:
        business_day = previous_business_day(bdays, d)
        next_bd = next_business_day(bdays, d)

    n = min((next_bd - d).days, (end - d).days)
    obs = shift_back_business_days(index(bdays), bdays, business_day, L)
    r = rates[obs]  # decimal

    C *= (1 + r * n / N)
    if SONIA: C = quantize_18dp(C)

    d += n

# Day-count fractions for margin/CAS
dc = (end - start).days
DCF_total = dc / N
(pre_days, post_days), (m1, m2) = split_margin_step(...)
DCF_pre = pre_days / N
DCF_post = post_days / N

interest_rfr    = (C - 1) * principal
interest_margin = (m1 * DCF_pre + m2 * DCF_post) * principal
interest_cas    = cas * DCF_total * principal
interest_total  = interest_rfr + interest_margin + interest_cas
```

### Edge cases and behaviors
- If the margin change date is before or on `start_date`, the entire period uses `margin_after`.
- If the margin change date is on or after `end_date`, the change is ignored.
- If the rate history does not extend far enough back for the required lookback on the first business day, the calculation fails with a descriptive error.
- CSV parsing accepts header/no header; values > 1 are treated as percentages (divided by 100).
- Monetary outputs shown to 2 decimals; internal compounding uses high precision, with SONIA steps quantized to 18 decimals.

### Outputs
- `interest_total`, `interest_rfr`, `interest_margin`, `interest_cas`
- `compounded_factor` (C)
- `rfr_annualized`, `applicable_annualized_rate`
- `dc` (calendar days), `N` (basis days)
- `margin_breakdown`: pre/post day counts and rates (with effective date if applicable)
- Optional `daily_details` stream for audit and export
