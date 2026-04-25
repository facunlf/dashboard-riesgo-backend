import os
import json
import urllib.parse
import urllib.request
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

def fetch_json(url: str, method: str = "GET", body: dict | None = None):
    data = None
    headers = {}

    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fetch_fred_latest(series_id: str, api_key: str, limit: int = 2):
    params = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    })

    url = f"https://api.stlouisfed.org/fred/series/observations?{params}"
    payload = fetch_json(url)

    observations = [
        item for item in payload.get("observations", [])
        if item.get("value") not in (".", "", None)
    ]

    if not observations:
        raise ValueError(f"FRED {series_id}: sin datos")

    latest = float(observations[0]["value"])
    previous = float(observations[1]["value"]) if len(observations) > 1 else latest

    return {
        "latest": latest,
        "previous": previous,
        "date": observations[0]["date"],
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

def fetch_bls_core_yoy(series_id: str, registration_key: str | None = None):
    current_year = datetime.now().year

    body = {
        "seriesid": [series_id],
        "startyear": str(current_year - 2),
        "endyear": str(current_year),
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

    monthly.sort(
        key=lambda item: (int(item["year"]), int(item["period"][1:])),
        reverse=True,
    )

    if len(monthly) < 13:
        raise ValueError("BLS: datos insuficientes para calcular variación interanual")

    latest = monthly[0]

    same_month_previous_year = next(
        (
            item for item in monthly
            if item["period"] == latest["period"]
            and int(item["year"]) == int(latest["year"]) - 1
        ),
        None,
    )

    if not same_month_previous_year:
        raise ValueError("BLS: no se encontró el mismo mes del año previo")

    latest_value = float(latest["value"])
    previous_value = float(same_month_previous_year["value"])
    yoy = ((latest_value / previous_value) - 1) * 100

    return {
        "latest": round(yoy, 2),
        "latestIndex": latest_value,
        "previousIndex": previous_value,
        "date": f"{latest['year']}-{latest['period'][1:]}",
        "series": series_id,
    }

@app.get("/")
def index():
    return jsonify({
        "ok": True,
        "message": "Backend PRO Supply del dashboard funcionando",
        "endpoints": ["/health", "/api/official-data"],
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
            "version": "pro-supply-env-keys-v1",
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
                "version": "pro-supply-env-keys-v1",
            }
        }

        if fred_key:
            fred_jobs = {
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

            for key, (series, label) in fred_jobs.items():
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
