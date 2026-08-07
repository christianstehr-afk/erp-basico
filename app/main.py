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
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db, exportar, sii_bhe, sii_docs, sync
from .sii_client import SIIAuthError, SIIClient, SIISessionExpirada

BASE_DIR = Path(__file__).resolve().parent

# RUT de la empresa a administrar en el SII (E-Auto SpA)
EMPRESA_RUT = os.environ.get("EMPRESA_RUT", "77708215-9")
ANIO = int(os.environ.get("ANIO", "2026"))
# Inicio en producción del ERP: solo se sincronizan documentos desde esta
# fecha en adelante. Vuelto a "2026-06-01" el 2026-08-05 (estuvo en
# "2025-11-01" solo para el backfill puntual de nov-dic 2025, ya hecho). Lo
# sincronizado antes de este corte queda intacto en la BD: este valor solo
# acota qué se vuelve a consultar en el SII de aquí en adelante, nunca borra
# nada. sii_bhe/sii_docs ya soportan recorrer varios años si hiciera falta
# otro backfill (ver sync.py:_anios_a_sincronizar).
DESDE_SYNC = os.environ.get("DESDE_SYNC", "2026-06-01")

# Carpeta donde se guardan los adjuntos de rendiciones (boletas/facturas).
ADJUNTOS_DIR = Path(
    os.environ.get("ADJUNTOS_DIR", BASE_DIR.parent / "data" / "adjuntos" / "rendiciones")
)
# Carpeta donde se guardan los adjuntos de la gestión de una factura (pago a
# proveedores / ingresos): documentos de respaldo aparte de la descripción.
# Por defecto, HERMANA de ADJUNTOS_DIR (no una ruta fija aparte): así, en
# producción, donde ADJUNTOS_DIR=/data/adjuntos/rendiciones (dentro del
# volumen persistente de Railway), esto cae solo en /data/adjuntos/facturas
# sin necesitar una variable de entorno nueva. Igual se puede sobreescribir
# con ADJUNTOS_FACTURAS_DIR si hiciera falta.
ADJUNTOS_FACTURAS_DIR = Path(
    os.environ.get("ADJUNTOS_FACTURAS_DIR", ADJUNTOS_DIR.parent / "facturas")
)

# Carpeta donde queda guardada cada copia del Excel de log descargado desde el
# Cockpit (además de servirse como descarga en el navegador). Vive dentro del
# proyecto (Dropbox): son archivos estáticos, sin el riesgo que sí tiene la BD
# viva ahí (ver comentario de DB_PATH en db.py).
LOG_DIR = Path(os.environ.get("LOG_DIR", BASE_DIR.parent / "Log"))

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
# Segunda sesión SII en paralelo, con la cuenta "empresa" (RUT + clave propios
# de la empresa, distintos de los personales de SII_SESSIONS). Se usa SOLO
# para boletas de honorarios recibidas (ver sii_bhe.py); si el login empresa
# falla, sencillamente no hay entrada acá para ese sid y las boletas no se
# sincronizan (no bloquea el resto de la app).
SII_SESSIONS_BHE: dict[str, SIIClient] = {}


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def _current_client(request: Request) -> SIIClient | None:
    sid = request.session.get("sid")
    return SII_SESSIONS.get(sid) if sid else None


def _current_client_bhe(request: Request) -> SIIClient | None:
    """Sesión "empresa" (boletas de honorarios). Puede no existir aunque haya
    sesión personal activa, si ese login falló o no se sincronizó boletas."""
    sid = request.session.get("sid")
    return SII_SESSIONS_BHE.get(sid) if sid else None


def _invalidar_sesion(request: Request) -> None:
    """Descarta la sesión SII guardada (por sid) cuando se detecta que el SII
    ya la cerró de su lado. No vuelve a loguear solo: la próxima carga de "/"
    muestra el formulario de acceso de nuevo."""
    sid = request.session.pop("sid", None)
    if sid:
        SII_SESSIONS.pop(sid, None)
        SII_SESSIONS_BHE.pop(sid, None)


def _log_evento(request: Request, accion: str) -> None:
    """Registra una operación en el LOG de auditoría (fecha, hora, acción y
    usuario), para poder reconstruir qué pasó si algo se borra por error (ver
    /log/excel y el botón "Descargar LOG" del Cockpit). Usa su propia conexión
    corta, aparte de la de la operación que se está registrando."""
    client = _current_client(request)
    conn = db.get_conn()
    try:
        db.registrar_log(conn, accion, usuario=(client.rut if client else None))
        conn.commit()
    finally:
        conn.close()


_MSG_SESION_PERDIDA = (
    "Se perdió la sesión con el SII (probablemente por inactividad prolongada). "
    "Volvé a ingresar tu Clave Tributaria para continuar."
)


