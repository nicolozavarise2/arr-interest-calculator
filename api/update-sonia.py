from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from collections import OrderedDict
from typing import Tuple
import urllib.request
import ssl
import csv
import re
from io import StringIO

try:
    import certifi
    HAS_CERTIFI = True
except ImportError:
    HAS_CERTIFI = False

try:
    from bs4 import BeautifulSoup
    HAS_BEAUTIFULSOUP = True
except ImportError:
    HAS_BEAUTIFULSOUP = False


def fetch_sonia_rates_from_boe() -> str:
    """Fetch SONIA rates HTML page from Bank of England website and parse the table."""
    # URL returns HTML page with table - we'll parse the table
    url = "https://www.bankofengland.co.uk/boeapps/database/fromshowcolumns.asp?Travel=NIxSUx&FromSeries=1&ToSeries=50&DAT=ALL&FNY=&CSVF=TT&html.x=183&html.y=58&C=5JK&Filter=N"
    
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
            html_content = response.read().decode('utf-8')
        
        return html_content
            
    except Exception as e:
        try:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_context))
            urllib.request.install_opener(opener)
            
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=30) as response:
                html_content = response.read().decode('utf-8')
            
            return html_content
        except Exception as e2:
            raise ValueError(f"Failed to fetch SONIA rates from Bank of England: {str(e2)}")


def parse_boe_html_table(html_content: str) -> Tuple[OrderedDict, str]:
    """Parse Bank of England HTML table and return (rates dict, last_date)."""
    rates = OrderedDict()
    last_date = None
    
    if HAS_BEAUTIFULSOUP:
        return parse_boe_html_bs4(html_content)
    else:
        return parse_boe_html_regex(html_content)


def parse_boe_html_bs4(html_content: str) -> Tuple[OrderedDict, str]:
    """Parse BoE HTML table using BeautifulSoup."""
    soup = BeautifulSoup(html_content, 'html.parser')
    rates = OrderedDict()
    last_date = None
    
    # Find the data table (look for table with date and rate columns)
    tables = soup.find_all('table')
    for table in tables:
        rows = table.find_all('tr')
        for row in rows:
            cells = row.find_all(['td', 'th'])
            if len(cells) >= 2:
                try:
                    date_str = cells[0].get_text(strip=True)
                    rate_str = cells[1].get_text(strip=True)
                    
                    # Skip header rows
                    if date_str.lower() in ['date', 'day', ''] or not date_str:
                        continue
                    
                    # Parse date - try different formats
                    date_obj = None
                    for fmt in ['%d %b %Y', '%d %b %y']:
                        try:
                            date_obj = datetime.strptime(date_str, fmt).date()
                            # Handle 2-digit years
                            if fmt == '%d %b %y' and date_obj.year < 1950:
                                date_obj = date_obj.replace(year=date_obj.year + 100)
                            break
                        except ValueError:
                            continue
                    
                    if date_obj is None:
                        continue
                    
                    # Parse rate
                    rate_clean = re.sub(r'[^\d.]', '', rate_str)
                    if not rate_clean:
                        continue
                    
                    rate_val = Decimal(rate_clean)
                    # Convert to decimal if it looks like a percentage
                    if rate_val > 1:
                        rate_val = rate_val / Decimal(100)
                    
                    rates[date_obj] = rate_val
                    last_date = date_obj
                except (ValueError, IndexError, InvalidOperation, AttributeError):
                    continue
    
    return rates, last_date.isoformat() if last_date else None


def parse_boe_html_regex(html_content: str) -> Tuple[OrderedDict, str]:
    """Parse BoE HTML table using regex (fallback)."""
    rates = OrderedDict()
    last_date = None
    
    # Pattern to match table rows: <td>DD MMM YY</td><td>RATE</td>
    # Also handle <th> for headers
    pattern = r'<(?:td|th)[^>]*>(\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})</(?:td|th)>\s*<(?:td|th)[^>]*>([^<]+)</(?:td|th)>'
    
    matches = re.findall(pattern, html_content, re.IGNORECASE)
    
    for date_str, rate_str in matches:
        try:
            # Skip if looks like header
            if date_str.lower() in ['date', 'day']:
                continue
            
            # Parse date
            date_obj = None
            for fmt in ['%d %b %Y', '%d %b %y']:
                try:
                    date_obj = datetime.strptime(date_str.strip(), fmt).date()
                    if fmt == '%d %b %y' and date_obj.year < 1950:
                        date_obj = date_obj.replace(year=date_obj.year + 100)
                    break
                except ValueError:
                    continue
            
            if date_obj is None:
                continue
            
            # Parse rate
            rate_clean = re.sub(r'[^\d.]', '', rate_str.strip())
            if not rate_clean:
                continue
            
            rate_val = Decimal(rate_clean)
            if rate_val > 1:
                rate_val = rate_val / Decimal(100)
            
            rates[date_obj] = rate_val
            last_date = date_obj
        except (ValueError, InvalidOperation):
            continue
    
    return rates, last_date.isoformat() if last_date else None


