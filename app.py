import os
import json
import urllib.parse
import urllib.request
from datetime import datetime
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
    "gscpi": ("GSCPI", "Global Supply Chain Pressure Index"),
}

def fetch_json(url: str, method: str = "GET", body: dict | None = None):
    data = None
    headers = {}

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))

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
        if value in (".", "", None):
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
        month = item["period"][1:]
        rows.append({
            "date": f"{item['year']}-{month.zfill(2)}-01",
            "value": float(item["value"]),
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
    # Pedimos un año extra hacia atrás para poder calcular YoY desde el primer año seleccionado.
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
        except Exception as error:
            # No frenamos todo por una serie.
            pass

    # Curva 10Y-2Y
    try:
        dgs10 = last_observation_per_month(fred_observations("DGS10", fred_key, start, end))
        dgs2 = last_observation_per_month(fred_observations("DGS2", fred_key, start, end))
        for date_key in sorted(set(dgs10) & set(dgs2)):
            monthly.setdefault(date_key, {"date": date_key})
            monthly[date_key]["yieldCurve10y2y"] = round(dgs10[date_key] - dgs2[date_key], 4)
    except Exception:
        pass

    # Core CPI YoY desde BLS
    try:
        core_history = fetch_bls_core_yoy_history(bls_series, start_year, end_year, bls_key)
        for obs in core_history:
            date_key = month_key(obs["date"])
            monthly.setdefault(date_key, {"date": date_key})
            monthly[date_key]["coreInflation"] = obs["value"]
    except Exception:
        pass

    rows = [monthly[k] for k in sorted(monthly.keys())]

    # Supply Stress calculado a nivel backend para históricos.
    for row in rows:
        brent = float(row.get("brent", 0) or 0)
        gscpi = float(row.get("gscpi", 0) or 0)
        logistics = float(row.get("shippingStress", 50) or 50)

        brent_score = 20 if brent <= 80 else 50 if brent <= 100 else 80
        gscpi_score = 20 if gscpi <= 0 else 50 if gscpi <= 1 else 80
        logistics_score = max(0, min(100, logistics))

        row["supplyStress"] = round(brent_score * 0.4 + gscpi_score * 0.3 + logistics_score * 0.3)

    return rows

@app.get("/")
def index():
    return jsonify({
        "ok": True,
        "message": "Backend PRO History del dashboard funcionando",
        "endpoints": ["/health", "/api/official-data", "/api/history"],
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
            "version": "pro-history-v1",
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
                "version": "pro-history-v1",
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
            core = fetch_bls_core_yoy(bls_series, bls_key or None)
            out["updates"]["coreInflation"] = core
            out["messages"].append(f"Inflación core interanual actualizada ({core['date']})")
        except Exception as error:
            out["messages"].append(f"Error BLS: {error}")

        return jsonify(out)

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
