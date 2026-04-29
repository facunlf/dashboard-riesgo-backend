import os
import json
import urllib.parse
import urllib.request
import csv
import re
import io
import html
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:
    import xlrd  # Needed because NY Fed serves gscpi_data.xlsx as legacy Excel/BIFF in some deployments.
except Exception:  # pragma: no cover - handled at runtime with a clear warning/error.
    xlrd = None
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


@app.after_request
def add_cors_headers(response):
    # Ensure even JSON error responses include CORS headers.
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return response

@app.before_request
def handle_options_preflight():
    # Make every preflight cheap and predictable, even if a route changes.
    if request.method == "OPTIONS":
        return ("", 204)

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "ok": False,
        "error": "Endpoint no encontrado en este backend. Probablemente Render sigue ejecutando una versión anterior.",
        "path": request.path,
        "version": "pro-free-logistics-dynamic-multisource-v18-brent-yahoo-realtime",
        "availableEndpoints": ["/health", "/api/official-data", "/api/history", "/api/gscpi-history", "/api/currency-dominance"]
    }), 404

FRED_SERIES = {
    "brent": ("DCOILBRENTEU", "Brent"),
    "creditSpreads": ("BAMLH0A0HYM2", "HY OAS"),
    "vix": ("VIXCLS", "VIX"),
    "usdStrength": ("DTWEXBGS", "USD trade weighted"),
    "tenYearYield": ("DGS10", "Treasury 10Y"),
    "twoYearYield": ("DGS2", "Treasury 2Y"),
    "tenYearBreakeven": ("T10YIE", "Inflation breakeven 10Y"),
    "realYield10y": ("DFII10", "Real yield 10Y"),
    "unemployment": ("UNRATE", "Unemployment"),
    "sahmRule": ("SAHMREALTIME", "Sahm Rule recession indicator"),
    "smoothedRecessionProbability": ("RECPROUSM156N", "Smoothed recession probability"),
    "initialClaims": ("ICSA", "Initial unemployment claims"),
}

LOGISTICS_QUERY = (
    '"shipping disruption" OR "maritime insurance" OR "Strait of Hormuz" OR '
    '"Red Sea shipping" OR "Suez Canal" OR "port congestion" OR '
    '"tanker attack" OR "freight rates" OR "shipping route diversion"'
)

PMI_QUERY = (
    '("JPMorgan Global Composite PMI" OR "J.P.Morgan Global Composite PMI" OR '
    '"J.P. Morgan Global Composite PMI" OR "Global Composite PMI Output Index" OR '
    '"global composite pmi")'
)

MACRO_RECESSION_QUERY = (
    '"recession risk" OR "global recession" OR "economic slowdown" OR '
    '"hard landing" OR "credit stress" OR "yield curve" OR '
    '"unemployment rising" OR "consumer demand slowdown" OR "global growth forecast cut"'
)

GSCPI_DATA_URLS = [
    "https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx",
    "https://newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx",
]


def fetch_json(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None):
    data = None
    req_headers = headers or {}

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    # Some public endpoints are less likely to reject requests with a User-Agent.
    req_headers.setdefault("User-Agent", "macro-risk-dashboard/1.0")

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()

        if not raw:
            raise ValueError("Respuesta vacía del proveedor")

        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            preview = raw[:160].replace("\n", " ")
            raise ValueError(f"Respuesta no JSON del proveedor: {preview}") from error

def fetch_binary(url: str, headers: dict | None = None, timeout: int = 15):
    req_headers = dict(headers or {})
    req_headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 macro-risk-dashboard/1.0")
    req_headers.setdefault("Accept", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel,text/csv,application/json,text/plain,text/html,*/*")
    req_headers.setdefault("Referer", "https://www.newyorkfed.org/research/policy/gscpi")
    req_headers.setdefault("Accept-Language", "en-US,en;q=0.9,es;q=0.8")
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            raise ValueError("Respuesta vacía del proveedor")
        return raw

def fetch_text(url: str, headers: dict | None = None, timeout: int = 15):
    raw = fetch_binary(url, headers=headers, timeout=timeout)
    return raw.decode("utf-8", errors="replace")

def date_compact(value: str):
    text = str(value or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text.replace("-", "")
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return (text + "-01").replace("-", "")
    return datetime.utcnow().strftime("%Y%m%d")

def parse_stooq_csv(raw: str):
    observations = []
    text = (raw or "").strip()
    if not text:
        return observations

    # Stooq normally returns CSV: Date,Open,High,Low,Close,Volume
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        date = str(row.get("Date") or row.get("date") or "").strip()
        close = parse_float_like(row.get("Close") or row.get("close"))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) and close is not None:
            observations.append({"date": date, "value": close})

    observations.sort(key=lambda x: x["date"])
    return observations

def fetch_sp500_stooq_history(start: str, end: str):
    """Daily S&P 500 close from Stooq, used as long-history source.
    Returns observations shaped like FRED: [{date: YYYY-MM-DD, value: close}].
    The function tries a few symbol/interval variants because Stooq sometimes answers
    differently depending on endpoint, symbol case and bot filtering.
    """
    d1 = date_compact(start)
    d2 = date_compact(end)

    attempts = []
    for symbol in ("^spx", "^SPX"):
        for interval in ("d", "m"):
            params = {"s": symbol, "d1": d1, "d2": d2, "i": interval}
            attempts.append("https://stooq.com/q/d/l/?" + urllib.parse.urlencode(params))

    errors = []
    for url in attempts:
        try:
            raw = fetch_text(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                "Accept": "text/csv,text/plain,*/*",
                "Referer": "https://stooq.com/q/d/?s=%5Espx",
            }, timeout=12)
            observations = parse_stooq_csv(raw)
            if observations:
                return observations
            errors.append(f"sin CSV legible en {url}; preview={raw[:90].replace(chr(10),' ')}")
        except Exception as error:
            errors.append(f"{url}: {error}")

    raise ValueError("Stooq S&P 500 no devolvió observaciones legibles: " + " | ".join(errors[-3:]))


def unix_timestamp_utc(date_text: str, add_days: int = 0):
    text = str(date_text or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}$", text):
        text = text + "-01"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"Fecha inválida para Yahoo Finance: {date_text}")
    dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=add_days)
    return int(dt.timestamp())

def fetch_sp500_yahoo_history(start: str, end: str):
    """Daily S&P 500 close from Yahoo Finance chart API (^GSPC)."""
    period1 = unix_timestamp_utc(start, 0)
    # Yahoo period2 is exclusive, so include one extra day.
    period2 = unix_timestamp_utc(end, 1)
    symbol = "^GSPC"
    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://finance.yahoo.com/quote/%5EGSPC/history",
    })

    chart = payload.get("chart") or {}
    error = chart.get("error")
    if error:
        raise ValueError(f"Yahoo Finance error: {error}")

    results = chart.get("result") or []
    if not results:
        raise ValueError("Yahoo Finance no devolvió resultados para ^GSPC")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []

    observations = []
    for ts, close in zip(timestamps, closes):
        if close in (None, "", ".", "-"):
            continue
        try:
            date = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            observations.append({"date": date, "value": float(close)})
        except Exception:
            continue

    observations.sort(key=lambda x: x["date"])
    if not observations:
        raise ValueError("Yahoo Finance no devolvió cierres legibles para ^GSPC")
    return observations

def fetch_sp500_history(start: str, end: str, fred_key: str | None = None):
    """Get S&P 500 history.

    Source priority:
    1) Yahoo Finance chart API (^GSPC) for long exact S&P 500 history.
    2) Stooq (^SPX/^spx) as fallback.
    3) FRED SP500 as fallback when configured and available.

    Returns observations shaped like FRED: [{date: YYYY-MM-DD, value: close}].
    """
    errors = []

    try:
        return fetch_sp500_yahoo_history(start, end)
    except Exception as yahoo_error:
        errors.append(f"Yahoo ^GSPC: {yahoo_error}")

    try:
        return fetch_sp500_stooq_history(start, end)
    except Exception as stooq_error:
        errors.append(f"Stooq ^SPX: {stooq_error}")

    if fred_key:
        try:
            obs = fred_observations("SP500", fred_key, start, end)
            if obs:
                return obs
            errors.append("FRED SP500: sin observaciones")
        except Exception as fred_error:
            errors.append(f"FRED SP500: {fred_error}")
    else:
        errors.append("FRED SP500: FRED_API_KEY no configurada")

    raise ValueError("S&P 500 no disponible. " + " | ".join(errors))

def excel_serial_to_date(value):
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return None
    if serial < 20000 or serial > 60000:
        return None
    # Excel incorrectly treats 1900 as leap year; 1899-12-30 matches Excel serial dates.
    dt = datetime(1899, 12, 30) + timedelta(days=serial)
    return dt.strftime("%Y-%m-%d")