def _html_sesion_perdida(rut: str | None) -> HTMLResponse:
    """Respuesta para /pdf/{codigo}/ver y /descargar cuando se detecta sesión
    SII vencida. target="_top" porque /ver se sirve embebido en un <iframe>
    (ver pdf_viewer.html): el link necesita romper el iframe para llevar al
    login real."""
    html = (
        "<div style=\"font-family:Arial,sans-serif;background:#0f0f0f;color:#eee;"
        "height:100vh;display:flex;flex-direction:column;align-items:center;"
        "justify-content:center;gap:16px;text-align:center;padding:24px\">"
        f"<p style=\"max-width:420px\">{_MSG_SESION_PERDIDA}</p>"
        f"<a href=\"/?relogin=1&rut={rut or ''}\" target=\"_top\" "
        "style=\"color:#2ecc71;font-weight:700;text-decoration:none\">"
        "Iniciar sesión de nuevo →</a></div>"
    )
    return HTMLResponse(html, status_code=401)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, relogin: int = 0, rut: str = ""):
    client = _current_client(request)
    if client and client.rut:
        conn = db.get_conn()
        try:
            hoy = date.today().isoformat()
            primer_dia_mes = date.today().replace(day=1).isoformat()
            rechazadas = db.facturas_rechazadas(conn)
            pagos_vencidos = db.documentos_vencidos(conn, "compra", hoy)
            cobranza_vencida = db.documentos_vencidos(conn, "venta", hoy)
            rendiciones_pend = db.rendiciones_pendientes(conn)
            movimientos_mes = db.movimientos_cc_en_rango(conn, primer_dia_mes, hoy)
        finally:
            conn.close()
        total_ing_mes = sum(m["monto"] for m in movimientos_mes if m["flujo"] == "Ingreso")
        total_egr_mes = sum(m["monto"] for m in movimientos_mes if m["flujo"] == "Egreso")
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request, "rut": client.rut,
                "sync": sync.estado_sync, "anio": ANIO,
                "rechazadas": rechazadas, "pagos_vencidos": pagos_vencidos,
                "cobranza_vencida": cobranza_vencida,
                "rendiciones_pend": rendiciones_pend,
                "movimientos_mes": movimientos_mes,
                "total_ing_mes": total_ing_mes, "total_egr_mes": total_egr_mes,
            },
        )
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": _MSG_SESION_PERDIDA if relogin else None,
            "rut_prefill": rut,
            # A propósito en blanco (no EMPRESA_RUT): no exponer el RUT de la
            # empresa a cualquiera que llegue a la URL, ver pedido de
            # Christian 2026-08-03 (riesgo de seguridad).
            "rut_empresa_prefill": "",
        },
    )


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, rut: str = Form(...), clave: str = Form(...),
          rut_empresa: str = Form(...), clave_empresa: str = Form(...)):
    client = SIIClient()
    try:
        client.login(rut, clave)
        client.seleccionar_empresa(EMPRESA_RUT)
    except SIIAuthError as exc:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": str(exc), "rut_prefill": rut,
             "rut_empresa_prefill": rut_empresa},
            status_code=401,
        )

    # Segundo login, con la cuenta "empresa" (usado para boletas de
    # honorarios, ver sii_bhe.py). A pedido de Christian (2026-08-03, riesgo
    # de seguridad): AMBOS logins deben ser válidos para entrar al Cockpit —
    # antes este segundo login podía fallar sin bloquear el acceso, lo que
    # permitía entrar con la cuenta empresa vacía/incorrecta. Si falla, se
    # descarta también el login personal recién hecho: no se crea sid ni
    # sesión, y se vuelve al formulario con error (no se llega al Cockpit).
    client_bhe = SIIClient()
    try:
        client_bhe.login(rut_empresa, clave_empresa)
    except SIIAuthError as exc:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": str(exc), "rut_prefill": rut,
             "rut_empresa_prefill": rut_empresa},
            status_code=401,
        )

    sid = secrets.token_urlsafe(24)
    SII_SESSIONS[sid] = client
    SII_SESSIONS_BHE[sid] = client_bhe
    request.session["sid"] = sid
    sync.estado_sync["boletas_error"] = None
    _log_evento(request, "Inicio de sesión")

    # Dispara la sincronización en segundo plano (no bloquea el login): el
    # cockpit, al cargar, ve `sync.corriendo=True` y muestra su barra de
    # progreso automáticamente mientras la sincronización avanza.
    sync.sincronizar_async(client, anio=ANIO, desde=DESDE_SYNC,
                           client_bhe=client_bhe, rut_empresa=rut_empresa)

    return RedirectResponse("/", status_code=303)


@app.post("/sync")
def sincronizar_ahora(request: Request):
    client = _current_client(request)
    if not client or not client.rut:
        return JSONResponse({"ok": False, "error": "no-session"}, status_code=401)
    _log_evento(request, "Sincronización manual con el SII")
    client_bhe = _current_client_bhe(request)
    sync.sincronizar_async(client, anio=ANIO, desde=DESDE_SYNC,
                           client_bhe=client_bhe, rut_empresa=client_bhe.rut if client_bhe else None)
    return JSONResponse({"ok": True})


@app.get("/sync/estado")
def sync_estado(request: Request):
    """Estado actual de la sincronización, para la barra de progreso.

    Si el sync detectó que el SII cerró la sesión (sesion_perdida), se
    invalida acá la sesión guardada: el próximo `/?relogin=1` al que el panel
    redirige ya encuentra el formulario de login, no el cockpit viejo.
    """
    if sync.estado_sync.get("sesion_perdida"):
        _invalidar_sesion(request)
    return JSONResponse(sync.estado_sync)


@app.get("/sii/estado")
def sii_estado(request: Request):
    """Chequeo liviano y en vivo de la sesión con el SII, para el Cockpit.

    Antes la sesión perdida solo se notaba cuando corría una sincronización
    o al pedir un PDF puntual: si la sesión moría entre medio, el Cockpit se
    quedaba mostrando datos viejos como si todo siguiera bien hasta que el
    usuario disparara alguna de esas dos acciones. El JS de dashboard.html
    llama a este endpoint apenas carga la página para detectarlo altiro.

    Pide la primera página de "recibidos" (una sola llamada al SII, no todo
    el sync) y reutiliza la misma señal ya validada en
    sii_docs.obtener_documentos: esta empresa siempre tiene documentos ese
    año, así que ninguna fila reconocida en la primera página es sesión
    perdida (no falta de documentos). Es la misma lógica que ya se usa para
    la sincronización completa, no la detección por frases de texto que
    resultó frágil en la descarga de PDF individual (ver conversación
    2026-07-31).
    """
    client = _current_client(request)
    if not client or not client.rut:
        return JSONResponse({"conectado": False, "rut": ""})
    try:
        sii_docs.obtener_documentos(client.session, "recibidos", anio=ANIO, max_paginas=1)
    except SIISessionExpirada:
        rut = client.rut
        _invalidar_sesion(request)
        return JSONResponse({"conectado": False, "rut": rut})
    except Exception:
        # Error de red u otro problema no relacionado a la sesión: no se
        # interrumpe al usuario con un aviso de sesión perdida que sería falso.
        return JSONResponse({"conectado": True, "rut": client.rut})
    return JSONResponse({"conectado": True, "rut": client.rut})


@app.get("/debug/bhe/inspeccionar", response_class=PlainTextResponse)
def debug_bhe_inspeccionar(request: Request, anio: int = ANIO, url: str = ""):
    """Herramienta temporal de diagnóstico: usa la sesión empresa real (ya
    logueada) para volcar los links/tablas/formularios de una página del SII
    de boletas de honorarios, sin necesitar credenciales ni acceso directo al
    SII para ajustar sii_bhe.py.

    Sin `url`: consulta el informe anual (mismo primer paso que el sync).
    Con `url`: consulta esa URL tal cual (para inspeccionar, p. ej., el link
    a un mes que haya aparecido en el volcado anterior).
    """
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    client_bhe = _current_client_bhe(request)
    if not client_bhe:
        return PlainTextResponse(
            "No hay sesión empresa activa. Cerrá sesión y volvé a entrar con "
            "el usuario y clave empresa.", status_code=401,
        )
    from .sii_client import normalizar_rut
    if url:
        target, params = url, {}
    else:
        numero, dv = normalizar_rut(client_bhe.rut)
        target = sii_bhe.INFORME_ANUAL_URL
        params = {"rut_arrastre": numero, "dv_arrastre": dv, "cbanoinformeanual": anio}
    texto = sii_bhe.diagnostico(client_bhe.session, target, params)
    return PlainTextResponse(texto)


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


