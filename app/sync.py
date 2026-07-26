"""
Sincronización con el SII: actualiza la base de datos con las facturas
recibidas y emitidas nuevas, y descarga los PDF pendientes de las recibidas.

Se ejecuta al iniciar sesión (y puede dispararse manualmente). El guardado de
metadatos es rápido y sincrónico; la descarga de PDF corre en segundo plano
para no demorar la carga del panel.

Recibidas y emitidas descargan PDF, cada una con su propio endpoint del SII
(ver sii_docs.FUENTES).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from . import db, sii_docs, sii_rcv
from .sii_client import SIIClient

PDF_DIR = Path(
    os.environ.get("PDF_DIR", Path(__file__).resolve().parent.parent / "data" / "pdfs")
)

# Estado simple de sincronización (para mostrar en el panel)
estado_sync: dict = {
    "corriendo": False,
    "recibidas": 0,
    "emitidas": 0,
    "pdf_pendientes": 0,
    "error": None,
}

# Mapa tipo BD -> (fuente sii_docs, subcarpeta de PDFs)
_TIPOS = {"compra": ("recibidos", "recibidas"), "venta": ("emitidos", "emitidas")}


def _descargar_pdfs_en_background(client: SIIClient, anio: int) -> None:
    conn = db.get_conn()
    try:
        def total_pendientes() -> int:
            return sum(len(db.pendientes_de_pdf(conn, tipo=t)) for t in _TIPOS)

        estado_sync["pdf_pendientes"] = total_pendientes()
        for tipo, (fuente, subcarpeta) in _TIPOS.items():
            destino = PDF_DIR / subcarpeta / str(anio)
            for codigo in db.pendientes_de_pdf(conn, tipo=tipo):
                try:
                    ruta = sii_docs.descargar_pdf(client.session, fuente, codigo, destino)
                    if ruta:
                        db.marcar_pdf(conn, codigo, ruta)
                        conn.commit()
                except Exception:  # una descarga fallida no debe cortar el resto
                    continue
                estado_sync["pdf_pendientes"] = total_pendientes()
    finally:
        conn.close()
        estado_sync["corriendo"] = False


def sincronizar(client: SIIClient, anio: int = 2026, desde: str | None = None,
                descargar_pdfs: bool = True) -> dict:
    """Actualiza la BD con recibidas y emitidas del año; lanza descarga de PDFs.

    Devuelve un resumen con cuántos documentos se trajeron de cada tipo.
    """
    estado_sync.update(corriendo=True, error=None)
    try:
        recibidos = sii_docs.obtener_documentos(client.session, "recibidos", anio=anio)
        emitidos = sii_docs.obtener_documentos(client.session, "emitidos", anio=anio)
    except Exception as exc:
        estado_sync.update(corriendo=False, error=str(exc))
        raise

    # Solo documentos desde `desde` (YYYY-MM-DD) en adelante: inicio en
    # producción del ERP (junio 2026). Antes de eso no se sincroniza nada.
    if desde:
        recibidos = [d for d in recibidos if (d.get("fecha") or "") >= desde]
        emitidos = [d for d in emitidos if (d.get("fecha") or "") >= desde]

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

        estado_sync["recibidas"] = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo='compra'"
        ).fetchone()[0]
        estado_sync["emitidas"] = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo='venta'"
        ).fetchone()[0]
        estado_sync["pdf_pendientes"] = sum(
            len(db.pendientes_de_pdf(conn, tipo=t)) for t in ("compra", "venta")
        )
    finally:
        conn.close()

    if descargar_pdfs:
        hilo = threading.Thread(
            target=_descargar_pdfs_en_background, args=(client, anio), daemon=True
        )
        hilo.start()
    else:
        estado_sync["corriendo"] = False

    return {
        "recibidas": len(recibidos),
        "emitidas": len(emitidos),
        "total_recibidas": estado_sync["recibidas"],
        "total_emitidas": estado_sync["emitidas"],
    }
