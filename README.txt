# Backend listo para Render

## Archivos
- `app.py`
- `requirements.txt`

## Configuración en Render
- Runtime: Python
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`

## Endpoint principal
- `POST /api/official-data`

## Health check
- `GET /health`