@app.get("/respaldo")
def respaldo_db(request: Request):
    """Descarga un .zip con TODO lo necesario para reconstruir la app frente a
    un desastre informático (botón "Descargar Respaldo" del Cockpit): la base
    de datos, los adjuntos de rendiciones y de gestión de facturas, y una
    copia del código tal como está corriendo. Ver exportar.construir_respaldo_completo."""
    client = _current_client(request)
    if not client or not client.rut:
        return RedirectResponse("/", status_code=303)
    db_bytes = db.respaldo_bytes()
    data = exportar.construir_respaldo_completo(
        db_bytes, ADJUNTOS_DIR, ADJUNTOS_FACTURAS_DIR, BASE_DIR.parent,
        fecha=date.today().isoformat(),
    )
    nombre = f"RespaldoCompletoERP_{date.today():%Y%m%d}.zip"
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@app.get("/log/excel")
def log_excel(request: Request):
    """Descarga un Excel con el historial completo del LOG de auditoría
    (botón "Descargar LOG" del Cockpit). Además de servirse como descarga,
    deja una copia guardada en la carpeta Log del proyecto (Dropbox), con
    nombre LogERP_AAAAMMDD.xlsx (se sobrescribe si ya se descargó hoy)."""
    client = _current_client(request)
    if not client or not client.rut:
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        logs = db.listar_logs(conn)
    finally:
        conn.close()
    data = exportar.construir_excel_logs(logs)
    nombre = f"LogERP_{date.today():%Y%m%d}.xlsx"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / nombre).write_bytes(data)
    except OSError:
        pass  # si no se pudo guardar en la carpeta del proyecto, igual se entrega la descarga
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


# Los PDF de facturas ya NO se guardan en disco: se piden al SII al momento
# de verlos, con la sesión activa del usuario. `tipo` en BD -> fuente sii_docs.
_FUENTE_POR_TIPO = {"compra": "recibidos", "venta": "emitidos"}


def _factura_por_codigo(codigo: str):
    conn = db.get_conn()
    try:
        return conn.execute(
            "SELECT documento, folio, razon_social, rut_contraparte, tipo, pdf_href_bhe "
            "FROM facturas WHERE codigo_sii = ?",
            (codigo,),
        ).fetchone()
    finally:
        conn.close()


_MSG_BHE_SIN_SESION = (
    "No hay sesión activa con la cuenta empresa (necesaria para boletas de honorarios). "
    "Volvé a iniciar sesión ingresando también el usuario y clave empresa."
)


def _html_bhe_sin_sesion() -> HTMLResponse:
    """Igual que _html_sesion_perdida, pero para cuando falta/se perdió la
    sesión "empresa" que se usa solo para boletas de honorarios."""
    html = (
        "<div style=\"font-family:Arial,sans-serif;background:#0f0f0f;color:#eee;"
        "height:100vh;display:flex;flex-direction:column;align-items:center;"
        "justify-content:center;gap:16px;text-align:center;padding:24px\">"
        f"<p style=\"max-width:420px\">{_MSG_BHE_SIN_SESION}</p>"
        "<a href=\"/?relogin=1\" target=\"_top\" "
        "style=\"color:#2ecc71;font-weight:700;text-decoration:none\">"
        "Iniciar sesión de nuevo →</a></div>"
    )
    return HTMLResponse(html, status_code=401)


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
    if not row:
        return Response("PDF no disponible", status_code=404)

    # Boletas de honorarios: PDF distinto, con la sesión "empresa" (no la
    # personal) y el href guardado al parsear el mes (ver sii_bhe.py).
    if codigo.startswith("BHE-"):
        client_bhe = _current_client_bhe(request)
        if not client_bhe:
            return _html_bhe_sin_sesion()
        try:
            data = sii_bhe.obtener_pdf_bytes(client_bhe.session, row["pdf_href_bhe"])
        except sii_bhe.BHEError:
            sid = request.session.get("sid")
            if sid:
                SII_SESSIONS_BHE.pop(sid, None)
            return _html_bhe_sin_sesion()
        if not data:
            return Response("No se pudo obtener el PDF de la boleta desde el SII. Intenta de nuevo.", status_code=502)
        return Response(content=data, media_type="application/pdf")

    fuente = _FUENTE_POR_TIPO.get(row["tipo"])
    if not fuente:
        return Response("PDF no disponible", status_code=404)
    # obtener_pdf_bytes ya no lanza SIISessionExpirada (ver docstring en
    # sii_docs.py): una falla puntual de ESTE PDF ya no invalida toda la
    # sesión SII guardada. La sesión de verdad perdida se detecta aparte, en
    # el sync y en /sii/estado.
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
    if not row:
        return Response("PDF no disponible", status_code=404)

    if codigo.startswith("BHE-"):
        client_bhe = _current_client_bhe(request)
        if not client_bhe:
            return _html_bhe_sin_sesion()
        try:
            data = sii_bhe.obtener_pdf_bytes(client_bhe.session, row["pdf_href_bhe"])
        except sii_bhe.BHEError:
            sid = request.session.get("sid")
            if sid:
                SII_SESSIONS_BHE.pop(sid, None)
            return _html_bhe_sin_sesion()
        if not data:
            return Response("No se pudo obtener el PDF de la boleta desde el SII. Intenta de nuevo.", status_code=502)
        return Response(
            content=data, media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{_nombre_pdf(row)}"'},
        )

    fuente = _FUENTE_POR_TIPO.get(row["tipo"])
    if not fuente:
        return Response("PDF no disponible", status_code=404)
    # Ver nota en /pdf/{codigo}/ver: una falla puntual ya no invalida la sesión.
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
        "estado_ok": "Pagada", "label_pagado": "Pagado", "label_deuda": "Saldo", "accion": "pago",
    },
    "ingresos": {
        "tipo": "venta", "direccion": "recibido",
        "titulo": "Ingresos", "col_contraparte": "Cliente",
        "estado_ok": "Cobrada", "label_pagado": "Cobrado", "label_deuda": "Saldo", "accion": "cobro",
    },
}