def parse_boe_csv(csv_content: str) -> Tuple[OrderedDict, str]:
    """Parse Bank of England CSV format and return (rates dict, last_date)."""
    rates = OrderedDict()
    last_date = None
    
    # Clean the content
    csv_content = csv_content.strip()
    if not csv_content:
        return rates, None
    
    # Try different CSV delimiters (tab is common for BoE exports)
    rows = None
    for delimiter in ['\t', ',', ';']:
        try:
            reader = csv.reader(StringIO(csv_content), delimiter=delimiter)
            rows = list(reader)
            # Check if we got meaningful data (at least 2 columns)
            if rows and len(rows) > 0:
                # Check if first row has at least 2 columns with data
                first_row = rows[0]
                if len(first_row) >= 2 and first_row[0].strip() and first_row[1].strip():
                    break
        except Exception:
            continue
    
    # If CSV parsing failed, try line-by-line parsing
    if rows is None or (rows and len(rows[0]) < 2):
        lines = csv_content.split('\n')
        rows = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Try splitting by tab, then comma, then whitespace
            parts = line.split('\t')
            if len(parts) < 2:
                parts = line.split(',')
            if len(parts) < 2:
                parts = line.split()
            if len(parts) >= 2:
                rows.append(parts)
    
    if not rows:
        return rates, None
    
    # Find the date and rate columns (might have headers)
    date_col_idx = 0
    rate_col_idx = 1
    
    # Check first row to see if it's a header
    first_row = rows[0]
    if len(first_row) >= 2:
        first_col = str(first_row[0]).strip().lower()
        # If first column looks like a header, skip it
        if first_col in ['date', 'day', 'time']:
            rows = rows[1:]  # Skip header
    
    # Date format patterns to try (BoE typically uses "DD MMM YY" or "DD MMM YYYY")
    date_formats = [
        '%d %b %Y',      # 02 Jan 1997
        '%d %b %y',      # 02 Jan 97
        '%d-%b-%Y',      # 02-Jan-1997
        '%d/%b/%Y',      # 02/Jan/1997
        '%Y-%m-%d',      # 1997-01-02
        '%d/%m/%Y',      # 02/01/1997
        '%d-%m-%Y',      # 02-01-1997
    ]
    
    for row in rows:
        if len(row) < 2:
            continue
        
        # Get date from first column
        date_str = str(row[date_col_idx]).strip()
        if not date_str or date_str.lower() in ['date', 'day', '']:
            continue
        
        # Try to parse date with different formats
        date_obj = None
        for fmt in date_formats:
            try:
                date_obj = datetime.strptime(date_str, fmt).date()
                # Handle 2-digit years: assume 1900s for years < 50, 2000s for >= 50
                if fmt == '%d %b %y':
                    if date_obj.year < 1950:
                        date_obj = date_obj.replace(year=date_obj.year + 100)
                break
            except ValueError:
                continue
        
        if date_obj is None:
            continue
        
        # Find rate column - try second column, then search for numeric column
        rate_val = None
        for col_idx in [rate_col_idx] + list(range(2, min(len(row), 10))):
            if col_idx >= len(row):
                continue
            rate_str = str(row[col_idx]).strip()
            if not rate_str:
                continue
            
            # Clean rate string - keep digits, decimal point, and minus sign
            rate_clean = ''.join(c for c in rate_str if c.isdigit() or c == '.' or c == '-')
            if not rate_clean or rate_clean == '-' or rate_clean == '.':
                continue
            
            try:
                rate_val = Decimal(rate_clean)
                # If rate looks like a percentage (> 1), convert to decimal
                if rate_val > 1:
                    rate_val = rate_val / Decimal(100)
                break
            except (ValueError, InvalidOperation):
                continue
        
        if rate_val is None:
            continue
        
        rates[date_obj] = rate_val
        last_date = date_obj
    
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
            # Fetch HTML page from BoE (contains table with rates)
            html_content = fetch_sonia_rates_from_boe()
            
            # Debug: check if HTML is empty
            if not html_content or len(html_content.strip()) == 0:
                return self._send(500, {
                    "error": "Received empty response from Bank of England",
                    "debug": "Response content length: 0"
                })
            
            # Parse HTML table
            rates, last_date = parse_boe_html_table(html_content)
            
            # Check if parsing resulted in empty rates
            if len(rates) == 0:
                # Return first 1000 chars of raw HTML for debugging
                preview = html_content[:1000] if len(html_content) > 1000 else html_content
                return self._send(500, {
                    "error": "Failed to parse any rates from HTML table. The table format may have changed.",
                    "debug": f"HTML preview (first 1000 chars): {preview}",
                    "html_length": len(html_content)
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
        # POST also uses the same direct CSV download (all data)
        return self.do_GET()

