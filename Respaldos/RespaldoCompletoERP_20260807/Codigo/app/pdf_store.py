"""
Almacén local de PDFs de facturas y boletas.

Los documentos del SII no cambian una vez emitidos, así que la copia local es
permanente: se descarga UNA vez (durante el sync, ver sync._precargar_pdfs, o
al primer clic si aún no estaba, ver main._cachear_pdf) y de ahí en adelante
se sirve desde disco — el clic en un folio deja de depender de la latencia
del SII (medida 2026-08-07: entre 3 y >45 segundos para el mismo documento).

Rutas: PDF_DIR/<grupo>/<anio>/<codigo>.pdf, con grupo = boletas (códigos
BHE-*), recibidas (tipo compra) o emitidas (tipo venta). En producción
(Railway) PDF_DIR=/data/pdfs vive en el volumen persistente (ver Dockerfile);
en local es data/pdfs dentro de la carpeta del proyecto (escribir archivos
estáticos en la carpeta Dropbox es seguro; solo la BD transaccional no debe
vivir ahí, ver db.py).
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from . import db

PDF_DIR = Path(
    os.environ.get("PDF_DIR", Path(__file__).resolve().parent.parent / "data" / "pdfs")
)

_GRUPO_POR_TIPO = {"compra": "recibidas", "venta": "emitidas"}


def _sanear(codigo: str) -> str:
    """Nombre de archivo seguro a partir del codigo_sii (alfanumérico en la
    práctica, pero por si acaso)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", codigo or "")


def ruta_local(codigo: str, tipo: str, fecha: str | None) -> Path:
    """Ruta canónica donde guardar/buscar el PDF de un documento."""
    if (codigo or "").startswith("BHE-"):
        grupo = "boletas"
    else:
        grupo = _GRUPO_POR_TIPO.get(tipo or "", "otros")
    anio = (fecha or "")[:4] or "sin-fecha"
    return PDF_DIR / grupo / anio / f"{_sanear(codigo)}.pdf"


def tiene_copia(pdf_path: str | None) -> bool:
    """True si pdf_path apunta a un archivo existente y no vacío. Chequeo
    liviano para decidir qué falta descargar (sin leer el archivo entero)."""
    if not pdf_path:
        return False
    try:
        p = Path(pdf_path)
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def leer(pdf_path: str | None) -> bytes | None:
    """Bytes del PDF guardado, o None si no hay copia local utilizable.

    Valida la firma %PDF- para no servir jamás un archivo corrupto: en ese
    caso se devuelve None y el llamador cae a la descarga en vivo del SII
    (que además vuelve a guardar la copia buena).
    """
    if not pdf_path:
        return None
    try:
        p = Path(pdf_path)
        if not p.is_file():
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if data[:5] != b"%PDF-":
        return None
    return data


def guardar(conn: sqlite3.Connection, codigo: str, tipo: str, fecha: str | None,
            data: bytes) -> str | None:
    """Guarda el PDF en disco y deja pdf_path apuntándole en la BD (SIN
    commit: el llamador decide cuándo confirmar). Devuelve la ruta guardada,
    o None si los bytes no son un PDF o la escritura falló.

    La escritura es atómica (archivo temporal + replace): nunca queda un
    .pdf a medias aunque el proceso muera en plena descarga.
    """
    if not data or data[:5] != b"%PDF-":
        return None
    path = ruta_local(codigo, tipo, fecha)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".pdf.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        return None
    db.marcar_pdf(conn, codigo, str(path))
    return str(path)