# Flujo de Movimientos CC que corresponde a cada dirección de pago: un pago a
# proveedores ('emitido') sale como Egreso; un cobro de ingresos ('recibido')
# entra como Ingreso. Se usa en "Buscar pagos ya realizados".
_FLUJO_DE_DIRECCION = {"recibido": "Ingreso", "emitido": "Egreso"}


def _guard(request: Request):
    client = _current_client(request)
    return client if (client and client.rut) else None


@app.get("/pagos", response_class=HTMLResponse)
def pagos_home(request: Request):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("pagos.html", {"request": request, "rut": client.rut})


def _vista_lista(request: Request, seccion: str, desde: str = "", hasta: str = ""):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    cfg = SECCIONES[seccion]
    d, h = _rango_movimientos(desde, hasta)
    conn = db.get_conn()
    try:
        filas = db.facturas_con_pago_en_rango(conn, tipo=cfg["tipo"], desde=d, hasta=h)
        # Los totales y el conteo del encabezado ignoran rechazadas y anuladas
        # (no se cobrarán/pagarán).
        vigentes = [f for f in filas if not f["fecha_reclamo"] and not f["anulada_por"]]
        total_monto = sum(f["total"] for f in vigentes)
        total_pendiente = sum(max(f["total"] - f["pagado"], 0) for f in vigentes)
        n_rechazadas = len([f for f in filas if f["fecha_reclamo"]])
        n_anuladas = len([f for f in filas if f["anulada_por"] and not f["fecha_reclamo"]])
    finally:
        conn.close()
    return templates.TemplateResponse(
        "pagos_lista.html",
        {
            "request": request, "rut": client.rut, "anio": ANIO,
            "seccion": seccion, "cfg": cfg, "filas": filas,
            "desde": d, "hasta": h,
            "n_vigentes": len(vigentes), "n_rechazadas": n_rechazadas,
            "n_anuladas": n_anuladas,
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
        if f["anulada_por"]:
            # Anulada por una NC: no hay nada que gestionar aquí.
            return RedirectResponse(f"/pagos/{seccion}", status_code=303)
        pagos = db.pagos_de_factura(conn, f["id"])
        adjuntos = db.adjuntos_de_factura(conn, f["id"])
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
        movs_manuales = db.movimientos_cc_manuales(conn, _FLUJO_DE_DIRECCION[cfg["direccion"]])
    finally:
        conn.close()
    return templates.TemplateResponse(
        "pago_detalle.html",
        {
            "request": request, "rut": client.rut, "anio": ANIO,
            "seccion": seccion, "cfg": cfg, "f": f, "pagos": pagos,
            "adjuntos": adjuntos,
            "hoy": date.today().isoformat(), "saldo": (f["total"] - f["pagado"]),
            "error": error, "rendiciones": rendiciones, "rend_asociada": rend_asociada,
            "movs_manuales": movs_manuales,
        },
        status_code=status_code,
    )


def _pdf_gestion(request: Request, seccion: str, codigo: str):
    """Devuelve el PDF del detalle de gestión de una factura (inline, para
    abrirlo en el navegador): resumen + movimientos parciales + el PDF
    original de la factura (obtenido en vivo desde el SII) + los adjuntos de
    la gestión, todo anexado al final."""
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    cfg = SECCIONES[seccion]
    conn = db.get_conn()
    try:
        f = db.factura_pago_por_codigo(conn, codigo)
        if not f:
            return HTMLResponse("<p>Factura no encontrada.</p>", status_code=404)
        pagos = db.pagos_de_factura(conn, f["id"])
        adjuntos = db.adjuntos_de_factura(conn, f["id"])
    finally:
        conn.close()

    # El documento original vive solo en el SII (no se guarda copia local);
    # se obtiene con la sesión activa del usuario, igual que /pdf/{codigo}/ver.
    # Si la sesión expiró o el SII no responde, igual se devuelve el PDF de
    # gestión (sin el original) en vez de fallar. Las boletas de honorarios
    # usan la sesión "empresa" y su propio href guardado al sincronizar.
    factura_bytes = None
    if codigo.startswith("BHE-"):
        client_bhe = _current_client_bhe(request)
        if client_bhe:
            try:
                factura_bytes = sii_bhe.obtener_pdf_bytes(client_bhe.session, f["pdf_href_bhe"])
            except sii_bhe.BHEError:
                factura_bytes = None
    else:
        fuente = _FUENTE_POR_TIPO.get(cfg["tipo"])
        if fuente:
            # Ya no lanza SIISessionExpirada; None si no se pudo obtener.
            factura_bytes = sii_docs.obtener_pdf_bytes(client.session, fuente, codigo)

    data = exportar._pdf_de_gestion_pago(
        f, pagos, cfg,
        incluye_original=bool(factura_bytes),
        incluye_adjuntos=bool(adjuntos),
    )
    if factura_bytes:
        data = exportar.anexar_pdf(data, factura_bytes)
    if adjuntos:
        data = exportar.anexar_archivos(data, adjuntos)
    return Response(content=data, media_type="application/pdf")


def _guardar_fecha_tope(request: Request, seccion: str, codigo: str, fecha_tope: str):
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        db.set_fecha_tope(conn, codigo, fecha_tope.strip())
        conn.commit()
    finally:
        conn.close()
    _log_evento(request, f"Fecha tope actualizada · {seccion} {codigo} → {fecha_tope.strip()}")
    return RedirectResponse(f"/pagos/{seccion}/{codigo}", status_code=303)


def _guardar_descripcion(request: Request, seccion: str, codigo: str, descripcion: str):
    """Guarda la nota de la gestión: un solo campo por factura, no por pago parcial."""
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        db.set_descripcion(conn, codigo, descripcion.strip())
        conn.commit()
    finally:
        conn.close()
    _log_evento(request, f"Descripción actualizada · {seccion} {codigo}")
    return RedirectResponse(f"/pagos/{seccion}/{codigo}", status_code=303)


def _agregar_movimiento(request: Request, seccion: str, codigo: str,
                        fecha: str, monto: str, rendicion_id: str = "", externo: str = ""):
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
        if f["anulada_por"]:
            return RedirectResponse(f"/pagos/{seccion}", status_code=303)
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
        # Pago vía rendición o pago externo (solo pago a proveedores): mutuamente
        # excluyentes entre sí. Vía rendición valida la rendición y la regla
        # "una factura -> una sola rendición". Externo no lleva más validación:
        # significa que no se pagó desde la CC empresa ni vía una rendición.
        rid = None
        rid_str = (rendicion_id or "").strip()
        es_externo = bool((externo or "").strip())
        if rid_str and es_externo:
            return _render_detalle(request, client, seccion, codigo,
                                   error="Un pago no puede ser vía rendición y externo a la vez.",
                                   status_code=400)
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
                        direccion=cfg["direccion"], rendicion_id=rid, externo=es_externo)
        db.sincronizar_movimientos_cc(conn)
        conn.commit()
    finally:
        conn.close()
    _log_evento(request, f"Pago registrado · {seccion} {codigo} · ${monto_int} el {fecha}")
    return RedirectResponse(f"/pagos/{seccion}/{codigo}", status_code=303)


def _eliminar_movimiento(request: Request, seccion: str, codigo: str, pago_id: int):
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    borrado = False
    try:
        f = db.factura_pago_por_codigo(conn, codigo)
        if f:
            db.eliminar_pago(conn, pago_id, f["id"])
            db.sincronizar_movimientos_cc(conn)
            conn.commit()
            borrado = True
    finally:
        conn.close()
    if borrado:
        _log_evento(request, f"Pago eliminado · {seccion} {codigo} · pago id {pago_id}")
    return RedirectResponse(f"/pagos/{seccion}/{codigo}", status_code=303)


def _agregar_movimientos_desde_cc(request: Request, seccion: str, codigo: str,
                                  mov_ids: list[int]):
    """Convierte movimientos CC manuales, ya seleccionados en "Buscar pagos ya
    realizados", en pagos/cobros parciales de la factura. Al sincronizar,
    cada pago nuevo agrega su propio movimiento (origen='factura') a
    Movimientos CC; para no duplicar la caja, las filas manuales originales
    se eliminan justo después (solo esas, nada más)."""
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    cfg = SECCIONES[seccion]
    mov_ids = sorted(set(i for i in mov_ids if i))
    if not mov_ids:
        return _render_detalle(request, client, seccion, codigo,
                               error="No se seleccionó ningún movimiento.", status_code=400)

    conn = db.get_conn()
    try:
        f = db.factura_pago_por_codigo(conn, codigo)
        if not f:
            return HTMLResponse("<p>Factura no encontrada.</p>", status_code=404)
        if f["anulada_por"]:
            return RedirectResponse(f"/pagos/{seccion}", status_code=303)
        if f["fecha_reclamo"]:
            return _render_detalle(request, client, seccion, codigo,
                                   error="La factura está rechazada; no admite movimientos.",
                                   status_code=400)

        flujo_esperado = _FLUJO_DE_DIRECCION[cfg["direccion"]]
        movs = []
        for mid in mov_ids:
            m = db.movimiento_cc_por_id(conn, mid)
            if m and m["origen"] == "manual" and m["flujo"] == flujo_esperado:
                movs.append(m)
        if not movs:
            return _render_detalle(request, client, seccion, codigo,
                                   error="Los movimientos seleccionados ya no están disponibles.",
                                   status_code=400)

        saldo = f["total"] - f["pagado"]
        total_sel = sum(m["monto"] for m in movs)
        if total_sel > saldo:
            saldo_fmt = "{:,.0f}".format(saldo).replace(",", ".")
            return _render_detalle(
                request, client, seccion, codigo,
                error=f"Los movimientos seleccionados suman más que el saldo pendiente (${saldo_fmt}).",
                status_code=400)

        for m in movs:
            db.agregar_pago(conn, f["id"], m["fecha"], m["monto"], direccion=cfg["direccion"])
        db.sincronizar_movimientos_cc(conn)
        for m in movs:
            db.eliminar_movimiento_manual(conn, m["id"])
        conn.commit()
    finally:
        conn.close()
    _log_evento(
        request,
        f"{len(movs)} {cfg['accion']}(s) agregados desde Movimientos CC · {seccion} {codigo} · ${total_sel}",
    )
    return RedirectResponse(f"/pagos/{seccion}/{codigo}", status_code=303)


async def _agregar_adjunto_factura(request: Request, seccion: str, codigo: str,
                                   archivos: list[UploadFile]) -> Response:
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    agregado = False
    try:
        f = db.factura_pago_por_codigo(conn, codigo)
        if f:
            _guardar_adjuntos_factura(conn, f["id"], archivos)
            conn.commit()
            agregado = True
    finally:
        conn.close()
    if agregado:
        _log_evento(request, f"Adjunto agregado · {seccion} {codigo}")
    return RedirectResponse(f"/pagos/{seccion}/{codigo}", status_code=303)


def _descargar_adjunto_factura(request: Request, seccion: str, codigo: str, adj_id: int) -> Response:
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        f = db.factura_pago_por_codigo(conn, codigo)
        adj = db.adjunto_factura_por_id(conn, adj_id)
    finally:
        conn.close()
    if not f or not adj or adj["factura_id"] != f["id"] or not Path(adj["path"]).exists():
        return Response("Adjunto no disponible", status_code=404)
    return FileResponse(adj["path"], filename=adj["nombre_archivo"])


def _eliminar_adjunto_factura(request: Request, seccion: str, codigo: str, adj_id: int) -> Response:
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    eliminado = False
    try:
        f = db.factura_pago_por_codigo(conn, codigo)
        adj = db.adjunto_factura_por_id(conn, adj_id)
        if f and adj and adj["factura_id"] == f["id"]:
            db.eliminar_adjunto_factura(conn, adj_id, f["id"])
            conn.commit()
            eliminado = True
            try:
                Path(adj["path"]).unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        conn.close()
    if eliminado:
        _log_evento(request, f"Adjunto eliminado · {seccion} {codigo} · adjunto id {adj_id}")
    return RedirectResponse(f"/pagos/{seccion}/{codigo}", status_code=303)


# ---- Pago a proveedores (facturas recibidas) ----

@app.get("/pagos/proveedores", response_class=HTMLResponse)
def proveedores_lista(request: Request, desde: str = "", hasta: str = ""):
    return _vista_lista(request, "proveedores", desde, hasta)


@app.get("/pagos/proveedores/{codigo}", response_class=HTMLResponse)
def proveedores_detalle(request: Request, codigo: str):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    return _render_detalle(request, client, "proveedores", codigo)


@app.get("/pagos/proveedores/{codigo}/pdf")
def proveedores_pdf(request: Request, codigo: str):
    return _pdf_gestion(request, "proveedores", codigo)


@app.post("/pagos/proveedores/{codigo}/fecha-tope")
def proveedores_fecha_tope(request: Request, codigo: str, fecha_tope: str = Form(...)):
    return _guardar_fecha_tope(request, "proveedores", codigo, fecha_tope)


@app.post("/pagos/proveedores/{codigo}/descripcion")
def proveedores_descripcion(request: Request, codigo: str, descripcion: str = Form("")):
    return _guardar_descripcion(request, "proveedores", codigo, descripcion)


@app.post("/pagos/proveedores/{codigo}/pago")
def proveedores_agregar(request: Request, codigo: str,
                        fecha: str = Form(...), monto: str = Form(...),
                        rendicion_id: str = Form(""), externo: str = Form("")):
    return _agregar_movimiento(request, "proveedores", codigo, fecha, monto, rendicion_id, externo)


@app.post("/pagos/proveedores/{codigo}/pago/{pago_id}/eliminar")
def proveedores_eliminar(request: Request, codigo: str, pago_id: int):
    return _eliminar_movimiento(request, "proveedores", codigo, pago_id)


@app.post("/pagos/proveedores/{codigo}/movimientos-desde-cc")
async def proveedores_movimientos_desde_cc(request: Request, codigo: str):
    form = await request.form()
    mov_ids = [int(v) for v in form.getlist("mov_ids") if str(v).strip().isdigit()]
    return _agregar_movimientos_desde_cc(request, "proveedores", codigo, mov_ids)


@app.post("/pagos/proveedores/{codigo}/adjunto")
async def proveedores_agregar_adjunto(request: Request, codigo: str,
                                      archivos: list[UploadFile] = File(default=[])):
    return await _agregar_adjunto_factura(request, "proveedores", codigo, archivos)


@app.get("/pagos/proveedores/{codigo}/adjunto/{adj_id}/descargar")
def proveedores_descargar_adjunto(request: Request, codigo: str, adj_id: int):
    return _descargar_adjunto_factura(request, "proveedores", codigo, adj_id)


@app.post("/pagos/proveedores/{codigo}/adjunto/{adj_id}/eliminar")
def proveedores_eliminar_adjunto(request: Request, codigo: str, adj_id: int):
    return _eliminar_adjunto_factura(request, "proveedores", codigo, adj_id)


# ---- Ingresos (facturas emitidas) ----

@app.get("/pagos/ingresos", response_class=HTMLResponse)
def ingresos_lista(request: Request, desde: str = "", hasta: str = ""):
    return _vista_lista(request, "ingresos", desde, hasta)


@app.get("/pagos/ingresos/{codigo}", response_class=HTMLResponse)
def ingresos_detalle(request: Request, codigo: str):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    return _render_detalle(request, client, "ingresos", codigo)


@app.get("/pagos/ingresos/{codigo}/pdf")
def ingresos_pdf(request: Request, codigo: str):
    return _pdf_gestion(request, "ingresos", codigo)


@app.post("/pagos/ingresos/{codigo}/fecha-tope")
def ingresos_fecha_tope(request: Request, codigo: str, fecha_tope: str = Form(...)):
    return _guardar_fecha_tope(request, "ingresos", codigo, fecha_tope)


@app.post("/pagos/ingresos/{codigo}/descripcion")
def ingresos_descripcion(request: Request, codigo: str, descripcion: str = Form("")):
    return _guardar_descripcion(request, "ingresos", codigo, descripcion)


@app.post("/pagos/ingresos/{codigo}/pago")
def ingresos_agregar(request: Request, codigo: str,
                     fecha: str = Form(...), monto: str = Form(...)):
    return _agregar_movimiento(request, "ingresos", codigo, fecha, monto)


@app.post("/pagos/ingresos/{codigo}/pago/{pago_id}/eliminar")
def ingresos_eliminar(request: Request, codigo: str, pago_id: int):
    return _eliminar_movimiento(request, "ingresos", codigo, pago_id)


@app.post("/pagos/ingresos/{codigo}/movimientos-desde-cc")
async def ingresos_movimientos_desde_cc(request: Request, codigo: str):
    form = await request.form()
    mov_ids = [int(v) for v in form.getlist("mov_ids") if str(v).strip().isdigit()]
    return _agregar_movimientos_desde_cc(request, "ingresos", codigo, mov_ids)


@app.post("/pagos/ingresos/{codigo}/adjunto")
async def ingresos_agregar_adjunto(request: Request, codigo: str,
                                   archivos: list[UploadFile] = File(default=[])):
    return await _agregar_adjunto_factura(request, "ingresos", codigo, archivos)


@app.get("/pagos/ingresos/{codigo}/adjunto/{adj_id}/descargar")
def ingresos_descargar_adjunto(request: Request, codigo: str, adj_id: int):
    return _descargar_adjunto_factura(request, "ingresos", codigo, adj_id)


@app.post("/pagos/ingresos/{codigo}/adjunto/{adj_id}/eliminar")
def ingresos_eliminar_adjunto(request: Request, codigo: str, adj_id: int):
    return _eliminar_adjunto_factura(request, "ingresos", codigo, adj_id)


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


def _guardar_adjuntos_factura(conn, factura_id: int, archivos: list[UploadFile]) -> None:
    """Guarda en disco los adjuntos de la gestión de una factura (pago a
    proveedores / ingresos) y los registra en la BD. Mismo patrón que
    _guardar_adjuntos, colgando de facturas en vez de rendiciones."""
    destino = ADJUNTOS_FACTURAS_DIR / str(factura_id)
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
        db.agregar_adjunto_factura(conn, factura_id, up.filename, str(ruta))


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
def rendiciones_lista(request: Request, desde: str = "", hasta: str = ""):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango_movimientos(desde, hasta)
    conn = db.get_conn()
    try:
        filas = db.rendiciones_en_rango(conn, d, h)
        total_monto = sum(f["total"] for f in filas)
        total_pendiente = sum(max(f["total"] - f["pagado"], 0) for f in filas)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "rendiciones_lista.html",
        {
            "request": request, "rut": client.rut, "filas": filas,
            "desde": d, "hasta": h,
            "total_monto": total_monto, "total_pendiente": total_pendiente,
        },
    )


