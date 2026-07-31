"""
Documentos del sistema de facturación gratuito del SII: recibidos y emitidos.

Ambos comparten la misma estructura de tabla (8 columnas) y el mismo detalle
(mipeGesDoc*.cgi?CODIGO=...). Solo cambian el CGI de la lista y el primer
parámetro de filtro:

  - Recibidos: mipeAdminDocsRcp.cgi, primer parámetro RUT_EMI (emisor).
  - Emitidos:  mipeAdminDocsEmi.cgi, primer parámetro RUT_RECP (receptor).

La columna 1 es la contraparte (emisor en recibidos, receptor en emitidos); se
guarda de forma neutra como `rut_contraparte` + `razon_social`.

PDF: los recibidos se obtienen con mipeShowPdf.cgi?CODIGO=... Los emitidos usan
otra ruta (aún por definir), por eso `descargar_pdf` solo aplica a recibidos.

La página del SII viene en codificación ISO-8859-1 (latin-1).
"""
from __future__ import annotations

import io
import re
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .sii_client import SIISessionExpirada

BASE = "https://www1.sii.cl/cgi-bin/Portal001/"
DETALLE_RE = re.compile(r"mipeGesDoc")
CODIGO_RE = re.compile(r"CODIGO=(\d+)")

# Configuración por fuente de documentos.
#   url / primer_param: lista de documentos (mipeAdminDocs*.cgi).
#   pdf_url / pdf_param: endpoint del PDF (distinto en recibidos vs emitidos).
FUENTES = {
    "recibidos": {
        "url": BASE + "mipeAdminDocsRcp.cgi", "primer_param": "RUT_EMI", "tipo": "compra",
        "pdf_url": BASE + "mipeShowPdf.cgi", "pdf_param": "CODIGO",
    },
    "emitidos": {
        "url": BASE + "mipeAdminDocsEmi.cgi", "primer_param": "RUT_RECP", "tipo": "venta",
        "pdf_url": BASE + "mipeDisplayPDF.cgi", "pdf_param": "DHDR_CODIGO",
    },
}

# Texto del tipo de documento -> código DTE del SII
TIPOS_DTE = {
    "factura electronica": 33,
    "factura no afecta o exenta electronica": 34,
    "factura exenta electronica": 34,
    "nota de credito electronica": 61,
    "nota de debito electronica": 56,
    "guia de despacho electronica": 52,
    "factura de compra electronica": 46,
    "liquidacion factura electronica": 43,
}


