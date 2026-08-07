"""
RCV — Registro de Compras y Ventas (SPA Angular en www4.sii.cl).

Se usa para obtener el ESTADO de las facturas/notas emitidas, en particular el
RECHAZO (reclamo) del receptor. Una emitida rechazada pierde la obligación de
pago, por lo que no debe contarse como cobrable.

A diferencia de los módulos anteriores (páginas HTML), el RCV expone una API
JSON. El endpoint del detalle de ventas:

    POST https://www4.sii.cl/consdcvinternetui/services/data/facadeService/getDetalleVenta

Campos relevantes de cada fila de la respuesta:
    detNroDoc        -> folio
    detRutDoc/detDvDoc -> RUT receptor
    detFecReclamado  -> fecha/hora del rechazo (no-null == RECHAZADA)
    detFecAcuse      -> fecha/hora del acuse de recibo (aceptada)

Notas importantes:
- El `tokenRecaptcha` es un placeholder literal que el SII acepta en sesión
  autenticada.
- El `conversationId` NO es aleatorio: debe ser el token de sesión del SII, que
  el login deja en la cookie `TOKEN`. Un valor aleatorio devuelve codRespuesta 99
  (sin datos). Por eso se lee de la sesión.
- Antes de consultar conviene un "warm-up" GET al RCV (www4) para asegurar las
  cookies de sesión de ese subdominio.
"""
from __future__ import annotations

import uuid

import requests

from .sii_client import normalizar_rut

RCV_BASE = "https://www4.sii.cl/consdcvinternetui/"
RCV_URL = RCV_BASE + "services/data/facadeService/getDetalleVenta"
NAMESPACE = "cl.sii.sdi.lob.diii.consdcv.data.api.interfaces.FacadeService/getDetalleVenta"

# Tipos de documento de venta que nos interesan (factura, exenta, nota de crédito)
TIPOS_VENTA = (33, 34, 61)


def conversation_id(session: requests.Session) -> str:
    """El conversationId del RCV es el token de sesión (cookie TOKEN)."""
    return session.cookies.get("TOKEN") or ""


def obtener_detalle_venta(
    session: requests.Session, rut_num: str, dv: str, periodo: str,
    cod_tipo_doc: int, conv_id: str,
) -> list[dict]:
    """POST getDetalleVenta para un periodo (YYYYMM) y tipo de documento.

    Devuelve la lista `data` (filas) o [] si no hay o si la respuesta falla.
    """
    body = {
        "metaData": {
            "namespace": NAMESPACE,
            "conversationId": conv_id,
            "transactionId": str(uuid.uuid4()),
            "page": None,
        },
        "data": {
            "rutEmisor": rut_num,
            "dvEmisor": dv,
            "ptributario": periodo,
            "codTipoDoc": str(cod_tipo_doc),
            "operacion": "",
            "estadoContab": "",
            "accionRecaptcha": "RCV_DETV",
            "tokenRecaptcha": "t-o-k-e-n-web",
        },
    }
    try:
        resp = session.post(RCV_URL, json=body, timeout=60)
        j = resp.json()
    except (requests.RequestException, ValueError):
        return []
    if (j.get("respEstado") or {}).get("codRespuesta") != 0:
        return []
    return j.get("data") or []


def estados_de_venta(
    session: requests.Session, rut_empresa: str, periodos: list[str], tipos=TIPOS_VENTA
) -> list[dict]:
    """Consulta el RCV de ventas para varios periodos y tipos.

    Devuelve una lista de dicts con el estado de cada documento emitido:
        {tipo_dte, folio, rut_receptor, fecha_reclamo, fecha_acuse}
    `periodos` es una lista de strings 'YYYYMM'.
    """
    # Warm-up: asegura las cookies de sesión del subdominio del RCV (www4).
    try:
        session.get(RCV_BASE, timeout=30)
    except requests.RequestException:
        pass

    conv_id = conversation_id(session)
    if not conv_id:
        return []  # sin token de sesión el RCV no responde datos

    num, dv = normalizar_rut(rut_empresa)
    out: list[dict] = []
    for periodo in periodos:
        for cod in tipos:
            for r in obtener_detalle_venta(session, num, dv, periodo, cod, conv_id):
                folio = r.get("detNroDoc")
                if folio is None:
                    continue
                out.append(
                    {
                        "tipo_dte": cod,
                        "folio": int(folio),
                        "rut_receptor": f'{r.get("detRutDoc")}-{r.get("detDvDoc")}',
                        "fecha_reclamo": r.get("detFecReclamado") or None,
                        "fecha_acuse": r.get("detFecAcuse") or None,
                    }
                )
    return out