@app.get("/pagos/rendiciones/zip")
def rendiciones_zip(request: Request, desde: str = "", hasta: str = ""):
    """Descarga un .zip con el PDF de cada rendición del rango (mismo filtro
    que se ve en /pagos/rendiciones)."""
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango_movimientos(desde, hasta)
    conn = db.get_conn()
    try:
        filas = db.rendiciones_en_rango(conn, d, h)
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
            "<p>No hay rendiciones en el rango seleccionado.</p>", status_code=404,
        )
    data = exportar.construir_zip_rendiciones(rends)
    headers = {"Content-Disposition": f'attachment; filename="rendiciones_{d}_a_{h}.zip"'}
    return Response(content=data, media_type="application/zip", headers=headers)


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
    _log_evento(request, f"Rendición creada · {nombre} (id {rid})")
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
        db.sincronizar_movimientos_cc(conn)
        conn.commit()
    finally:
        conn.close()
    _log_evento(request, f"Pago registrado en rendición {rid} · ${monto_int} el {fecha}")
    return RedirectResponse(f"/pagos/rendiciones/{rid}", status_code=303)


@app.post("/pagos/rendiciones/{rid}/pago/{pago_id}/eliminar")
def rendicion_eliminar_pago(request: Request, rid: int, pago_id: int):
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        db.eliminar_pago_rendicion(conn, pago_id, rid)
        db.sincronizar_movimientos_cc(conn)
        conn.commit()
    finally:
        conn.close()
    _log_evento(request, f"Pago eliminado de rendición {rid} · pago id {pago_id}")
    return RedirectResponse(f"/pagos/rendiciones/{rid}", status_code=303)


