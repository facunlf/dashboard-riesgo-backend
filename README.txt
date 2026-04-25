Backend v14 + recesión calculada — FIX de errores.

Cambios:
- Se quitó GSCPI de FRED porque el código GSCPI devuelve HTTP 400 en FRED.
- BLS ahora ignora valores "-", ".", vacíos o no numéricos.
- GDELT ahora falla de forma segura: si no devuelve JSON o no responde, usa fallback neutral y no rompe el dashboard.
- Se mantienen:
  - FRED/BLS
  - PMI por noticias GDELT
  - estrés logístico por noticias GDELT
  - noticias macro/recesión por GDELT

Render:
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app

Variables:
FRED_API_KEY
BLS_API_KEY opcional
BLS_SERIES = CUUR0000SA0L1E
