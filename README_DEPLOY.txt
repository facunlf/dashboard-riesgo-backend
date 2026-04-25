Backend PresuFlow/Macro Dashboard - estrés logístico dinámico

Qué cambia:
- Reemplaza el cálculo simple de estrés logístico por un cálculo dinámico multi-query en GDELT.
- Busca señales de crisis: ataques, barcos capturados, cierre/bloqueo, navieras suspendiendo rutas, desvíos, war risk insurance, fletes, congestión.
- Busca señales de normalización: acuerdo, ceasefire, reapertura, shipping resumes, traffic resumes, normal traffic, rutas restauradas, caída de seguros/fletes.
- No usa override fijo. Si mañana las noticias muestran normalización real, el score puede caer automáticamente.
- Añade diagnostics al resultado para entender articleCount, sourceDiversity, crisisArticles, severeArticles, reliefArticles, weightedCrisis y weightedRelief.

Dónde cargarlo:
- Este archivo app.py va en el repositorio BACKEND que desplegás en Render.
- NO va en Netlify. Netlify solo usa el HTML/frontend.

Render:
Build Command:
pip install -r requirements.txt

Start Command:
gunicorn app:app

Variable necesaria:
FRED_API_KEY=tu_clave_de_fred

Pasos:
1. Reemplazá el app.py actual de tu repositorio backend por este app.py.
2. Conservá requirements.txt.
3. Hacé commit y push a GitHub.
4. Render debería redesplegar automáticamente.
5. Probá en el navegador:
   https://TU-BACKEND.onrender.com/health
6. Luego, desde el dashboard en Netlify, pulsá "Actualizar indicadores".

Prueba directa del endpoint:
POST https://TU-BACKEND.onrender.com/api/logistics-stress-news

Respuesta esperada:
{
  "ok": true,
  "result": {
    "latest": 0-100,
    "level": "bajo|leve|moderado|alto|severo",
    "source": "GDELT multi-query dynamic heuristic",
    "articleCount": número,
    "diagnostics": {...}
  }
}

Si articleCount = 0:
- El problema no es la fórmula: Render no pudo obtener noticias de GDELT o GDELT no devolvió resultados.
- En ese caso revisá logs de Render y conectividad externa.
