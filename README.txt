Backend PRO gratuito con Estrés Logístico + PMI por noticias.

NO requiere OpenAI.
NO requiere tarjeta.
NO requiere OPENAI_API_KEY.

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
- Calcula Estrés logístico con GDELT + heurística gratuita.
- Intenta extraer JPMorgan Global Composite PMI desde noticias/titulares recientes vía GDELT.
- Si PMI no puede extraerse, mantiene el último valor guardado en el frontend.
