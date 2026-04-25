Backend basado en v14 + Riesgo de Recesión por noticias macro.

NO requiere OpenAI.
NO requiere tarjeta.

CONFIGURACIÓN EN RENDER:
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app

VARIABLES DE ENTORNO:
FRED_API_KEY = tu clave de FRED
BLS_API_KEY = tu clave de BLS, opcional
BLS_SERIES = CUUR0000SA0L1E

QUÉ HACE:
- Actualiza FRED/BLS.
- Calcula estrés logístico vía GDELT + heurística.
- Intenta extraer JPMorgan Global Composite PMI desde noticias/titulares vía GDELT.
- Calcula un score de noticias macro/recesión vía GDELT + heurística.
- El frontend usa ese score como parte del cálculo ponderado de Riesgo de Recesión.
