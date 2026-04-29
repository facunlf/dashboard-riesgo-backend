RIESGO MACRO - PAQUETE COMPLETO v18

Estructura del repositorio
-------------------------
app.py                     Backend Flask para Render
requirements.txt           Dependencias Python del backend
render.yaml                Configuración Render del backend
netlify.toml               Configuración Netlify para desplegar solo el frontend
frontend-netlify/index.html Frontend estático para Netlify

Render
------
Render debe desplegar el backend completo desde la raíz del repositorio.

La configuración está en render.yaml:
- buildCommand: pip install -r requirements.txt
- startCommand: gunicorn app:app
- autoDeploy: true
- buildFilter: solo dispara autodeploy si cambian app.py, requirements.txt o render.yaml

Esto evita que Render redepliegue por cambios hechos únicamente en frontend-netlify/.

Netlify
-------
Netlify debe apuntar al mismo repositorio, pero usando:
- Base directory: frontend-netlify
- Publish directory: .
- Build command: echo 'Frontend estático: no build necesario'

Además, netlify.toml ya deja configurado:
- base = "frontend-netlify"
- publish = "."
- ignore = "git diff --quiet $CACHED_COMMIT_REF $COMMIT_REF -- frontend-netlify/ netlify.toml"

Importante: en Netlify, cuando el comando ignore devuelve 0, el deploy se cancela. Por eso esta configuración cancela deploys si solo cambió el backend.

Flujo recomendado
-----------------
1. Subir este ZIP descomprimido a GitHub.
2. En Render, conectar el repo usando render.yaml.
3. En Netlify, conectar el repo y dejar que lea netlify.toml.
4. Si modificás solo app.py o requirements.txt: despliega Render, Netlify no.
5. Si modificás solo frontend-netlify/index.html: despliega Netlify, Render no.
6. Si modificás render.yaml: Render procesa el cambio de configuración.
7. Si modificás netlify.toml: Netlify despliega porque puede afectar su configuración.

Brent realtime
--------------
El backend usa Yahoo Finance BZ=F como fuente principal para Brent y FRED DCOILBRENTEU solo como respaldo, porque FRED puede publicar con retraso.


CAMBIO v19 - Brent NYMEX:BZW00
- Brent ahora intenta primero Google Finance NYMEX:BZW00.
- Si Google Finance no responde, usa Yahoo Finance BZ=F como fallback de mercado.
- Si también falla y existe FRED_API_KEY, usa FRED DCOILBRENTEU como fallback oficial.
- La versión esperada en /health es pro-free-logistics-dynamic-multisource-v20-brent-nymex-vix-cboe.

CAMBIO v20 - VIX INDEXCBOE:VIX
- VIX ahora intenta primero Google Finance INDEXCBOE:VIX.
- Si esa fuente falla, usa Yahoo Finance ^VIX.
- FRED VIXCLS queda como fallback final, porque puede ir con retraso frente a mercado.
- Se mantiene la separación de despliegue: Netlify solo frontend y Render backend.
