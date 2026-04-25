
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

def fetch_fred_latest(series_id: str, api_key: str):
    params = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 2,
    })
    url = f"https://api.stlouisfed.org/fred/series/observations?{params}"
    payload = fetch_json(url)
    obs = [o for o in payload.get("observations", []) if o.get("value") not in (".", "", None)]
    if not obs:
        raise ValueError(f"FRED {series_id}: sin datos")
    latest = float(obs[0]["value"])
    previous = float(obs[1]["value"]) if len(obs) > 1 else latest
    return {"latest": latest, "previous": previous, "date": obs[0]["date"]}

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
    monthly = [x for x in series if str(x.get("period", "")).startswith("M") and x.get("period") != "M13"]
    monthly.sort(key=lambda x: (int(x["year"]), int(x["period"][1:])), reverse=True)

    if len(monthly) < 13:
        raise ValueError("BLS: datos insuficientes para variación interanual")

    latest = monthly[0]
    same_month_prev = next(
        (x for x in monthly if x["period"] == latest["period"] and int(x["year"]) == int(latest["year"]) - 1),
        None,
    )
    if not same_month_prev:
        raise ValueError("BLS: no se encontró el mismo mes del año previo")

    latest_val = float(latest["value"])
    prev_val = float(same_month_prev["value"])
    yoy = ((latest_val / prev_val) - 1) * 100
    return {
        "latest": round(yoy, 2),
        "latestIndex": latest_val,
        "previousIndex": prev_val,
        "date": f"{latest['year']}-{latest['period'][1:]}",
    }

@app.get("/health")
def health():
    return jsonify({"ok": True, "message": "Backend Render funcionando"})

@app.post("/api/official-data")
def official_data():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        fred_key = str(payload.get("fredKey", "")).strip()
        bls_key = str(payload.get("blsKey", "")).strip()
        bls_series = str(payload.get("blsSeries", "CUUR0000SA0L1E")).strip() or "CUUR0000SA0L1E"

        out = {"ok": True, "messages": [], "updates": {}}

        if fred_key:
            brent = fetch_fred_latest("DCOILBRENTEU", fred_key)
            hy = fetch_fred_latest("BAMLH0A0HYM2", fred_key)
            out["updates"]["brent"] = brent
            out["updates"]["creditSpreads"] = hy
            out["messages"].append(f"Brent actualizado ({brent['date']})")
            out["messages"].append(f"HY OAS actualizado ({hy['date']})")
        else:
            out["messages"].append("FRED sin API key: Brent y HY OAS no se actualizaron")

        try:
            core = fetch_bls_core_yoy(bls_series, bls_key or None)
            out["updates"]["coreInflation"] = core
            out["messages"].append(f"Inflación core interanual actualizada ({core['date']})")
        except Exception as e:
            out["messages"].append(f"Error BLS: {e}")

        return jsonify(out)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