@app.post("/pagos/rendiciones/{rid}/adjunto")
async def rendicion_agregar_adjunto(request: Request, rid: int,
                                    archivos: list[UploadFile] = File(default=[])):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    agregado = False
    try:
        if db.rendicion_por_id(conn, rid):
            _guardar_adjuntos(conn, rid, archivos)
            conn.commit()
            agregado = True
    finally:
        conn.close()
    if agregado:
        _log_evento(request, f"Adjunto agregado a rendición {rid}")
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
    eliminado = False
    try:
        adj = db.adjunto_por_id(conn, adj_id)
        if adj and adj["rendicion_id"] == rid:
            db.eliminar_adjunto(conn, adj_id, rid)
            conn.commit()
            eliminado = True
            try:
                Path(adj["path"]).unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        conn.close()
    if eliminado:
        _log_evento(request, f"Adjunto eliminado de rendición {rid} · adjunto id {adj_id}")
    return RedirectResponse(f"/pagos/rendiciones/{rid}", status_code=303)


@app.post("/pagos/rendiciones/{rid}/eliminar")
def rendicion_eliminar(request: Request, rid: int):
    if not _guard(request):
        return RedirectResponse("/", status_code=303)
    conn = db.get_conn()
    try:
        r = db.rendicion_por_id(conn, rid)
        nombre = r["nombre"] if r else f"id {rid}"
        paths = db.eliminar_rendicion(conn, rid)
        db.sincronizar_movimientos_cc(conn)
        conn.commit()
    finally:
        conn.close()
    for p in paths:  # borra archivos del disco
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
    _log_evento(request, f"Rendición ELIMINADA · {nombre} (id {rid})")
    return RedirectResponse("/pagos/rendiciones", status_code=303)


