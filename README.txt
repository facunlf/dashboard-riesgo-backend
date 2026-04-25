Backend v16 — correcciones de fuentes oficiales y visualización histórica.

Cambios principales:
- GSCPI ya no se pide como serie FRED dentro del flujo principal. Ahora se intenta leer desde el Excel oficial de la New York Fed.
- Si GSCPI falla, se intenta fallback con FRED y, si tampoco responde, se conserva el último valor sin romper toda la actualización.
- Inflación core intenta BLS primero. Si BLS devuelve valores no numéricos o falla, se calcula fallback interanual con la serie FRED CPILFESL.
- GDELT falla de forma segura: si no devuelve JSON o no responde, usa fallback neutral/no rompe el dashboard.
- Los mensajes ya no muestran errores técnicos para GSCPI/BLS/GDELT cuando hay fallback o conservación del último valor.
- Históricos enriquecidos con estrés logístico proxy, Supply Stress, Financial Stress, Risk Score, riesgo de recesión, drawdown esperado y probabilidades de escenarios.

Render:
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app

Variables:
FRED_API_KEY
BLS_API_KEY opcional
BLS_SERIES = CUUR0000SA0L1E