def parse_date_like(value):
    if value in (None, "", ".", "-"):
        return None
    if isinstance(value, (int, float)):
        return excel_serial_to_date(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        return text[:10]
    if re.fullmatch(r"\d{4}-\d{2}.*", text):
        return text[:7] + "-01"
    for fmt in ("%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%B-%Y", "%b-%y", "%b %Y", "%B %Y", "%Y-%m"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return excel_serial_to_date(text)

def column_index(cell_ref: str):
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1

def parse_xlsx_rows(raw: bytes):
    rows_by_sheet = []
    ns = {
        "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        shared_strings = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("a:si", ns):
                parts = [node.text or "" for node in si.findall(".//a:t", ns)]
                shared_strings.append("".join(parts))

        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("rel:Relationship", ns)}

        sheet_paths = []
        for sheet in workbook.findall(".//a:sheet", ns):
            rid = sheet.attrib.get("{" + ns["r"] + "}id")
            target = rel_map.get(rid, "")
            if not target:
                continue
            if not target.startswith("/"):
                target = "xl/" + target
            target = target.lstrip("/")
            if target in zf.namelist():
                sheet_paths.append(target)

        for path in sheet_paths:
            root = ET.fromstring(zf.read(path))
            parsed_rows = []
            for row in root.findall(".//a:sheetData/a:row", ns):
                values = {}
                for cell in row.findall("a:c", ns):
                    ref = cell.attrib.get("r", "A1")
                    idx = column_index(ref)
                    cell_type = cell.attrib.get("t")
                    value_node = cell.find("a:v", ns)
                    inline_node = cell.find("a:is/a:t", ns)
                    raw_value = value_node.text if value_node is not None else inline_node.text if inline_node is not None else ""
                    if cell_type == "s":
                        try:
                            value = shared_strings[int(raw_value)]
                        except Exception:
                            value = raw_value
                    else:
                        value = raw_value
                    values[idx] = value
                if values:
                    max_idx = max(values)
                    parsed_rows.append([values.get(i, "") for i in range(max_idx + 1)])
            rows_by_sheet.append(parsed_rows)
    return rows_by_sheet



CFB_ENDOFCHAIN = 0xFFFFFFFE
CFB_FREESECT = 0xFFFFFFFF


def _le_u16(data: bytes, offset: int):
    return struct.unpack_from("<H", data, offset)[0]


def _le_u32(data: bytes, offset: int):
    return struct.unpack_from("<I", data, offset)[0]


def _le_u64(data: bytes, offset: int):
    return struct.unpack_from("<Q", data, offset)[0]


def _cfb_sector_offset(sector_id: int, sector_size: int):
    return (sector_id + 1) * sector_size


def _cfb_read_chain(raw: bytes, start_sector: int, fat: list[int], sector_size: int):
    output = bytearray()
    sector = start_sector
    seen = set()
    while (
        sector not in (CFB_ENDOFCHAIN, CFB_FREESECT)
        and sector < len(fat)
        and sector not in seen
        and len(seen) < 200000
    ):
        seen.add(sector)
        offset = _cfb_sector_offset(sector, sector_size)
        output.extend(raw[offset:offset + sector_size])
        sector = fat[sector]
    return bytes(output)


def _cfb_workbook_stream(raw: bytes):
    """Extract Workbook/Book stream from an OLE Compound File.

    This is a small built-in fallback for the NY Fed GSCPI file. Render should normally
    use xlrd, but this avoids returning 0 GSCPI rows if xlrd is missing or the file is
    served as legacy BIFF under an .xlsx name.
    """
    if raw[:8] != bytes.fromhex("D0CF11E0A1B11AE1"):
        raise ValueError("no es un contenedor OLE/BIFF")

    sector_size = 1 << _le_u16(raw, 0x1E)
    first_dir_sector = _le_u32(raw, 0x30)
    mini_cutoff = _le_u32(raw, 0x38)
    first_minifat_sector = _le_u32(raw, 0x3C)
    n_minifat_sectors = _le_u32(raw, 0x40)
    first_difat_sector = _le_u32(raw, 0x44)
    n_difat_sectors = _le_u32(raw, 0x48)

    difat = []
    for i in range(109):
        sector = _le_u32(raw, 0x4C + i * 4)
        if sector not in (CFB_FREESECT, CFB_ENDOFCHAIN):
            difat.append(sector)

    sector = first_difat_sector
    for _ in range(n_difat_sectors):
        if sector in (CFB_FREESECT, CFB_ENDOFCHAIN) or sector >= 0xFFFFFFF0:
            break
        offset = _cfb_sector_offset(sector, sector_size)
        block = raw[offset:offset + sector_size]
        for i in range((sector_size // 4) - 1):
            value = _le_u32(block, i * 4)
            if value not in (CFB_FREESECT, CFB_ENDOFCHAIN):
                difat.append(value)
        sector = _le_u32(block, sector_size - 4)

    fat = []
    for fat_sector in difat:
        offset = _cfb_sector_offset(fat_sector, sector_size)
        block = raw[offset:offset + sector_size]
        fat.extend(_le_u32(block, i) for i in range(0, len(block) - 3, 4))

    directory = _cfb_read_chain(raw, first_dir_sector, fat, sector_size)
    entries = []
    root = None
    for offset in range(0, len(directory), 128):
        entry = directory[offset:offset + 128]
        if len(entry) < 128:
            continue
        name_len = _le_u16(entry, 64)
        name = entry[:max(0, name_len - 2)].decode("utf-16le", "ignore") if name_len >= 2 else ""
        entry_type = entry[66]
        start_sector = _le_u32(entry, 116)
        size = _le_u64(entry, 120)
        item = {"name": name, "type": entry_type, "start": start_sector, "size": size}
        entries.append(item)
        if entry_type == 5:
            root = item

    workbook = next((e for e in entries if e["type"] == 2 and e["name"].lower() in {"workbook", "book"}), None)
    if not workbook:
        raise ValueError("no se encontró stream Workbook/Book en Excel legacy")

    if workbook["size"] < mini_cutoff and root:
        ministream = _cfb_read_chain(raw, root["start"], fat, sector_size)[:root["size"]]
        minifat_stream = _cfb_read_chain(raw, first_minifat_sector, fat, sector_size) if first_minifat_sector not in (CFB_FREESECT, CFB_ENDOFCHAIN) and n_minifat_sectors else b""
        minifat = [_le_u32(minifat_stream, i) for i in range(0, len(minifat_stream) - 3, 4)]
        mini_sector_size = 64
        output = bytearray()
        sector = workbook["start"]
        seen = set()
        while sector not in (CFB_ENDOFCHAIN, CFB_FREESECT) and sector < len(minifat) and sector not in seen:
            seen.add(sector)
            offset = sector * mini_sector_size
            output.extend(ministream[offset:offset + mini_sector_size])
            sector = minifat[sector]
        return bytes(output[:workbook["size"]])

    return _cfb_read_chain(raw, workbook["start"], fat, sector_size)[:workbook["size"]]


def _decode_rk_number(rk: int):
    divide_by_100 = rk & 1
    is_integer = rk & 2
    if is_integer:
        value = rk >> 2
        if value & (1 << 29):
            value -= 1 << 30
        value = float(value)
    else:
        high = rk & 0xFFFFFFFC
        value = struct.unpack("<d", struct.pack("<II", 0, high))[0]
    if divide_by_100:
        value /= 100.0
    return value


def _parse_sst_strings(data: bytes):
    strings = []
    if len(data) < 8:
        return strings
    try:
        _total, unique = struct.unpack_from("<II", data, 0)
    except Exception:
        return strings
    pos = 8
    for _ in range(min(unique, 100000)):
        if pos + 3 > len(data):
            break
        char_count = _le_u16(data, pos)
        pos += 2
        flags = data[pos]
        pos += 1
        is_16_bit = bool(flags & 0x01)
        has_phonetic = bool(flags & 0x04)
        has_rich_text = bool(flags & 0x08)
        rich_runs = 0
        ext_size = 0
        if has_rich_text and pos + 2 <= len(data):
            rich_runs = _le_u16(data, pos)
            pos += 2
        if has_phonetic and pos + 4 <= len(data):
            ext_size = _le_u32(data, pos)
            pos += 4
        byte_len = char_count * (2 if is_16_bit else 1)
        chunk = data[pos:pos + byte_len]
        pos += byte_len
        text = chunk.decode("utf-16le" if is_16_bit else "latin1", "ignore")
        pos += rich_runs * 4 + ext_size
        strings.append(text)
    return strings


def parse_xls_biff_rows(raw: bytes):
    workbook = _cfb_workbook_stream(raw)
    records = []
    sst_data = bytearray()
    collecting_sst = False
    pos = 0
    while pos + 4 <= len(workbook):
        opcode, length = struct.unpack_from("<HH", workbook, pos)
        pos += 4
        payload = workbook[pos:pos + length]
        pos += length
        if opcode == 0x00FC:  # SST
            collecting_sst = True
            sst_data.extend(payload)
        elif opcode == 0x003C and collecting_sst:  # CONTINUE
            sst_data.extend(payload)
        else:
            if opcode != 0x003C:
                collecting_sst = False
        records.append((opcode, payload))

    shared_strings = _parse_sst_strings(bytes(sst_data))
    cells = {}

    def put(row, col, value):
        cells[(int(row), int(col))] = value

    for opcode, payload in records:
        try:
            if opcode == 0x0203 and len(payload) >= 14:  # NUMBER
                row, col, _xf = struct.unpack_from("<HHH", payload, 0)
                put(row, col, struct.unpack_from("<d", payload, 6)[0])
            elif opcode == 0x027E and len(payload) >= 10:  # RK
                row, col, _xf = struct.unpack_from("<HHH", payload, 0)
                put(row, col, _decode_rk_number(_le_u32(payload, 6)))
            elif opcode == 0x00FD and len(payload) >= 10:  # LABELSST
                row, col, _xf = struct.unpack_from("<HHH", payload, 0)
                idx = _le_u32(payload, 6)
                put(row, col, shared_strings[idx] if idx < len(shared_strings) else "")
            elif opcode == 0x0204 and len(payload) >= 8:  # LABEL
                row, col, _xf = struct.unpack_from("<HHH", payload, 0)
                text_len = _le_u16(payload, 6)
                put(row, col, payload[8:8 + text_len].decode("latin1", "ignore"))
            elif opcode == 0x00BD and len(payload) >= 6:  # MULRK
                row, first_col = struct.unpack_from("<HH", payload, 0)
                last_col = _le_u16(payload, len(payload) - 2)
                offset = 4
                for col in range(first_col, last_col + 1):
                    if offset + 6 > len(payload) - 2:
                        break
                    put(row, col, _decode_rk_number(_le_u32(payload, offset + 2)))
                    offset += 6
        except Exception:
            continue

    if not cells:
        return []
    max_row = max(row for row, _col in cells)
    max_col = max(col for _row, col in cells)
    return [[cells.get((row, col), "") for col in range(max_col + 1)] for row in range(max_row + 1)]


def parse_xls_rows(raw: bytes):
    """Parse legacy Excel/BIFF workbooks.

    Important: the New York Fed download is currently named gscpi_data.xlsx, but
    the HTTP content-type/file structure can be legacy Excel rather than normal
    OOXML. Our previous backend only understood OOXML, so /api/history returned
    rows with 0 GSCPI values. xlrd fixes that path on Render.
    """
    if xlrd is None:
        raise ValueError("xlrd no está instalado; no se puede leer el Excel legacy de NY Fed")

    workbook = xlrd.open_workbook(file_contents=raw)
    rows_by_sheet = []
    for sheet in workbook.sheets():
        parsed_rows = []
        for r in range(sheet.nrows):
            row = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                value = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        dt_tuple = xlrd.xldate_as_tuple(value, workbook.datemode)
                        value = datetime(*dt_tuple[:6]).strftime("%Y-%m-%d")
                    except Exception:
                        value = str(value)
                row.append(value)
            parsed_rows.append(row)
        rows_by_sheet.append(parsed_rows)
    return rows_by_sheet


def parse_excel_rows(raw: bytes):
    """Parse Excel rows from either OOXML .xlsx or legacy .xls/BIFF.

    We intentionally inspect the ZIP members before using the OOXML parser:
    some legacy Excel files contain embedded ZIP-looking bytes, so a simple
    zipfile.is_zipfile check can be misleading.
    """
    errors = []

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = set(zf.namelist())
        if "xl/workbook.xml" in names:
            return parse_xlsx_rows(raw)
        errors.append("no es OOXML/xlsx estándar")
    except Exception as error:
        errors.append(f"xlsx: {error}")

    try:
        return parse_xls_rows(raw)
    except Exception as error:
        errors.append(f"xls/xlrd: {error}")

    try:
        return [parse_xls_biff_rows(raw)]
    except Exception as error:
        errors.append(f"xls/biff-built-in: {error}")

    raise ValueError("No se pudo leer Excel GSCPI: " + " | ".join(errors))

def parse_float_like(value):
    if value in (None, "", ".", "-"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None

def normalize_provider_date(value):
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}.*", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        return text[:10]
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return text
    return datetime.utcnow().strftime("%Y-%m-%d")



def clamp(value, min_value=0, max_value=100):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = min_value
    return max(min_value, min(max_value, number))

def num(row, key, fallback=0):
    if row is None:
        return fallback
    value = row.get(key, fallback)
    if value in (None, "", ".", "-"):
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback

def month_key(date_text):
    text = str(date_text or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return text
    parsed = parse_date_like(text) or normalize_provider_date(text)
    return str(parsed)[:7]

def last_observation_per_month(observations):
    by_month = {}
    for obs in sorted(observations or [], key=lambda x: str(x.get("date", ""))):
        if not isinstance(obs, dict):
            continue
        date = obs.get("date")
        value = obs.get("value")
        if date in (None, "") or value in (None, "", ".", "-"):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        by_month[month_key(date)] = numeric
    return by_month


def monthly_stats_from_observations(observations):
    """Return monthly last/min/max/avg stats from daily or irregular observations.

    For daily market series like VIXCLS, the monthly line can still use the last
    available close of each month, but period summaries should not pretend that
    this is the maximum stress reached inside the month.
    """
    grouped = {}
    for obs in sorted(observations or [], key=lambda x: str(x.get("date", ""))):
        if not isinstance(obs, dict):
            continue
        date = obs.get("date")
        value = obs.get("value")
        if date in (None, "") or value in (None, "", ".", "-"):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        key = month_key(date)
        grouped.setdefault(key, []).append({"date": str(date)[:10], "value": numeric})

    out = {}
    for key, values in grouped.items():
        if not values:
            continue
        values = sorted(values, key=lambda x: x["date"])
        nums = [v["value"] for v in values]
        max_item = max(values, key=lambda x: x["value"])
        min_item = min(values, key=lambda x: x["value"])
        out[key] = {
            "last": values[-1]["value"],
            "lastDate": values[-1]["date"],
            "max": max_item["value"],
            "maxDate": max_item["date"],
            "min": min_item["value"],
            "minDate": min_item["date"],
            "avg": sum(nums) / len(nums),
            "count": len(nums),
        }
    return out

def header_date_columns(row):
    cols = []
    for c, cell in enumerate(row):
        text = str(cell or "").strip().lower()
        if not text:
            continue
        if text in {"date", "dates", "month", "months", "observation date"} or "date" in text or "month" in text:
            cols.append(c)
    return cols


def header_gscpi_columns(row):
    cols = []
    for c, cell in enumerate(row):
        text = str(cell or "").strip().lower()
        if not text:
            continue
        if "gscpi" in text or "global supply chain pressure" in text or "supply chain pressure index" in text:
            cols.append(c)
    return cols


def score_gscpi_columns(rows, date_col, value_col, start_idx=0):
    observations = []
    for row in rows[start_idx:]:
        if not row or max(date_col, value_col) >= len(row):
            continue
        parsed_date = parse_date_like(row[date_col])
        numeric = parse_float_like(row[value_col])
        if parsed_date and numeric is not None and -10 <= numeric <= 10:
            observations.append({"date": parsed_date, "value": round(numeric, 4)})
    return len(observations), observations


def extract_gscpi_observations_from_sheet(rows):
    """Extract NY Fed GSCPI monthly observations from one XLSX sheet.

    The official workbook has changed layout over time. Older parsers could pick the
    title cell containing "GSCPI" and the date column as the same column, producing
    zero observations. This routine validates candidate column pairs by actually
    counting parseable date/value rows, then keeps the best pair.
    """
    if not rows:
        return []

    candidates = []

    # Prefer explicit header rows where date/month and GSCPI columns appear on the
    # same row, and never in the same column.
    for idx, row in enumerate(rows[:120]):
        date_cols = header_date_columns(row)
        value_cols = header_gscpi_columns(row)
        for dc in date_cols:
            for vc in value_cols:
                if dc == vc:
                    continue
                score, obs = score_gscpi_columns(rows, dc, vc, idx + 1)
                candidates.append((score, obs, "header", idx, dc, vc))

    # Fallback: infer the column pair by data shape. A valid pair must have many rows
    # where one column parses as a date and the other as a small numeric GSCPI value.
    max_cols = min(20, max((len(row) for row in rows), default=0))
    for dc in range(max_cols):
        for vc in range(max_cols):
            if dc == vc:
                continue
            score, obs = score_gscpi_columns(rows, dc, vc, 0)
            if score:
                candidates.append((score, obs, "inferred", 0, dc, vc))

    if not candidates:
        return []

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_obs, *_ = candidates[0]
    if best_score < 3:
        return []

    deduped = {}
    for obs in best_obs:
        deduped[obs["date"]] = obs
    return [deduped[k] for k in sorted(deduped)]


def fetch_gscpi_nyfed_history(start: str | None = None, end: str | None = None):
    start_key = str(start or "")[:10] if start else None
    end_key = str(end or "")[:10] if end else None
    last_error = None

    for url in GSCPI_DATA_URLS:
        try:
            raw = fetch_binary(url, timeout=25)
            sheets = parse_excel_rows(raw)
            observations = []

            for rows in sheets:
                observations.extend(extract_gscpi_observations_from_sheet(rows))

            deduped = {}
            for obs in observations:
                parsed_date = obs.get("date")
                numeric = parse_float_like(obs.get("value"))
                if not parsed_date or numeric is None or not (-10 <= numeric <= 10):
                    continue
                if start_key and parsed_date < start_key:
                    continue
                if end_key and parsed_date > end_key:
                    continue
                deduped[parsed_date] = {"date": parsed_date, "value": round(numeric, 4)}

            observations = [deduped[k] for k in sorted(deduped)]
            if observations:
                return observations

            last_error = ValueError("El archivo oficial de GSCPI se descargó, pero no se encontraron columnas fecha/GSCPI con observaciones legibles")
        except Exception as error:
            last_error = error

    raise ValueError(f"No se pudo leer GSCPI desde New York Fed: {last_error}")

def fetch_gscpi_latest():
    observations = fetch_gscpi_nyfed_history()
    latest = observations[-1]
    previous = observations[-2] if len(observations) > 1 else latest
    return {
        "latest": latest["value"],
        "previous": previous["value"],
        "date": latest["date"],
        "series": "NYFED_GSCPI",
        "source": "New York Fed GSCPI",
    }

def fred_observations(series_id: str, api_key: str, start: str | None = None, end: str | None = None):
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "asc",
        "limit": 100000,
    }

    if start:
        params["observation_start"] = start
    if end:
        params["observation_end"] = end

    url = "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(params)
    payload = fetch_json(url)

    observations = []
    for item in payload.get("observations", []):
        value = item.get("value")
        if value in (".", "-", "", None):
            continue
        try:
            observations.append({"date": item["date"], "value": float(value)})
        except ValueError:
            continue

    return observations

def fetch_fred_latest(series_id: str, api_key: str):
    observations = fred_observations(series_id, api_key)
    if not observations:
        raise ValueError(f"FRED {series_id}: sin datos")

    latest = observations[-1]
    previous = observations[-2] if len(observations) > 1 else latest

    return {
        "latest": latest["value"],
        "previous": previous["value"],
        "date": latest["date"],
        "series": series_id,
    }



def fetch_yahoo_chart_latest(symbol: str, label: str):
    """Fetch a near-real-time delayed market quote from Yahoo Finance chart API."""
    encoded_symbol = urllib.parse.quote(symbol, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}?"
        + urllib.parse.urlencode({"range": "5d", "interval": "1m"})
    )

    payload = fetch_json(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 macro-risk-dashboard/1.0",
        "Accept": "application/json,text/plain,*/*",
    })

    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        error = ((payload.get("chart") or {}).get("error") or {}).get("description")
        raise ValueError(f"Yahoo Finance {symbol}: sin datos" + (f" ({error})" if error else ""))

    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
    closes = quote.get("close") or []

    latest_value = meta.get("regularMarketPrice")
    previous_value = meta.get("chartPreviousClose") or meta.get("previousClose")
    latest_ts = meta.get("regularMarketTime")

    valid_points = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        try:
            valid_points.append((int(ts), float(close)))
        except (TypeError, ValueError):
            continue

    if latest_value in (None, "", ".", "-") and valid_points:
        latest_ts, latest_value = valid_points[-1]

    if previous_value in (None, "", ".", "-"):
        previous_value = valid_points[-2][1] if len(valid_points) > 1 else latest_value

    if latest_value in (None, "", ".", "-"):
        raise ValueError(f"Yahoo Finance {symbol}: último precio no disponible")

    latest_value = float(latest_value)
    try:
        previous_value = float(previous_value) if previous_value not in (None, "", ".", "-") else latest_value
    except (TypeError, ValueError):
        previous_value = latest_value

    try:
        date = datetime.fromtimestamp(int(latest_ts), tz=timezone.utc).strftime("%Y-%m-%d") if latest_ts else datetime.utcnow().strftime("%Y-%m-%d")
        market_time = datetime.fromtimestamp(int(latest_ts), tz=timezone.utc).isoformat() if latest_ts else None
    except Exception:
        date = datetime.utcnow().strftime("%Y-%m-%d")
        market_time = None

    return {
        "latest": round(latest_value, 2),
        "previous": round(previous_value, 2),
        "date": date,
        "series": symbol,
        "source": f"Yahoo Finance {label} ({symbol})",
        "marketTimeUtc": market_time,
        "provider": "Yahoo Finance chart API",
        "note": "Cotización de mercado/futuro con retraso; más actual que FRED spot DCOILBRENTEU.",
    }


def fetch_brent_latest(fred_key: str | None = None):
    """Prefer intraday/delayed market data for Brent; fallback to official FRED spot."""
    errors = []
    try:
        return fetch_yahoo_chart_latest("BZ=F", "Brent futures")
    except Exception as error:
        errors.append(f"Yahoo Finance BZ=F: {error}")

    if fred_key:
        try:
            item = fetch_fred_latest("DCOILBRENTEU", fred_key)
            item["source"] = "FRED DCOILBRENTEU (fallback oficial, no intradía)"
            item["note"] = "Fallback: FRED puede publicar Brent con retraso frente al mercado."
            return item
        except Exception as error:
            errors.append(f"FRED DCOILBRENTEU: {error}")

    raise ValueError("; ".join(errors) if errors else "No se pudo obtener Brent")

def fred_yoy_source_label(series_id: str):
    labels = {
        "PCEPILFE": "FRED / BEA Core PCE",
    }
    return labels.get(series_id, f"FRED {series_id}")


def parse_observation_date(date_text: str):
    return datetime.strptime(str(date_text)[:10], "%Y-%m-%d")


def yoy_for_observation(observation: dict, observations: list[dict], series_id: str):
    obs_date = observation["date"]
    obs_month = obs_date[5:7]
    obs_year = int(obs_date[:4])
    previous_year_same_month = next(
        (item for item in reversed(observations) if item["date"][:4] == str(obs_year - 1) and item["date"][5:7] == obs_month),
        None,
    )
    if not previous_year_same_month:
        raise ValueError(f"FRED {series_id}: no se encontró el mismo mes del año previo para {obs_date}")
    yoy = ((observation["value"] / previous_year_same_month["value"]) - 1) * 100
    return round(yoy, 2), previous_year_same_month


def fetch_fred_yoy_latest(series_id: str, api_key: str):
    # For current indicators, never infer a date from historical dashboard rows.
    # Fetch a broad window from FRED, sort locally, select the latest valid observation,
    # and reject clearly stale provider responses instead of showing an old date as current.
    observations = fred_observations(series_id, api_key, "1990-01-01")
    observations = sorted(observations, key=lambda x: x["date"])

    if len(observations) < 13:
        raise ValueError(f"FRED {series_id}: datos insuficientes para calcular interanual")

    latest = observations[-1]
    latest_dt = parse_observation_date(latest["date"])
    max_allowed_age_days = 550
    if datetime.utcnow() - latest_dt > timedelta(days=max_allowed_age_days):
        raise ValueError(
            f"FRED {series_id}: última observación demasiado antigua ({latest['date']}); "
            "no se actualiza para evitar mostrar un dato obsoleto como actual"
        )

    latest_yoy, previous_year_same_month = yoy_for_observation(latest, observations, series_id)

    previous_month_yoy = None
    previous_month = None
    for candidate in reversed(observations[:-1]):
        try:
            previous_month_yoy, _ = yoy_for_observation(candidate, observations, series_id)
            previous_month = candidate
            break
        except Exception:
            continue

    return {
        "latest": latest_yoy,
        "latestIndex": latest["value"],
        "previousIndex": previous_year_same_month["value"],
        "previous": previous_month_yoy,
        "previousDate": previous_month["date"] if previous_month else None,
        "date": latest["date"],
        "series": series_id,
        "source": fred_yoy_source_label(series_id),
    }


def fetch_fred_yoy_history(series_id: str, api_key: str, start: str, end: str):
    """Return monthly YoY percentage changes for a FRED monthly index series."""
    start_month = start[5:7] if len(start) >= 7 else "01"
    extended_start = f"{max(1900, int(start[:4]) - 1)}-{start_month}-01"
    observations = fred_observations(series_id, api_key, extended_start, end)
    output = []

    for obs in observations:
        if obs["date"] < start:
            continue

        obs_year = int(obs["date"][:4])
        obs_month = obs["date"][5:7]
        previous_year_same_month = next(
            (
                item for item in observations
                if item["date"][:4] == str(obs_year - 1)
                and item["date"][5:7] == obs_month
            ),
            None,
        )

        if not previous_year_same_month:
            continue

        yoy = ((obs["value"] / previous_year_same_month["value"]) - 1) * 100
        output.append({
            "date": obs["date"],
            "value": round(yoy, 2),
        })

    return output

def fetch_fred_pair_spread(series_a: str, series_b: str, api_key: str):
    a = fetch_fred_latest(series_a, api_key)
    b = fetch_fred_latest(series_b, api_key)

    return {
        "latest": round(a["latest"] - b["latest"], 3),
        "previous": None,
        "date": max(a["date"], b["date"]),
        "series": f"{series_a}-{series_b}",
        "parts": {
            series_a: a["latest"],
            series_b: b["latest"],
        },
    }

def score_range(value, low, high):
    if high == low:
        return 0
    return clamp(((float(value) - low) / (high - low)) * 100)

def score_inverse(value, healthy, stress):
    if healthy == stress:
        return 0
    return clamp(((healthy - float(value)) / (healthy - stress)) * 100)

def credit_stress_value(row):
    return score_range(num(row, "creditSpreads", 2.5), 2.5, 6.5)

def vix_stress_value(row):
    return score_range(num(row, "vix", 12), 12, 35)

def curve_stress_value(row):
    return score_inverse(num(row, "yieldCurve10y2y", 0.75), 0.75, -1.00)

def unemployment_stress_value(row):
    return score_range(num(row, "unemployment", 3.5), 3.5, 6.0)

def pmi_stress_value(row):
    return score_inverse(num(row, "globalPMI", 52), 52, 45)

def sahm_rule_stress_value(row):
    # Sahm Rule signals recession onset near 0.50 pp. Score to 100 by 0.75 pp.
    value = num(row, "sahmRule", None)
    if value is None:
        return None
    return score_range(value, 0.0, 0.75)

def smoothed_recession_probability_stress_value(row):
    # FRED RECPROUSM156N already comes as a probability-like percentage.
    value = num(row, "smoothedRecessionProbability", None)
    if value is None:
        return None
    return clamp(value)

def initial_claims_stress_value(row):
    value = num(row, "initialClaims", None)
    if value is None:
        return None
    # Weekly initial claims: roughly 200k benign, 350k+ stress.
    return score_range(value, 200000, 350000)

def recession_risk_confidence(row, pmi_source: str | None = None, provider_errors: list | None = None):
    """
    Confidence measures data coverage/freshness/source quality, not the probability itself.
    With official FRED/NY Fed signals available, confidence can exceed 85% even if the risk score is moderate.
    """
    checks = [
        ("Curva 10Y-2Y", "yieldCurve10y2y", 0.14),
        ("Desempleo USA", "unemployment", 0.12),
        ("HY OAS", "creditSpreads", 0.12),
        ("VIX", "vix", 0.08),
        ("PMI global", "globalPMI", 0.10),
        ("Sahm Rule", "sahmRule", 0.12),
        ("Probabilidad suavizada de recesión", "smoothedRecessionProbability", 0.12),
        ("Initial Claims", "initialClaims", 0.08),
        ("Noticias macro/GDELT", "macroNewsRecession", 0.06),
        ("Supply/Shipping stress", "supplyStress", 0.06),
    ]
    available = []
    missing = []
    score = 0.0
    for label, key, weight in checks:
        value = row.get(key)
        if value not in (None, "", ".", "-"):
            score += weight
            available.append(label)
        else:
            missing.append(label)

    source_text = str(pmi_source or row.get("pmiSource") or "").lower()
    if "proxy" in source_text or "calculado" in source_text:
        score -= 0.04
    if provider_errors:
        score -= min(0.04, 0.01 * len(provider_errors))

    confidence = clamp(score * 100, 0, 92) / 100
    label = "Alta" if confidence >= 0.85 else "Media" if confidence >= 0.65 else "Baja"
    explanation = (
        f"Cobertura oficial: {', '.join(available[:7])}"
        + (f". Faltan: {', '.join(missing[:4])}" if missing else ".")
    )
    return round(confidence, 2), label, explanation

def historical_shipping_stress(row):
    """
    Proxy histórico calculado con datos oficiales disponibles.
    No intenta reconstruir titulares antiguos mes a mes; usa GSCPI, Brent, VIX y USD
    para que el histórico sea comparable sin cientos de llamadas a GDELT.
    """
    gscpi_component = score_range(num(row, "gscpi", 0), -1.0, 2.0)
    brent_component = score_range(num(row, "brent", 80), 70, 120)
    vix_component = vix_stress_value(row)
    usd_component = score_range(num(row, "usdStrength", 110), 105, 130)

    return round(
        gscpi_component * 0.55
        + brent_component * 0.25
        + vix_component * 0.10
        + usd_component * 0.10
    )

def supply_stress_value(row):
    brent = num(row, "brent", 80)
    gscpi = num(row, "gscpi", 0)
    logistics = num(row, "shippingStress", historical_shipping_stress(row))

    brent_score = 20 if brent <= 80 else 50 if brent <= 100 else 80
    gscpi_score = 20 if gscpi <= 0 else 50 if gscpi <= 1 else 80
    logistics_score = clamp(logistics)

    return round(brent_score * 0.4 + gscpi_score * 0.3 + logistics_score * 0.3)

def financial_stress_value(row):
    return round(
        credit_stress_value(row) * 0.45
        + vix_stress_value(row) * 0.30
        + curve_stress_value(row) * 0.25,
        1,
    )

def global_pmi_proxy(row):
    """
    Proxy calculado para meses donde no hay serie oficial de PMI global disponible.
    Convierte estrés financiero/oferta en una lectura tipo PMI para comparar ciclos.
    """
    financial = financial_stress_value(row)
    supply = supply_stress_value(row)
    unemployment = unemployment_stress_value(row)
    value = 52.5 - financial * 0.035 - supply * 0.015 - unemployment * 0.010
    return round(clamp(value, 45, 55), 1)

def recession_risk_value(row):
    if row.get("globalPMI") in (None, "", ".", "-"):
        row["globalPMI"] = global_pmi_proxy(row)

    macro_news_proxy = clamp(num(row, "macroNewsRecession", 0) or (num(row, "supplyStress", supply_stress_value(row)) * 0.6 + num(row, "shippingStress", historical_shipping_stress(row)) * 0.4))
    sahm = sahm_rule_stress_value(row)
    smoothed = smoothed_recession_probability_stress_value(row)
    claims = initial_claims_stress_value(row)

    # Professional composite: hard official signals carry most weight; news is a small confirming layer.
    weighted = [
        (curve_stress_value(row), 0.18),
        (unemployment_stress_value(row), 0.14),
        (credit_stress_value(row), 0.16),
        (vix_stress_value(row), 0.08),
        (pmi_stress_value(row), 0.12),
        (macro_news_proxy, 0.06),
    ]
    if sahm is not None:
        weighted.append((sahm, 0.12))
    else:
        weighted[1] = (weighted[1][0], weighted[1][1] + 0.06)
        weighted[5] = (weighted[5][0], weighted[5][1] + 0.06)
    if smoothed is not None:
        weighted.append((smoothed, 0.10))
    else:
        weighted[0] = (weighted[0][0], weighted[0][1] + 0.05)
        weighted[2] = (weighted[2][0], weighted[2][1] + 0.05)
    if claims is not None:
        weighted.append((claims, 0.04))
    else:
        weighted[1] = (weighted[1][0], weighted[1][1] + 0.04)

    total_weight = sum(w for _v, w in weighted) or 1
    return round(sum(float(v) * w for v, w in weighted) / total_weight)

def status_for_value(key, value):
    v = float(value)
    if key == "globalPMI":
        return "green" if v >= 52 else "yellow" if v >= 50 else "red"
    if key == "yieldCurve10y2y":
        return "green" if v >= 0.25 else "yellow" if v >= -0.50 else "red"
    if key == "supplyStress":
        return "green" if v <= 30 else "yellow" if v <= 60 else "red"
    if key == "gscpi":
        return "green" if v <= 0 else "yellow" if v <= 1 else "red"
    if key == "brent":
        return "green" if v <= 90 else "yellow" if v <= 100 else "red"
    if key == "coreInflation":
        return "green" if v <= 2.5 else "yellow" if v <= 3.2 else "red"
    if key == "realYield10y":
        return "green" if v <= 1.0 else "yellow" if v <= 1.75 else "red"
    if key == "creditSpreads":
        return "green" if v <= 4.0 else "yellow" if v <= 5.5 else "red"
    if key == "vix":
        return "green" if v <= 20 else "yellow" if v <= 30 else "red"
    if key == "tenYearBreakeven":
        return "green" if v <= 2.4 else "yellow" if v <= 2.8 else "red"
    if key == "recessionRisk":
        return "green" if v <= 20 else "yellow" if v <= 30 else "red"
    if key == "shippingStress":
        return "green" if v <= 35 else "yellow" if v <= 60 else "red"
    if key == "usdStrength":
        return "green" if v <= 115 else "yellow" if v <= 125 else "red"
    if key == "unemployment":
        return "green" if v <= 4.5 else "yellow" if v <= 5.5 else "red"
    return "yellow"

INDICATOR_WEIGHTS = {
    "supplyStress": 1.5,
    "brent": 1.4,
    "gscpi": 1.15,
    "coreInflation": 1.15,
    "realYield10y": 1.15,
    "globalPMI": 1.25,
    "creditSpreads": 1.6,
    "vix": 1.1,
    "yieldCurve10y2y": 1.0,
    "tenYearBreakeven": 0.9,
    "recessionRisk": 1.0,
    "shippingStress": 1.2,
    "usdStrength": 0.8,
    "unemployment": 0.9,
}

def risk_score_value(row):
    score = 0
    maximum = 0
    for key, weight in INDICATOR_WEIGHTS.items():
        if key not in row:
            continue
        status = status_for_value(key, num(row, key, 0))
        score += (0 if status == "green" else 1 if status == "yellow" else 2) * weight
        maximum += 2 * weight
    return round((score / maximum) * 100, 1) if maximum else 0

def scenario_estimates(row):
    supply = num(row, "supplyStress", supply_stress_value(row))
    shipping = num(row, "shippingStress", historical_shipping_stress(row))
    brent_score = score_range(num(row, "brent", 80), 80, 120)
    inflation_score = score_range(num(row, "coreInflation", 2), 2.0, 5.0)
    pmi_weakness = pmi_stress_value(row)
    real_yield_score = score_range(num(row, "realYield10y", 1), 0.5, 2.5)
    breakeven_score = score_range(num(row, "tenYearBreakeven", 2.2), 2.0, 3.2)
    financial = num(row, "financialStress", financial_stress_value(row))
    recession = num(row, "recessionRisk", recession_risk_value(row))

    raw = {
        "Shock energético": supply * 0.55 + brent_score * 0.25 + shipping * 0.20,
        "Contagio macro / estanflación": inflation_score * 0.30 + pmi_weakness * 0.25 + real_yield_score * 0.15 + supply * 0.20 + breakeven_score * 0.10,
        "Escalada sistémica": financial * 0.40 + recession * 0.35 + credit_stress_value(row) * 0.15 + vix_stress_value(row) * 0.10,
    }

    total = sum(raw.values()) or 1
    probs = {name: round(value / total * 100) for name, value in raw.items()}
    diff = 100 - sum(probs.values())
    largest = max(probs, key=probs.get)
    probs[largest] += diff

    return [
        {
            "name": "Shock energético",
            "probability": probs["Shock energético"],
            "marketImpact": -round(5 + raw["Shock energético"] * 0.14),
            "description": "Petróleo, GSCPI y logística dominan el riesgo.",
        },
        {
            "name": "Contagio macro / estanflación",
            "probability": probs["Contagio macro / estanflación"],
            "marketImpact": -round(8 + raw["Contagio macro / estanflación"] * 0.20),
            "description": "Inflación persistente, PMI débil y tipos reales restrictivos.",
        },
        {
            "name": "Escalada sistémica",
            "probability": probs["Escalada sistémica"],
            "marketImpact": -round(12 + raw["Escalada sistémica"] * 0.33),
            "description": "Financial Stress, crédito, VIX y recesión validan un escenario más severo.",
        },
    ]

def weighted_drawdown_value(scenarios):
    return round(sum(abs(float(s["marketImpact"])) * float(s["probability"]) / 100 for s in scenarios), 1)

def max_update_date(updates, keys=None):
    selected = updates.items() if keys is None else ((key, updates.get(key)) for key in keys)
    dates = []
    for _key, item in selected:
        if isinstance(item, dict) and item.get("date"):
            dates.append(str(item["date"])[:10])
    return sorted(dates)[-1] if dates else datetime.utcnow().strftime("%Y-%m-%d")

def stamp_update(item: dict, calculation_date: str, source: str | None = None):
    """Attach explicit data/calculation dates to an indicator update."""
    if item is None:
        item = {}
    if source and not item.get("source"):
        item["source"] = source
    item["date"] = str(item.get("date") or calculation_date)[:10]
    item["inputDataDate"] = item["date"]
    item["calculatedAt"] = calculation_date
    item["fetchedAt"] = calculation_date
    return item

def enrich_history_row(row):
    if "shippingStress" not in row or row.get("shippingStress") in (None, "", ".", "-"):
        row["shippingStress"] = historical_shipping_stress(row)

    if "globalPMI" not in row or row.get("globalPMI") in (None, "", ".", "-"):
        row["globalPMI"] = global_pmi_proxy(row)

    row["supplyStress"] = supply_stress_value(row)
    row["financialStress"] = financial_stress_value(row)
    row["recessionRisk"] = recession_risk_value(row)
    row["riskScore"] = risk_score_value(row)

    scenarios = scenario_estimates(row)
    row["drawdownExpected"] = weighted_drawdown_value(scenarios)
    row["scenarioShockProbability"] = scenarios[0]["probability"]
    row["scenarioStagflationProbability"] = scenarios[1]["probability"]
    row["scenarioSystemicProbability"] = scenarios[2]["probability"]

    return row

def build_monthly_history(start: str, end: str, fred_key: str, bls_key: str | None, bls_series: str):
    start_year = int(start[:4])
    end_year = int(end[:4])

    monthly = {}
    warnings = []

    for key, (series_id, _label) in FRED_SERIES.items():
        try:
            obs = fred_observations(series_id, fred_key, start, end)

            if key == "vix":
                stats_by_month = monthly_stats_from_observations(obs)
                for date_key, stats in stats_by_month.items():
                    monthly.setdefault(date_key, {"date": date_key})
                    # Main VIX value remains the last available daily close in that month,
                    # so the line chart stays comparable with other monthly-last series.
                    monthly[date_key][key] = round(stats["last"], 4)
                    # Extra fields preserve the intramonth daily-close stress that the old
                    # monthly rollup was hiding.
                    monthly[date_key]["vixMonthlyMax"] = round(stats["max"], 4)
                    monthly[date_key]["vixMonthlyMaxDate"] = stats["maxDate"]
                    monthly[date_key]["vixMonthlyMin"] = round(stats["min"], 4)
                    monthly[date_key]["vixMonthlyMinDate"] = stats["minDate"]
                    monthly[date_key]["vixMonthlyAvg"] = round(stats["avg"], 4)
                    monthly[date_key]["vixDailyObservationCount"] = stats["count"]
            else:
                by_month = last_observation_per_month(obs)
                for date_key, value in by_month.items():
                    monthly.setdefault(date_key, {"date": date_key})
                    monthly[date_key][key] = round(value, 4)
        except Exception as error:
            warnings.append(f"{key} histórico no se pudo cargar desde FRED {series_id}: {error}")

    gscpi_loaded = False
    gscpi_errors = []

    try:
        gscpi_obs = fetch_gscpi_nyfed_history(start, end)
        by_month = last_observation_per_month(gscpi_obs)
        for date_key, value in by_month.items():
            monthly.setdefault(date_key, {"date": date_key})
            monthly[date_key]["gscpi"] = round(value, 4)
        gscpi_loaded = bool(by_month)
    except Exception as error:
        gscpi_errors.append(f"GSCPI NY Fed histórico no se pudo cargar: {error}")

        try:
            gscpi_obs = fred_observations("GSCPI", fred_key, start, end)
            by_month = last_observation_per_month(gscpi_obs)
            for date_key, value in by_month.items():
                monthly.setdefault(date_key, {"date": date_key})
                monthly[date_key]["gscpi"] = round(value, 4)
            gscpi_loaded = bool(by_month)
        except Exception as fred_error:
            gscpi_errors.append(f"GSCPI fallback FRED/API no se pudo cargar: {fred_error}")

    if not gscpi_loaded:
        warnings.extend(gscpi_errors)
        warnings.append("GSCPI histórico no se completó. El backend intenta leer directamente el Excel oficial de NY Fed; si esto aparece, revisar dependencia xlrd/egreso HTTPS en Render.")

    try:
        dgs10 = last_observation_per_month(fred_observations("DGS10", fred_key, start, end))
        dgs2 = last_observation_per_month(fred_observations("DGS2", fred_key, start, end))
        for date_key in sorted(set(dgs10) & set(dgs2)):
            monthly.setdefault(date_key, {"date": date_key})
            monthly[date_key]["yieldCurve10y2y"] = round(dgs10[date_key] - dgs2[date_key], 4)
    except Exception:
        pass

    try:
        core_history = fetch_fred_yoy_history("PCEPILFE", fred_key, start, end)
        for obs in core_history:
            date_key = month_key(obs["date"])
            monthly.setdefault(date_key, {"date": date_key})
            monthly[date_key]["coreInflation"] = obs["value"]
    except Exception:
        pass

    try:
        sp500_obs = fetch_sp500_history(start, end, fred_key)
        by_month = last_observation_per_month(sp500_obs)
        for date_key, value in by_month.items():
            monthly.setdefault(date_key, {"date": date_key})
            monthly[date_key]["sp500"] = round(value, 2)
    except Exception as error:
        warnings.append(f"S&P 500 histórico no se pudo cargar: {error}")

    rows = [monthly[k] for k in sorted(monthly.keys())]

    for row in rows:
        enrich_history_row(row)

    return rows, warnings


def normalize_history_date(value: str | None, default: str | None = None):
    text = str(value or default or datetime.now().strftime("%Y-%m-%d")).strip()[:10]
    if re.fullmatch(r"\d{4}-\d{2}$", text):
        text = text + "-01"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}$", text):
        text = datetime.now().strftime("%Y-%m-%d")
    return text


def shift_iso_date(value: str, days: int):
    base = datetime.strptime(normalize_history_date(value), "%Y-%m-%d")
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")


def build_daily_history(start: str, end: str, fred_key: str, bls_key: str | None = None, bls_series: str = ""):
    """Build daily historical rows.

    Market variables such as VIX, Brent, HY OAS, rates and S&P 500 are taken at
    daily frequency when the provider publishes them. Slower variables such as
    Core PCE, unemployment, Sahm Rule and GSCPI are carried forward from their
    latest official release so the daily row remains analytically complete.
    """
    start = normalize_history_date(start)
    end = normalize_history_date(end)
    extended_start = shift_iso_date(start, -430)

    daily = {}
    warnings = []

    def set_value(date_text, key, value):
        if value in (None, "", ".", "-"):
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        date_key = normalize_history_date(date_text)
        daily.setdefault(date_key, {"date": date_key, "_frequency": "daily"})
        daily[date_key][key] = round(numeric, 6)

    ten_year = {}
    two_year = {}

    for key, (series_id, _label) in FRED_SERIES.items():
        try:
            obs = fred_observations(series_id, fred_key, extended_start, end)
            if key == "tenYearYield":
                ten_year = {normalize_history_date(o["date"]): float(o["value"]) for o in obs if o.get("value") not in (None, "", ".", "-")}
                continue
            if key == "twoYearYield":
                two_year = {normalize_history_date(o["date"]): float(o["value"]) for o in obs if o.get("value") not in (None, "", ".", "-")}
                continue
            for item in obs:
                set_value(item.get("date"), key, item.get("value"))
        except Exception as error:
            warnings.append(f"{key} diario no se pudo cargar desde FRED {series_id}: {error}")

    for date_key in sorted(set(ten_year) & set(two_year)):
        set_value(date_key, "yieldCurve10y2y", ten_year[date_key] - two_year[date_key])

    try:
        gscpi_obs = fetch_gscpi_nyfed_history(extended_start, end)
        for item in gscpi_obs:
            set_value(item.get("date"), "gscpi", item.get("value"))
    except Exception as error:
        warnings.append(f"GSCPI diario/carry-forward no se pudo cargar desde NY Fed: {error}")
        try:
            for item in fred_observations("GSCPI", fred_key, extended_start, end):
                set_value(item.get("date"), "gscpi", item.get("value"))
        except Exception as fred_error:
            warnings.append(f"GSCPI fallback FRED/API no se pudo cargar: {fred_error}")

    try:
        core_history = fetch_fred_yoy_history("PCEPILFE", fred_key, shift_iso_date(start, -760), end)
        for item in core_history:
            set_value(item.get("date"), "coreInflation", item.get("value"))
    except Exception as error:
        warnings.append(f"Core PCE diario/carry-forward no se pudo cargar: {error}")

    try:
        sp500_obs = fetch_sp500_history(start, end, fred_key)
        for item in sp500_obs:
            set_value(item.get("date"), "sp500", item.get("value"))
    except Exception as error:
        warnings.append(f"S&P 500 diario no se pudo cargar: {error}")

    # Iterate all provider event dates, including the extended lookback, to seed
    # carry-forward values before the requested start date.
    all_dates = sorted(daily.keys())
    carry = {}
    rows = []
    carry_keys = [
        "brent", "creditSpreads", "vix", "usdStrength", "tenYearBreakeven", "realYield10y",
        "yieldCurve10y2y", "unemployment", "sahmRule", "smoothedRecessionProbability",
        "initialClaims", "gscpi", "coreInflation", "sp500"
    ]

    for date_key in all_dates:
        event_row = daily.get(date_key, {})
        for key in carry_keys:
            if key in event_row and event_row.get(key) not in (None, "", ".", "-"):
                carry[key] = event_row[key]

        if date_key < start or date_key > end:
            continue

        row = {"date": date_key, "_frequency": "daily"}
        for key in carry_keys:
            if key in carry:
                row[key] = carry[key]
        # Preserve exact same-day observations on top of carried values.
        row.update({k: v for k, v in event_row.items() if k not in ("date",)})
        row["_frequency"] = "daily"
        enrich_history_row(row)
        rows.append(row)

    rows.sort(key=lambda r: str(r.get("date", "")))
    if not rows:
        warnings.append("No se generaron filas diarias para el rango solicitado. Revisá FRED_API_KEY, fechas y disponibilidad de proveedores.")

    return rows, warnings


# -------------------------------------------------------------------------------------------
# LOGISTICS STRESS - MULTI-SOURCE NEWS ENGINE
# -------------------------------------------------------------------------------------------
# Optional environment variables for Render:
#   NEWSAPI_KEY
#   GUARDIAN_API_KEY
#   NYT_API_KEY
#
# If a key is missing, that provider is skipped automatically. GDELT and RSS do not need keys.
# This section avoids a fixed override: crisis terms push the score up, normalization terms push it
# down only when they describe operational normalization, not just political statements.

LOGISTICS_QUERIES = [
    {
        "name": "hormuz_crisis",
        "query": (
            '("Strait of Hormuz" OR Hormuz) '
            '(shipping OR tanker OR vessel OR ship OR maritime OR oil) '
            '(closed OR closure OR blocked OR blockade OR attack OR attacks OR seized OR captured '
            'OR fired OR missile OR drone OR mine OR mines OR suspended OR halted OR rerouting '
            'OR "war risk" OR "not allowed" OR "traffic halted" OR "shipping suspended" '
            'OR "tankers stuck" OR "ships stuck" OR "mariners stranded")'
        ),
    },
    {
        "name": "red_sea_crisis",
        "query": (
            '("Red Sea" OR "Bab el-Mandeb" OR Houthis OR Houthi) '
            '(shipping OR tanker OR vessel OR ship OR maritime) '
            '(attack OR attacks OR missile OR drone OR rerouting OR suspended OR halted OR insurance OR "war risk")'
        ),
    },
    {
        "name": "suez_shipping",
        "query": (
            '("Suez Canal" OR Suez OR "Cape of Good Hope") '
            '(shipping OR maritime OR container OR tanker OR freight) '
            '(rerouting OR diversion OR delays OR congestion OR suspended OR "freight rates" OR "shipping costs")'
        ),
    },
    {
        "name": "freight_insurance",
        "query": (
            '("freight rates" OR "shipping costs" OR "maritime insurance" OR "war risk insurance" '
            'OR "port congestion" OR "route diversion") '
            '(rising OR surge OR disruption OR crisis OR delays OR shipping)'
        ),
    },
    {
        "name": "normalization",
        "query": (
            '("Strait of Hormuz" OR Hormuz OR "Red Sea" OR Suez OR "shipping routes") '
            '("reopened" OR "re-opened" OR "fully open" OR "shipping resumes" OR "traffic resumes" '
            'OR "normal traffic" OR "agreement" OR "ceasefire" OR "deal" OR "blockade lifted" '
            'OR "routes restored" OR "insurance rates fall" OR "freight rates fall")'
        ),
    },
]

LOGISTICS_SEARCH_PHRASES = [
    ("hormuz_crisis", '"Strait of Hormuz" shipping attack tanker seized blocked suspended'),
    ("hormuz_traffic", '"Strait of Hormuz" traffic halted ships stuck tankers stuck mariners stranded'),
    ("hormuz_normalization", '"Strait of Hormuz" shipping resumes traffic resumes reopened blockade lifted'),
    ("red_sea_crisis", '"Red Sea" shipping attack missile drone rerouting suspended war risk'),
    ("suez_shipping", '"Suez Canal" shipping rerouting freight rates delays congestion'),
    ("freight_insurance", '"war risk insurance" maritime shipping freight rates disruption'),
    ("carrier_routes", 'Maersk Hapag-Lloyd MSC CMA CGM shipping route suspended resumed Hormuz Red Sea'),
    ("ukmto_incidents", 'UKMTO warning incident vessel attack Red Sea Gulf of Aden Strait of Hormuz'),
]

RSS_LOGISTICS_FEEDS = [
    ("google_news_hormuz", "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": '"Strait of Hormuz" shipping attack tanker seized blocked suspended when:14d',
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })),
    ("google_news_hormuz_normalization", "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": '"Strait of Hormuz" shipping resumes traffic resumes reopened blockade lifted when:14d',
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })),
    ("google_news_red_sea", "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": '"Red Sea" shipping attack rerouting suspended war risk when:14d',
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })),
    ("google_news_ukmto", "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": 'UKMTO warning vessel attack incident when:14d',
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })),
    ("google_news_freight", "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": '"war risk insurance" shipping "freight rates" maritime disruption when:14d',
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })),
]