# ---------------------------------------------------------------------------
# Módulo 5 · Movimientos CC (espejo editable de la cuenta corriente)
#
# Pantalla "Editar Movimientos" (link desde el cuadro "Movimientos CC" del
# Cockpit): lista completa de movimientos_cc, con filtro de fechas (default
# desde DESDE_MOVIMIENTOS_CC hasta hoy). Los que vienen de una factura/boleta
# o de una rendición son de solo lectura acá (se editan/borran desde su
# origen y el espejo se sincroniza solo, ver db.sincronizar_movimientos_cc);
# los manuales se pueden agregar, editar y borrar libremente.
# ---------------------------------------------------------------------------

def _rango_movimientos(desde: str | None, hasta: str | None) -> tuple[str, str]:
    """Normaliza el rango de fechas de la pantalla de Movimientos CC.
    Default: mes actual (el filtro Desde/Hasta permite ampliarlo hasta
    DESDE_MOVIMIENTOS_CC, todo lo disponible)."""
    hoy = date.today().isoformat()
    primer_dia_mes = date.today().replace(day=1).isoformat()
    d = (desde or "").strip() or primer_dia_mes
    h = (hasta or "").strip() or hoy
    try:
        date.fromisoformat(d)
    except ValueError:
        d = primer_dia_mes
    try:
        date.fromisoformat(h)
    except ValueError:
        h = hoy
    if d > h:
        d, h = h, d
    return d, h


@app.get("/movimientos", response_class=HTMLResponse)
def movimientos_lista(request: Request, desde: str = "", hasta: str = "",
                      error: str = ""):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango_movimientos(desde, hasta)
    conn = db.get_conn()
    try:
        movs = db.movimientos_cc_en_rango(conn, d, h)
    finally:
        conn.close()
    # Orden por defecto en esta pantalla: fecha descendente (lo más reciente
    # primero). movimientos_cc_en_rango() devuelve ascendente porque así lo
    # necesita el cuadro del Cockpit; acá se invierte solo para mostrar.
    movs = list(reversed(movs))
    total_ing = sum(m["monto"] for m in movs if m["flujo"] == "Ingreso")
    total_egr = sum(m["monto"] for m in movs if m["flujo"] == "Egreso")
    return templates.TemplateResponse(
        "movimientos_cc.html",
        {
            "request": request, "rut": client.rut,
            "desde": d, "hasta": h, "movs": movs, "hoy": date.today().isoformat(),
            "total_ingresos": total_ing, "total_egresos": total_egr,
            "neto": total_ing - total_egr, "error": error or None,
        },
    )


@app.get("/movimientos/pdf")
def movimientos_pdf(request: Request, desde: str = "", hasta: str = ""):
    """PDF del listado de Movimientos CC (botón "Ver PDF" de /movimientos):
    mismo rango y mismo orden (fecha descendente) que se ve en pantalla."""
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango_movimientos(desde, hasta)
    conn = db.get_conn()
    try:
        movs = db.movimientos_cc_en_rango(conn, d, h)
    finally:
        conn.close()
    movs = list(reversed(movs))  # mismo orden que /movimientos: fecha descendente
    total_ing = sum(m["monto"] for m in movs if m["flujo"] == "Ingreso")
    total_egr = sum(m["monto"] for m in movs if m["flujo"] == "Egreso")
    data = exportar.construir_pdf_movimientos_cc(movs, d, h, total_ing, total_egr)
    return Response(content=data, media_type="application/pdf")


