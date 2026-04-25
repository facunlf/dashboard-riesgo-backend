Backend listo para Render con claves guardadas en variables de entorno.

ARCHIVOS:
- app.py
- requirements.txt

CONFIGURACIÓN EN RENDER:
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app

VARIABLES DE ENTORNO EN RENDER:
FRED_API_KEY = tu clave de FRED
BLS_API_KEY = tu clave de BLS, opcional
BLS_SERIES = CUUR0000SA0L1E

ENDPOINTS:
GET /health
POST /api/official-data

NOTA:
La FRED API key y la BLS API key quedan guardadas en Render, no en el navegador ni en el HTML.