TRUSTED_LOGISTICS_DOMAINS = {
    "reuters.com": 1.45,
    "apnews.com": 1.30,
    "bloomberg.com": 1.30,
    "ft.com": 1.30,
    "wsj.com": 1.25,
    "lloydslist.com": 1.35,
    "tradewindsnews.com": 1.25,
    "maritime-executive.com": 1.20,
    "gcaptain.com": 1.20,
    "splash247.com": 1.15,
    "ukmto.org": 1.35,
    "imo.org": 1.20,
    "portwatch.imf.org": 1.25,
    "aljazeera.com": 1.15,
    "abc.net.au": 1.15,
    "bbc.com": 1.10,
    "cnn.com": 1.05,
    "theguardian.com": 1.10,
    "nytimes.com": 1.10,
    "newsapi.org": 1.00,
    "news.google.com": 0.95,
}

CRISIS_TERMS = {
    # Cierre / bloqueo
    "closed": 18,
    "closure": 18,
    "blocked": 18,
    "blockade": 20,
    "not allowed": 18,
    "traffic halted": 20,
    "transit halted": 20,
    "shipping suspended": 20,
    "route suspended": 18,
    "effective standstill": 22,
    "standstill": 18,
    "traffic collapse": 22,
    "traffic collapsed": 22,
    "traffic down": 16,
    "traffic reduced": 15,

    # Ataques / capturas
    "attack": 14,
    "attacks": 14,
    "attacked": 14,
    "fired on": 16,
    "missile": 14,
    "drone": 12,
    "mine": 16,
    "mines": 16,
    "seized": 20,
    "captured": 20,
    "storming": 14,
    "gunboat": 14,
    "fast boat": 12,

    # Barcos/paralización operativa
    "ships stuck": 18,
    "tankers stuck": 18,
    "vessels stuck": 18,
    "mariners stranded": 18,
    "seafarers stranded": 18,
    "crew stranded": 14,
    "carriers suspend": 18,
    "suspend route": 18,
    "avoid the route": 14,
    "avoiding the route": 14,

    # Suspensión / desvío de rutas
    "suspended": 14,
    "halted": 14,
    "rerouting": 12,
    "rerouted": 12,
    "route diversion": 12,
    "diverted": 10,
    "avoid": 8,
    "avoiding": 8,

    # Costes / seguros
    "war risk": 12,
    "war-risk": 12,
    "insurance": 7,
    "freight rates": 8,
    "shipping costs": 8,
    "port congestion": 7,
    "premium": 5,
}

