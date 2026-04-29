RIESGO MACRO - PAQUETE COMPLETO ÚLTIMA VERSIÓN

Contenido:
- app.py: backend Flask listo para Render.
- requirements.txt: dependencias del backend.
- frontend-netlify/index.html: frontend estático listo para Netlify.

Render:
1) Subir este contenido a GitHub.
2) En Render crear/actualizar Web Service apuntando a este repositorio.
3) Si Render pregunta por Root Directory, dejar vacío si app.py queda en la raíz.
4) Build Command: pip install -r requirements.txt
5) Start Command: gunicorn app:app
6) Variables recomendadas:
   - FRED_API_KEY: tu clave FRED.
   - PYTHON_VERSION: 3.11.9 o compatible.

Netlify:
- Si también querés desplegar el frontend, en Netlify usar:
  Base directory: frontend-netlify
  Publish directory: .
  Build command: vacío

Nota versión:
- Backend: pro-free-logistics-dynamic-multisource-v18-brent-yahoo-realtime.
- Brent intenta primero Yahoo Finance BZ=F para dato de mercado más reciente.
- Si Yahoo falla, usa FRED DCOILBRENTEU como fallback oficial, que puede venir con retraso.
