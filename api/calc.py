from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, date, timedelta
from decimal import Decimal, getcontext, ROUND_HALF_UP
from collections import OrderedDict
from typing import List, Dict, Optional
import urllib.request
import urllib.parse
import ssl

try:
    import certifi  # type: ignore
    HAS_CERTIFI = True
except Exception:
    HAS_CERTIFI = False

getcontext().prec = 34

# -------- Core calculation utilities (extracted/minified from desktop app) -------- #

def parse_rate_input(value: str) -> Decimal:
    x = Decimal(str(value).strip())
    return x / Decimal(100) if x > 1 else x


def quantize_money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def previous_business_day(bdays: List[date], d: date) -> date:
    lo, hi = 0, len(bdays) - 1
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if bdays[mid] <= d:
            ans = bdays[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    if ans is None:
        raise ValueError(f"No business day on/before {d} in the supplied rates.")
    return ans


def shift_back_business_days(bdays_index: Dict[date, int], bdays: List[date], d: date, n: int) -> date:
    if d not in bdays_index:
        raise ValueError(f"{d} is not a business day in the supplied rates.")
    i = bdays_index[d] - n
    if i < 0:
        raise ValueError(f"Rates do not go back {n} business days before {d}. Add more history.")
    return bdays[i]


def next_business_day(bdays: List[date], d: date) -> date:
    lo, hi = 0, len(bdays) - 1
    ans = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if bdays[mid] >= d:
            ans = bdays[mid]
            hi = mid - 1
        else:
            lo = mid + 1
    if ans is None:
        raise ValueError(f"No business day on/after {d} in the supplied rates.")
    return ans


def parse_csv_content(content: str) -> OrderedDict:
    """Parse CSV content with date in first col (YYYY-MM-DD) and rate in second.
    Accepts header or no header; rates can be percent or decimal."""
    import csv
    from io import StringIO

    f = StringIO(content)
    # Robust dialect detection with fallback
    try:
        sniffer = csv.Sniffer()
        sample = f.read(2048)
        f.seek(0)
        try:
            dialect = sniffer.sniff(sample) if sample else csv.excel
        except csv.Error:
            dialect = csv.excel
    except Exception:
        dialect = csv.excel

    reader = csv.reader(f, dialect)
    rows = []

    # Check first row; if not a date, treat as header
    first = next(reader, None)
    def is_date_str(x: str) -> bool:
        try:
            datetime.strptime(x.strip(), "%Y-%m-%d")
            return True
        except Exception:
            return False
    if first is not None and (len(first) >= 2 and is_date_str(first[0])):
        rows.append(first)
    for row in reader:
        if not row or len(row) < 2:
            continue
        rows.append(row)

    dmap = {}
    for r in rows:
        try:
            d = datetime.strptime(str(r[0]).strip(), "%Y-%m-%d").date()
        except Exception:
            continue
        try:
            raw = Decimal(str(r[1]).strip())
        except Exception:
            continue
        rate_dec = raw / Decimal(100) if raw > 1 else raw
        dmap[d] = rate_dec

    if not dmap:
        raise ValueError("No valid rate data found in CSV content")

    return OrderedDict(sorted(dmap.items(), key=lambda kv: kv[0]))


def download_csv_from_url(url: str) -> str:
    """Download CSV content from an HTTPS URL. Supports Google Drive share links.
    Tries verified SSL first (using certifi if available), then falls back to a
    non-verifying SSL context as a last resort to avoid CERTIFICATE_VERIFY_FAILED.
    """
    if 'drive.google.com' in url and '/file/d/' in url:
        try:
            file_id = url.split('/file/d/')[1].split('/')[0]
            url = f"https://drive.google.com/uc?export=download&id={file_id}"
        except Exception:
            pass

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    def _open_with_context(context: ssl.SSLContext) -> bytes:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
        return opener.open(req, timeout=30)

    # Attempt: verified SSL (prefer certifi if available)
    try:
        ctx = ssl.create_default_context()
        if HAS_CERTIFI:
            ctx.load_verify_locations(certifi.where())
        data = _open_with_context(ctx).read()
    except Exception:
        # Fallback: non-verifying SSL (insecure; acceptable for public CSV if needed)
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            data = _open_with_context(ctx).read()
        except Exception as e:
            raise ValueError(f"Failed to download CSV: {e}")

    # Try to decode using server-declared charset, then utf-8 fallback
    try:
        # Make a lightweight HEAD-equivalent to get charset if possible
        # If not available, default to utf-8
        charset = 'utf-8'
        content_type = ''
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                content_type = resp.headers.get('Content-Type', '')
        except Exception:
            pass
        if 'charset=' in content_type:
            charset = content_type.split('charset=')[-1].split(';')[0].strip()
        return data.decode(charset or 'utf-8', errors='replace')
    except Exception:
        return data.decode('utf-8', errors='replace')


def compute_interest_compounded_in_arrears(
    principal: Decimal,
    start: date,
    end: date,
    lookback_bdays: int,
    rates: OrderedDict,
    basis_days: int,
    margin_pa: Decimal,
    cas_pa: Decimal,
    margin_change_date: Optional[date] = None,
    margin_pa_after: Optional[Decimal] = None,
    is_sonia: bool = False,
    return_daily_details: bool = False,
) -> dict:
    if lookback_bdays < 1:
        raise ValueError("Lookback must be at least 1 business day.")
    if end <= start:
        raise ValueError("End date must be after start date.")
    bdays = list(rates.keys())
    if not bdays:
        raise ValueError("No rates provided.")
    bdays_index = {bd: i for i, bd in enumerate(bdays)}

    first_needed_bd = previous_business_day(bdays, start)
    _ = shift_back_business_days(bdays_index, bdays, first_needed_bd, lookback_bdays)

    N = Decimal(basis_days)
    C = Decimal(1)

    daily_details = [] if return_daily_details else None

    current_date = start
    while current_date < end:
        if current_date in bdays:
            business_day = current_date
            next_bd = next_business_day(bdays, current_date + timedelta(days=1))
        else:
            business_day = previous_business_day(bdays, current_date)
            next_bd = next_business_day(bdays, current_date)

        days_applied = min((next_bd - current_date).days, (end - current_date).days)
        obs = shift_back_business_days(bdays_index, bdays, business_day, lookback_bdays)
        r = rates[obs]

        period_factor = Decimal(1) + (r * Decimal(days_applied) / N)
        C *= period_factor
        if is_sonia:
            C = C.quantize(Decimal('0.000000000000000001'), rounding=ROUND_HALF_UP)

        if return_daily_details:
            for i in range(days_applied):
                detail_date = current_date + timedelta(days=i)
                if detail_date < end:
                    daily_details.append({
                        'date': detail_date.isoformat(),
                        'business_day': business_day.isoformat(),
                        'observation_date': obs.isoformat(),
                        'daily_rate': float(r),
                        'cumulative_factor': float(C),
                        'days_applied': days_applied,
                        'is_business_day': (detail_date == business_day)
                    })

        current_date += timedelta(days=days_applied)

    dc = Decimal((end - start).days)
    dcf_total = dc / N

    pre_days = int(dc)
    post_days = 0
    m1 = margin_pa
    m2 = margin_pa_after if margin_pa_after is not None else margin_pa
    eff = None

    if margin_change_date is not None:
        eff = margin_change_date
        if eff <= start:
            pre_days = 0
            post_days = int(dc)
            m1 = m2
        elif eff >= end:
            pre_days = int(dc)
            post_days = 0
            m2 = m1
        else:
            pre_days = (eff - start).days
            post_days = (end - eff).days

    dcf_pre = Decimal(pre_days) / N
    dcf_post = Decimal(post_days) / N

    interest_rfr = (C - Decimal(1)) * principal
    interest_margin = (m1 * dcf_pre + m2 * dcf_post) * principal
    interest_cas = cas_pa * dcf_total * principal
    interest_total = interest_rfr + interest_margin + interest_cas

    rfr_annualized = (C - Decimal(1)) * (N / dc) if dc != 0 else Decimal(0)
    margin_pa_weighted = ((m1 * dcf_pre + m2 * dcf_post) / (dcf_total if dcf_total != 0 else Decimal(1))) if dc != 0 else Decimal(0)
    applicable_annualized_rate = rfr_annualized + margin_pa_weighted + cas_pa

    result = {
        "interest_total": float(quantize_money(interest_total)),
        "interest_rfr": float(quantize_money(interest_rfr)),
        "interest_margin": float(quantize_money(interest_margin)),
        "interest_cas": float(quantize_money(interest_cas)),
        "compounded_factor": float(C),
        "rfr_annualized": float(rfr_annualized),
        "applicable_annualized_rate": float(applicable_annualized_rate),
        "dc": int(dc),
        "N": int(N),
        "margin_breakdown": {
            "pre": {"days": pre_days, "margin_pa": float(m1)},
            "post": {"days": post_days, "margin_pa": float(m2), "effective_date": eff.isoformat() if eff else None},
        },
    }

    if return_daily_details:
        result["daily_details"] = daily_details

    return result


# -------- HTTP handler for Vercel Serverless Function -------- #

class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get('content-length', '0'))
            raw = self.rfile.read(length) if length > 0 else b''
            data = json.loads(raw.decode('utf-8')) if raw else {}
        except Exception:
            return self._send(400, {"error": "Invalid JSON body"})

        try:
            principal = Decimal(str(data.get('principal')))
            start = datetime.strptime(data.get('start_date'), "%Y-%m-%d").date()
            end = datetime.strptime(data.get('end_date'), "%Y-%m-%d").date()
            pricing_option = data.get('pricing_option', 'SONIA').upper()
            lookback = int(data.get('lookback', 5))
            margin_pa = parse_rate_input(str(data.get('margin'))) if 'margin' in data else Decimal('0')
            cas_pa = parse_rate_input(str(data.get('cas'))) if 'cas' in data else Decimal('0')

            margin_after = data.get('margin_after')
            margin_change_date_str = data.get('margin_change_date')
            margin_pa_after = parse_rate_input(str(margin_after)) if margin_after is not None else None
            margin_change_date = datetime.strptime(margin_change_date_str, "%Y-%m-%d").date() if margin_change_date_str else None

            # Input rates: either explicit 'rates', or 'csv_text', or 'csv_url'
            rates_input = data.get('rates')
            csv_text = data.get('csv_text')
            csv_url = data.get('csv_url')

            if rates_input is None and csv_text is None and csv_url is None:
                raise ValueError("Provide one of: rates[], csv_text, or csv_url")

            if rates_input is not None:
                if not isinstance(rates_input, list) or len(rates_input) == 0:
                    raise ValueError("'rates' must be a non-empty array of {date, rate}")
                rate_map = {}
                for item in rates_input:
                    d = datetime.strptime(str(item['date']), "%Y-%m-%d").date()
                    raw = Decimal(str(item['rate']))
                    rate_map[d] = raw / Decimal(100) if raw > 1 else raw
                rates = OrderedDict(sorted(rate_map.items(), key=lambda kv: kv[0]))
            else:
                # Parse CSV either from text or by downloading
                if csv_text is None and csv_url is not None:
                    csv_text = download_csv_from_url(str(csv_url))
                if not isinstance(csv_text, str) or len(csv_text.strip()) == 0:
                    raise ValueError("CSV content is empty or invalid")
                rates = parse_csv_content(csv_text)

            basis = 365 if pricing_option == 'SONIA' else 360

            result = compute_interest_compounded_in_arrears(
                principal=principal,
                start=start,
                end=end,
                lookback_bdays=lookback,
                rates=rates,
                basis_days=basis,
                margin_pa=margin_pa,
                cas_pa=cas_pa,
                margin_change_date=margin_change_date,
                margin_pa_after=margin_pa_after,
                is_sonia=(pricing_option == 'SONIA'),
                return_daily_details=bool(data.get('return_daily_details', False)),
            )

            # Add last available rate date for UI/context
            try:
                last_rate_date = max(rates.keys()).isoformat()
                result["rates_last_date"] = last_rate_date
            except Exception:
                pass

            return self._send(200, result)

        except Exception as e:
            return self._send(400, {"error": str(e)})
