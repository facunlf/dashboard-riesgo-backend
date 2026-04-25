Backend PRO + Estrés Logístico gratuito listo para Render.

NO requiere tarjeta de OpenAI.
NO requiere OPENAI_API_KEY.
NO requiere LOGISTICS_CACHE_HOURS.

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
- Actualiza indicadores FRED/BLS.
- Consulta titulares recientes vía GDELT.
- Calcula Estrés logístico con una heurística gratuita basada en palabras clave.
- No usa OpenAI API.
- No tiene coste por IA.