def _norm(texto: str) -> str:
    s = unicodedata.normalize("NFKD", texto or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


def _solo_digitos(texto: str) -> int:
    n = re.sub(r"[^0-9]", "", texto or "")
    return int(n) if n else 0


def _texto_propio(celda) -> str:
    """Texto de una celda EXCLUYENDO el contenido de celdas <td> anidadas.

    El HTML del SII deja el <td> de la contraparte sin cerrar, y html.parser
    anida las celdas siguientes dentro de él. Tomando solo el texto anterior a
    la primera celda anidada, obtenemos el valor real de la celda.
    """
    partes: list[str] = []
    for hijo in celda.children:
        nombre = getattr(hijo, "name", None)
        if nombre == "td":
            break  # desde aquí es una celda anidada (continuación de la fila)
        if isinstance(hijo, str):
            partes.append(hijo)
        elif nombre is not None:
            partes.append(hijo.get_text(" ", strip=True))
    return " ".join(" ".join(partes).split())


def tipo_dte(documento_texto: str) -> int | None:
    return TIPOS_DTE.get(_norm(documento_texto))


def parse_lista(html: str) -> list[dict]:
    """Extrae las filas de la tabla de documentos (recibidos o emitidos)."""
    soup = BeautifulSoup(html, "html.parser")

    tabla = None
    for t in soup.find_all("table"):
        if t.find("a", href=DETALLE_RE):
            tabla = t
            break
    if tabla is None:
        return []

    filas: list[dict] = []
    for tr in tabla.find_all("tr"):
        enlace = tr.find("a", href=DETALLE_RE)
        if not enlace:
            continue
        m = CODIGO_RE.search(enlace.get("href", ""))
        if not m:
            continue
        celdas = tr.find_all("td")
        if len(celdas) < 8:
            continue
        texto = [_texto_propio(c) for c in celdas]
        # 0=Ver 1=Contraparte(RUT) 2=RazonSocial 3=Documento 4=Folio 5=Fecha 6=Monto 7=Estado
        documento = texto[3]
        filas.append(
            {
                "codigo": m.group(1),
                "rut_contraparte": texto[1],
                "razon_social": texto[2],
                "documento": documento,
                "tipo_dte": tipo_dte(documento),
                "folio": _solo_digitos(texto[4]),
                "fecha": texto[5],
                "monto": _solo_digitos(texto[6]),
                "estado": texto[7],
            }
        )
    return filas


def obtener_documentos(
    session: requests.Session, fuente: str, anio: int = 2026, max_paginas: int = 50
) -> list[dict]:
    """Recorre las páginas y devuelve los documentos del año dado.

    `fuente` es 'recibidos' o 'emitidos'. La lista viene por fecha descendente,
    así que en cuanto una página completa queda bajo el año buscado, se detiene.
    """
    cfg = FUENTES[fuente]
    docs: list[dict] = []
    desde = f"{anio}-01-01"
    otros = ["FOLIO", "RZN_SOC", "FEC_DESDE", "FEC_HASTA", "TPO_DOC", "ESTADO", "ORDEN"]
    for pagina in range(1, max_paginas + 1):
        params = {cfg["primer_param"]: ""}
        params.update({k: "" for k in otros})
        params["NUM_PAG"] = pagina
        resp = session.get(cfg["url"], params=params, timeout=60)
        html = resp.content.decode("iso-8859-1", "replace")
        filas = parse_lista(html)
        if pagina == 1 and not filas:
            # No se reconoció ninguna fila de documento en la primera página.
            # Antes esto solo se trataba como sesión perdida si el HTML calzaba
            # con alguna de las frases de _MARCADORES_LOGIN (best-effort, nunca
            # probado contra una sesión real vencida). En producción el SII
            # devolvió una pantalla distinta que NO calzó con esas frases, así
            # que el sync terminó "Listo" con 0 documentos sin avisar que en
            # realidad la sesión se había caído.
            # Esta empresa tiene documentos todos los meses desde mayo 2026, así
            # que una primera página sin ninguna fila reconocible es en la
            # práctica siempre síntoma de sesión vencida (o de que el SII
            # cambió el HTML de esa pantalla) — nunca de que de verdad no hay
            # documentos ese año. Tratamos ambos casos igual: hay que pedirle a
            # Christian que vuelva a ingresar su Clave Tributaria.
            raise SIISessionExpirada(
                "La sesión con el SII se perdió (probablemente por inactividad)."
            )
        if not filas:
            break
        docs.extend(f for f in filas if (f["fecha"] or "").startswith(str(anio)))
        fechas = [f["fecha"] for f in filas if f["fecha"]]
        if fechas and max(fechas) < desde:
            break
    return docs


def obtener_pdf_bytes(session: requests.Session, fuente: str, codigo: str) -> bytes:
    """Descarga el PDF del documento y lo devuelve en memoria (sin guardarlo en disco).

    Se usa para servirlo al vuelo cuando el usuario lo pide (ver /pdf/{codigo}/ver
    en main.py), en vez de guardar una copia local como hacía `descargar_pdf`.

    Con sesión válida y un código de documento existente, esta URL solo tiene
    dos resultados posibles: el PDF, o una pantalla de login porque el SII
    cerró la sesión. Antes se distinguía el segundo caso buscando frases
    conocidas ("clave tributaria", "sesión ha expirado", etc.) en el HTML, pero
    en producción el SII devolvió una variante de esa pantalla que no calzó
    con ninguna frase reconocida: el usuario vio un error genérico ("Intenta
    de nuevo") en vez del aviso de sesión perdida con el link para reingresar
    su Clave Tributaria. Por eso ahora se trata cualquier respuesta que no sea
    el PDF como sesión perdida, sin depender de reconocer el HTML exacto.
    """
    cfg = FUENTES[fuente]
    resp = session.get(cfg["pdf_url"], params={cfg["pdf_param"]: codigo}, timeout=60)
    if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
        return resp.content
    raise SIISessionExpirada(
        "La sesión con el SII se perdió (probablemente por inactividad)."
    )


# Notas de Crédito Electrónicas que dejan sin efecto una factura completa
# incluyen en su PDF una línea de referencia como:
#   "ANULA DOCUMENTO DE LA REFERENCIA- Fact.Electronica N° 124 del 2026-07-27"
# El texto exacto del tipo de documento varía; lo único estable es la frase
# "ANULA DOCUMENTO DE LA REFERENCIA" seguida, en algún punto cercano, del
# folio tras "N°"/"Nº".
ANULA_REF_RE = re.compile(
    r"ANULA\s+DOCUMENTO\s+DE\s+LA\s+REFERENCIA[\s\S]{0,80}?N[°ºo]\s*(\d+)",
    re.IGNORECASE,
)


def folio_anulado_en_nc(pdf_bytes: bytes) -> int | None:
    """Si el PDF de una Nota de Crédito trae la frase de anulación, devuelve el
    folio del documento que anula. Devuelve None si no aplica (p. ej. una NC
    normal de descuento) o si el PDF no se pudo leer."""
    try:
        texto = "\n".join(
            (pagina.extract_text() or "") for pagina in PdfReader(io.BytesIO(pdf_bytes)).pages
        )
    except Exception:
        return None
    m = ANULA_REF_RE.search(texto)
    return int(m.group(1)) if m else None


def descargar_pdf(
    session: requests.Session, fuente: str, codigo: str, destino_dir: Path
) -> str | None:
    """Descarga el PDF del documento y lo guarda como <codigo>.pdf.

    Cada fuente usa su propio endpoint:
      - recibidos: mipeShowPdf.cgi?CODIGO=...
      - emitidos:  mipeDisplayPDF.cgi?DHDR_CODIGO=...
    """
    cfg = FUENTES[fuente]
    destino_dir.mkdir(parents=True, exist_ok=True)
    ruta = destino_dir / f"{codigo}.pdf"
    if ruta.exists() and ruta.stat().st_size > 0:
        return str(ruta)
    resp = session.get(cfg["pdf_url"], params={cfg["pdf_param"]: codigo}, timeout=60)
    if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
        ruta.write_bytes(resp.content)
        return str(ruta)
    return None
