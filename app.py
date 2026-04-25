import os
import json
import urllib.parse
import urllib.request
import csv
import re
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


@app.after_request
def add_cors_headers(response):
    # Ensure even JSON error responses include CORS headers.
    # If Render/Gunicorn kills the worker before Flask returns, those proxy errors
    # can still appear as CORS in the browser; the route below is shortened to avoid that.
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return response

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
    req_headers.setdefault("Accept", "text/csv,application/json,text/plain,text/html,*/*")
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
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return text + "-01"
    for fmt in ("%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%b-%y", "%b %Y", "%B %Y", "%Y-%m"):
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

def fetch_gscpi_nyfed_history(start: str | None = None, end: str | None = None):
    last_error = None
    for url in GSCPI_DATA_URLS:
        try:
            raw = fetch_binary(url)
            sheets = parse_xlsx_rows(raw)
            observations = []

            for rows in sheets:
                header_idx = None
                date_col = None
                value_col = None

                for idx, row in enumerate(rows[:80]):
                    lowered = [str(x).strip().lower() for x in row]
                    for c, cell in enumerate(lowered):
                        if "date" in cell or "month" in cell:
                            date_col = c
                        if "gscpi" in cell or "global supply chain pressure" in cell:
                            value_col = c
                    if date_col is not None and value_col is not None:
                        header_idx = idx
                        break

                if header_idx is not None:
                    data_rows = rows[header_idx + 1:]
                else:
                    data_rows = rows

                for row in data_rows:
                    if not row:
                        continue

                    if date_col is not None and value_col is not None and max(date_col, value_col) < len(row):
                        date_value = row[date_col]
                        gscpi_value = row[value_col]
                    else:
                        parsed = [(i, parse_date_like(v)) for i, v in enumerate(row)]
                        parsed = [(i, d) for i, d in parsed if d]
                        if not parsed:
                            continue
                        date_i, parsed_date = parsed[0]
                        numeric_candidates = [parse_float_like(v) for i, v in enumerate(row) if i != date_i]
                        numeric_candidates = [v for v in numeric_candidates if v is not None and -10 <= v <= 10]
                        if not numeric_candidates:
                            continue
                        date_value = parsed_date
                        gscpi_value = numeric_candidates[0]

                    parsed_date = parse_date_like(date_value)
                    numeric = parse_float_like(gscpi_value)
                    if parsed_date and numeric is not None and -10 <= numeric <= 10:
                        if start and parsed_date < start:
                            continue
                        if end and parsed_date > end:
                            continue
                        observations.append({"date": parsed_date, "value": round(numeric, 4)})

            observations.sort(key=lambda x: x["date"])
            # de-duplicate same date if workbook contains chart/helper sheets
            deduped = {}
            for obs in observations:
                deduped[obs["date"]] = obs
            observations = list(deduped.values())
            observations.sort(key=lambda x: x["date"])

            if observations:
                return observations
            last_error = ValueError("El archivo oficial de GSCPI no contenía observaciones legibles")
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

    return round(
        curve_stress_value(row) * 0.25
        + unemployment_stress_value(row) * 0.20
        + credit_stress_value(row) * 0.20
        + vix_stress_value(row) * 0.10
        + pmi_stress_value(row) * 0.15
        + macro_news_proxy * 0.10
    )

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
            by_month = last_observation_per_month(obs)

            for date_key, value in by_month.items():
                monthly.setdefault(date_key, {"date": date_key})
                monthly[date_key][key] = round(value, 4)
        except Exception:
            pass

    try:
        gscpi_obs = fetch_gscpi_nyfed_history(start, end)
        by_month = last_observation_per_month(gscpi_obs)
        for date_key, value in by_month.items():
            monthly.setdefault(date_key, {"date": date_key})
            monthly[date_key]["gscpi"] = round(value, 4)
    except Exception:
        try:
            gscpi_obs = fred_observations("GSCPI", fred_key, start, end)
            by_month = last_observation_per_month(gscpi_obs)
            for date_key, value in by_month.items():
                monthly.setdefault(date_key, {"date": date_key})
                monthly[date_key]["gscpi"] = round(value, 4)
        except Exception:
            pass

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