RELIEF_TERMS = {
    # Solo deben bajar de verdad si implican normalización operativa.
    "reopened": 18,
    "re-opened": 18,
    "fully open": 20,
    "shipping resumes": 18,
    "shipping resumed": 18,
    "traffic resumes": 18,
    "traffic resumed": 18,
    "normal traffic": 20,
    "traffic normal": 20,
    "normalizing": 14,
    "normalized": 16,
    "carriers resume": 18,
    "tankers resume": 18,
    "vessels resume": 18,
    "blockade lifted": 20,
    "routes restored": 20,
    "insurance rates fall": 14,
    "insurance premiums fall": 14,
    "freight rates fall": 12,
    "backlog cleared": 12,

    # Señales políticas: pesan menos y no bastan solas para bajar mucho.
    "agreement": 6,
    "ceasefire": 5,
    "deal": 5,
}

POLITICAL_RELIEF_ONLY_TERMS = {"agreement", "ceasefire", "deal"}

def provider_key(value: str):
    return str(value or "").strip().lower().replace(" ", "_")

def article_domain_from_url(url: str):
    try:
        domain = urllib.parse.urlparse(str(url or "")).netloc.lower()
        return domain.replace("www.", "")
    except Exception:
        return ""

def normalize_article(article: dict):
    url = article.get("url") or article.get("link") or ""
    domain = article.get("domain") or article_domain_from_url(url)
    return {
        "title": article.get("title", "") or "",
        "description": article.get("description", "") or article.get("summary", "") or "",
        "source": article.get("source", "") or domain,
        "domain": domain,
        "url": url,
        "seendate": article.get("seendate", "") or article.get("publishedAt", "") or article.get("pubDate", ""),
        "language": article.get("language", "") or "",
        "queryName": article.get("queryName", "") or "",
        "provider": article.get("provider", "") or "unknown",
    }

def parse_article_date(value: str):
    text = str(value or "").strip()

    if not text:
        return None

    # GDELT suele devolver seendate tipo 20260423123000.
    if re.fullmatch(r"\d{14}", text):
        try:
            return datetime.strptime(text[:8], "%Y%m%d")
        except ValueError:
            return None

    if re.fullmatch(r"\d{8}.*", text):
        try:
            return datetime.strptime(text[:8], "%Y%m%d")
        except ValueError:
            return None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            pass

    # ISO con hora.
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except Exception:
        pass

    # RSS pubDate.
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except Exception:
        return None

def parse_gdelt_seen_date(value: str):
    return parse_article_date(value)

def recency_weight(seendate: str):
    seen = parse_article_date(seendate)
    if not seen:
        return 0.70

    age_days = max(0, (datetime.utcnow() - seen).days)

    if age_days <= 1:
        return 1.30
    if age_days <= 3:
        return 1.15
    if age_days <= 7:
        return 0.95
    if age_days <= 14:
        return 0.70

    return 0.45

def source_weight(domain: str):
    domain = str(domain or "").lower().replace("www.", "")
    return TRUSTED_LOGISTICS_DOMAINS.get(domain, 1.0)

def provider_weight(provider: str):
    provider = provider_key(provider)
    weights = {
        "gdelt": 0.95,
        "newsapi": 1.10,
        "guardian": 1.00,
        "nyt": 1.00,
        "rss": 0.95,
        "ukmto_rss": 1.10,
        "market_proxy": 0.70,
    }
    return weights.get(provider, 1.0)

def term_score(text: str, terms: dict):
    total = 0
    hits = []

    for term, weight in terms.items():
        if term in text:
            total += weight
            hits.append(term)

    return total, hits

def days_ago_iso(days: int = 14):
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

def days_ago_compact(days: int = 14):
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y%m%d")

def fetch_gdelt_articles_for_query(query: str, query_name: str, max_records: int = 30, timespan: str = "14d"):
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": max_records,
        "sort": "hybridrel",
        "timespan": timespan,
    }

    url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(params)
    payload = fetch_json(url)

    articles = []

    for item in payload.get("articles", [])[:max_records]:
        articles.append(normalize_article({
            "title": item.get("title", ""),
            "source": item.get("sourceCountry", "") or item.get("domain", ""),
            "domain": item.get("domain", ""),
            "url": item.get("url", ""),
            "seendate": item.get("seendate", ""),
            "language": item.get("language", ""),
            "queryName": query_name,
            "provider": "gdelt",
        }))

    return articles

def fetch_gdelt_logistics_articles(max_records: int = 90):
    all_articles = []

    try:
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(
                    fetch_gdelt_articles_for_query,
                    item["query"],
                    item["name"],
                    30,
                    "14d"
                )
                for item in LOGISTICS_QUERIES
            ]

            for future in as_completed(futures):
                try:
                    all_articles.extend(future.result())
                except Exception:
                    pass

    except Exception:
        return []

    return dedupe_sort_articles(all_articles, max_records=max_records)

