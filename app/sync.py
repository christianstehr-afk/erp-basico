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

from . import db, sii_bhe, sii_docs, sii_rcv
from .sii_client import SIIClient, SIISessionExpirada

# Estado simple de sincronización (para mostrar en el panel)
estado_sync: dict = {
    "corriendo": False,
    "fase": "",           # texto de la etapa actual (para la barra de progreso)
    "recibidas": 0,
    "emitidas": 0,
    "boletas": 0,
    "error": None,
    # Distinto de un error cualquiera: la sesión con el SII se cerró de su
    # lado (típicamente por inactividad) y hay que volver a iniciar sesión
    # para seguir sincronizando. El panel usa esto para mandar al usuario a
    # /?relogin=1 en vez de solo mostrar "Error: ...".
    "sesion_perdida": False,
    # Las boletas de honorarios usan una sesión SII aparte (login "empresa").
    # Un problema ahí NUNCA bloquea el sync de facturas: se guarda acá y el
    # panel lo muestra como aviso, no como error duro.
    "boletas_error": None,
}


def sincronizar(client: SIIClient, anio: int = 2026, desde: str | None = None,
                client_bhe: SIIClient | None = None, rut_empresa: str | None = None) -> dict:
    """Actualiza la BD con recibidas, emitidas y boletas de honorarios del año.

    `client_bhe`/`rut_empresa`: sesión y RUT de la cuenta "empresa", usada
    solo para boletas de honorarios recibidas (ver sii_bhe.py). Si vienen en
    None, simplemente no se sincronizan boletas (no es un error).

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

        # Boletas de honorarios recibidas (BHE): requiere la sesión "empresa"
        # (login separado, ver main.py). Nunca bloquea el resto del sync: un
        # fallo acá se guarda en estado_sync["boletas_error"] y se sigue.
        estado_sync["boletas_error"] = None
        if client_bhe is not None and rut_empresa:
            estado_sync["fase"] = "Consultando boletas de honorarios…"
            try:
                boletas = sii_bhe.obtener_boletas_recibidas(client_bhe.session, rut_empresa, anio, desde=desde)
                if desde:
                    boletas = [b for b in boletas if (b.get("fecha") or "") >= desde]
                for b in boletas:
                    db.upsert_boleta(conn, b)
                conn.commit()
            except Exception as exc:
                estado_sync["boletas_error"] = str(exc)

        estado_sync["recibidas"] = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo='compra'"
        ).fetchone()[0]
        estado_sync["emitidas"] = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo='venta'"
        ).fetchone()[0]
        estado_sync["boletas"] = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo='compra' AND codigo_sii LIKE 'BHE-%'"
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
        "total_boletas": estado_sync["boletas"],
    }


def sincronizar_async(client: SIIClient, anio: int = 2026, desde: str | None = None,
                      client_bhe: SIIClient | None = None, rut_empresa: str | None = None) -> None:
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
            sincronizar(client, anio=anio, desde=desde, client_bhe=client_bhe, rut_empresa=rut_empresa)
        except SIISessionExpirada:
            pass  # ya quedó reflejado en estado_sync (sesion_perdida=True)
        except Exception as exc:
            estado_sync.update(corriendo=False, error=str(exc), fase="Error")

    threading.Thread(target=_worker, daemon=True).start()
