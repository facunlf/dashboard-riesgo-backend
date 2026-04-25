import os
import json
import urllib.parse
import urllib.request
import re
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

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

    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()

        if not raw:
            raise ValueError("Respuesta vacía del proveedor")

        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            preview = raw[:160].replace("\n", " ")
            raise ValueError(f"Respuesta no JSON del proveedor: {preview}") from error

def fetch_binary(url: str, headers: dict | None = None):
    req_headers = headers or {}
    req_headers.setdefault("User-Agent", "macro-risk-dashboard/1.0")
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
        if not raw:
            raise ValueError("Respuesta vacía del proveedor")
        return raw

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

def fetch_fred_yoy_latest(series_id: str, api_key: str):
    observations = fred_observations(series_id, api_key)
    if len(observations) < 13:
        raise ValueError(f"FRED {series_id}: datos insuficientes para calcular interanual")

    latest = observations[-1]
    latest_month = latest["date"][5:7]
    latest_year = int(latest["date"][:4])
    previous_year_same_month = next(
        (obs for obs in reversed(observations) if obs["date"][:4] == str(latest_year - 1) and obs["date"][5:7] == latest_month),
        None,
    )
    if not previous_year_same_month:
        raise ValueError(f"FRED {series_id}: no se encontró el mismo mes del año previo")

    yoy = ((latest["value"] / previous_year_same_month["value"]) - 1) * 100
    return {
        "latest": round(yoy, 2),
        "latestIndex": latest["value"],
        "previousIndex": previous_year_same_month["value"],
        "previous": None,
        "date": latest["date"][:7],
        "series": series_id,
        "source": "FRED CPILFESL",
    }

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

def fetch_bls_series(series_id: str, start_year: int, end_year: int, registration_key: str | None = None):
    body = {
        "seriesid": [series_id],
        "startyear": str(start_year),
        "endyear": str(end_year),
    }

    if registration_key:
        body["registrationKey"] = registration_key

    payload = fetch_json(
        "https://api.bls.gov/publicAPI/v2/timeseries/data/",
        method="POST",
        body=body,
    )

    series = payload.get("Results", {}).get("series", [{}])[0].get("data", [])

    monthly = [
        item for item in series
        if str(item.get("period", "")).startswith("M") and item.get("period") != "M13"
    ]

    rows = []
    for item in monthly:
        raw_value = item.get("value")
        if raw_value in (None, "", "-", "."):
            continue

        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError):
            continue

        month = item["period"][1:]
        rows.append({
            "date": f"{item['year']}-{month.zfill(2)}-01",
            "value": numeric_value,
            "year": int(item["year"]),
            "month": int(month),
        })

    rows.sort(key=lambda x: x["date"])
    return rows

def fetch_bls_core_yoy(series_id: str, registration_key: str | None = None):
    current_year = datetime.now().year
    rows = fetch_bls_series(series_id, current_year - 2, current_year, registration_key)

    if len(rows) < 13:
        raise ValueError("BLS: datos insuficientes para calcular variación interanual")

    latest = rows[-1]
    previous_year_same_month = next(
        (
            item for item in rows
            if item["month"] == latest["month"]
            and item["year"] == latest["year"] - 1
        ),
        None,
    )

    if not previous_year_same_month:
        raise ValueError("BLS: no se encontró el mismo mes del año previo")

    yoy = ((latest["value"] / previous_year_same_month["value"]) - 1) * 100

    return {
        "latest": round(yoy, 2),
        "latestIndex": latest["value"],
        "previousIndex": previous_year_same_month["value"],
        "date": latest["date"][:7],
        "series": series_id,
    }

def fetch_bls_core_yoy_history(series_id: str, start_year: int, end_year: int, registration_key: str | None = None):
    rows = fetch_bls_series(series_id, max(1990, start_year - 1), end_year, registration_key)
    output = []

    for row in rows:
        if row["year"] < start_year:
            continue

        previous_year_same_month = next(
            (
                item for item in rows
                if item["month"] == row["month"]
                and item["year"] == row["year"] - 1
            ),
            None,
        )

        if not previous_year_same_month:
            continue

        yoy = ((row["value"] / previous_year_same_month["value"]) - 1) * 100
        output.append({
            "date": row["date"],
            "value": round(yoy, 2),
        })

    return output

def month_key(date_str: str):
    return date_str[:7]

def last_observation_per_month(observations):
    out = {}
    for obs in observations:
        out[month_key(obs["date"])] = obs["value"]
    return out


def clamp(value, minimum=0, maximum=100):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0
    return max(minimum, min(maximum, value))