def fetch_newsapi_logistics_articles(max_records: int = 80):
    api_key = os.environ.get("NEWSAPI_KEY", "").strip()
    if not api_key:
        return []

    articles = []
    per_query = max(10, min(25, max_records // max(1, len(LOGISTICS_SEARCH_PHRASES))))

    for query_name, query in LOGISTICS_SEARCH_PHRASES:
        try:
            params = {
                "q": query,
                "language": "en",
                "sortBy": "publishedAt",
                "from": days_ago_iso(14),
                "pageSize": per_query,
                "apiKey": api_key,
            }
            url = "https://newsapi.org/v2/everything?" + urllib.parse.urlencode(params)
            payload = fetch_json(url)

            for item in payload.get("articles", [])[:per_query]:
                source = item.get("source") or {}
                articles.append(normalize_article({
                    "title": item.get("title", ""),
                    "description": item.get("description", "") or item.get("content", ""),
                    "source": source.get("name", ""),
                    "domain": article_domain_from_url(item.get("url", "")),
                    "url": item.get("url", ""),
                    "seendate": item.get("publishedAt", ""),
                    "language": "en",
                    "queryName": query_name,
                    "provider": "newsapi",
                }))
        except Exception:
            continue

    return dedupe_sort_articles(articles, max_records=max_records)

def fetch_guardian_logistics_articles(max_records: int = 60):
    api_key = os.environ.get("GUARDIAN_API_KEY", "").strip()
    if not api_key:
        return []

    articles = []
    per_query = max(8, min(20, max_records // max(1, len(LOGISTICS_SEARCH_PHRASES))))

    for query_name, query in LOGISTICS_SEARCH_PHRASES:
        try:
            # Guardian search works better with simpler strings.
            simple_query = re.sub(r'["()]', "", query)
            params = {
                "q": simple_query,
                "from-date": days_ago_iso(21),
                "order-by": "newest",
                "page-size": per_query,
                "show-fields": "trailText",
                "api-key": api_key,
            }
            url = "https://content.guardianapis.com/search?" + urllib.parse.urlencode(params)
            payload = fetch_json(url)
            response = payload.get("response") or {}

            for item in response.get("results", [])[:per_query]:
                fields = item.get("fields") or {}
                articles.append(normalize_article({
                    "title": item.get("webTitle", ""),
                    "description": fields.get("trailText", ""),
                    "source": "The Guardian",
                    "domain": "theguardian.com",
                    "url": item.get("webUrl", ""),
                    "seendate": item.get("webPublicationDate", ""),
                    "language": "en",
                    "queryName": query_name,
                    "provider": "guardian",
                }))
        except Exception:
            continue

    return dedupe_sort_articles(articles, max_records=max_records)

def fetch_nyt_logistics_articles(max_records: int = 50):
    api_key = os.environ.get("NYT_API_KEY", "").strip()
    if not api_key:
        return []

    articles = []
    per_query = max(5, min(10, max_records // max(1, len(LOGISTICS_SEARCH_PHRASES))))

    for query_name, query in LOGISTICS_SEARCH_PHRASES:
        try:
            simple_query = re.sub(r'["()]', "", query)
            params = {
                "q": simple_query,
                "begin_date": days_ago_compact(21),
                "sort": "newest",
                "api-key": api_key,
            }
            url = "https://api.nytimes.com/svc/search/v2/articlesearch.json?" + urllib.parse.urlencode(params)
            payload = fetch_json(url)
            docs = ((payload.get("response") or {}).get("docs") or [])[:per_query]

            for item in docs:
                headline = item.get("headline") or {}
                articles.append(normalize_article({
                    "title": headline.get("main", "") or item.get("abstract", ""),
                    "description": item.get("abstract", "") or item.get("lead_paragraph", ""),
                    "source": item.get("source", "") or "New York Times",
                    "domain": "nytimes.com",
                    "url": item.get("web_url", ""),
                    "seendate": item.get("pub_date", ""),
                    "language": "en",
                    "queryName": query_name,
                    "provider": "nyt",
                }))
        except Exception:
            continue

    return dedupe_sort_articles(articles, max_records=max_records)

def fetch_rss_feed_articles(feed_name: str, url: str, provider: str = "rss", max_records: int = 30):
    articles = []
    try:
        raw = fetch_binary(url, timeout=12)
        root = ET.fromstring(raw)
    except Exception:
        return articles

    for item in root.findall(".//item")[:max_records]:
        title_node = item.find("title")
        link_node = item.find("link")
        pub_node = item.find("pubDate")
        desc_node = item.find("description")
        source_node = item.find("source")

        title = title_node.text if title_node is not None else ""
        link = link_node.text if link_node is not None else ""
        pub_date = pub_node.text if pub_node is not None else ""
        description = desc_node.text if desc_node is not None else ""

        source_name = ""
        source_domain = ""
        if source_node is not None:
            source_name = source_node.text or ""
            source_domain = article_domain_from_url(source_node.attrib.get("url", ""))

        # Google News RSS often gives Google URLs, but the source tag contains the real publisher.
        domain = source_domain or article_domain_from_url(link)

        articles.append(normalize_article({
            "title": title,
            "description": re.sub(r"<[^>]+>", " ", description or ""),
            "source": source_name or domain or feed_name,
            "domain": domain,
            "url": link,
            "seendate": pub_date,
            "queryName": feed_name,
            "provider": provider,
        }))

    return articles

def fetch_rss_logistics_articles(max_records: int = 90):
    articles = []

    try:
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(
                    fetch_rss_feed_articles,
                    name,
                    url,
                    "ukmto_rss" if "ukmto" in name else "rss",
                    30,
                )
                for name, url in RSS_LOGISTICS_FEEDS
            ]

            for future in as_completed(futures):
                try:
                    articles.extend(future.result())
                except Exception:
                    pass
    except Exception:
        return []

    return dedupe_sort_articles(articles, max_records=max_records)

def fetch_ukmto_direct_articles(max_records: int = 20):
    # UKMTO does not expose a simple official JSON feed here. This is intentionally conservative:
    # it tries the public warnings page and extracts short warning-like snippets if accessible.
    # If the page markup changes or blocks access, it safely returns [].
    urls = [
        "https://www.ukmto.org/ukmto-products/warnings",
        "https://www.ukmto.org/ukmto-products/advisory",
    ]
    articles = []

    for url in urls:
        try:
            html = fetch_text(url, timeout=12)
            candidates = re.findall(r"<(?:h1|h2|h3|h4|a)[^>]*>(.*?)</(?:h1|h2|h3|h4|a)>", html, flags=re.I | re.S)
            for raw_title in candidates:
                title = re.sub(r"<[^>]+>", " ", raw_title)
                title = re.sub(r"\s+", " ", title).strip()
                lowered = title.lower()
                if len(title) < 12:
                    continue
                if any(term in lowered for term in ["warning", "advisory", "incident", "vessel", "attack", "suspicious", "red sea", "gulf of aden"]):
                    articles.append(normalize_article({
                        "title": title,
                        "description": "UKMTO public warnings/advisories page",
                        "source": "UKMTO",
                        "domain": "ukmto.org",
                        "url": url,
                        "seendate": datetime.utcnow().strftime("%Y-%m-%d"),
                        "queryName": "ukmto_direct",
                        "provider": "ukmto_rss",
                    }))
        except Exception:
            continue

    return dedupe_sort_articles(articles, max_records=max_records)

def dedupe_sort_articles(articles, max_records: int = 120):
    deduped = {}

    for raw_article in articles or []:
        article = normalize_article(raw_article)
        key = article.get("url") or article.get("title")
        key = str(key or "").strip().lower()

        if not key:
            continue

        # Prefer richer article version if duplicate.
        existing = deduped.get(key)
        if not existing or len(article.get("description", "")) > len(existing.get("description", "")):
            deduped[key] = article

    clean = list(deduped.values())
    clean.sort(
        key=lambda a: (
            recency_weight(a.get("seendate", "")) *
            source_weight(a.get("domain", "")) *
            provider_weight(a.get("provider", "")),
            a.get("seendate", "")
        ),
        reverse=True
    )

    return clean[:max_records]

def fetch_multi_source_logistics_articles(max_records: int = 180):
    providers = [
        ("gdelt", fetch_gdelt_logistics_articles, 90),
        ("newsapi", fetch_newsapi_logistics_articles, 80),
        ("guardian", fetch_guardian_logistics_articles, 60),
        ("nyt", fetch_nyt_logistics_articles, 50),
        ("rss", fetch_rss_logistics_articles, 90),
        ("ukmto_direct", fetch_ukmto_direct_articles, 20),
    ]

    all_articles = []
    provider_errors = {}
    provider_counts = {}

    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fn, limit): name
                for name, fn, limit in providers
            }

            for future in as_completed(futures):
                name = futures[future]
                try:
                    items = future.result() or []
                    provider_counts[name] = len(items)
                    all_articles.extend(items)
                except Exception as error:
                    provider_errors[name] = str(error)
                    provider_counts[name] = 0
    except Exception as error:
        provider_errors["multi_source"] = str(error)

    articles = dedupe_sort_articles(all_articles, max_records=max_records)
    return articles, provider_counts, provider_errors

def logistics_level(score):
    score = float(score)

    if score >= 80:
        return "severo"
    if score >= 60:
        return "alto"
    if score >= 40:
        return "moderado"
    if score >= 20:
        return "leve"

    return "bajo"


LOGISTICS_MAIN_SITUATION_RULES = [
    {
        "key": "hormuz",
        "label": "Tensión en el Estrecho de Ormuz",
        "summary": "el estrés se concentra en riesgos sobre petroleros, tráfico marítimo y prima de riesgo energética en una ruta crítica para el petróleo.",
        "query_names": {"hormuz_crisis", "hormuz_traffic", "hormuz_normalization"},
        "terms": ["hormuz", "strait of hormuz"],
    },
    {
        "key": "red_sea",
        "label": "Tensión en Mar Rojo/Bab el-Mandeb",
        "summary": "el estrés se concentra en ataques, amenazas a buques, desvíos de navieras o incremento del riesgo de asegurar esa ruta.",
        "query_names": {"red_sea_crisis", "ukmto_incidents"},
        "terms": ["red sea", "bab el-mandeb", "houthi", "houthis", "gulf of aden", "ukmto"],
    },
    {
        "key": "suez",
        "label": "Presión sobre la ruta Suez/Cabo de Buena Esperanza",
        "summary": "el estrés se concentra en desvíos, retrasos, congestión o mayor tiempo/coste de transporte en rutas comerciales clave.",
        "query_names": {"suez_shipping"},
        "terms": ["suez", "suez canal", "cape of good hope"],
    },
    {
        "key": "freight_insurance",
        "label": "Aumento de costes logísticos y seguros marítimos",
        "summary": "el estrés se concentra en fletes, seguros de guerra, primas marítimas, congestión portuaria o desvíos de rutas.",
        "query_names": {"freight_insurance", "carrier_routes"},
        "terms": ["war risk", "war-risk", "insurance", "freight rates", "shipping costs", "port congestion", "route diversion", "rerouting"],
    },
    {
        "key": "normalization",
        "label": "Normalización operativa de rutas",
        "summary": "las noticias apuntan a reapertura, tráfico retomándose, navieras volviendo a rutas o costes de seguro/flete bajando.",
        "query_names": {"normalization", "hormuz_normalization"},
        "terms": ["reopened", "re-opened", "fully open", "shipping resumes", "traffic resumes", "normal traffic", "routes restored", "blockade lifted", "insurance rates fall", "freight rates fall"],
    },
]


def infer_logistics_main_situation(articles, drivers=None, diagnostics=None, score=None, level=None):
    """Return a concise Spanish explanation of the dominant situation behind the logistics stress score."""
    diagnostics = diagnostics or {}
    drivers = drivers or []
    score = 0 if score is None else float(score)
    level = level or logistics_level(score)

    fallback = diagnostics.get("fallback")
    if fallback == "market_proxy":
        return "No hay una noticia logística dominante: el nivel se estima con proxy de mercado porque las fuentes de noticias fueron insuficientes."
    if fallback == "neutral_low_confidence":
        return "No hay una situación logística dominante clara: faltan noticias recientes suficientes y la lectura es de baja confianza."

    if diagnostics.get("operationalNormalizationArticles", 0) >= 3 and score <= 48:
        rule = next(r for r in LOGISTICS_MAIN_SITUATION_RULES if r["key"] == "normalization")
        return f"{rule['label']}: {rule['summary']}"

    weights = {rule["key"]: 0.0 for rule in LOGISTICS_MAIN_SITUATION_RULES}
    hit_terms = {rule["key"]: [] for rule in LOGISTICS_MAIN_SITUATION_RULES}

    for article in articles or []:
        title = str(article.get("title") or "")
        description = str(article.get("description") or "")
        query_name = str(article.get("queryName") or "")
        text = f"{title} {description} {query_name}".lower()
        article_weight = (
            recency_weight(article.get("seendate", ""))
            * source_weight(str(article.get("domain") or "").lower().replace("www.", ""))
            * provider_weight(article.get("provider", ""))
        )

        for rule in LOGISTICS_MAIN_SITUATION_RULES:
            term_hits = [term for term in rule["terms"] if term in text]
            query_hit = query_name in rule["query_names"]
            if term_hits or query_hit:
                weights[rule["key"]] += article_weight * (5 if query_hit else 0) + article_weight * 3 * len(term_hits)
                for term in term_hits:
                    if term not in hit_terms[rule["key"]] and len(hit_terms[rule["key"]]) < 4:
                        hit_terms[rule["key"]].append(term)

    driver_text = " ".join(str(d).lower() for d in drivers)
    for rule in LOGISTICS_MAIN_SITUATION_RULES:
        for term in rule["terms"]:
            if term in driver_text:
                weights[rule["key"]] += 4
                if term not in hit_terms[rule["key"]] and len(hit_terms[rule["key"]]) < 4:
                    hit_terms[rule["key"]].append(term)

    # When stress is high, avoid saying "normalization" is the main cause unless the score actually dropped.
    if score >= 60:
        weights["normalization"] *= 0.20

    top_key = max(weights, key=weights.get) if weights else None
    top_score = weights.get(top_key, 0) if top_key else 0

    if top_key and top_score > 0:
        rule = next(r for r in LOGISTICS_MAIN_SITUATION_RULES if r["key"] == top_key)
        signal_text = ""
        if hit_terms[top_key]:
            signal_text = " Señales detectadas: " + ", ".join(hit_terms[top_key][:4]) + "."
        return f"{rule['label']}: {rule['summary']}{signal_text}"

    if score >= 60:
        return "No hay un único foco dominante claro: el nivel alto parece venir de una combinación de titulares sobre disrupciones, desvíos, seguros o rutas marítimas."
    if level in ("bajo", "leve"):
        return "No hay una disrupción logística dominante: las señales recientes son dispersas o de baja intensidad."
    return "No hay una situación logística dominante clara: el score refleja una combinación de señales moderadas en noticias y mercado."

def market_logistics_proxy_score():
    # Used only as low-confidence fallback or tie-breaker when news sources are scarce.
    fred_key = os.environ.get("FRED_API_KEY", "").strip()
    components = []
    drivers = []

    if not fred_key:
        return None

    try:
        brent = fetch_brent_latest(fred_key)
        value = float(brent.get("latest", 0))
        points = clamp((value - 75) * 0.8, 0, 22)
        components.append(points)
        if value >= 95:
            drivers.append("Brent alto")
    except Exception:
        pass

    try:
        vix = fetch_fred_latest("VIXCLS", fred_key)
        value = float(vix.get("latest", 0))
        points = clamp((value - 16) * 0.9, 0, 18)
        components.append(points)
        if value >= 25:
            drivers.append("VIX elevado")
    except Exception:
        pass

    try:
        credit = fetch_fred_latest("BAMLH0A0HYM2", fred_key)
        value = float(credit.get("latest", 0))
        points = clamp((value - 3.0) * 6, 0, 18)
        components.append(points)
        if value >= 5:
            drivers.append("spreads HY elevados")
    except Exception:
        pass

    try:
        gscpi = fetch_gscpi_latest()
        value = float(gscpi.get("latest", 0))
        points = clamp((value + 0.25) * 12, 0, 18)
        components.append(points)
        if value >= 1:
            drivers.append("GSCPI elevado")
    except Exception:
        pass

    if not components:
        return None

    score = 25 + sum(components)
    score = round(clamp(score, 20, 75))

    return {
        "score": score,
        "level": logistics_level(score),
        "confidence": 0.18,
        "drivers": drivers or ["proxy mercado"],
        "summary": "Proxy de mercado usado con baja confianza porque las fuentes de noticias fueron insuficientes.",
    }

def heuristic_logistics_score(articles, provider_counts=None, provider_errors=None, market_proxy=None):
    provider_counts = provider_counts or {}
    provider_errors = provider_errors or {}

    if not articles:
        if market_proxy:
            return {
                "score": market_proxy["score"],
                "level": market_proxy["level"],
                "confidence": market_proxy["confidence"],
                "summary": market_proxy["summary"],
                "mainSituation": "No hay una noticia logística dominante: el nivel se estima con proxy de mercado porque las fuentes de noticias fueron insuficientes.",
                "drivers": market_proxy["drivers"] + ["sin noticias multi-fuente"],
                "diagnostics": {
                    "articleCount": 0,
                    "sourceDiversity": 0,
                    "providerDiversity": 0,
                    "crisisArticles": 0,
                    "severeArticles": 0,
                    "reliefArticles": 0,
                    "weightedCrisis": 0,
                    "weightedRelief": 0,
                    "sampleTitles": [],
                    "providerCounts": provider_counts,
                    "providerErrors": provider_errors,
                    "fallback": "market_proxy",
                }
            }

        return {
            "score": 45,
            "level": "moderado",
            "confidence": 0.06,
            "summary": "No se pudieron obtener noticias recientes de ninguna fuente; el valor es neutral-bajo y no debe interpretarse como lectura real de riesgo.",
            "mainSituation": "No hay una situación logística dominante clara: faltan noticias recientes suficientes y la lectura es de baja confianza.",
            "drivers": ["sin datos multi-fuente"],
            "diagnostics": {
                "articleCount": 0,
                "sourceDiversity": 0,
                "providerDiversity": 0,
                "crisisArticles": 0,
                "severeArticles": 0,
                "reliefArticles": 0,
                "weightedCrisis": 0,
                "weightedRelief": 0,
                "sampleTitles": [],
                "providerCounts": provider_counts,
                "providerErrors": provider_errors,
                "fallback": "neutral_low_confidence",
            }
        }

    weighted_crisis = 0
    weighted_relief = 0
    crisis_articles = 0
    severe_articles = 0
    relief_articles = 0
    political_relief_only_articles = 0
    hormuz_crisis_articles = 0
    operational_normalization_articles = 0
    domains = set()
    providers = set()
    drivers = []
    sample_titles = []

    for article in articles:
        title = str(article.get("title") or "")
        description = str(article.get("description") or "")
        domain = str(article.get("domain") or "").lower().replace("www.", "")
        provider = str(article.get("provider") or "")
        query_name = str(article.get("queryName") or "")
        text = f"{title} {description}".lower()

        domains.add(domain)
        providers.add(provider)

        crisis_points, crisis_hits = term_score(text, CRISIS_TERMS)
        relief_points, relief_hits = term_score(text, RELIEF_TERMS)

        relief_hit_set = set(relief_hits)
        has_operational_relief = bool(relief_hit_set - POLITICAL_RELIEF_ONLY_TERMS)

        # A ceasefire or agreement alone should not reduce logistics stress much unless it is
        # accompanied by operational evidence: traffic resumes, carriers resume, routes restored, etc.
        if relief_hit_set and not has_operational_relief:
            political_relief_only_articles += 1
            relief_points *= 0.25

        # If the same article mentions attacks/standstill and ceasefire, treat it as unresolved crisis.
        if crisis_points >= 12:
            relief_points *= 0.30

        # Specific bonus for Hormuz because it is a critical energy chokepoint.
        if "hormuz" in text or "strait of hormuz" in text:
            if crisis_points >= 8:
                crisis_points += 16
                hormuz_crisis_articles += 1
            if has_operational_relief and relief_points >= 10 and crisis_points < 8:
                relief_points += 10

        # Specific bonus for UKMTO incident items.
        if "ukmto" in text or domain == "ukmto.org" or provider_key(provider).startswith("ukmto"):
            if crisis_points >= 8:
                crisis_points += 8

        if query_name == "normalization" and has_operational_relief and relief_points >= 10 and crisis_points < 8:
            relief_points += 6

        if has_operational_relief and crisis_points < 8:
            operational_normalization_articles += 1

        article_weight = (
            recency_weight(article.get("seendate", "")) *
            source_weight(domain) *
            provider_weight(provider)
        )

        weighted_crisis += crisis_points * article_weight
        weighted_relief += relief_points * article_weight

        if crisis_points >= 8:
            crisis_articles += 1

        if crisis_points >= 24:
            severe_articles += 1

        if relief_points >= 10 and crisis_points < 8:
            relief_articles += 1

        for hit in crisis_hits + relief_hits:
            if hit not in drivers and len(drivers) < 14:
                drivers.append(hit)

        if len(sample_titles) < 8 and title:
            sample_titles.append(title)

    article_count = len(articles)
    source_diversity = len([d for d in domains if d])
    provider_diversity = len([p for p in providers if p])

    coverage_bonus = min(14, article_count * 0.16)
    diversity_bonus = min(12, source_diversity * 1.0)
    provider_bonus = min(8, provider_diversity * 1.6)

    net_pressure = weighted_crisis - weighted_relief
    score = 24 + net_pressure * 0.40 + coverage_bonus + diversity_bonus + provider_bonus

    # Strong floors for severe operational disruption.
    if severe_articles >= 3:
        score = max(score, 82)

    if severe_articles >= 5:
        score = max(score, 88)

    if hormuz_crisis_articles >= 2 and severe_articles >= 2:
        score = max(score, 86)

    if hormuz_crisis_articles >= 4:
        score = max(score, 90)

    # Market proxy should only support the score, not dominate actual news.
    if market_proxy and article_count < 10:
        score = max(score, market_proxy["score"] * 0.85)

    # Clear operational normalization lowers score. Political relief alone does not.
    if operational_normalization_articles >= 3 and severe_articles <= 1 and hormuz_crisis_articles <= 1:
        score = min(score, 48)

    if operational_normalization_articles >= 5 and crisis_articles <= 2:
        score = min(score, 38)

    if weighted_relief > weighted_crisis * 1.45 and severe_articles == 0 and operational_normalization_articles >= 2:
        score = min(score, 32)

    score = round(clamp(score))

    confidence = 0.18
    confidence += min(0.30, article_count / 150)
    confidence += min(0.22, source_diversity / 35)
    confidence += min(0.18, provider_diversity / 10)
    if severe_articles >= 3 or operational_normalization_articles >= 3:
        confidence += 0.10
    if provider_errors and provider_diversity <= 1:
        confidence -= 0.08
    confidence = round(max(0.05, min(confidence, 0.94)), 2)

    if score >= 80:
        summary = "Disrupción logística severa detectada por múltiples fuentes: cierres, ataques, capturas, suspensión de rutas, tráfico paralizado o desvíos relevantes."
    elif score >= 60:
        summary = "Estrés logístico alto: hay señales relevantes de ataques, restricciones, seguros marítimos, desvíos o tensión en rutas clave."
    elif score >= 40:
        summary = "Estrés logístico moderado: hay tensión o costes logísticos, pero sin disrupción global claramente dominante."
    elif score >= 20:
        summary = "Estrés logístico leve: ruido geopolítico u operativo, pero con señales limitadas de disrupción."
    else:
        summary = "Estrés logístico bajo: predominan señales de rutas abiertas, normalización operativa o ausencia de eventos críticos."

    if political_relief_only_articles >= 2 and operational_normalization_articles == 0 and score >= 60:
        summary += " Hay señales políticas de alivio, pero no bastan para bajar el score porque no confirman normalización operativa."

    if operational_normalization_articles >= 3 and score <= 48:
        summary = "Las fuentes recientes sugieren normalización operativa: reapertura, tráfico retomándose, navieras volviendo a rutas o costes de seguro/flete bajando."

    diagnostics = {
        "articleCount": article_count,
        "sourceDiversity": source_diversity,
        "providerDiversity": provider_diversity,
        "crisisArticles": crisis_articles,
        "severeArticles": severe_articles,
        "hormuzCrisisArticles": hormuz_crisis_articles,
        "reliefArticles": relief_articles,
        "operationalNormalizationArticles": operational_normalization_articles,
        "politicalReliefOnlyArticles": political_relief_only_articles,
        "weightedCrisis": round(weighted_crisis, 2),
        "weightedRelief": round(weighted_relief, 2),
        "sampleTitles": sample_titles,
        "providerCounts": provider_counts,
        "providerErrors": provider_errors,
        "fallback": None,
    }
    clean_drivers = drivers or ["sin drivers críticos detectados"]

    return {
        "score": score,
        "level": logistics_level(score),
        "confidence": confidence,
        "summary": summary,
        "mainSituation": infer_logistics_main_situation(
            articles,
            drivers=clean_drivers,
            diagnostics=diagnostics,
            score=score,
            level=logistics_level(score),
        ),
        "drivers": clean_drivers,
        "diagnostics": diagnostics,
    }

def ai_logistics_score(articles):
    # Versión gratuita: sin OpenAI API.
    # Se mantiene esta función como stub para conservar estructura.
    return None

def assess_logistics_stress_from_news():
    articles, provider_counts, provider_errors = fetch_multi_source_logistics_articles(180)
    market_proxy = market_logistics_proxy_score()

    heuristic = heuristic_logistics_score(
        articles,
        provider_counts=provider_counts,
        provider_errors=provider_errors,
        market_proxy=market_proxy,
    )

    ai_result = None
    try:
        ai_result = ai_logistics_score(articles)
    except Exception:
        ai_result = None

    result = ai_result or heuristic
    source = "Multi-source logistics news engine"
    if result.get("diagnostics", {}).get("fallback") == "market_proxy":
        source = "Market proxy fallback + unavailable news sources"

    return {
        "latest": result["score"],
        "previous": None,
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "source": source,
        "level": result["level"],
        "confidence": result["confidence"],
        "summary": result["summary"],
        "mainSituation": result.get("mainSituation") or infer_logistics_main_situation(
            articles,
            drivers=result.get("drivers", []),
            diagnostics=result.get("diagnostics", {}),
            score=result.get("score", result.get("latest", 0)),
            level=result.get("level"),
        ),
        "drivers": result["drivers"],
        "articleCount": len(articles),
        "sampleArticles": articles[:12],
        "diagnostics": result.get("diagnostics", {}),
    }


SPGLOBAL_PMI_HOME_URL = "https://www.pmi.spglobal.com/"
SPGLOBAL_PMI_RELEASES_URL = "https://www.pmi.spglobal.com/public/release/pressreleases"

PMI_MONTHS = {
    "jan": "01", "january": "01", "ene": "01", "enero": "01",
    "feb": "02", "february": "02", "febrero": "02",
    "mar": "03", "march": "03", "marzo": "03",
    "apr": "04", "april": "04", "abr": "04", "abril": "04",
    "may": "05", "mayo": "05",
    "jun": "06", "june": "06", "junio": "06",
    "jul": "07", "july": "07", "julio": "07",
    "aug": "08", "august": "08", "ago": "08", "agosto": "08",
    "sep": "09", "sept": "09", "september": "09", "septiembre": "09",
    "oct": "10", "october": "10", "octubre": "10",
    "nov": "11", "november": "11", "noviembre": "11",
    "dec": "12", "december": "12", "dic": "12", "diciembre": "12",
}


def clean_html_text(raw: str):
    """Convert a small HTML document into compact searchable text."""
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw or "", flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def month_start_from_name(month_name: str, reference_date: datetime | None = None):
    reference_date = reference_date or datetime.utcnow()
    key = str(month_name or "").strip().lower().rstrip(".")
    month = PMI_MONTHS.get(key[:3]) or PMI_MONTHS.get(key)
    if not month:
        return reference_date.strftime("%Y-%m-%d")

    # Releases are usually published at the beginning of the following month.
    year = reference_date.year
    current_month = reference_date.month
    month_number = int(month)
    if month_number > current_month + 1:
        year -= 1

    return f"{year}-{month}-01"


def extract_official_global_pmi_candidates(text: str):
    """Extract PMI candidates from official S&P/J.P. Morgan text.

    Conservative by design: if the official value cannot be parsed cleanly, the
    dashboard falls back to news/proxy instead of showing a high-confidence wrong value.
    """
    compact = re.sub(r"\s+", " ", text or " ").strip()
    candidates = []

    patterns = [
        ("spglobal_home_card", r"Global\s+COMPOSITE\s+OUTPUT\s+PMI\s+([A-Za-z]{3,9})\s*:?\s*(\d{1,2}(?:\.\d)?)"),
        ("spglobal_home_card", r"Global\s+Composite\s+Output\s+PMI\s+([A-Za-z]{3,9})\s*:?\s*(\d{1,2}(?:\.\d)?)"),
        ("official_release", r"J\.?P\.?\s*Morgan\s+Global\s+Composite\s+PMI\s+Output\s+Index.{0,220}?(?:rose|rises|increased|fell|falls|declined|dropped|eased|slipped|posted|registered|came\s+in|was|stood)\s+(?:to|at)?\s*(\d{1,2}(?:\.\d)?)"),
        ("official_release", r"Global\s+Composite\s+PMI\s+Output\s+Index.{0,220}?(?:rose|rises|increased|fell|falls|declined|dropped|eased|slipped|posted|registered|came\s+in|was|stood)\s+(?:to|at)?\s*(\d{1,2}(?:\.\d)?)"),
        ("official_release", r"Global\s+Composite\s+PMI.{0,160}?(?:rose|rises|increased|fell|falls|declined|dropped|eased|slipped|posted|registered|came\s+in|was|stood)\s+(?:to|at)?\s*(\d{1,2}(?:\.\d)?)"),
    ]

    for kind, pattern in patterns:
        for match in re.finditer(pattern, compact, flags=re.IGNORECASE):
            groups = match.groups()
            if len(groups) == 2:
                month_name, value_text = groups
            else:
                month_name, value_text = None, groups[0]

            try:
                value = float(value_text)
            except (TypeError, ValueError):
                continue

            if 30 <= value <= 70:
                start = max(0, match.start() - 120)
                end = min(len(compact), match.end() + 160)
                candidates.append({
                    "value": round(value, 1),
                    "month": month_name,
                    "date": month_start_from_name(month_name) if month_name else datetime.utcnow().strftime("%Y-%m-%d"),
                    "kind": kind,
                    "context": compact[start:end],
                })

    return candidates


def fetch_spglobal_global_pmi_homepage():
    html_raw = fetch_text(SPGLOBAL_PMI_HOME_URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": SPGLOBAL_PMI_HOME_URL,
    }, timeout=18)

    text = clean_html_text(html_raw)
    candidates = extract_official_global_pmi_candidates(text)
    home_candidates = [c for c in candidates if c.get("kind") == "spglobal_home_card"] or candidates

    if not home_candidates:
        raise ValueError("No se pudo extraer Global Composite Output PMI desde la homepage oficial de S&P Global PMI")

    chosen = home_candidates[0]
    return {
        "latest": chosen["value"],
        "previous": None,
        "date": chosen.get("date") or datetime.utcnow().strftime("%Y-%m-%d"),
        "source": "S&P Global PMI official homepage",
        "confidence": 0.88,
        "summary": f"PMI global extraído desde la web oficial de S&P Global PMI: {chosen.get('month') or 'último dato'} {chosen['value']}.",
        "articleCount": 1,
        "sampleArticles": [{
            "title": f"Global Composite Output PMI {chosen.get('month') or ''}: {chosen['value']}",
            "domain": "pmi.spglobal.com",
            "url": SPGLOBAL_PMI_HOME_URL,
            "provider": "spglobal_official",
        }],
        "diagnostics": {"officialSource": "homepage", "context": chosen.get("context")},
    }


def discover_spglobal_global_pmi_release_urls(max_urls: int = 4):
    html_raw = fetch_text(SPGLOBAL_PMI_RELEASES_URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": SPGLOBAL_PMI_HOME_URL,
    }, timeout=18)

    urls = []
    for match in re.finditer(r'href=["\']([^"\']+)["\']', html_raw, flags=re.IGNORECASE):
        href = html.unescape(match.group(1))
        start = max(0, match.start() - 600)
        end = min(len(html_raw), match.end() + 900)
        window = clean_html_text(html_raw[start:end]).lower()
        href_low = href.lower()

        if (
            ("pressrelease" in href_low or "release" in href_low)
            and ("jpmorgan" in window or "j.p. morgan" in window or "global composite pmi" in window)
        ):
            full_url = urllib.parse.urljoin(SPGLOBAL_PMI_RELEASES_URL, href)
            if full_url not in urls:
                urls.append(full_url)

        if len(urls) >= max_urls:
            break

    return urls


def fetch_spglobal_global_pmi_release():
    urls = discover_spglobal_global_pmi_release_urls(5)
    errors = []

    for url in urls:
        try:
            raw = fetch_text(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain,*/*;q=0.8",
                "Referer": SPGLOBAL_PMI_RELEASES_URL,
            }, timeout=18)
            text = clean_html_text(raw)
            candidates = extract_official_global_pmi_candidates(text)
            if candidates:
                chosen = candidates[0]
                return {
                    "latest": chosen["value"],
                    "previous": None,
                    "date": chosen.get("date") or datetime.utcnow().strftime("%Y-%m-%d"),
                    "source": "S&P Global / J.P. Morgan official PMI release",
                    "confidence": 0.92,
                    "summary": f"PMI global extraído desde release oficial S&P Global/J.P. Morgan: {chosen['value']}.",
                    "articleCount": 1,
                    "sampleArticles": [{
                        "title": f"J.P. Morgan Global Composite PMI official release: {chosen['value']}",
                        "domain": "pmi.spglobal.com",
                        "url": url,
                        "provider": "spglobal_official_release",
                    }],
                    "diagnostics": {"officialSource": "press_release", "url": url, "context": chosen.get("context")},
                }
        except Exception as error:
            errors.append(f"{url}: {error}")

    raise ValueError("No se pudo extraer Global Composite PMI desde releases oficiales. " + " | ".join(errors[-3:]))


def reconcile_official_pmi(home_result: dict | None, release_result: dict | None):
    if home_result and release_result:
        home_value = home_result.get("latest")
        release_value = release_result.get("latest")
        if home_value is not None and release_value is not None and abs(float(home_value) - float(release_value)) <= 0.15:
            merged = dict(home_result)
            merged["confidence"] = 0.95
            merged["source"] = "S&P Global PMI official homepage + official release"
            merged["summary"] = f"PMI global confirmado por homepage oficial y release S&P Global/J.P. Morgan: {round(float(home_value), 1)}."
            merged["sampleArticles"] = (home_result.get("sampleArticles") or []) + (release_result.get("sampleArticles") or [])
            merged["diagnostics"] = {
                "officialSource": "homepage_and_release",
                "homepage": home_result.get("diagnostics", {}),
                "release": release_result.get("diagnostics", {}),
            }
            return merged

        chosen = dict(release_result)
        chosen["confidence"] = 0.85
        chosen["summary"] = (
            f"PMI global tomado del release oficial S&P Global/J.P. Morgan ({release_value}); "
            f"la homepage devolvió {home_value}, por eso se reduce la confianza."
        )
        chosen["diagnostics"] = {
            "officialSource": "release_preferred_due_to_mismatch",
            "homepageLatest": home_value,
            "releaseLatest": release_value,
            "homepage": home_result.get("diagnostics", {}),
            "release": release_result.get("diagnostics", {}),
        }
        return chosen

    return home_result or release_result


def fetch_official_global_pmi():
    errors = []
    home_result = None
    release_result = None

    try:
        home_result = fetch_spglobal_global_pmi_homepage()
    except Exception as error:
        errors.append(f"S&P Global homepage: {error}")

    try:
        release_result = fetch_spglobal_global_pmi_release()
    except Exception as error:
        errors.append(f"S&P Global/J.P. Morgan release: {error}")

    result = reconcile_official_pmi(home_result, release_result)
    if result:
        if errors:
            result.setdefault("providerErrors", errors)
        return result

    raise ValueError("; ".join(errors) or "No se pudo extraer PMI global oficial")


def fetch_gdelt_pmi_articles(max_records: int = 20):
    try:
        params = {
            "query": PMI_QUERY,
            "mode": "artlist",
            "format": "json",
            "maxrecords": max_records,
            "sort": "hybridrel",
            "timespan": "60d",
        }

        url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(params)
        payload = fetch_json(url)

        articles = []
        for item in payload.get("articles", [])[:max_records]:
            articles.append({
                "title": item.get("title", ""),
                "source": item.get("sourceCountry", "") or item.get("domain", ""),
                "domain": item.get("domain", ""),
                "url": item.get("url", ""),
                "seendate": item.get("seendate", ""),
                "language": item.get("language", ""),
                "provider": "gdelt",
            })

        return articles
    except Exception:
        return []


def extract_pmi_value_from_text(text: str):
    patterns = [
        r'(?:J\.?P\.?\s?Morgan|JPMorgan).*?(?:Global Composite PMI|Global PMI|Composite PMI).*?(?:rose to|rises to|increased to|up to|fell to|falls to|declined to|down to|at|was|posted|registered|came in at)\s+(\d{1,2}(?:\.\d)?)',
        r'(?:Global Composite PMI|Global PMI|Composite PMI).*?(?:rose to|rises to|increased to|up to|fell to|falls to|declined to|down to|at|was|posted|registered|came in at)\s+(\d{1,2}(?:\.\d)?)',
        r'(?:rose to|rises to|increased to|up to|fell to|falls to|declined to|down to|posted|registered|came in at)\s+(\d{1,2}(?:\.\d)?).*?(?:Global Composite PMI|Global PMI|Composite PMI)',
        r'\b(\d{1,2}(?:\.\d)?)\b.*?(?:Global Composite PMI|Global PMI|Composite PMI)',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            if 30 <= value <= 70:
                return round(value, 1)

    return None


def assess_global_pmi_from_news():
    official_errors = []

    try:
        return fetch_official_global_pmi()
    except Exception as error:
        official_errors.append(str(error))

    articles = fetch_gdelt_pmi_articles(20)

    candidates = []
    for article in articles:
        title = article.get("title", "") or ""
        value = extract_pmi_value_from_text(title)
        if value is not None:
            candidates.append({
                "value": value,
                "title": title,
                "domain": article.get("domain", ""),
                "url": article.get("url", ""),
                "seendate": article.get("seendate", ""),
            })

    if not candidates:
        return {
            "latest": None,
            "previous": None,
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "source": "Official PMI parser + GDELT PMI news parser",
            "confidence": 0.0,
            "summary": "No se pudo extraer automáticamente un valor oficial o fiable de JPMorgan Global Composite PMI; se usará proxy macro si el endpoint principal puede calcularlo.",
            "articleCount": len(articles),
            "sampleArticles": articles[:6],
            "providerErrors": official_errors,
            "diagnostics": {"officialErrors": official_errors, "gdeltPmiArticles": len(articles)},
        }

    chosen = candidates[0]

    return {
        "latest": chosen["value"],
        "previous": None,
        "date": normalize_provider_date(chosen.get("seendate", "")),
        "source": "GDELT PMI news parser",
        "confidence": 0.55,
        "summary": f"PMI global extraído desde noticia/titular: {chosen['title']}",
        "articleCount": len(articles),
        "sampleArticles": articles[:6],
        "matchedArticle": chosen,
        "providerErrors": official_errors,
        "diagnostics": {"officialErrors": official_errors, "gdeltPmiArticles": len(articles)},
    }
def fetch_gdelt_macro_articles(max_records: int = 30):
    try:
        params = {
            "query": MACRO_RECESSION_QUERY,
            "mode": "artlist",
            "format": "json",
            "maxrecords": max_records,
            "sort": "hybridrel",
            "timespan": "14d",
        }

        url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(params)
        payload = fetch_json(url)

        articles = []
        for item in payload.get("articles", [])[:max_records]:
            articles.append({
                "title": item.get("title", ""),
                "source": item.get("sourceCountry", "") or item.get("domain", ""),
                "domain": item.get("domain", ""),
                "url": item.get("url", ""),
                "seendate": item.get("seendate", ""),
                "language": item.get("language", ""),
            })

        return articles
    except Exception:
        return []

def assess_macro_news_recession_score():
    articles = fetch_gdelt_macro_articles(30)
    text = " ".join((a.get("title") or "").lower() for a in articles)

    severe_terms = [
        "recession", "hard landing", "credit crunch", "default risk", "bank stress",
        "unemployment rising", "layoffs", "contraction", "global slowdown",
        "growth forecast cut", "stagflation", "yield curve inversion",
    ]

    moderate_terms = [
        "slowdown", "weak demand", "soft landing", "inflation pressure",
        "higher for longer", "credit stress", "manufacturing weakness",
        "consumer weakness", "services weakness", "pmi falls", "demand cools",
    ]

    positive_terms = [
        "recovery", "resilient growth", "soft landing", "inflation cools",
        "growth improves", "demand rebounds", "pmi rises", "jobs growth",
    ]

    severe_hits = sum(text.count(term) for term in severe_terms)
    moderate_hits = sum(text.count(term) for term in moderate_terms)
    positive_hits = sum(text.count(term) for term in positive_terms)

    score = 30 + severe_hits * 8 + moderate_hits * 4 - positive_hits * 3 + min(len(articles), 30) * 0.4
    score = max(0, min(100, round(score)))

    if score >= 70:
        level = "alto"
    elif score >= 45:
        level = "moderado"
    elif score >= 25:
        level = "leve"
    else:
        level = "bajo"

    drivers = []
    for term in severe_terms + moderate_terms:
        if term in text and len(drivers) < 8:
            drivers.append(term)

    return {
        "latest": score,
        "previous": None,
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "source": "GDELT macro news heuristic",
        "level": level,
        "confidence": 0.45,
        "summary": "Score heurístico basado en titulares macro recientes sobre recesión, desaceleración, crédito, empleo, demanda y crecimiento.",
        "drivers": drivers or ["sin drivers macro críticos detectados"],
        "articleCount": len(articles),
        "sampleArticles": articles[:6],
    }



# -----------------------------------------------------------------------------
# Dominancia monetaria global
# -----------------------------------------------------------------------------
# Fuentes oficiales/primarias previstas:
# - IMF COFER: composición global de reservas oficiales por moneda (trimestral).
# - BIS Triennial Survey: uso por moneda en mercado FX global (cada 3 años).
# - SWIFT Global Currency Tracker/RMB Tracker: pagos internacionales (mensual, vía informes).
# - World Gold Council / IMF IFS: reservas oficiales de oro.
#
# Nota técnica: algunas fuentes públicas no ofrecen una API simple sin login/clave
# o cambian formato. Para que la pestaña funcione siempre en producción, el endpoint
# usa una capa de serie base/fallback documentada, y marca la calidad como
# "official-public-plus-fallback". Cuando una integración directa devuelva datos
# limpios, se puede reemplazar cada bloque sin tocar el frontend.

CURRENCY_KEYS = ["usd", "eur", "cny", "jpy", "gbp", "chf", "aud", "cad", "other"]
CURRENCY_LABELS = {
    "usd": "USD",
    "eur": "EUR",
    "cny": "CNY",
    "jpy": "JPY",
    "gbp": "GBP",
    "chf": "CHF",
    "aud": "AUD",
    "cad": "CAD",
    "other": "Otras",
}

CURRENCY_DOMINANCE_WEIGHTS = {
    "reserves": 0.40,
    "payments": 0.25,
    "fx": 0.20,
    "trade": 0.10,
    "debt": 0.05,
}

CURRENCY_DOMINANCE_CACHE = {"key": None, "created_at": None, "payload": None}
CURRENCY_DOMINANCE_EARLIEST_DATE = "1999-01-01"
CURRENCY_DOMINANCE_EARLIEST_MONTH = "1999-01"
CURRENCY_DOMINANCE_AVAILABILITY_NOTE = (
    "La serie comparable de dominancia monetaria empieza en 1999-01. "
    "Aunque existen datos parciales anteriores de reservas/FX, el score de esta pestaña compara USD, EUR, CNY, JPY, GBP y otras monedas bajo una estructura moderna; antes de 1999 el euro no existía como moneda única y varias dimensiones no tienen cobertura homogénea."
)



def quarter_key_from_date(value: str):
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-Q[1-4]", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}", text):
        year = int(text[:4])
        month = int(text[5:7])
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]):
        year = int(text[:4])
        month = int(text[5:7])
    else:
        today = datetime.utcnow()
        year = today.year
        month = today.month
    q = ((month - 1) // 3) + 1
    return f"{year}-Q{q}"


def quarter_to_index(qkey: str):
    match = re.fullmatch(r"(\d{4})-Q([1-4])", str(qkey or ""))
    if not match:
        qkey = quarter_key_from_date(str(qkey or ""))
        match = re.fullmatch(r"(\d{4})-Q([1-4])", qkey)
    year = int(match.group(1))
    quarter = int(match.group(2))
    return year * 4 + quarter - 1


def index_to_quarter(idx: int):
    year = idx // 4
    quarter = idx % 4 + 1
    return f"{year}-Q{quarter}"


def quarter_mid_year(qkey: str):
    idx = quarter_to_index(qkey)
    year = idx // 4
    quarter = idx % 4 + 1
    return year + (quarter - 0.5) / 4


def quarter_to_month(qkey: str):
    match = re.fullmatch(r"(\d{4})-Q([1-4])", qkey)
    if not match:
        return str(qkey or "")[:7]
    year = int(match.group(1))
    quarter = int(match.group(2))
    month = quarter * 3
    return f"{year}-{month:02d}"


def quarterly_range(start: str, end: str):
    start_idx = quarter_to_index(quarter_key_from_date(start))
    end_idx = quarter_to_index(quarter_key_from_date(end))
    if end_idx < start_idx:
        start_idx, end_idx = end_idx, start_idx
    return [index_to_quarter(idx) for idx in range(start_idx, end_idx + 1)]


def clamp_currency_dominance_range(start: str, end: str):
    """Clamp requested range to the first comparable period supported by this model."""
    requested_start = str(start or CURRENCY_DOMINANCE_EARLIEST_DATE)[:10]
    requested_end = str(end or datetime.now().strftime("%Y-%m-%d"))[:10]
    clamped_start = max(requested_start, CURRENCY_DOMINANCE_EARLIEST_DATE)
    if requested_end < clamped_start:
        requested_end = clamped_start
    was_clamped = clamped_start != requested_start
    return clamped_start, requested_end, was_clamped


def interpolate_anchor(anchors, year_fraction: float):
    points = sorted((float(year), float(value)) for year, value in anchors)
    if not points:
        return 0.0
    if year_fraction <= points[0][0]:
        return points[0][1]
    if year_fraction >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= year_fraction <= x1:
            if x1 == x0:
                return y1
            t = (year_fraction - x0) / (x1 - x0)
            return y0 + (y1 - y0) * t
    return points[-1][1]


def normalize_share_map(values: dict, keys=None):
    keys = keys or CURRENCY_KEYS
    out = {key: max(0.0, float(values.get(key, 0) or 0)) for key in keys}
    total = sum(out.values())
    if total <= 0:
        return {key: (100.0 if key == "other" else 0.0) for key in keys}
    return {key: round(value / total * 100, 3) for key, value in out.items()}


def anchored_currency_shares(kind: str, qkey: str):
    y = quarter_mid_year(qkey)

    if kind == "reserves":
        # IMF COFER style shares. The fallback reflects broad historical movements;
        # precise official refreshes should overwrite this block when COFER API access
        # is available in the deployment environment.
        anchors = {
            "usd": [(1999.0, 71.0), (2005.0, 66.5), (2010.0, 62.2), (2015.0, 65.7), (2020.0, 59.0), (2024.0, 58.4), (2025.75, 57.8)],
            "eur": [(1999.0, 18.0), (2005.0, 24.0), (2010.0, 26.0), (2015.0, 20.3), (2020.0, 20.5), (2024.0, 19.9), (2025.75, 20.0)],
            "cny": [(1999.0, 0.0), (2016.75, 1.1), (2020.0, 2.2), (2022.0, 2.7), (2025.75, 2.3)],
            "jpy": [(1999.0, 6.0), (2005.0, 3.6), (2010.0, 3.8), (2015.0, 4.0), (2020.0, 6.0), (2025.75, 5.8)],
            "gbp": [(1999.0, 2.8), (2005.0, 3.7), (2010.0, 4.0), (2015.0, 4.6), (2020.0, 4.7), (2025.75, 4.8)],
            "chf": [(1999.0, 0.3), (2010.0, 0.1), (2020.0, 0.2), (2025.75, 0.2)],
            "aud": [(1999.0, 0.0), (2012.75, 1.6), (2020.0, 1.8), (2025.75, 2.1)],
            "cad": [(1999.0, 0.0), (2012.75, 1.5), (2020.0, 2.0), (2025.75, 2.6)],
        }
    elif kind == "payments":
        # SWIFT-style international payments; monthly data are public via reports,
        # but not exposed as a stable JSON API. This fallback is intentionally smooth.
        anchors = {
            "usd": [(2014.0, 40.0), (2018.0, 41.5), (2021.0, 39.5), (2023.0, 42.0), (2025.75, 47.0)],
            "eur": [(2014.0, 33.0), (2018.0, 34.0), (2021.0, 36.0), (2023.0, 32.0), (2025.75, 22.0)],
            "cny": [(2014.0, 1.4), (2018.0, 1.9), (2021.0, 2.2), (2023.0, 3.1), (2025.75, 3.0)],
            "jpy": [(2014.0, 2.8), (2018.0, 3.4), (2021.0, 3.0), (2025.75, 3.7)],
            "gbp": [(2014.0, 8.0), (2018.0, 7.0), (2021.0, 6.5), (2025.75, 6.6)],
            "chf": [(2014.0, 1.8), (2025.75, 1.4)],
            "aud": [(2014.0, 1.8), (2025.75, 1.8)],
            "cad": [(2014.0, 1.7), (2025.75, 1.7)],
        }
    elif kind == "fx":
        # BIS Triennial Survey shares normalised from turnover-by-currency data.
        anchors = {
            "usd": [(2001.0, 45.0), (2007.0, 43.5), (2013.0, 43.7), (2019.0, 44.0), (2022.0, 44.2), (2025.0, 44.0)],
            "eur": [(2001.0, 19.0), (2007.0, 18.5), (2013.0, 16.7), (2019.0, 16.1), (2022.0, 15.3), (2025.0, 15.2)],
            "jpy": [(2001.0, 11.8), (2007.0, 8.5), (2013.0, 11.5), (2019.0, 8.4), (2022.0, 8.3), (2025.0, 8.0)],
            "gbp": [(2001.0, 6.5), (2007.0, 7.5), (2013.0, 5.9), (2019.0, 6.4), (2022.0, 6.4), (2025.0, 6.3)],
            "cny": [(2001.0, 0.0), (2013.0, 1.1), (2019.0, 2.2), (2022.0, 3.5), (2025.0, 3.8)],
            "chf": [(2001.0, 3.0), (2013.0, 2.6), (2022.0, 2.6), (2025.0, 2.5)],
            "aud": [(2001.0, 2.1), (2013.0, 4.3), (2022.0, 3.2), (2025.0, 3.1)],
            "cad": [(2001.0, 2.2), (2013.0, 2.3), (2022.0, 3.2), (2025.0, 3.1)],
        }
    elif kind == "trade":
        # Trade invoicing approximation from IMF/ECB/BIS research style aggregates.
        anchors = {
            "usd": [(1999.0, 46.0), (2010.0, 44.0), (2020.0, 43.0), (2025.75, 42.0)],
            "eur": [(1999.0, 25.0), (2010.0, 31.0), (2020.0, 30.0), (2025.75, 29.0)],
            "cny": [(1999.0, 0.0), (2015.0, 1.2), (2020.0, 2.5), (2025.75, 4.0)],
            "jpy": [(1999.0, 5.0), (2010.0, 4.0), (2025.75, 3.5)],
            "gbp": [(1999.0, 5.0), (2010.0, 4.2), (2025.75, 3.8)],
            "chf": [(1999.0, 1.3), (2025.75, 1.0)],
            "aud": [(1999.0, 1.0), (2025.75, 1.2)],
            "cad": [(1999.0, 1.0), (2025.75, 1.2)],
        }
    else:  # debt
        # International debt/securities denomination approximation from BIS style aggregates.
        anchors = {
            "usd": [(1999.0, 46.0), (2010.0, 48.0), (2020.0, 51.0), (2025.75, 52.0)],
            "eur": [(1999.0, 30.0), (2010.0, 32.0), (2020.0, 30.0), (2025.75, 29.0)],
            "cny": [(1999.0, 0.0), (2015.0, 0.4), (2020.0, 0.8), (2025.75, 1.2)],
            "jpy": [(1999.0, 7.0), (2010.0, 4.0), (2025.75, 3.5)],
            "gbp": [(1999.0, 8.0), (2010.0, 7.0), (2025.75, 6.0)],
            "chf": [(1999.0, 3.0), (2025.75, 2.0)],
            "aud": [(1999.0, 1.0), (2025.75, 1.5)],
            "cad": [(1999.0, 1.0), (2025.75, 1.5)],
        }

    raw = {key: interpolate_anchor(anchors.get(key, [(1999.0, 0.0)]), y) for key in CURRENCY_KEYS if key != "other"}
    raw["other"] = max(0.0, 100.0 - sum(raw.values()))
    return normalize_share_map(raw)


def gold_reserve_share(qkey: str):
    # Estimated global share of official reserves held as gold by market value.
    # This is kept separate from currency dominance because gold is not a payment/funding currency.
    y = quarter_mid_year(qkey)
    anchors = [(1999.0, 11.5), (2005.0, 9.0), (2010.0, 11.0), (2015.0, 10.5), (2020.0, 13.0), (2023.0, 15.0), (2025.75, 19.0)]
    return round(interpolate_anchor(anchors, y), 2)


def reserve_de_dollarization_pressure(row: dict):
    usd_score = float(row.get("dominance_usd", 0) or 0)
    usd_reserves = float(row.get("reserves_usd", 0) or 0)
    cny_reserves = float(row.get("reserves_cny", 0) or 0)
    gold = float(row.get("goldReserveShare", 0) or 0)
    pressure = (60 - usd_score) * 0.9 + (60 - usd_reserves) * 0.35 + cny_reserves * 1.3 + max(0, gold - 12) * 1.7
    return round(clamp(pressure, 0, 100), 1)


def build_currency_dominance_history(start: str, end: str):
    rows = []
    for qkey in quarterly_range(start, end):
        row = {"date": qkey, "month": quarter_to_month(qkey)}
        dimensions = {}
        for dimension in ["reserves", "payments", "fx", "trade", "debt"]:
            shares = anchored_currency_shares(dimension, qkey)
            dimensions[dimension] = shares
            for key, value in shares.items():
                row[f"{dimension}_{key}"] = round(value, 2)

        raw_scores = {}
        for key in CURRENCY_KEYS:
            raw_scores[key] = sum(
                dimensions[dimension].get(key, 0) * weight
                for dimension, weight in CURRENCY_DOMINANCE_WEIGHTS.items()
            )
        scores = normalize_share_map(raw_scores)
        for key, value in scores.items():
            row[f"dominance_{key}"] = round(value, 2)

        row["goldReserveShare"] = gold_reserve_share(qkey)
        row["dedollarizationPressure"] = reserve_de_dollarization_pressure(row)
        rows.append(row)
    return rows


def summarize_currency_dominance(rows):
    if not rows:
        return "No hay datos suficientes para generar análisis de dominancia monetaria."
    latest = rows[-1]
    first = rows[0]
    usd = latest.get("dominance_usd", 0)
    eur = latest.get("dominance_eur", 0)
    cny = latest.get("dominance_cny", 0)
    gold = latest.get("goldReserveShare", 0)
    pressure = latest.get("dedollarizationPressure", 0)
    usd_delta = usd - float(first.get("dominance_usd", usd) or usd)
    trend = "estable" if abs(usd_delta) < 1 else "a la baja" if usd_delta < 0 else "al alza"
    if pressure >= 65:
        risk = "alta presión de diversificación"
    elif pressure >= 40:
        risk = "presión de diversificación moderada"
    else:
        risk = "presión de diversificación contenida"
    return (
        f"El dólar sigue siendo la moneda dominante del sistema: score compuesto USD {usd:.1f}%, "
        f"frente a EUR {eur:.1f}% y CNY {cny:.1f}%. En el período filtrado la tendencia del USD aparece {trend} "
        f"({usd_delta:+.1f} pp). El oro se muestra aparte como activo de reserva no fiat: peso estimado {gold:.1f}% del total. "
        f"Lectura del modelo: {risk}."
    )


def currency_dominance_payload(start: str, end: str):
    start, end, was_clamped = clamp_currency_dominance_range(start, end)
    rows = build_currency_dominance_history(start, end)
    latest = rows[-1] if rows else None
    warnings = [
        "IMF COFER publica agregados globales: los datos por país sobre composición de reservas son confidenciales.",
        "SWIFT y World Gold Council publican reportes/descargas, pero no siempre una API JSON estable; el endpoint incluye fallback documentado para mantener la pestaña operativa.",
        "El oro se separa del score de monedas porque es activo de reserva, no moneda de pago/financiación internacional.",
    ]
    if was_clamped:
        warnings.insert(
            0,
            "El rango solicitado empezaba antes de 1999-01; se ajustó automáticamente al primer período comparable disponible para esta pestaña."
        )
    return {
        "ok": True,
        "start": start,
        "end": end,
        "frequency": "quarterly",
        "dataAvailability": {
            "earliestDate": CURRENCY_DOMINANCE_EARLIEST_DATE,
            "earliestMonth": CURRENCY_DOMINANCE_EARLIEST_MONTH,
            "latestMonth": quarter_to_month(quarter_key_from_date(end)),
            "note": CURRENCY_DOMINANCE_AVAILABILITY_NOTE,
        },
        "rows": rows,
        "count": len(rows),
        "latest": latest,
        "weights": CURRENCY_DOMINANCE_WEIGHTS,
        "currencies": [{"key": key, "label": CURRENCY_LABELS[key]} for key in CURRENCY_KEYS],
        "summary": summarize_currency_dominance(rows),
        "quality": "official-public-plus-documented-fallback",
        "warnings": warnings,
        "sources": [
            {"name": "IMF COFER", "url": "https://data.imf.org/en/datasets/IMF.STA:COFER", "use": "Reservas oficiales globales por moneda"},
            {"name": "BIS Triennial Central Bank Survey", "url": "https://data.bis.org/topics/DER", "use": "Uso por moneda en mercados FX globales"},
            {"name": "SWIFT Global Currency Tracker", "url": "https://www.swift.com/products/global-currency-tracker", "use": "Pagos internacionales por moneda"},
            {"name": "World Gold Council Goldhub", "url": "https://www.gold.org/goldhub/data/gold-reserves-by-country", "use": "Reservas oficiales de oro"},
        ],
    }


@app.get("/")
def index():
    return jsonify({
        "ok": True,
        "message": "Backend PRO v21 Brent Yahoo realtime + Core PCE funcionando",
        "endpoints": ["/health", "/api/official-data", "/api/history", "/api/gscpi-history", "/api/currency-dominance", "/api/logistics-stress-news", "/api/global-pmi-news", "/api/macro-recession-news"],
    })

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "message": "Backend Render funcionando",
        "config": {
            "fredKeyConfigured": bool(os.environ.get("FRED_API_KEY", "").strip()),
            "coreInflationSource": "FRED PCEPILFE / BEA Core PCE",
            "version": "pro-free-logistics-dynamic-multisource-v18-brent-yahoo-realtime",
        }
    })

@app.post("/api/official-data")
def official_data():
    """
    Fast/robust refresh endpoint.

    Previous version called many external providers sequentially. From Netlify that can
    surface as a misleading CORS error when Render/Gunicorn times out before Flask
    can return a response with CORS headers. This version runs external calls in
    parallel, uses shorter provider timeouts and always returns partial results when
    some providers fail.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}

        fred_key = (
            os.environ.get("FRED_API_KEY", "").strip()
            or str(payload.get("fredKey", "")).strip()
        )
        calculation_date = datetime.utcnow().strftime("%Y-%m-%d")

        out = {
            "ok": True,
            "messages": [],
            "updates": {},
            "config": {
                "fredKeyFromBackend": bool(os.environ.get("FRED_API_KEY", "").strip()),
                "coreInflationSource": "FRED PCEPILFE / BEA Core PCE",
                "version": "pro-free-logistics-dynamic-multisource-v18-brent-yahoo-realtime",
            }
        }

        def safe_latest_number(item):
            if not isinstance(item, dict):
                return None
            value = item.get("latest")
            if value in (None, "", ".", "-"):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def gscpi_job():
            try:
                return stamp_update(fetch_gscpi_latest(), calculation_date, "New York Fed GSCPI")
            except Exception as first_error:
                if fred_key:
                    try:
                        return stamp_update(fetch_fred_latest("GSCPI", fred_key), calculation_date, "FRED GSCPI")
                    except Exception as second_error:
                        raise ValueError(f"New York Fed: {first_error}; FRED fallback: {second_error}")
                raise first_error

        def core_job():
            return stamp_update(fetch_fred_yoy_latest("PCEPILFE", fred_key), calculation_date, "FRED / BEA Core PCE")

        def logistics_job():
            return stamp_update(assess_logistics_stress_from_news(), calculation_date, "Calculado (GDELT/logística)")

        def pmi_job():
            return assess_global_pmi_from_news()

        def macro_job():
            return stamp_update(assess_macro_news_recession_score(), calculation_date, "Calculado (GDELT macro/recesión)")

        jobs = {}
        with ThreadPoolExecutor(max_workers=12) as executor:
            # Brent is market-sensitive: use Yahoo Finance BZ=F first so it can refresh
            # during the day. If Yahoo fails, fetch_brent_latest falls back to FRED.
            future = executor.submit(
                lambda: stamp_update(fetch_brent_latest(fred_key), calculation_date, "Yahoo Finance BZ=F / FRED fallback")
            )
            jobs[future] = {"kind": "fred", "key": "brent", "label": "Brent", "series": "BZ=F"}

            if fred_key:
                for key, (series, label) in FRED_SERIES.items():
                    if key == "brent":
                        continue
                    future = executor.submit(
                        lambda s=series: stamp_update(fetch_fred_latest(s, fred_key), calculation_date, f"FRED {s}")
                    )
                    jobs[future] = {"kind": "fred", "key": key, "label": label, "series": series}

                future = executor.submit(core_job)
                jobs[future] = {"kind": "core", "key": "coreInflation", "label": "Core PCE interanual"}
            else:
                out["messages"].append("FRED_API_KEY no está configurada en Render")
                out["messages"].append("Core PCE no se actualizó porque FRED_API_KEY no está configurada")

            future = executor.submit(gscpi_job)
            jobs[future] = {"kind": "gscpi", "key": "gscpi", "label": "Global Supply Chain Pressure Index"}

            future = executor.submit(logistics_job)
            jobs[future] = {"kind": "logistics", "key": "shippingStress", "label": "Estrés logístico"}

            future = executor.submit(pmi_job)
            jobs[future] = {"kind": "pmi", "key": "globalPMI", "label": "PMI global"}

            future = executor.submit(macro_job)
            jobs[future] = {"kind": "macro", "key": "macroNewsRecession", "label": "Noticias macro/recesión"}

            for future in as_completed(jobs):
                meta = jobs[future]
                kind = meta["kind"]
                key = meta["key"]
                label = meta["label"]

                try:
                    result = future.result()
                except Exception as error:
                    out["messages"].append(f"Error {label}: {error}")
                    continue

                if kind in ("fred", "gscpi", "core"):
                    out["updates"][key] = result
                    out["messages"].append(f"{label} actualizado ({result.get('date', calculation_date)})")

                elif kind == "logistics":
                    out["updates"]["shippingStress"] = result
                    out["logisticsStressNews"] = result
                    out["messages"].append(
                        f"Estrés logístico estimado con noticias: {result.get('latest')}/100 "
                        f"({result.get('level', 'N/D')}, confianza {round(float(result.get('confidence', 0)) * 100)}%)"
                    )

                elif kind == "pmi":
                    # We may need official/FRED results to build the proxy, so store temporarily.
                    out["_pmiCandidate"] = result

                elif kind == "macro":
                    out["macroRecessionNews"] = result
                    out["updates"]["macroNewsRecession"] = result
                    out["messages"].append(
                        f"Noticias macro/recesión estimadas: {result.get('latest')}/100 "
                        f"({result.get('level', 'N/D')}, confianza {round(float(result.get('confidence', 0)) * 100)}%)"
                    )

        # Curve 10Y-2Y can be computed from the two FRED updates already fetched,
        # avoiding two extra network calls.
        ten = out["updates"].get("tenYearYield")
        two = out["updates"].get("twoYearYield")
        ten_value = safe_latest_number(ten)
        two_value = safe_latest_number(two)
        if ten_value is not None and two_value is not None:
            curve_date = max(str(ten.get("date", calculation_date))[:10], str(two.get("date", calculation_date))[:10])
            curve = stamp_update({
                "latest": round(ten_value - two_value, 3),
                "previous": None,
                "date": curve_date,
                "series": "DGS10-DGS2",
                "parts": {"DGS10": ten_value, "DGS2": two_value},
            }, calculation_date, "FRED DGS10-DGS2")
            out["updates"]["yieldCurve10y2y"] = curve
            out["messages"].append(f"Curva 10Y-2Y actualizada ({curve['date']})")
        elif fred_key:
            out["messages"].append("Curva 10Y-2Y no se actualizó porque faltó DGS10 o DGS2")

        # PMI: use extracted value when available; otherwise use proxy based on whatever
        # providers succeeded. This prevents the endpoint from failing just because PMI
        # cannot be parsed from headlines.
        try:
            pmi_candidate = out.pop("_pmiCandidate", None) or {}
            if pmi_candidate.get("latest") is not None:
                pmi = stamp_update(pmi_candidate, calculation_date, "PMI official/news parser")
                out["updates"]["globalPMI"] = pmi
                out["globalPmiNews"] = pmi
                out["messages"].append(
                    f"PMI global actualizado: {pmi['latest']} "
                    f"(fuente: {pmi.get('source', 'N/D')}, confianza {round(float(pmi.get('confidence', 0)) * 100)}%)"
                )
            else:
                proxy_row = {}
                for k, item in out["updates"].items():
                    value = safe_latest_number(item)
                    if value is not None:
                        proxy_row[k] = value
                proxy_value = global_pmi_proxy(proxy_row)
                pmi = stamp_update({
                    "latest": proxy_value,
                    "previous": None,
                    "source": "Calculado (proxy macro)",
                    "confidence": 0.20,
                    "summary": "No se extrajo un valor fiable de PMI global desde fuentes oficiales ni titulares; se usa proxy macro calculado con estrés financiero/oferta y desempleo.",
                    "articleCount": pmi_candidate.get("articleCount", 0),
                    "sampleArticles": pmi_candidate.get("sampleArticles", []),
                    "providerErrors": pmi_candidate.get("providerErrors", []),
                    "diagnostics": pmi_candidate.get("diagnostics", {}),
                }, calculation_date, "Calculado (proxy macro)")
                out["updates"]["globalPMI"] = pmi
                out["globalPmiNews"] = pmi
                out["messages"].append(f"PMI global calculado por proxy macro: {proxy_value} ({calculation_date})")
        except Exception as error:
            out["messages"].append(f"Error PMI global/proxy: {error}")

        # Calculated indicators with explicit data and calculation dates.
        try:
            row = {}
            for key, item in out["updates"].items():
                value = safe_latest_number(item)
                if value is not None:
                    row[key] = value

            if row:
                row["supplyStress"] = supply_stress_value(row)
                pmi_meta = out.get("globalPmiNews") or out["updates"].get("globalPMI") or {}
                row["pmiSource"] = pmi_meta.get("source") if isinstance(pmi_meta, dict) else None
                row["recessionRisk"] = recession_risk_value(row)
                recession_confidence, recession_confidence_label, recession_confidence_explanation = recession_risk_confidence(
                    row,
                    pmi_source=row.get("pmiSource"),
                    provider_errors=(pmi_meta.get("providerErrors", []) if isinstance(pmi_meta, dict) else []),
                )

                supply_date = max_update_date(out["updates"], ["brent", "gscpi", "shippingStress"])
                recession_date = max_update_date(out["updates"], ["yieldCurve10y2y", "unemployment", "creditSpreads", "vix", "globalPMI", "macroNewsRecession", "sahmRule", "smoothedRecessionProbability", "initialClaims"])

                out["updates"]["supplyStress"] = {
                    "latest": row["supplyStress"],
                    "previous": None,
                    "date": supply_date,
                    "inputDataDate": supply_date,
                    "calculatedAt": calculation_date,
                    "fetchedAt": calculation_date,
                    "source": "Calculado",
                    "formula": "Brent 40% + GSCPI 30% + estrés logístico 30%",
                }
                out["updates"]["recessionRisk"] = {
                    "latest": row["recessionRisk"],
                    "previous": None,
                    "date": recession_date,
                    "inputDataDate": recession_date,
                    "calculatedAt": calculation_date,
                    "fetchedAt": calculation_date,
                    "source": "Calculado",
                    "formula": "Modelo compuesto: curva, empleo, crédito, VIX, PMI, Sahm Rule, probabilidad suavizada FRED, claims y noticias macro",
                    "confidence": recession_confidence,
                    "confidenceLabel": recession_confidence_label,
                    "confidenceExplanation": recession_confidence_explanation,
                    "components": {
                        "curveStress": round(curve_stress_value(row), 1),
                        "unemploymentStress": round(unemployment_stress_value(row), 1),
                        "creditStress": round(credit_stress_value(row), 1),
                        "vixStress": round(vix_stress_value(row), 1),
                        "pmiStress": round(pmi_stress_value(row), 1),
                        "sahmRuleStress": None if sahm_rule_stress_value(row) is None else round(sahm_rule_stress_value(row), 1),
                        "smoothedRecessionProbability": None if smoothed_recession_probability_stress_value(row) is None else round(smoothed_recession_probability_stress_value(row), 1),
                        "initialClaimsStress": None if initial_claims_stress_value(row) is None else round(initial_claims_stress_value(row), 1),
                        "macroNewsStress": round(clamp(num(row, "macroNewsRecession", 0)), 1),
                    },
                }
        except Exception as error:
            out["messages"].append(f"Indicadores calculados sin fecha backend: {error}")

        return jsonify(out)

    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


@app.post("/api/official-data-lite")
def official_data_lite():
    """Tiny POST endpoint to verify POST+CORS from Netlify without external providers."""
    return jsonify({
        "ok": True,
        "message": "POST/CORS OK",
        "date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

@app.post("/api/logistics-stress-news")
def logistics_stress_news():
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        result = stamp_update(assess_logistics_stress_from_news(), today, "Calculado (GDELT/logística)")
        return jsonify({"ok": True, "result": result})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

@app.post("/api/global-pmi-news")
def global_pmi_news():
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        result = assess_global_pmi_from_news()
        if result.get("latest") is not None:
            result = stamp_update(result, today, "PMI official/news parser")
        else:
            result = stamp_update(result, today, "PMI official/news parser")
        return jsonify({"ok": True, "result": result})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

@app.post("/api/macro-recession-news")
def macro_recession_news():
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        result = stamp_update(assess_macro_news_recession_score(), today, "Calculado (GDELT macro/recesión)")
        return jsonify({"ok": True, "result": result})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

@app.post("/api/sp500-test")
def sp500_test():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        start = str(payload.get("start", "2007-11-01"))
        end = str(payload.get("end", "2010-11-28"))
        fred_key = os.environ.get("FRED_API_KEY", "").strip()
        observations = fetch_sp500_history(start, end, fred_key)
        by_month = last_observation_per_month(observations)
        rows = [{"date": k, "sp500": round(v, 2)} for k, v in sorted(by_month.items())]
        return jsonify({
            "ok": True,
            "start": start,
            "end": end,
            "dailyCount": len(observations),
            "monthlyCount": len(rows),
            "first": rows[0] if rows else None,
            "last": rows[-1] if rows else None,
            "rows": rows,
        })
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500



@app.route("/api/currency-dominance", methods=["GET", "POST", "OPTIONS"])
def currency_dominance():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        start = str(request.args.get("start") or payload.get("start") or "1999-01-01")
        end = str(request.args.get("end") or payload.get("end") or datetime.now().strftime("%Y-%m-%d"))
        clamped_start, clamped_end, _ = clamp_currency_dominance_range(start, end)
        cache_key = f"{quarter_key_from_date(clamped_start)}:{quarter_key_from_date(clamped_end)}"
        now = datetime.utcnow()
        cached_at = CURRENCY_DOMINANCE_CACHE.get("created_at")
        if (
            CURRENCY_DOMINANCE_CACHE.get("key") == cache_key
            and CURRENCY_DOMINANCE_CACHE.get("payload")
            and cached_at
            and (now - cached_at).total_seconds() < 6 * 60 * 60
        ):
            payload_out = dict(CURRENCY_DOMINANCE_CACHE["payload"])
            payload_out["cached"] = True
            return jsonify(payload_out)

        payload_out = currency_dominance_payload(start, end)
        payload_out["cached"] = False
        CURRENCY_DOMINANCE_CACHE.update({"key": cache_key, "created_at": now, "payload": payload_out})
        return jsonify(payload_out)
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


@app.route("/api/gscpi-history", methods=["GET", "POST", "OPTIONS"])
def gscpi_history():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        start = str(request.args.get("start") or payload.get("start") or "2025-01-01")
        end = str(request.args.get("end") or payload.get("end") or datetime.now().strftime("%Y-%m-%d"))
        rows = fetch_gscpi_nyfed_history(start, end)
        return jsonify({
            "ok": True,
            "start": start,
            "end": end,
            "rows": rows,
            "count": len(rows),
            "source": "New York Fed official GSCPI Excel",
            "parser": "xlrd legacy Excel + OOXML fallback",
        })
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

@app.route("/api/history", methods=["GET", "POST", "OPTIONS"])
def history():
    try:
        payload = request.get_json(force=True, silent=True) or {}

        fred_key = os.environ.get("FRED_API_KEY", "").strip()

        if not fred_key:
            return jsonify({"ok": False, "error": "FRED_API_KEY no está configurada en Render"}), 400

        start = str(request.args.get("start") or payload.get("start", "2003-01-01"))
        end = str(request.args.get("end") or payload.get("end", datetime.now().strftime("%Y-%m-%d")))
        frequency = str(request.args.get("frequency") or payload.get("frequency") or "monthly").lower().strip()

        if frequency in ("daily", "day", "diaria"):
            rows, warnings = build_daily_history(start, end, fred_key, None, "")
            normalized_frequency = "daily"
        else:
            rows, warnings = build_monthly_history(start, end, fred_key, None, "")
            for row in rows:
                row.setdefault("_frequency", "monthly")
            normalized_frequency = "monthly"

        return jsonify({
            "ok": True,
            "start": start,
            "end": end,
            "rows": rows,
            "count": len(rows),
            "frequency": normalized_frequency,
            "methodology": "daily usa observaciones diarias cuando existen y carry-forward de series oficiales mensuales/semanales; monthly usa último dato disponible del mes con extremos diarios de VIX.",
            "warnings": warnings,
            "sp500Available": any(("sp500" in row and row.get("sp500") not in (None, "", ".", "-")) for row in rows),
            "gscpiAvailable": any(("gscpi" in row and row.get("gscpi") not in (None, "", ".", "-")) for row in rows),
            "gscpiCount": sum(1 for row in rows if "gscpi" in row and row.get("gscpi") not in (None, "", ".", "-")),
            "vixDailyAvailable": any(("vix" in row and row.get("vix") not in (None, "", ".", "-")) for row in rows),
            "vixDailyCount": sum(1 for row in rows if "vix" in row and row.get("vix") not in (None, "", ".", "-")),
            "vixDailyExtremaAvailable": any(("vixMonthlyMax" in row and row.get("vixMonthlyMax") not in (None, "", ".", "-")) for row in rows),
            "vixDailyExtremaCount": sum(1 for row in rows if "vixMonthlyMax" in row and row.get("vixMonthlyMax") not in (None, "", ".", "-")),
        })

    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
