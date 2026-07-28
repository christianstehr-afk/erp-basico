"""
Sincronización con el SII: actualiza la base de datos con las facturas
recibidas y emitidas nuevas.

Se ejecuta al iniciar sesión (y puede dispararse manualmente). El guardado de
metadatos es rápido y sincrónico.

Los PDF ya NO se descargan ni se guardan en disco durante el sync: se piden
al SII al momento de verlos, con la sesión activa del usuario (ver
GET /pdf/{codigo}/ver en main.py, que usa sii_docs.obtener_pdf_bytes).
"""
from __future__ import annotations

import threading

from . import db, sii_docs, sii_rcv
from .sii_client import SIIClient, SIISessionExpirada

# Estado simple de sincronización (para mostrar en el panel)
estado_sync: dict = {
    "corriendo": False,
    "fase": "",           # texto de la etapa actual (para la barra de progreso)
    "recibidas": 0,
    "emitidas": 0,
    "error": None,
    # Distinto de un error cualquiera: la sesión con el SII se cerró de su
    # lado (típicamente por inactividad) y hay que volver a iniciar sesión
    # para seguir sincronizando. El panel usa esto para mandar al usuario a
    # /?relogin=1 en vez de solo mostrar "Error: ...".
    "sesion_perdida": False,
}


def sincronizar(client: SIIClient, anio: int = 2026, desde: str | None = None) -> dict:
    """Actualiza la BD con recibidas y emitidas del año.

    Devuelve un resumen con cuántos documentos se trajeron de cada tipo.
    """
    estado_sync.update(corriendo=True, error=None, fase="Consultando SII…", sesion_perdida=False)
    try:
        recibidos = sii_docs.obtener_documentos(client.session, "recibidos", anio=anio)
        emitidos = sii_docs.obtener_documentos(client.session, "emitidos", anio=anio)
    except SIISessionExpirada as exc:
        estado_sync.update(corriendo=False, error=str(exc), fase="Sesión perdida", sesion_perdida=True)
        raise
    except Exception as exc:
        estado_sync.update(corriendo=False, error=str(exc), fase="Error")
        raise

    # Solo documentos desde `desde` (YYYY-MM-DD) en adelante: inicio en
    # producción del ERP (junio 2026). Antes de eso no se sincroniza nada.
    if desde:
        recibidos = [d for d in recibidos if (d.get("fecha") or "") >= desde]
        emitidos = [d for d in emitidos if (d.get("fecha") or "") >= desde]

    estado_sync["fase"] = "Guardando documentos…"
    conn = db.get_conn()
    try:
        for d in recibidos:
            db.upsert_documento(conn, d, tipo="compra")
        for d in emitidos:
            db.upsert_documento(conn, d, tipo="venta")
        conn.commit()

        # Estado de rechazo/acuse de las emitidas, cruzando con el RCV por periodo.
        if client.rut_empresa:
            periodos = sorted({
                (d["fecha"] or "")[:7].replace("-", "")
                for d in emitidos if d.get("fecha")
            })
            periodos = [p for p in periodos if len(p) == 6]
            try:
                for estado in sii_rcv.estados_de_venta(client.session, client.rut_empresa, periodos):
                    db.marcar_estado_venta(conn, estado)
                conn.commit()
            except Exception:
                pass  # si el RCV no responde, seguimos sin marcar rechazos

        # Notas de crédito de anulación: revisa el PDF de cada NC emitida aún no
        # procesada en busca de "ANULA DOCUMENTO DE LA REFERENCIA..." y, si la
        # encuentra, marca como ANULADA la factura que referencia (folio +
        # contraparte). Solo se descarga el PDF una vez por NC (ver
        # ref_procesada); si la descarga falla se reintenta en el próximo sync.
        pendientes_nc = db.notas_credito_sin_procesar(conn)
        if pendientes_nc:
            estado_sync["fase"] = "Revisando notas de crédito…"
            for nc in pendientes_nc:
                try:
                    pdf_bytes = sii_docs.obtener_pdf_bytes(client.session, "emitidos", nc["codigo_sii"])
                except Exception:
                    pdf_bytes = None
                if not pdf_bytes:
                    continue
                folio_ref = sii_docs.folio_anulado_en_nc(pdf_bytes)
                if folio_ref:
                    db.marcar_anulada(conn, folio_ref, nc["rut_contraparte"], nc["codigo_sii"])
                db.marcar_referencia_procesada(conn, nc["codigo_sii"])
            conn.commit()

        estado_sync["recibidas"] = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo='compra'"
        ).fetchone()[0]
        estado_sync["emitidas"] = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo='venta'"
        ).fetchone()[0]
    finally:
        conn.close()

    estado_sync["fase"] = "Listo"
    estado_sync["corriendo"] = False

    return {
        "recibidas": len(recibidos),
        "emitidas": len(emitidos),
        "total_recibidas": estado_sync["recibidas"],
        "total_emitidas": estado_sync["emitidas"],
    }


def sincronizar_async(client: SIIClient, anio: int = 2026,
                      desde: str | None = None) -> None:
    """Dispara la sincronización completa en segundo plano.

    Marca corriendo=True de inmediato (antes de devolver) para que el panel
    muestre la barra de progreso apenas se pulsa el botón; el trabajo
    (consulta al SII y guardado) corre en un hilo aparte.
    """
    if estado_sync.get("corriendo"):
        return  # ya hay una sincronización en curso
    estado_sync.update(corriendo=True, error=None, fase="Iniciando…", sesion_perdida=False)

    def _worker() -> None:
        try:
            sincronizar(client, anio=anio, desde=desde)
        except SIISessionExpirada:
            pass  # ya quedó reflejado en estado_sync (sesion_perdida=True)
        except Exception as exc:
            estado_sync.update(corriendo=False, error=str(exc), fase="Error")

    threading.Thread(target=_worker, daemon=True).start()
