Backend PRO listo para Render.

ARCHIVOS:
- app.py
- requirements.txt

CONFIGURACIÓN EN RENDER:
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app

VARIABLES DE ENTORNO:
FRED_API_KEY = tu clave de FRED
BLS_API_KEY = tu clave de BLS, opcional
BLS_SERIES = CUUR0000SA0L1E

INDICADORES AUTOMÁTICOS:
- Brent: DCOILBRENTEU
- High Yield OAS: BAMLH0A0HYM2
- VIX: VIXCLS
- USD trade weighted: DTWEXBGS
- 10Y yield: DGS10
- 2Y yield: DGS2
- Curva 10Y-2Y: DGS10 - DGS2
- 10Y breakeven inflation: T10YIE
- 10Y real yield: DFII10
- Unemployment: UNRATE
- Core CPI YoY: BLS CUUR0000SA0L1E