LOGISTICS_QUERIES = [
    {
        "name": "hormuz_crisis",
        "query": (
            '("Strait of Hormuz" OR Hormuz) '
            '(shipping OR tanker OR vessel OR ship OR maritime OR oil) '
            '(closed OR closure OR blocked OR blockade OR attack OR attacks OR seized OR captured '
            'OR fired OR missile OR drone OR mine OR mines OR suspended OR halted OR rerouting '
            'OR "war risk" OR "not allowed" OR "traffic halted" OR "shipping suspended")'
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


TRUSTED_LOGISTICS_DOMAINS = {
    "reuters.com": 1.35,
    "apnews.com": 1.25,
    "bloomberg.com": 1.25,
    "ft.com": 1.25,
    "wsj.com": 1.20,
    "lloydslist.com": 1.25,
    "tradewindsnews.com": 1.20,
    "maritime-executive.com": 1.15,
    "gcaptain.com": 1.15,
    "splash247.com": 1.10,
    "aljazeera.com": 1.10,
    "abc.net.au": 1.10,
    "bbc.com": 1.10,
    "cnn.com": 1.05,
}


CRISIS_TERMS = {
    # Cierre / bloqueo
    "closed": 18,
    "closure": 18,
    "blocked": 18,
    "blockade": 20,
    "not allowed": 18,
    "traffic halted": 18,
    "shipping suspended": 18,
    "effective standstill": 20,
    "standstill": 16,

    # Ataques / capturas
    "attack": 14,
    "attacks": 14,
    "attacked": 14,
    "fired on": 16,
    "missile": 14,
    "drone": 12,
    "mine": 16,
    "mines": 16,
    "seized": 18,
    "captured": 18,
    "storming": 14,
    "gunboat": 14,
    "fast boat": 12,

    # Suspensión / desvío de rutas
    "suspended": 14,
    "halted": 14,
    "rerouting": 12,
    "rerouted": 12,
    "route diversion": 12,
    "avoid": 10,
    "avoiding": 10,

    # Costes / seguros
    "war risk": 12,
    "insurance": 8,
    "freight rates": 8,
    "shipping costs": 8,
    "port congestion": 7,
}


RELIEF_TERMS = {
    "reopened": 18,
    "re-opened": 18,
    "fully open": 18,
    "shipping resumes": 16,
    "traffic resumes": 16,
    "normal traffic": 18,
    "normalizing": 14,
    "normalized": 14,
    "agreement": 12,
    "ceasefire": 10,
    "deal": 10,
    "blockade lifted": 18,
    "routes restored": 18,
    "insurance rates fall": 14,
    "freight rates fall": 12,
}


def parse_gdelt_seen_date(value: str):
    text = str(value or "").strip()

    # GDELT suele devolver seendate tipo 20260423123000
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
            return None

    return None


def recency_weight(seendate: str):
    seen = parse_gdelt_seen_date(seendate)
    if not seen:
        return 0.70

    age_days = max(0, (datetime.utcnow() - seen).days)

    if age_days <= 1:
        return 1.25
    if age_days <= 3:
        return 1.10
    if age_days <= 7:
        return 0.90
    if age_days <= 14:
        return 0.65

    return 0.45


def source_weight(domain: str):
    domain = str(domain or "").lower().replace("www.", "")
    return TRUSTED_LOGISTICS_DOMAINS.get(domain, 1.0)


def term_score(text: str, terms: dict):
    total = 0
    hits = []

    for term, weight in terms.items():
        if term in text:
            total += weight
            hits.append(term)

    return total, hits


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
        articles.append({
            "title": item.get("title", ""),
            "source": item.get("sourceCountry", "") or item.get("domain", ""),
            "domain": item.get("domain", ""),
            "url": item.get("url", ""),
            "seendate": item.get("seendate", ""),
            "language": item.get("language", ""),
            "queryName": query_name,
        })

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

    # Deduplicar por URL o título
    deduped = {}
    for article in all_articles:
        key = article.get("url") or article.get("title")
        key = str(key or "").strip().lower()

        if not key:
            continue

        if key not in deduped:
            deduped[key] = article

    articles = list(deduped.values())

    # Priorizar recientes y fuentes fuertes
    articles.sort(
        key=lambda a: (
            recency_weight(a.get("seendate", "")) * source_weight(a.get("domain", "")),
            a.get("seendate", "")
        ),
        reverse=True
    )

    return articles[:max_records]


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


def heuristic_logistics_score(articles):
    if not articles:
        return {
            "score": 45,
            "level": "moderado",
            "confidence": 0.10,
            "summary": "No se pudieron obtener titulares recientes de GDELT; se usa valor neutral-bajo y baja confianza.",
            "drivers": ["sin datos GDELT"],
            "diagnostics": {
                "articleCount": 0,
                "sourceDiversity": 0,
                "crisisArticles": 0,
                "severeArticles": 0,
                "reliefArticles": 0,
                "weightedCrisis": 0,
                "weightedRelief": 0,
                "sampleTitles": [],
            }
        }

    weighted_crisis = 0
    weighted_relief = 0
    crisis_articles = 0
    severe_articles = 0
    relief_articles = 0
    domains = set()
    drivers = []
    sample_titles = []

    for article in articles:
        title = str(article.get("title") or "")
        domain = str(article.get("domain") or "").lower().replace("www.", "")
        query_name = str(article.get("queryName") or "")
        text = title.lower()

        domains.add(domain)

        crisis_points, crisis_hits = term_score(text, CRISIS_TERMS)
        relief_points, relief_hits = term_score(text, RELIEF_TERMS)

        # Si una noticia habla de "ceasefire" pero también de standstill/ataques,
        # no debe contar como alivio real.
        if crisis_points >= 12:
            relief_points *= 0.30

        # Bonus específico para Ormuz porque es una ruta energética crítica.
        if "hormuz" in text or "strait of hormuz" in text:
            if crisis_points >= 10:
                crisis_points += 12
            if relief_points >= 10 and crisis_points < 8:
                relief_points += 8

        # Bonus si la consulta que la encontró era de normalización.
        if query_name == "normalization" and relief_points >= 10 and crisis_points < 8:
            relief_points += 6

        article_weight = recency_weight(article.get("seendate", "")) * source_weight(domain)

        weighted_crisis += crisis_points * article_weight
        weighted_relief += relief_points * article_weight

        if crisis_points >= 8:
            crisis_articles += 1

        if crisis_points >= 20:
            severe_articles += 1

        if relief_points >= 10 and crisis_points < 8:
            relief_articles += 1

        for hit in crisis_hits + relief_hits:
            if hit not in drivers and len(drivers) < 12:
                drivers.append(hit)

        if len(sample_titles) < 6 and title:
            sample_titles.append(title)

    article_count = len(articles)
    source_diversity = len([d for d in domains if d])

    coverage_bonus = min(12, article_count * 0.18)
    diversity_bonus = min(10, source_diversity * 1.25)

    net_pressure = weighted_crisis - weighted_relief

    score = 25 + net_pressure * 0.42 + coverage_bonus + diversity_bonus

    # Si hay varias noticias severas recientes, elevar el piso.
    if severe_articles >= 3:
        score = max(score, 82)

    if severe_articles >= 5:
        score = max(score, 88)

    # Si hay señales claras de normalización y pocas señales severas, bajar techo.
    if relief_articles >= 3 and severe_articles <= 1:
        score = min(score, 45)

    if relief_articles >= 5 and crisis_articles <= 2:
        score = min(score, 35)

    # Si predominan noticias positivas sobre negativas, bajar más.
    if weighted_relief > weighted_crisis * 1.4 and severe_articles == 0:
        score = min(score, 30)

    score = round(clamp(score))

    confidence = 0.25
    confidence += min(0.35, article_count / 120)
    confidence += min(0.25, source_diversity / 40)
    if severe_articles >= 3 or relief_articles >= 3:
        confidence += 0.10
    confidence = round(min(confidence, 0.92), 2)

    if score >= 80:
        summary = "Disrupción logística severa detectada por noticias recientes: cierres, ataques, capturas, suspensión de rutas o desvíos relevantes."
    elif score >= 60:
        summary = "Estrés logístico alto: hay señales relevantes de ataques, restricciones, seguros marítimos, desvíos o tensión en rutas clave."
    elif score >= 40:
        summary = "Estrés logístico moderado: hay tensión o costes logísticos, pero sin disrupción global claramente dominante."
    elif score >= 20:
        summary = "Estrés logístico leve: ruido geopolítico u operativo, pero con señales limitadas de disrupción."
    else:
        summary = "Estrés logístico bajo: predominan señales de rutas abiertas, normalización o ausencia de eventos críticos."

    if relief_articles >= 3 and score <= 45:
        summary = "Las noticias recientes sugieren normalización: reapertura, acuerdo, tráfico retomándose o reducción de tensiones logísticas."

    return {
        "score": score,
        "level": logistics_level(score),
        "confidence": confidence,
        "summary": summary,
        "drivers": drivers or ["sin drivers críticos detectados"],
        "diagnostics": {
            "articleCount": article_count,
            "sourceDiversity": source_diversity,
            "crisisArticles": crisis_articles,
            "severeArticles": severe_articles,
            "reliefArticles": relief_articles,
            "weightedCrisis": round(weighted_crisis, 2),
            "weightedRelief": round(weighted_relief, 2),
            "sampleTitles": sample_titles,
        }
    }


def ai_logistics_score(articles):
    # Versión gratuita: sin OpenAI API.
    # Se mantiene esta función como stub para conservar estructura.
    return None


def assess_logistics_stress_from_news():
    articles = fetch_gdelt_logistics_articles(90)
    heuristic = heuristic_logistics_score(articles)

    ai_result = None
    try:
        ai_result = ai_logistics_score(articles)
    except Exception:
        ai_result = None

    result = ai_result or heuristic

    return {
        "latest": result["score"],
        "previous": None,
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "source": "GDELT multi-query dynamic heuristic",
        "level": result["level"],
        "confidence": result["confidence"],
        "summary": result["summary"],
        "drivers": result["drivers"],
        "articleCount": len(articles),
        "sampleArticles": articles[:10],
        "diagnostics": result.get("diagnostics", {}),
    }


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
            })

        return articles
    except Exception:
        return []

def extract_pmi_value_from_text(text: str):
    patterns = [
        r'(?:J\\.?P\\.?\\s?Morgan|JPMorgan).*?(?:Global Composite PMI|Global PMI|Composite PMI).*?(?:rose to|rises to|increased to|up to|fell to|falls to|declined to|down to|at|was|posted|registered|came in at)\\s+(\\d{1,2}(?:\\.\\d)?)',
        r'(?:Global Composite PMI|Global PMI|Composite PMI).*?(?:rose to|rises to|increased to|up to|fell to|falls to|declined to|down to|at|was|posted|registered|came in at)\\s+(\\d{1,2}(?:\\.\\d)?)',
        r'(?:rose to|rises to|increased to|up to|fell to|falls to|declined to|down to|posted|registered|came in at)\\s+(\\d{1,2}(?:\\.\\d)?).*?(?:Global Composite PMI|Global PMI|Composite PMI)',
        r'\\b(\\d{1,2}(?:\\.\\d)?)\\b.*?(?:Global Composite PMI|Global PMI|Composite PMI)',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            if 30 <= value <= 70:
                return round(value, 1)

    return None

def assess_global_pmi_from_news():
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
            "source": "GDELT PMI news parser",
            "confidence": 0.0,
            "summary": "No se pudo extraer automáticamente un valor de JPMorgan Global Composite PMI desde titulares recientes.",
            "articleCount": len(articles),
            "sampleArticles": articles[:6],
        }

    # Preferimos el resultado más reciente que haya pasado validación.
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

@app.get("/")
def index():
    return jsonify({
        "ok": True,
        "message": "Backend PRO v20 Core PCE + fechas completas por indicador funcionando",
        "endpoints": ["/health", "/api/official-data", "/api/history", "/api/logistics-stress-news", "/api/global-pmi-news", "/api/macro-recession-news"],
    })

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "message": "Backend Render funcionando",
        "config": {
            "fredKeyConfigured": bool(os.environ.get("FRED_API_KEY", "").strip()),
            "coreInflationSource": "FRED PCEPILFE / BEA Core PCE",
            "version": "pro-free-logistics-dynamic-multisource-v9",
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
                "version": "pro-free-logistics-dynamic-multisource-v9-fast-official-data",
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
            if fred_key:
                for key, (series, label) in FRED_SERIES.items():
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
                pmi = stamp_update(pmi_candidate, calculation_date, "Calculado (GDELT PMI)")
                out["updates"]["globalPMI"] = pmi
                out["globalPmiNews"] = pmi
                out["messages"].append(
                    f"PMI global estimado desde noticias: {pmi['latest']} "
                    f"(confianza {round(float(pmi.get('confidence', 0)) * 100)}%)"
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
                    "summary": "No se extrajo un valor fiable de PMI global desde titulares; se usa proxy macro calculado con estrés financiero/oferta y desempleo.",
                    "articleCount": pmi_candidate.get("articleCount", 0),
                    "sampleArticles": pmi_candidate.get("sampleArticles", []),
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
                row["recessionRisk"] = recession_risk_value(row)

                supply_date = max_update_date(out["updates"], ["brent", "gscpi", "shippingStress"])
                recession_date = max_update_date(out["updates"], ["yieldCurve10y2y", "unemployment", "creditSpreads", "vix", "globalPMI", "macroNewsRecession"])

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
                    "formula": "25% curva + 20% desempleo + 20% HY OAS + 10% VIX + 15% PMI + 10% noticias macro",
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
            result = stamp_update(result, today, "Calculado (GDELT PMI)")
        else:
            result = stamp_update(result, today, "Calculado (GDELT PMI)")
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

@app.post("/api/history")
def history():
    try:
        payload = request.get_json(force=True, silent=True) or {}

        fred_key = os.environ.get("FRED_API_KEY", "").strip()

        if not fred_key:
            return jsonify({"ok": False, "error": "FRED_API_KEY no está configurada en Render"}), 400

        start = str(payload.get("start", "2003-01-01"))
        end = str(payload.get("end", datetime.now().strftime("%Y-%m-%d")))

        rows, warnings = build_monthly_history(start, end, fred_key, None, "")

        return jsonify({
            "ok": True,
            "start": start,
            "end": end,
            "rows": rows,
            "count": len(rows),
            "frequency": "monthly-last-observation",
            "warnings": warnings,
            "sp500Available": any(("sp500" in row and row.get("sp500") not in (None, "", ".", "-")) for row in rows),
        })

    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
