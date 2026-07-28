"""
ERP Básico · e-auto — aplicación FastAPI.

Módulo 1: acceso al SII (RUT + Clave Tributaria).
Módulo 2: al iniciar sesión, selecciona la empresa E-Auto, sincroniza las
facturas recibidas 2026 en la base de datos y descarga sus PDF.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db, exportar, sii_docs, sync
from .sii_client import SIIAuthError, SIIClient

BASE_DIR = Path(__file__).resolve().parent

# RUT de la empresa a administrar en el SII (E-Auto SpA)
EMPRESA_RUT = os.environ.get("EMPRESA_RUT", "77708215-9")
ANIO = int(os.environ.get("ANIO", "2026"))
# Inicio en producción del ERP: solo se sincronizan documentos desde esta
# fecha (junio 2026) en adelante.
DESDE_SYNC = os.environ.get("DESDE_SYNC", "2026-05-01")

# Carpeta donde se guardan los adjuntos de rendiciones (boletas/facturas).
ADJUNTOS_DIR = Path(
    os.environ.get("ADJUNTOS_DIR", BASE_DIR.parent / "data" / "adjuntos" / "rendiciones")
)

app = FastAPI(title="ERP Básico · e-auto")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", secrets.token_hex(32)),
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Sesiones SII activas en memoria: sid -> SIIClient (nunca se guarda la clave)
SII_SESSIONS: dict[str, SIIClient] = {}


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def _current_client(request: Request) -> SIIClient | None:
    sid = request.session.get("sid")
    return SII_SESSIONS.get(sid) if sid else None


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    client = _current_client(request)
    if client and client.rut:
        conn = db.get_conn()
        try:
            hoy = date.today().isoformat()
            rechazadas = db.facturas_rechazadas(conn)
            pagos_vencidos = db.documentos_vencidos(conn, "compra", hoy)
            cobranza_vencida = db.documentos_vencidos(conn, "venta", hoy)
            rendiciones_pend = db.rendiciones_pendientes(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request, "rut": client.rut,
                "sync": sync.estado_sync, "anio": ANIO,
                "rechazadas": rechazadas, "pagos_vencidos": pagos_vencidos,
                "cobranza_vencida": cobranza_vencida,
                "rendiciones_pend": rendiciones_pend,
            },
        )
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, rut: str = Form(...), clave: str = Form(...)):
    client = SIIClient()
    try:
        client.login(rut, clave)
        client.seleccionar_empresa(EMPRESA_RUT)
    except SIIAuthError as exc:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": str(exc)}, status_code=401
        )

    sid = secrets.token_urlsafe(24)
    SII_SESSIONS[sid] = client
    request.session["sid"] = sid

    # Actualiza la BD con las facturas recibidas nuevas (PDFs en segundo plano)
    try:
        sync.sincronizar(client, anio=ANIO, desde=DESDE_SYNC)
    except Exception:
        pass  # si el SII falla, igual dejamos entrar al panel

    return RedirectResponse("/", status_code=303)


@app.post("/sync")
def sincronizar_ahora(request: Request):
    client = _current_client(request)
    if not client or not client.rut:
        return JSONResponse({"ok": False, "error": "no-session"}, status_code=401)
    sync.sincronizar_async(client, anio=ANIO, desde=DESDE_SYNC)
    return JSONResponse({"ok": True})


@app.get("/sync/estado")
def sync_estado(request: Request):
    """Estado actual de la sincronización, para la barra de progreso."""
    return JSONResponse(sync.estado_sync)


def _vista_documentos(request: Request, tipo: str, titulo: str, col_contraparte: str,
                      mostrar_pdf: bool, ruta: str):
    client = _current_client(request)
    if not client or not client.rut:
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        filas = conn.execute(
            "SELECT codigo_sii, documento, folio, rut_contraparte, razon_social, fecha_emision, "
            "total, estado, pdf_path, fecha_reclamo FROM facturas WHERE tipo=? "
            "ORDER BY fecha_emision DESC, folio DESC",
            (tipo,),
        ).fetchall()
        total_monto = conn.execute(
            "SELECT COALESCE(SUM(total),0) FROM facturas WHERE tipo=?", (tipo,)
        ).fetchone()[0]
    finally:
        conn.close()
    return templates.TemplateResponse(
        "facturas.html",
        {
            "request": request,
            "rut": client.rut,
            "filas": filas,
            "total_monto": total_monto,
            "anio": ANIO,
            "titulo": titulo,
            "col_contraparte": col_contraparte,
            "mostrar_pdf": mostrar_pdf,
            "ruta": ruta,
            "sync": sync.estado_sync,
        },
    )


@app.get("/recibidas", response_class=HTMLResponse)
def recibidas(request: Request):
    return _vista_documentos(request, "compra", "Facturas recibidas", "Emisor", True, "/recibidas")


@app.get("/emitidas", response_class=HTMLResponse)
def emitidas(request: Request):
    return _vista_documentos(request, "venta", "Facturas emitidas", "Receptor", True, "/emitidas")


@app.get("/facturas")
def facturas_alias():
    return RedirectResponse("/recibidas", status_code=303)


# Los PDF de facturas ya NO se guardan en disco: se piden al SII al momento
# de verlos, con la sesión activa del usuario. `tipo` en BD -> fuente sii_docs.
_FUENTE_POR_TIPO = {"compra": "recibidos", "venta": "emitidos"}


def _factura_por_codigo(codigo: str):
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT documento, folio, razon_social, rut_contraparte, tipo "
            "FROM facturas WHERE codigo_sii = ?",
            (codigo,),
        ).fetchone()
    finally:
        conn.close()


def _nombre_pdf(row) -> str:
    doc = (row["documento"] or "documento").replace(" ", "_")
    return f"{doc}_{row['folio']}.pdf"


@app.get("/pdf/{codigo}", response_class=HTMLResponse)
def pdf_viewer(request: Request, codigo: str):
    client = _current_client(request)
    if not client or not client.rut:
        return RedirectResponse("/", status_code=303)
    row = _factura_por_codigo(codigo)
    if not row:
        return HTMLResponse("<p>PDF no disponible.</p>", status_code=404)
    return templates.TemplateResponse(
        "pdf_viewer.html",
        {
            "request": request,
            "codigo": codigo,
            "documento": row["documento"],
            "folio": row["folio"],
            "razon_social": row["razon_social"],
            "rut_contraparte": row["rut_contraparte"],
        },
    )


@app.get("/pdf/{codigo}/ver")
def pdf_ver(request: Request, codigo: str):
    client = _current_client(request)
    if not client or not client.rut:
        return RedirectResponse("/", status_code=303)
    row = _factura_por_codigo(codigo)
    fuente = _FUENTE_POR_TIPO.get(row["tipo"]) if row else None
    if not row or not fuente:
        return Response("PDF no disponible", status_code=404)
    data = sii_docs.obtener_pdf_bytes(client.session, fuente, codigo)
    if not data:
        return Response("No se pudo obtener el PDF del SII. Intenta de nuevo.", status_code=502)
    # Sin filename => se muestra embebido (inline) en el visor
    return Response(content=data, media_type="application/pdf")


@app.get("/pdf/{codigo}/descargar")
def pdf_descargar(request: Request, codigo: str):
    client = _current_client(request)
    if not client or not client.rut:
        return RedirectResponse("/", status_code=303)
    row = _factura_por_codigo(codigo)
    fuente = _FUENTE_POR_TIPO.get(row["tipo"]) if row else None
    if not row or not fuente:
        return Response("PDF no disponible", status_code=404)
    data = sii_docs.obtener_pdf_bytes(client.session, fuente, codigo)
    if not data:
        return Response("No se pudo obtener el PDF del SII. Intenta de nuevo.", status_code=502)
    # Con filename => Content-Disposition attachment => fuerza la descarga
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_nombre_pdf(row)}"'},
    )


# ---------------------------------------------------------------------------
# Módulo 4 · Pagos
#
# Dos sub-módulos comparten toda la lógica y las plantillas, parametrizados por
# `seccion`: "proveedores" (facturas recibidas, E-Auto paga) e "ingresos"
# (facturas emitidas, E-Auto cobra). La única diferencia real es el tipo de
# factura, la dirección del movimiento y las etiquetas de la interfaz.
# ---------------------------------------------------------------------------

SECCIONES = {
    "proveedores": {
        "tipo": "compra", "direccion": "emitido",
        "titulo": "Pago a proveedores", "col_contraparte": "Proveedor",
        "estado_ok": "Pagada", "label_pagado": "Pagado", "accion": "pago",
    },
    "ingresos": {
        "tipo": "venta", "direccion": "recibido",
        "titulo": "Ingresos", "col_contraparte": "Cliente",
        "estado_ok": "Cobrada", "label_pagado": "Cobrado", "accion": "cobro",
    },
}


def _guard(request: Request):
    client = _current_client(request)
    return client if (client and client.rut) else None


@app.get("/pagos", response_class=HTMLResponse)
def pagos_home(request: Request):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("pagos.html", {"request": request, "rut": client.rut})


def _vista_lista(request: Request, seccion: str):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    cfg = SECCIONES[seccion]
    conn = db.get_conn()
    try:
        filas = db.facturas_con_pago(conn, tipo=cfg["tipo"])
        # Los totales y el conteo del encabezado ignoran las rechazadas (no se cobrarán).
        vigentes = [f for f in filas if not f["fecha_reclamo"]]
        total_monto = sum(f["total"] for f in vigentes)
        total_pendiente = sum(max(f["total"] - f["pagado"], 0) for f in vigentes)
        n_rechazadas = len(filas) - len(vigentes)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "pagos_lista.html",
        {
            "request": request, "rut": client.rut, "anio": ANIO,
            "seccion": seccion, "cfg": cfg, "filas": filas,
            "n_vigentes": len(vigentes), "n_rechazadas": n_rechazadas,
            "total_monto": total_monto, "total_pendiente": total_pendiente,
        },
    )


def _render_detalle(request: Request, client, seccion: str, codigo: str,
                    error: str | None = None, status_code: int = 200):
    cfg = SECCIONES[seccion]
    conn = db.get_conn()
    try:
        f = db.factura_pago_por_codigo(conn, codigo)
        if not f:
            return HTMLResponse("<p>Factura no encontrada.</p>", status_code=404)
        pagos = db.pagos_de_factura(conn, f["id"])
        rendiciones = []
        rend_asociada = None
        if seccion == "proveedores":
            rendiciones = [
                {"id": r["id"], "nombre": r["nombre"], "codigo": db.codigo_rendicion(r["id"])}
                for r in db.listar_rendiciones(conn)
            ]
            rid_asoc = db.rendicion_asociada_a_factura(conn, f["id"])
            if rid_asoc is not None:
                rend_asociada = {"id": rid_asoc, "codigo": db.codigo_rendicion(rid_asoc)}
    finally:
        conn.close()
    return templates.TemplateResponse(
        "pago_detalle.html",
        {
            "request": request, "rut": client.rut, "anio": ANIO,
            "seccion": seccion, "cfg": cfg, "f": f, "pagos": pagos,
            "hoy": date.today().isoformat(), "saldo": (f["total"] - f["pagado"]),
            "error": error, "rendiciones": rendiciones, "rend_asociada": rend_asociada,
        },
        status_code=status_code,
    )


def _guardar_fecha_tope(request: Request, seccion: str, codigo: str, fecha_tope: str):
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        db.set_fecha_tope(conn, codigo, fecha_tope.strip())
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/pagos/{seccion}/{codigo}", status_code=303)


def _agregar_movimiento(request: Request, seccion: str, codigo: str,
                        fecha: str, monto: str, rendicion_id: str = ""):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    cfg = SECCIONES[seccion]
    fecha = fecha.strip()
    try:
        monto_int = int(float(monto))
    except (ValueError, TypeError):
        monto_int = 0
    if monto_int <= 0:
        return _render_detalle(request, client, seccion, codigo,
                               error="El monto debe ser mayor a cero.", status_code=400)
    try:
        f_mov = date.fromisoformat(fecha)
    except ValueError:
        return _render_detalle(request, client, seccion, codigo,
                               error="Fecha inválida.", status_code=400)
    if f_mov > date.today():
        return _render_detalle(request, client, seccion, codigo,
                               error="No se permiten fechas futuras.", status_code=400)

    conn = db.get_conn()
    try:
        f = db.factura_pago_por_codigo(conn, codigo)
        if not f:
            return HTMLResponse("<p>Factura no encontrada.</p>", status_code=404)
        if f["fecha_reclamo"]:
            return _render_detalle(request, client, seccion, codigo,
                                   error="La factura está rechazada; no admite movimientos.",
                                   status_code=400)
        saldo = f["total"] - f["pagado"]
        if monto_int > saldo:
            saldo_fmt = "{:,.0f}".format(saldo).replace(",", ".")
            return _render_detalle(request, client, seccion, codigo,
                                   error=f"El monto supera el saldo pendiente (${saldo_fmt}).",
                                   status_code=400)
        # Pago vía rendición (solo pago a proveedores): valida la rendición y la
        # regla "una factura -> una sola rendición".
        rid = None
        rid_str = (rendicion_id or "").strip()
        if rid_str:
            try:
                rid = int(rid_str)
            except ValueError:
                rid = None
            if rid is None or not db.rendicion_por_id(conn, rid):
                return _render_detalle(request, client, seccion, codigo,
                                       error="Selecciona una rendición válida.",
                                       status_code=400)
            asoc = db.rendicion_asociada_a_factura(conn, f["id"])
            if asoc is not None and asoc != rid:
                return _render_detalle(
                    request, client, seccion, codigo,
                    error=f"La factura ya está asociada a la rendición {db.codigo_rendicion(asoc)}; "
                          "no puede asociarse a otra.",
                    status_code=400)
        db.agregar_pago(conn, f["id"], fecha, monto_int,
                        direccion=cfg["direccion"], rendicion_id=rid)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/pagos/{seccion}/{codigo}", status_code=303)


def _eliminar_movimiento(request: Request, seccion: str, codigo: str, pago_id: int):
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        f = db.factura_pago_por_codigo(conn, codigo)
        if f:
            db.eliminar_pago(conn, pago_id, f["id"])
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/pagos/{seccion}/{codigo}", status_code=303)


# ---- Pago a proveedores (facturas recibidas) ----

@app.get("/pagos/proveedores", response_class=HTMLResponse)
def proveedores_lista(request: Request):
    return _vista_lista(request, "proveedores")


@app.get("/pagos/proveedores/{codigo}", response_class=HTMLResponse)
def proveedores_detalle(request: Request, codigo: str):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    return _render_detalle(request, client, "proveedores", codigo)


@app.post("/pagos/proveedores/{codigo}/fecha-tope")
def proveedores_fecha_tope(request: Request, codigo: str, fecha_tope: str = Form(...)):
    return _guardar_fecha_tope(request, "proveedores", codigo, fecha_tope)


@app.post("/pagos/proveedores/{codigo}/pago")
def proveedores_agregar(request: Request, codigo: str,
                        fecha: str = Form(...), monto: str = Form(...),
                        rendicion_id: str = Form("")):
    return _agregar_movimiento(request, "proveedores", codigo, fecha, monto, rendicion_id)


@app.post("/pagos/proveedores/{codigo}/pago/{pago_id}/eliminar")
def proveedores_eliminar(request: Request, codigo: str, pago_id: int):
    return _eliminar_movimiento(request, "proveedores", codigo, pago_id)


# ---- Ingresos (facturas emitidas) ----

@app.get("/pagos/ingresos", response_class=HTMLResponse)
def ingresos_lista(request: Request):
    return _vista_lista(request, "ingresos")


@app.get("/pagos/ingresos/{codigo}", response_class=HTMLResponse)
def ingresos_detalle(request: Request, codigo: str):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    return _render_detalle(request, client, "ingresos", codigo)


@app.post("/pagos/ingresos/{codigo}/fecha-tope")
def ingresos_fecha_tope(request: Request, codigo: str, fecha_tope: str = Form(...)):
    return _guardar_fecha_tope(request, "ingresos", codigo, fecha_tope)


@app.post("/pagos/ingresos/{codigo}/pago")
def ingresos_agregar(request: Request, codigo: str,
                     fecha: str = Form(...), monto: str = Form(...)):
    return _agregar_movimiento(request, "ingresos", codigo, fecha, monto)


@app.post("/pagos/ingresos/{codigo}/pago/{pago_id}/eliminar")
def ingresos_eliminar(request: Request, codigo: str, pago_id: int):
    return _eliminar_movimiento(request, "ingresos", codigo, pago_id)


# ---------------------------------------------------------------------------
# Módulo 4 · Rendiciones (gastos pagados por la empresa)
# ---------------------------------------------------------------------------

def _nombre_seguro(nombre: str) -> str:
    """Sanea un nombre de archivo: conserva la base y caracteres seguros."""
    base = os.path.basename(nombre or "").strip() or "archivo"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    return base[:120]


def _guardar_adjuntos(conn, rid: int, archivos: list[UploadFile]) -> None:
    """Guarda en disco los archivos subidos y los registra en la BD."""
    destino = ADJUNTOS_DIR / str(rid)
    for up in archivos or []:
        if not up or not (up.filename or "").strip():
            continue  # input de archivo vacío
        destino.mkdir(parents=True, exist_ok=True)
        seguro = _nombre_seguro(up.filename)
        ruta = destino / seguro
        i = 1
        while ruta.exists():  # evita sobrescribir
            ruta = destino / f"{ruta.stem}_{i}{ruta.suffix}"
            i += 1
        with ruta.open("wb") as fh:
            shutil.copyfileobj(up.file, fh)
        db.agregar_adjunto(conn, rid, up.filename, str(ruta))


def _render_rendicion(request: Request, client, rid: int,
                      error: str | None = None, status_code: int = 200):
    conn = db.get_conn()
    try:
        r = db.rendicion_por_id(conn, rid)
        if not r:
            return HTMLResponse("<p>Rendición no encontrada.</p>", status_code=404)
        items = db.items_de_rendicion(conn, rid)
        adjuntos = db.adjuntos_de_rendicion(conn, rid)
        pagos = db.pagos_de_rendicion(conn, rid)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "rendicion_detalle.html",
        {
            "request": request, "rut": client.rut, "r": r, "items": items,
            "adjuntos": adjuntos, "pagos": pagos, "hoy": date.today().isoformat(),
            "saldo": (r["total"] - r["pagado"]), "error": error,
        },
        status_code=status_code,
    )


@app.get("/pagos/rendiciones", response_class=HTMLResponse)
def rendiciones_lista(request: Request):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        filas = db.listar_rendiciones(conn)
        total_monto = sum(f["total"] for f in filas)
        total_pendiente = sum(max(f["total"] - f["pagado"], 0) for f in filas)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "rendiciones_lista.html",
        {
            "request": request, "rut": client.rut, "filas": filas,
            "total_monto": total_monto, "total_pendiente": total_pendiente,
        },
    )


@app.get("/pagos/rendiciones/nueva", response_class=HTMLResponse)
def rendicion_nueva_form(request: Request):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "rendicion_nueva.html",
        {"request": request, "rut": client.rut, "hoy": date.today().isoformat(), "error": None},
    )


@app.post("/pagos/rendiciones/nueva")
async def rendicion_nueva_crear(
    request: Request,
    nombre: str = Form(...),
    fecha: str = Form(...),
    item_descripcion: list[str] = Form(default=[]),
    item_numero: list[str] = Form(default=[]),
    item_monto: list[str] = Form(default=[]),
    archivos: list[UploadFile] = File(default=[]),
):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)

    def _remostrar(msg: str):
        return templates.TemplateResponse(
            "rendicion_nueva.html",
            {"request": request, "rut": client.rut, "hoy": date.today().isoformat(),
             "error": msg, "nombre": nombre, "fecha": fecha},
            status_code=400,
        )

    nombre = (nombre or "").strip()
    fecha = (fecha or "").strip()
    if not nombre:
        return _remostrar("El nombre es obligatorio.")
    try:
        date.fromisoformat(fecha)
    except ValueError:
        return _remostrar("La fecha de la rendición no es válida.")

    # Arma los ítems válidos (descripción no vacía y monto > 0).
    items = []
    for i in range(len(item_descripcion)):
        desc = (item_descripcion[i] or "").strip()
        numero = (item_numero[i] if i < len(item_numero) else "").strip()
        monto_raw = item_monto[i] if i < len(item_monto) else "0"
        try:
            monto = int(float(monto_raw))
        except (ValueError, TypeError):
            monto = 0
        if desc and monto > 0:
            items.append({"descripcion": desc, "numero_doc": numero, "monto": monto})
    if not items:
        return _remostrar("Agrega al menos un ítem con descripción y monto mayor a cero.")

    conn = db.get_conn()
    try:
        rid = db.crear_rendicion(conn, nombre, fecha, items)
        _guardar_adjuntos(conn, rid, archivos)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/pagos/rendiciones/{rid}", status_code=303)


@app.get("/pagos/rendiciones/{rid}/pdf")
def rendicion_pdf(request: Request, rid: int):
    """Devuelve el PDF de una sola rendición (inline, para abrirlo en el navegador)."""
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        r = db.rendicion_por_id(conn, rid)
        if not r:
            return HTMLResponse("<p>Rendición no encontrada.</p>", status_code=404)
        items = db.items_de_rendicion(conn, rid)
        adjuntos = db.adjuntos_de_rendicion(conn, rid)
        pagos = db.pagos_de_rendicion(conn, rid)
    finally:
        conn.close()
    data = exportar._pdf_de_rendicion(r, items, adjuntos, pagos)
    return Response(content=data, media_type="application/pdf")


@app.get("/pagos/rendiciones/{rid}", response_class=HTMLResponse)
def rendicion_detalle(request: Request, rid: int):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    return _render_rendicion(request, client, rid)


@app.post("/pagos/rendiciones/{rid}/pago")
def rendicion_agregar_pago(request: Request, rid: int,
                           fecha: str = Form(...), monto: str = Form(...)):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    fecha = fecha.strip()
    try:
        monto_int = int(float(monto))
    except (ValueError, TypeError):
        monto_int = 0
    if monto_int <= 0:
        return _render_rendicion(request, client, rid,
                                 error="El monto debe ser mayor a cero.", status_code=400)
    try:
        f_pago = date.fromisoformat(fecha)
    except ValueError:
        return _render_rendicion(request, client, rid,
                                 error="Fecha inválida.", status_code=400)
    if f_pago > date.today():
        return _render_rendicion(request, client, rid,
                                 error="No se permiten fechas futuras.", status_code=400)
    conn = db.get_conn()
    try:
        r = db.rendicion_por_id(conn, rid)
        if not r:
            return HTMLResponse("<p>Rendición no encontrada.</p>", status_code=404)
        saldo = r["total"] - r["pagado"]
        if monto_int > saldo:
            saldo_fmt = "{:,.0f}".format(saldo).replace(",", ".")
            return _render_rendicion(request, client, rid,
                                     error=f"El monto supera el saldo pendiente (${saldo_fmt}).",
                                     status_code=400)
        db.agregar_pago_rendicion(conn, rid, fecha, monto_int)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/pagos/rendiciones/{rid}", status_code=303)


@app.post("/pagos/rendiciones/{rid}/pago/{pago_id}/eliminar")
def rendicion_eliminar_pago(request: Request, rid: int, pago_id: int):
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        db.eliminar_pago_rendicion(conn, pago_id, rid)
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/pagos/rendiciones/{rid}", status_code=303)


@app.post("/pagos/rendiciones/{rid}/adjunto")
async def rendicion_agregar_adjunto(request: Request, rid: int,
                                    archivos: list[UploadFile] = File(default=[])):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        if db.rendicion_por_id(conn, rid):
            _guardar_adjuntos(conn, rid, archivos)
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/pagos/rendiciones/{rid}", status_code=303)


@app.get("/pagos/rendiciones/{rid}/adjunto/{adj_id}/descargar")
def rendicion_descargar_adjunto(request: Request, rid: int, adj_id: int):
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        adj = db.adjunto_por_id(conn, adj_id)
    finally:
        conn.close()
    if not adj or adj["rendicion_id"] != rid or not Path(adj["path"]).exists():
        return Response("Adjunto no disponible", status_code=404)
    return FileResponse(adj["path"], filename=adj["nombre_archivo"])


@app.post("/pagos/rendiciones/{rid}/adjunto/{adj_id}/eliminar")
def rendicion_eliminar_adjunto(request: Request, rid: int, adj_id: int):
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        adj = db.adjunto_por_id(conn, adj_id)
        if adj and adj["rendicion_id"] == rid:
            db.eliminar_adjunto(conn, adj_id, rid)
            conn.commit()
            try:
                Path(adj["path"]).unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        conn.close()
    return RedirectResponse(f"/pagos/rendiciones/{rid}", status_code=303)


@app.post("/pagos/rendiciones/{rid}/eliminar")
def rendicion_eliminar(request: Request, rid: int):
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        paths = db.eliminar_rendicion(conn, rid)
        conn.commit()
    finally:
        conn.close()
    for p in paths:  # borra archivos del disco
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
    return RedirectResponse("/pagos/rendiciones", status_code=303)


# ---------------------------------------------------------------------------
# Módulo 5 · Export compras/ventas y Rendiciones
#
# Consolida los movimientos reales de caja (cada pago/cobro registrado) en un
# rango de fechas para que el contador los cruce con la cartola bancaria.
# Exporta el listado a Excel y genera un PDF por rendición (con sus adjuntos).
# ---------------------------------------------------------------------------

def _rango(desde: str | None, hasta: str | None) -> tuple[str, str]:
    """Normaliza el rango de fechas. Default: 1 de enero del año en curso a hoy."""
    hoy = date.today().isoformat()
    d = (desde or "").strip() or f"{ANIO}-01-01"
    h = (hasta or "").strip() or hoy
    try:
        date.fromisoformat(d)
    except ValueError:
        d = f"{ANIO}-01-01"
    try:
        date.fromisoformat(h)
    except ValueError:
        h = hoy
    if d > h:
        d, h = h, d
    return d, h


@app.get("/export", response_class=HTMLResponse)
def export_home(request: Request, desde: str = "", hasta: str = ""):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango(desde, hasta)
    conn = db.get_conn()
    try:
        movs = db.movimientos_en_rango(conn, d, h)
    finally:
        conn.close()
    total_ing = sum(m["monto"] for m in movs if m["flujo"] == "Ingreso")
    total_egr = sum(m["monto"] for m in movs if m["flujo"] == "Egreso")
    return templates.TemplateResponse(
        "export.html",
        {
            "request": request, "rut": client.rut,
            "desde": d, "hasta": h, "movs": movs,
            "total_ingresos": total_ing, "total_egresos": total_egr,
            "neto": total_ing - total_egr,
        },
    )


@app.get("/export/excel")
def export_excel(request: Request, desde: str = "", hasta: str = ""):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango(desde, hasta)
    conn = db.get_conn()
    try:
        movs = db.movimientos_en_rango(conn, d, h)
    finally:
        conn.close()
    data = exportar.construir_excel(movs, d, h)
    headers = {"Content-Disposition": f'attachment; filename="movimientos_{d}_a_{h}.xlsx"'}
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@app.get("/export/rendiciones")
def export_rendiciones(request: Request, desde: str = "", hasta: str = ""):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango(desde, hasta)
    conn = db.get_conn()
    try:
        filas = db.rendiciones_con_pago_en_rango(conn, d, h)
        rends = []
        for r in filas:
            rends.append({
                "r": r,
                "items": db.items_de_rendicion(conn, r["id"]),
                "adjuntos": db.adjuntos_de_rendicion(conn, r["id"]),
                "pagos": db.pagos_de_rendicion(conn, r["id"]),
            })
    finally:
        conn.close()
    if not rends:
        return HTMLResponse(
            "<p>No hay rendiciones con pagos en el rango seleccionado.</p>",
            status_code=404,
        )
    data = exportar.construir_zip_rendiciones(rends)
    headers = {"Content-Disposition": f'attachment; filename="rendiciones_{d}_a_{h}.zip"'}
    return Response(content=data, media_type="application/zip", headers=headers)


# ---------------------------------------------------------------------------
# Migración única: local (Mac de Christian) -> Railway (producción)
#
# Herramienta de administración: migración local -> Railway (rendiciones/pagos/
# cobros) y respaldo/descarga de la BD completa. Solo funciona si ADMIN_SECRET
# está seteada como variable de entorno; si no, los 5 endpoints devuelven 404
# (o sea: quitar la variable en Railway = apagar la herramienta). Ver
# migrar_a_railway.py.
# ---------------------------------------------------------------------------

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")


def _admin_guard(secret: str) -> bool:
    return bool(ADMIN_SECRET) and secrets.compare_digest((secret or ""), ADMIN_SECRET)


@app.get("/admin/backup")
def admin_backup(secret: str = ""):
    """Descarga una copia consistente del archivo completo de la BD (.db).

    Usa la API de backup de sqlite3 (no una copia de archivo cruda) para que
    la copia sea válida aunque haya escrituras en curso.
    """
    if not _admin_guard(secret):
        return Response(status_code=404)
    import sqlite3
    import tempfile
    from datetime import datetime as _dt

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        origen = sqlite3.connect(db.DB_PATH)
        destino = sqlite3.connect(tmp_path)
        with destino:
            origen.backup(destino)
        origen.close()
        destino.close()
        data = Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    nombre = f"erp_backup_{_dt.now():%Y%m%d_%H%M%S}.db"
    return Response(
        content=data, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@app.get("/admin/export")
def admin_export(secret: str = ""):
    if not _admin_guard(secret):
        return Response(status_code=404)
    conn = db.get_conn()
    try:
        rendiciones = []
        for r in conn.execute("SELECT id, nombre, fecha FROM rendiciones ORDER BY id").fetchall():
            items = [
                {"descripcion": i["descripcion"], "numero_doc": i["numero_doc"], "monto": i["monto"]}
                for i in conn.execute(
                    "SELECT descripcion, numero_doc, monto FROM rendicion_items "
                    "WHERE rendicion_id=? ORDER BY id", (r["id"],)
                ).fetchall()
            ]
            pagos_r = [
                {"fecha": p["fecha"], "monto": p["monto"]}
                for p in conn.execute(
                    "SELECT fecha, monto FROM rendicion_pagos WHERE rendicion_id=? ORDER BY id",
                    (r["id"],)
                ).fetchall()
            ]
            adjuntos = [
                {"local_id": a["id"], "nombre_archivo": a["nombre_archivo"]}
                for a in conn.execute(
                    "SELECT id, nombre_archivo FROM rendicion_adjuntos "
                    "WHERE rendicion_id=? ORDER BY id", (r["id"],)
                ).fetchall()
            ]
            rendiciones.append({
                "local_id": r["id"], "nombre": r["nombre"], "fecha": r["fecha"],
                "items": items, "pagos": pagos_r, "adjuntos": adjuntos,
            })

        pagos_facturas = [
            {
                "codigo_sii": p["codigo_sii"], "direccion": p["direccion"],
                "fecha": p["fecha"], "monto": p["monto"],
                "rendicion_local_id": p["rendicion_id"],
            }
            for p in conn.execute(
                "SELECT p.direccion, p.fecha, p.monto, p.rendicion_id, f.codigo_sii "
                "FROM pagos p JOIN facturas f ON f.id = p.factura_id "
                "ORDER BY p.id"
            ).fetchall()
        ]

        tope = [
            {"codigo_sii": t["codigo_sii"], "fecha_pago_tope": t["fecha_pago_tope"]}
            for t in conn.execute(
                "SELECT codigo_sii, fecha_pago_tope FROM facturas "
                "WHERE fecha_pago_tope IS NOT NULL AND fecha_pago_tope != '' "
                "AND fecha_pago_tope != fecha_emision"
            ).fetchall()
        ]
    finally:
        conn.close()
    return JSONResponse({"rendiciones": rendiciones, "pagos_facturas": pagos_facturas, "fecha_pago_tope": tope})


@app.get("/admin/export/adjunto/{local_id}")
def admin_export_adjunto(local_id: int, secret: str = ""):
    if not _admin_guard(secret):
        return Response(status_code=404)
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT nombre_archivo, path FROM rendicion_adjuntos WHERE id=?", (local_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not Path(row["path"]).exists():
        return Response(status_code=404)
    return FileResponse(row["path"], filename=row["nombre_archivo"])


@app.post("/admin/import")
async def admin_import(request: Request, secret: str = ""):
    if not _admin_guard(secret):
        return Response(status_code=404)
    payload = await request.json()
    resumen = {
        "rendiciones_creadas": 0, "pagos_facturas_ok": 0,
        "pagos_facturas_sin_factura": [], "id_map": {},
    }
    conn = db.get_conn()
    try:
        # Mapeo de ids ya conocido de antemano (rendiciones migradas en una corrida
        # anterior): local_id -> id ya existente en esta BD. Se aplica ANTES de crear
        # las nuevas para que los pagos de factura "vía rendición" resuelvan bien.
        resumen["id_map"].update(payload.get("rendicion_id_map", {}) or {})

        for r in payload.get("rendiciones", []):
            rid = db.crear_rendicion(conn, r["nombre"], r["fecha"], r.get("items", []))
            resumen["id_map"][str(r["local_id"])] = rid
            resumen["rendiciones_creadas"] += 1
            for p in r.get("pagos", []):
                db.agregar_pago_rendicion(conn, rid, p["fecha"], p["monto"])

        for p in payload.get("pagos_facturas", []):
            codigo = p.get("codigo_sii")
            if not codigo:
                resumen["pagos_facturas_sin_factura"].append(codigo)
                continue
            row = conn.execute("SELECT id FROM facturas WHERE codigo_sii=?", (codigo,)).fetchone()
            if not row:
                resumen["pagos_facturas_sin_factura"].append(codigo)
                continue
            rid_new = None
            if p.get("rendicion_local_id") is not None:
                rid_new = resumen["id_map"].get(str(p["rendicion_local_id"]))
            db.agregar_pago(conn, row["id"], p["fecha"], p["monto"],
                            direccion=p["direccion"], rendicion_id=rid_new)
            resumen["pagos_facturas_ok"] += 1

        for t in payload.get("fecha_pago_tope", []):
            if t.get("codigo_sii"):
                db.set_fecha_tope(conn, t["codigo_sii"], t["fecha_pago_tope"])

        conn.commit()
    finally:
        conn.close()
    return JSONResponse(resumen)


@app.post("/admin/import/adjunto")
async def admin_import_adjunto(rendicion_id: int = Form(...), secret: str = Form(...),
                               archivo: UploadFile = File(...)):
    if not _admin_guard(secret):
        return Response(status_code=404)
    conn = db.get_conn()
    try:
        if not db.rendicion_por_id(conn, rendicion_id):
            return JSONResponse({"ok": False, "error": "rendición no encontrada"}, status_code=404)
        _guardar_adjuntos(conn, rendicion_id, [archivo])
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})


@app.post("/logout")
def logout(request: Request):
    sid = request.session.get("sid")
    if sid and sid in SII_SESSIONS:
        SII_SESSIONS.pop(sid).logout()
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}
