# Despliegue · ERP Básico

La app corre completa en **Railway** (FastAPI + SQLite + PDFs), con un **volumen
persistente** montado en `/data` para no perder datos entre despliegues.
**Vercel** se usa solo como puerta de entrada (dominio que redirige a Railway).

## 1. Subir el código a GitHub (lo hace Christian)

Desde la carpeta del proyecto, en tu Mac:

```bash
cd "/Users/cstehr/Library/CloudStorage/Dropbox/Proyectos Claude/ERP Básico"
git init
git add .
git commit -m "ERP Básico — listo para desplegar"
git branch -M main
# Crea un repo vacío en https://github.com/new (ej: erp-basico) y luego:
git remote add origin https://github.com/christianstehr-afk/erp-basico.git
git push -u origin main
```

`data/`, `.venv/` y `.env` están en `.gitignore`: no se suben ni la base de
datos ni credenciales.

Cuando esté subido, avísame el nombre del repo (`owner/name`) y yo conecto
Railway y Vercel.

## 2. Railway (lo hago yo con los conectores)

- Proyecto nuevo → servicio desde el repo de GitHub (Dockerfile incluido).
- Volumen persistente en `/data`.
- Variables: `DB_PATH=/data/erp.db`, `PDF_DIR=/data/pdfs`,
  `ADJUNTOS_DIR=/data/adjuntos/rendiciones`, `SECRET_KEY=<fijo>`,
  `EMPRESA_RUT=77708215-9`, `ANIO=2026`, `DESDE_SYNC=2026-06-01`.
- Dominio `*.up.railway.app`.

## 3. Vercel (lo hago yo)

- Redirección del dominio a la URL de Railway.

## Móvil

La misma app está optimizada para teléfono con CSS responsivo: en pantallas
angostas el cockpit pasa a una columna, los botones se apilan y los campos se
agrandan. La vista de escritorio no cambia.
