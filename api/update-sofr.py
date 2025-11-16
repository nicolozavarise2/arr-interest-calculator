from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from collections import OrderedDict
from typing import Tuple
import urllib.request
import ssl
import csv
from io import StringIO, BytesIO

try:
    import certifi
    HAS_CERTIFI = True
except ImportError:
    HAS_CERTIFI = False

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


def fetch_sofr_rates_from_nyfed() -> bytes:
    """Fetch SOFR rates XLSX file from New York Fed website."""
    url = "https://markets.newyorkfed.org/read?productCode=50&eventCodes=520&limit=1000&startPosition=0&sort=postDt:-1&format=xlsx"
    
    try:
        ssl_context = ssl.create_default_context()
        if HAS_CERTIFI:
            ssl_context.load_verify_locations(certifi.where())
        else:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_context))
        urllib.request.install_opener(opener)
        
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            xlsx_content = response.read()
        
        return xlsx_content
            
    except Exception as e:
        try:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_context))
            urllib.request.install_opener(opener)
            
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=30) as response:
                xlsx_content = response.read()
            
            return xlsx_content
        except Exception as e2:
            raise ValueError(f"Failed to fetch SOFR rates from New York Fed: {str(e2)}")


def parse_sofr_xlsx(xlsx_content: bytes) -> Tuple[OrderedDict, str]:
    """Parse SOFR XLSX file. Column A = dates, Column C = rates."""
    rates = OrderedDict()
    last_date = None
    
    if not HAS_OPENPYXL:
        raise ValueError("openpyxl library is required to parse XLSX files. Please install it.")
    
    try:
        workbook = openpyxl.load_workbook(BytesIO(xlsx_content), data_only=True)
        sheet = workbook.active
        
        # Skip header row and process data
        for row_idx, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
            if len(row) < 3:
                continue
            
            # Column A (index 0) = date
            date_val = row[0]
            if date_val is None:
                continue
            
            # Handle different date formats
            date_obj = None
            if isinstance(date_val, date):
                date_obj = date_val
            elif isinstance(date_val, datetime):
                date_obj = date_val.date()
            elif isinstance(date_val, str):
                # Try parsing string dates
                for fmt in ['%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%Y-%m-%d %H:%M:%S']:
                    try:
                        dt = datetime.strptime(date_val.strip(), fmt)
                        date_obj = dt.date()
                        break
                    except ValueError:
                        continue
            
            if date_obj is None:
                continue
            
            # Column C (index 2) = rate
            rate_val_raw = row[2]
            if rate_val_raw is None:
                continue
            
            try:
                if isinstance(rate_val_raw, (int, float)):
                    rate_val = Decimal(str(rate_val_raw))
                else:
                    rate_str = str(rate_val_raw).strip()
                    rate_clean = ''.join(c for c in rate_str if c.isdigit() or c == '.' or c == '-')
                    if not rate_clean or rate_clean == '-' or rate_clean == '.':
                        continue
                    rate_val = Decimal(rate_clean)
                
                # Convert to decimal if it looks like a percentage
                if rate_val > 1:
                    rate_val = rate_val / Decimal(100)
                
                rates[date_obj] = rate_val
                last_date = date_obj
            except (ValueError, InvalidOperation, TypeError):
                continue
        
    except Exception as e:
        raise ValueError(f"Failed to parse SOFR XLSX file: {str(e)}")
    
    return rates, last_date.isoformat() if last_date else None


def convert_to_standard_csv(rates: OrderedDict) -> str:
    """Convert rates OrderedDict to standard CSV format (YYYY-MM-DD, percent)."""
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Rate'])
    for d, r in sorted(rates.items()):
        writer.writerow([d.isoformat(), str(float(r) * 100)])  # Convert to percent
    return output.getvalue()


class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict, content_type: str = "application/json"):
        body = json.dumps(payload).encode("utf-8") if content_type == "application/json" else payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            # Fetch XLSX from NY Fed
            xlsx_content = fetch_sofr_rates_from_nyfed()
            
            if not xlsx_content or len(xlsx_content) == 0:
                return self._send(500, {
                    "error": "Received empty XLSX file from New York Fed",
                    "debug": "XLSX content length: 0"
                })
            
            # Parse XLSX
            rates, last_date = parse_sofr_xlsx(xlsx_content)
            
            # Check if parsing resulted in empty rates
            if len(rates) == 0:
                return self._send(500, {
                    "error": "Failed to parse any rates from XLSX file. The file format may have changed.",
                    "debug": f"XLSX file size: {len(xlsx_content)} bytes"
                })
            
            # Convert to standard CSV format (for download if needed)
            standard_csv = convert_to_standard_csv(rates)
            
            # Convert to JSON array format for direct use in calculations
            rates_array = [{"date": d.isoformat(), "rate": float(r * 100)} for d, r in sorted(rates.items())]
            
            return self._send(200, {
                "success": True,
                "csv": standard_csv,
                "rates": rates_array,  # JSON array format for direct use
                "last_date": last_date,
                "count": len(rates)
            })
        except Exception as e:
            return self._send(500, {"error": str(e), "type": type(e).__name__})

    def do_POST(self):
        # POST also uses the same XLSX download
        return self.do_GET()

