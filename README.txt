Backend v18 — Core PCE desde FRED/BEA + fechas por indicador.

Cambios principales:
- El indicador interno coreInflation ahora representa Core PCE interanual.
- Se calcula desde FRED con la serie PCEPILFE, cuya fuente económica es BEA.
- Ya no se usa BLS para la métrica principal de inflación core del dashboard.
- Los históricos calculan Core PCE YoY para cada mes usando el mismo criterio.
- Se mantienen las correcciones previas:
  - GSCPI intenta leer el Excel oficial de la New York Fed y usa fallback seguro.
  - GDELT falla de forma segura si no devuelve JSON o no responde.
  - Históricos enriquecidos con estrés logístico proxy, Supply Stress, Financial Stress, Risk Score, riesgo de recesión, drawdown esperado y probabilidades de escenarios.

Render:
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app

Variables necesarias:
FRED_API_KEY


Cambios v18:
- /api/official-data devuelve también fecha y fuente para indicadores calculados como Supply Stress y Riesgo de recesión.
- Mantiene fechas individuales por indicador para que el frontend pueda mostrarlas debajo de cada valor.

Cambios v19:
- Core PCE se obtiene seleccionando explícitamente la observación más reciente válida de FRED PCEPILFE.
- Se devuelve la fecha completa de la observación mensual, por ejemplo 2026-02-01.
- Se calcula también el valor previo interanual del mes anterior para que el frontend pueda mostrar x% vs previo.
- Si FRED devolviera una observación demasiado antigua, el backend la rechaza y no la muestra como dato actual.


Cambios v20:
- /api/official-data devuelve date, inputDataDate, calculatedAt y fetchedAt para cada indicador actualizado.
- El PMI global ya no queda sin fecha: si no se extrae desde titulares, devuelve proxy macro calculado.
- El frontend muestra fecha del último dato obtenido/usado y fecha de actualización/cálculo con formato dd/mm/yyyy.