@app.post("/movimientos/agregar")
def movimientos_agregar(request: Request, fecha: str = Form(...), flujo: str = Form(...),
                        descripcion: str = Form(...), monto: str = Form(...),
                        desde: str = Form(""), hasta: str = Form("")):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango_movimientos(desde, hasta)
    fecha = fecha.strip()
    descripcion = descripcion.strip()
    flujo = flujo.strip()
    try:
        monto_int = int(float(monto))
    except (ValueError, TypeError):
        monto_int = 0

    def _error(msg: str) -> RedirectResponse:
        return RedirectResponse(
            f"/movimientos?desde={d}&hasta={h}&error={quote(msg)}", status_code=303
        )

    if flujo not in ("Ingreso", "Egreso"):
        return _error("Tipo de movimiento inválido.")
    if monto_int <= 0:
        return _error("El monto debe ser mayor a cero.")
    if not descripcion:
        return _error("La descripción es obligatoria.")
    try:
        f_mov = date.fromisoformat(fecha)
    except ValueError:
        return _error("Fecha inválida.")
    if f_mov > date.today():
        return _error("No se permiten fechas futuras.")

    conn = db.get_conn()
    try:
        db.agregar_movimiento_manual(conn, fecha, flujo, descripcion, monto_int)
        conn.commit()
    finally:
        conn.close()
    _log_evento(request, f"Movimiento CC agregado (manual) · {flujo} ${monto_int} el {fecha} · {descripcion}")
    return RedirectResponse(f"/movimientos?desde={d}&hasta={h}", status_code=303)


@app.post("/movimientos/{mid}/editar")
def movimientos_editar(request: Request, mid: int, fecha: str = Form(...),
                       flujo: str = Form(...), descripcion: str = Form(...),
                       monto: str = Form(...), desde: str = Form(""), hasta: str = Form("")):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango_movimientos(desde, hasta)
    fecha = fecha.strip()
    descripcion = descripcion.strip()
    flujo = flujo.strip()
    try:
        monto_int = int(float(monto))
    except (ValueError, TypeError):
        monto_int = 0

    def _error(msg: str) -> RedirectResponse:
        return RedirectResponse(
            f"/movimientos?desde={d}&hasta={h}&error={quote(msg)}", status_code=303
        )

    if flujo not in ("Ingreso", "Egreso"):
        return _error("Tipo de movimiento inválido.")
    if monto_int <= 0:
        return _error("El monto debe ser mayor a cero.")
    if not descripcion:
        return _error("La descripción es obligatoria.")
    try:
        f_mov = date.fromisoformat(fecha)
    except ValueError:
        return _error("Fecha inválida.")
    if f_mov > date.today():
        return _error("No se permiten fechas futuras.")

    conn = db.get_conn()
    try:
        ok = db.editar_movimiento_manual(conn, mid, fecha, flujo, descripcion, monto_int)
        conn.commit()
    finally:
        conn.close()
    if not ok:
        return _error("Ese movimiento no se puede editar acá (no es manual).")
    _log_evento(request, f"Movimiento CC editado (manual) id {mid} · {flujo} ${monto_int} el {fecha}")
    return RedirectResponse(f"/movimientos?desde={d}&hasta={h}", status_code=303)


@app.post("/movimientos/{mid}/eliminar")
def movimientos_eliminar(request: Request, mid: int, desde: str = Form(""), hasta: str = Form("")):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango_movimientos(desde, hasta)
    conn = db.get_conn()
    try:
        ok = db.eliminar_movimiento_manual(conn, mid)
        conn.commit()
    finally:
        conn.close()
    if ok:
        _log_evento(request, f"Movimiento CC eliminado (manual) id {mid}")
    return RedirectResponse(f"/movimientos?desde={d}&hasta={h}", status_code=303)


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
        cc_resumen = db.cc_banco_resumen(conn)
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
            "cc_resumen": cc_resumen,
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


@app.post("/export/cc/subir")
async def export_cc_subir(request: Request, desde: str = Form(""), hasta: str = Form(""),
                          archivo: UploadFile = File(...)):
    """Sube la cartola del banco (.txt) y reemplaza la que hubiera guardada."""
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango(desde, hasta)
    contenido = await archivo.read()
    movimientos = exportar.parsear_cartola_banco_chile(contenido)
    if not movimientos:
        return RedirectResponse(
            f"/export?desde={d}&hasta={h}&cc_error=1", status_code=303
        )
    conn = db.get_conn()
    try:
        db.reemplazar_cc_banco(conn, movimientos)
        conn.commit()
    finally:
        conn.close()
    _log_evento(request, f"Cartola bancaria cargada · {len(movimientos)} movimientos ({d} a {h})")
    return RedirectResponse(f"/export?desde={d}&hasta={h}&cc_ok=1", status_code=303)


@app.get("/export/comparacion")
def export_comparacion(request: Request, desde: str = "", hasta: str = ""):
    client = _guard(request)
    if not client:
        return RedirectResponse("/", status_code=303)
    d, h = _rango(desde, hasta)
    conn = db.get_conn()
    try:
        movs_app = db.movimientos_en_rango(conn, d, h)
        movs_banco = db.cc_banco_en_rango(conn, d, h)
    finally:
        conn.close()
    # Filtro defensivo adicional (además del WHERE en SQL): garantiza que la
    # comparación respete estrictamente el rango elegido, sin importar el
    # formato de fecha que traiga cada fila.
    movs_app = [m for m in movs_app if d <= (m["fecha"] or "") <= h]
    movs_banco = [b for b in movs_banco if d <= (b["fecha"] or "") <= h]
    if not movs_banco:
        return HTMLResponse(
            "<p>No hay cartola del banco cargada para este rango. "
            "Sube una con \"Agregar CC\" antes de exportar la comparación.</p>",
            status_code=404,
        )
    comp = exportar.comparar_cc(movs_app, movs_banco)
    data = exportar.construir_excel_comparacion(comp, d, h)
    headers = {"Content-Disposition": f'attachment; filename="comparacion_cc_{d}_a_{h}.xlsx"'}
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


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
    _log_evento(
        request,
        f"Importación admin (migración) · {resumen['rendiciones_creadas']} rendiciones, "
        f"{resumen['pagos_facturas_ok']} pagos",
    )
    return JSONResponse(resumen)


@app.post("/admin/import/adjunto")
async def admin_import_adjunto(request: Request, rendicion_id: int = Form(...),
                               secret: str = Form(...), archivo: UploadFile = File(...)):
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
    _log_evento(request, f"Adjunto importado (admin) a rendición {rendicion_id}")
    return JSONResponse({"ok": True})


@app.post("/logout")
def logout(request: Request):
    sid = request.session.get("sid")
    if sid and sid in SII_SESSIONS:
        _log_evento(request, "Cierre de sesión")
        SII_SESSIONS.pop(sid).logout()
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}
