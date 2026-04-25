Backend PRO + Históricos listo para Render.

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

ENDPOINTS:
GET /health
POST /api/official-data
POST /api/history

NUEVO:
POST /api/history permite descargar históricos mensuales desde 2003 o el rango que indiques.
