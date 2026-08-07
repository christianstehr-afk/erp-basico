# ERP Básico · e-auto

ERP mínimo y modular para controlar el día a día de E-Auto. El corazón es la
conexión al SII para obtener facturas emitidas y recibidas, y una base de datos
que registra esas facturas junto con los pagos que E-Auto realiza y recibe.

Estética heredada del proyecto **TAG** (tema oscuro, verde e-auto, Montserrat).

## Estado (paso a paso)

- **Módulo 1 — Conexión al SII** ✅ Pantalla de acceso (RUT + Clave Tributaria) y
  autenticación contra el portal del SII.
- **Módulo 2 — Facturas (RCV)** ⏳ Descarga automática del Registro de Compras y
  Ventas (scraping).
- **Módulo 3 — Pagos** ⏳ Registro y conciliación de pagos emitidos y recibidos.

## Stack

- Python + FastAPI (HTML renderizado en servidor con Jinja2)
- SQLite (`data/erp.db`)
- `requests` para hablar con el SII

## Correr localmente

```bash
# 1. Crear entorno virtual e instalar dependencias
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Levantar el servidor
uvicorn app.main:app --reload --port 8000
```

Luego abre http://localhost:8000

## Estructura

```
app/
  main.py          # App FastAPI y rutas (/, /login, /logout)
  sii_client.py    # Autenticación contra el SII
  db.py            # Esquema SQLite (facturas, pagos)
  templates/       # login.html, dashboard.html
  static/          # styles.css (sistema de diseño heredado de TAG)
data/erp.db        # Base de datos (se crea sola, ignorada por git)
```

## Seguridad

Las credenciales del SII se usan solo para iniciar sesión durante la sesión web
y **no se almacenan**. Para el scraping automático programado (más adelante) se
definirá un mecanismo seguro de almacenamiento de credenciales.