def num(row, key, default=0):
    try:
        value = row.get(key, default)
        if value in (None, "", ".", "-"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

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
        core_history = fetch_bls_core_yoy_history(bls_series, start_year, end_year, bls_key)
        for obs in core_history:
            date_key = month_key(obs["date"])
            monthly.setdefault(date_key, {"date": date_key})
            monthly[date_key]["coreInflation"] = obs["value"]
    except Exception:
        pass

    rows = [monthly[k] for k in sorted(monthly.keys())]

    for row in rows:
        enrich_history_row(row)

    return rows

def fetch_gdelt_logistics_articles(max_records: int = 25):
    try:
        params = {
            "query": LOGISTICS_QUERY,
            "mode": "artlist",
            "format": "json",
            "maxrecords": max_records,
            "sort": "hybridrel",
            "timespan": "7d",
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

def heuristic_logistics_score(articles):
    if not articles:
        return {
            "score": 50,
            "level": "moderado",
            "confidence": 0.15,
            "summary": "No se pudieron obtener titulares recientes de GDELT; se usa valor neutral preventivo.",
            "drivers": ["sin datos GDELT"],
        }

    text = " ".join((a.get("title") or "").lower() for a in articles)

    severe_terms = [
        "closed", "closure", "blocked", "blockade", "suspended", "halted",
        "attack", "attacks", "missile", "tanker attack", "strait of hormuz",
        "red sea", "war risk", "rerouting", "route diversion",
    ]
    moderate_terms = [
        "delay", "delays", "congestion", "insurance", "freight rates",
        "shipping costs", "port congestion", "suez", "maritime security",
    ]

    severe_hits = sum(text.count(term) for term in severe_terms)
    moderate_hits = sum(text.count(term) for term in moderate_terms)

    score = 25 + severe_hits * 10 + moderate_hits * 5 + min(len(articles), 25) * 0.8
    score = max(0, min(100, round(score)))

    if score >= 80:
        level = "severo"
    elif score >= 60:
        level = "alto"
    elif score >= 40:
        level = "moderado"
    elif score >= 20:
        level = "leve"
    else:
        level = "bajo"

    drivers = []
    for term in severe_terms + moderate_terms:
        if term in text and len(drivers) < 8:
            drivers.append(term)

    return {
        "score": score,
        "level": level,
        "confidence": 0.45,
        "summary": "Estimación heurística basada en titulares recientes de logística marítima, seguros, rutas y disrupciones.",
        "drivers": drivers or ["sin drivers críticos detectados"],
    }

def ai_logistics_score(articles):
    # Versión gratuita: sin OpenAI API.
    # Se mantiene esta función como stub para conservar estructura.
    return None

def assess_logistics_stress_from_news():
    articles = fetch_gdelt_logistics_articles(25)
    heuristic = heuristic_logistics_score(articles)

    ai_result = None
    try:
        ai_result = ai_logistics_score(articles)
    except Exception as error:
        ai_result = None

    result = ai_result or heuristic

    return {
        "latest": result["score"],
        "previous": None,
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "source": "GDELT heuristic",
        "level": result["level"],
        "confidence": result["confidence"],
        "summary": result["summary"],
        "drivers": result["drivers"],
        "articleCount": len(articles),
        "sampleArticles": articles[:6],
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
        "message": "Backend PRO Fixed Logistics + PMI + Recession v3 del dashboard funcionando",
        "endpoints": ["/health", "/api/official-data", "/api/history", "/api/logistics-stress-news", "/api/global-pmi-news", "/api/macro-recession-news"],
    })

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "message": "Backend Render funcionando",
        "config": {
            "fredKeyConfigured": bool(os.environ.get("FRED_API_KEY", "").strip()),
            "blsKeyConfigured": bool(os.environ.get("BLS_API_KEY", "").strip()),
            "blsSeries": os.environ.get("BLS_SERIES", "CUUR0000SA0L1E"),
            "version": "pro-free-logistics-pmi-recession-history-v3",
        }
    })

@app.post("/api/official-data")
def official_data():
    try:
        payload = request.get_json(force=True, silent=True) or {}

        fred_key = (
            os.environ.get("FRED_API_KEY", "").strip()
            or str(payload.get("fredKey", "")).strip()
        )

        bls_key = (
            os.environ.get("BLS_API_KEY", "").strip()
            or str(payload.get("blsKey", "")).strip()
        )

        bls_series = (
            os.environ.get("BLS_SERIES", "").strip()
            or str(payload.get("blsSeries", "CUUR0000SA0L1E")).strip()
            or "CUUR0000SA0L1E"
        )

        out = {
            "ok": True,
            "messages": [],
            "updates": {},
            "config": {
                "fredKeyFromBackend": bool(os.environ.get("FRED_API_KEY", "").strip()),
                "blsKeyFromBackend": bool(os.environ.get("BLS_API_KEY", "").strip()),
                "blsSeries": bls_series,
                "version": "pro-free-logistics-pmi-recession-history-v3",
            }
        }

        if fred_key:
            for key, (series, label) in FRED_SERIES.items():
                try:
                    value = fetch_fred_latest(series, fred_key)
                    out["updates"][key] = value
                    out["messages"].append(f"{label} actualizado ({value['date']})")
                except Exception as error:
                    out["messages"].append(f"Error {label}: {error}")

            try:
                curve = fetch_fred_pair_spread("DGS10", "DGS2", fred_key)
                out["updates"]["yieldCurve10y2y"] = curve
                out["messages"].append(f"Curva 10Y-2Y actualizada ({curve['date']})")
            except Exception as error:
                out["messages"].append(f"Error curva 10Y-2Y: {error}")

        else:
            out["messages"].append("FRED_API_KEY no está configurada en Render")

        try:
            gscpi = fetch_gscpi_latest()
            out["updates"]["gscpi"] = gscpi
            out["messages"].append(f"Global Supply Chain Pressure Index actualizado ({gscpi['date']})")
        except Exception:
            if fred_key:
                try:
                    gscpi = fetch_fred_latest("GSCPI", fred_key)
                    gscpi["source"] = "FRED GSCPI"
                    out["updates"]["gscpi"] = gscpi
                    out["messages"].append(f"Global Supply Chain Pressure Index actualizado ({gscpi['date']})")
                except Exception:
                    out["messages"].append("Global Supply Chain Pressure Index no se actualizó; se conserva el último valor cargado")
            else:
                out["messages"].append("Global Supply Chain Pressure Index no se actualizó; se conserva el último valor cargado")

        try:
            core = fetch_bls_core_yoy(bls_series, bls_key or None)
            out["updates"]["coreInflation"] = core
            out["messages"].append(f"Inflación core interanual actualizada ({core['date']})")
        except Exception:
            if fred_key:
                try:
                    core = fetch_fred_yoy_latest("CPILFESL", fred_key)
                    out["updates"]["coreInflation"] = core
                    out["messages"].append(f"Inflación core interanual actualizada con FRED ({core['date']})")
                except Exception:
                    out["messages"].append("Inflación core no se actualizó; se conserva el último valor cargado")
            else:
                out["messages"].append("Inflación core no se actualizó; se conserva el último valor cargado")

        try:
            logistics = assess_logistics_stress_from_news()
            out["updates"]["shippingStress"] = logistics
            out["logisticsStressNews"] = logistics
            out["messages"].append(
                f"Estrés logístico estimado con noticias: {logistics['latest']}/100 "
                f"({logistics['level']}, confianza {round(logistics['confidence'] * 100)}%)"
            )
        except Exception:
            out["messages"].append("Estrés logístico por noticias no se actualizó; se conserva el último valor cargado")

        try:
            pmi = assess_global_pmi_from_news()
            out["globalPmiNews"] = pmi
            if pmi.get("latest") is not None:
                out["updates"]["globalPMI"] = pmi
                out["messages"].append(
                    f"PMI global estimado desde noticias: {pmi['latest']} "
                    f"(confianza {round(pmi.get('confidence', 0) * 100)}%)"
                )
            else:
                out["messages"].append("PMI global no pudo extraerse automáticamente desde noticias recientes")
        except Exception as error:
            out["messages"].append(f"Error PMI global por noticias: {error}")

        try:
            macro_news = assess_macro_news_recession_score()
            out["macroRecessionNews"] = macro_news
            out["updates"]["macroNewsRecession"] = macro_news
            out["messages"].append(
                f"Noticias macro/recesión estimadas: {macro_news['latest']}/100 "
                f"({macro_news['level']}, confianza {round(macro_news['confidence'] * 100)}%)"
            )
        except Exception as error:
            out["messages"].append(f"Error noticias macro/recesión: {error}")

        return jsonify(out)

    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

@app.post("/api/logistics-stress-news")
def logistics_stress_news():
    try:
        result = assess_logistics_stress_from_news()
        return jsonify({"ok": True, "result": result})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

@app.post("/api/global-pmi-news")
def global_pmi_news():
    try:
        result = assess_global_pmi_from_news()
        return jsonify({"ok": True, "result": result})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

@app.post("/api/macro-recession-news")
def macro_recession_news():
    try:
        result = assess_macro_news_recession_score()
        return jsonify({"ok": True, "result": result})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

@app.post("/api/history")
def history():
    try:
        payload = request.get_json(force=True, silent=True) or {}

        fred_key = os.environ.get("FRED_API_KEY", "").strip()
        bls_key = os.environ.get("BLS_API_KEY", "").strip()
        bls_series = os.environ.get("BLS_SERIES", "CUUR0000SA0L1E").strip() or "CUUR0000SA0L1E"

        if not fred_key:
            return jsonify({"ok": False, "error": "FRED_API_KEY no está configurada en Render"}), 400

        start = str(payload.get("start", "2003-01-01"))
        end = str(payload.get("end", datetime.now().strftime("%Y-%m-%d")))

        rows = build_monthly_history(start, end, fred_key, bls_key or None, bls_series)

        return jsonify({
            "ok": True,
            "start": start,
            "end": end,
            "rows": rows,
            "count": len(rows),
            "frequency": "monthly-last-observation",
        })

    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
