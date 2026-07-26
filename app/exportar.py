"""
Módulo 5 · Generación de archivos para el export contable.

- construir_excel: listado de movimientos (ingresos/egresos) a un .xlsx.
- construir_zip_rendiciones: un PDF por rendición (información + adjuntos, una
  imagen por página) empaquetados en un .zip.

Estas funciones no tocan la base de datos; reciben los datos ya consultados.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from .db import codigo_rendicion

# Extensiones de imagen que se pintan "una por página" en el PDF.
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}


def _miles(n: int) -> str:
    """Formato de miles con punto (estilo chileno): 1234567 -> 1.234.567."""
    return "{:,.0f}".format(n or 0).replace(",", ".")


def _nombre_archivo(nombre: str, sufijo: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", (nombre or "").strip()) or "rendicion"
    return f"{base[:80]}{sufijo}"


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

def construir_excel(movimientos: list[dict], desde: str, hasta: str) -> bytes:
    """Arma el .xlsx del listado de movimientos y lo devuelve en bytes."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Movimientos"

    verde = "FF009406"
    tinta = "FF0A0A0A"
    rojo = "FFC0392B"
    gris = "FFF2F2F2"

    # Título y rango.
    ws["A1"] = "Movimientos de caja"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color=tinta)
    ws["A2"] = f"Rango: {desde} a {hasta}"
    ws["A2"].font = Font(name="Calibri", size=10, color="FF666666")

    encabezados = ["Fecha", "Ingreso/Egreso", "Descripción", "Monto"]
    fila_head = 4
    borde = Border(bottom=Side(style="thin", color="FFDDDDDD"))
    for col, texto in enumerate(encabezados, start=1):
        c = ws.cell(row=fila_head, column=col, value=texto)
        c.font = Font(bold=True, color="FFFFFFFF")
        c.fill = PatternFill("solid", fgColor=tinta)
        c.alignment = Alignment(horizontal="right" if texto == "Monto" else "left")

    total_ing = 0
    total_egr = 0
    fila = fila_head + 1
    for m in movimientos:
        es_ing = m["flujo"] == "Ingreso"
        if es_ing:
            total_ing += m["monto"]
        else:
            total_egr += m["monto"]
        ws.cell(row=fila, column=1, value=m["fecha"])
        cflujo = ws.cell(row=fila, column=2, value=m["flujo"])
        cflujo.font = Font(bold=True, color=verde if es_ing else rojo)
        ws.cell(row=fila, column=3, value=m["descripcion"])
        cmonto = ws.cell(row=fila, column=4, value=m["monto"])
        cmonto.number_format = '"$"#,##0'
        cmonto.alignment = Alignment(horizontal="right")
        for col in range(1, 5):
            ws.cell(row=fila, column=col).border = borde
        fila += 1

    # Totales.
    fila += 1
    ws.cell(row=fila, column=3, value="Total ingresos").font = Font(bold=True, color=verde)
    ci = ws.cell(row=fila, column=4, value=total_ing)
    ci.number_format = '"$"#,##0'; ci.font = Font(bold=True, color=verde)
    fila += 1
    ws.cell(row=fila, column=3, value="Total egresos").font = Font(bold=True, color=rojo)
    ce = ws.cell(row=fila, column=4, value=total_egr)
    ce.number_format = '"$"#,##0'; ce.font = Font(bold=True, color=rojo)
    fila += 1
    ws.cell(row=fila, column=3, value="Neto (ingresos − egresos)").font = Font(bold=True, color=tinta)
    cn = ws.cell(row=fila, column=4, value=total_ing - total_egr)
    cn.number_format = '"$"#,##0'; cn.font = Font(bold=True, color=tinta)
    ws.cell(row=fila, column=3).fill = PatternFill("solid", fgColor=gris)
    ws.cell(row=fila, column=4).fill = PatternFill("solid", fgColor=gris)

    anchos = {1: 14, 2: 16, 3: 60, 4: 16}
    for col, w in anchos.items():
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.freeze_panes = "A5"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF por rendición
# ---------------------------------------------------------------------------

def _pagina_info(c, r, items, pagos) -> None:
    """Dibuja la página de información (portada) de una rendición."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm

    W, H = A4
    mx = 20 * mm
    y = H - 22 * mm

    c.setFillColorRGB(0, 0.58, 0.023)  # verde e-auto
    c.setFont("Helvetica-Bold", 9)
    c.drawString(mx, y, f"E-AUTO · RENDICIÓN {codigo_rendicion(r['id'])}")
    y -= 9 * mm
    c.setFillColorRGB(0.04, 0.04, 0.04)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(mx, y, (r["nombre"] or "")[:70])
    y -= 7 * mm
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.setFont("Helvetica", 10)
    c.drawString(mx, y, f"Fecha de rendición: {r['fecha']}")
    y -= 12 * mm

    total = r["total"] or 0
    pagado = r["pagado"] or 0
    saldo = total - pagado
    c.setFillColorRGB(0.04, 0.04, 0.04)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(mx, y, f"Total: ${_miles(total)}    Pagado: ${_miles(pagado)}    Saldo: ${_miles(saldo)}")
    y -= 12 * mm

    # Ítems
    c.setFont("Helvetica-Bold", 10)
    c.drawString(mx, y, "Ítems")
    y -= 6 * mm
    c.setFont("Helvetica-Bold", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(mx, y, "DESCRIPCIÓN")
    c.drawString(mx + 95 * mm, y, "N° DOC")
    c.drawRightString(W - mx, y, "MONTO")
    y -= 2 * mm
    c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.line(mx, y, W - mx, y)
    y -= 5 * mm
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    for it in items:
        if y < 30 * mm:
            c.showPage()
            y = H - 22 * mm
            c.setFont("Helvetica", 9)
            c.setFillColorRGB(0.1, 0.1, 0.1)
        c.drawString(mx, y, (it["descripcion"] or "")[:60])
        c.drawString(mx + 95 * mm, y, str(it["numero_doc"] or "—")[:18])
        c.drawRightString(W - mx, y, f"${_miles(it['monto'])}")
        y -= 5.5 * mm

    # Pagos
    y -= 6 * mm
    if y < 40 * mm:
        c.showPage()
        y = H - 22 * mm
    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(0.04, 0.04, 0.04)
    c.drawString(mx, y, "Pagos registrados")
    y -= 6 * mm
    c.setFont("Helvetica-Bold", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(mx, y, "FECHA")
    c.drawRightString(W - mx, y, "MONTO")
    y -= 2 * mm
    c.line(mx, y, W - mx, y)
    y -= 5 * mm
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.1, 0.1, 0.1)
    for p in pagos:
        if y < 20 * mm:
            c.showPage()
            y = H - 22 * mm
            c.setFont("Helvetica", 9)
            c.setFillColorRGB(0.1, 0.1, 0.1)
        c.drawString(mx, y, str(p["fecha"]))
        c.drawRightString(W - mx, y, f"${_miles(p['monto'])}")
        y -= 5.5 * mm
    c.showPage()


def _pagina_imagen(c, path: str, caption: str) -> None:
    """Dibuja una imagen ocupando la página (con leyenda)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader

    W, H = A4
    mx = 15 * mm
    top = H - 15 * mm
    img = ImageReader(path)
    iw, ih = img.getSize()
    max_w = W - 2 * mx
    max_h = H - 30 * mm  # deja espacio para la leyenda
    escala = min(max_w / iw, max_h / ih)
    w = iw * escala
    h = ih * escala
    x = (W - w) / 2
    y = top - h
    c.drawImage(img, x, y, w, h, preserveAspectRatio=True, anchor="n")
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(mx, 10 * mm, f"Adjunto: {caption}"[:110])
    c.showPage()


def _pdf_de_rendicion(r, items, adjuntos, pagos) -> bytes:
    """Devuelve el PDF (bytes) de una rendición: info + imágenes + PDFs adjuntos."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader, PdfWriter

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _pagina_info(c, r, items, pagos)
    # Imágenes: una por página.
    for a in adjuntos:
        ext = Path(a["nombre_archivo"] or "").suffix.lower()
        if ext in IMG_EXT and Path(a["path"]).exists():
            try:
                _pagina_imagen(c, a["path"], a["nombre_archivo"])
            except Exception:
                pass
    c.save()

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(buf.getvalue())).pages:
        writer.add_page(page)
    # PDFs adjuntos: se anexan al final tal cual.
    for a in adjuntos:
        ext = Path(a["nombre_archivo"] or "").suffix.lower()
        if ext == ".pdf" and Path(a["path"]).exists():
            try:
                for page in PdfReader(a["path"]).pages:
                    writer.add_page(page)
            except Exception:
                pass
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def construir_zip_rendiciones(rendiciones: list[dict]) -> bytes:
    """Empaqueta un PDF por rendición en un .zip.

    `rendiciones` es una lista de dicts con: r (fila), items, adjuntos, pagos.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        usados: set[str] = set()
        for rd in rendiciones:
            r = rd["r"]
            pdf = _pdf_de_rendicion(r, rd["items"], rd["adjuntos"], rd["pagos"])
            cod = codigo_rendicion(r["id"])
            nombre = f"{cod}_" + _nombre_archivo(r["nombre"], ".pdf")
            while nombre in usados:
                nombre = f"{cod}_" + _nombre_archivo(r["nombre"], "_x.pdf")
            usados.add(nombre)
            zf.writestr(nombre, pdf)
    return buf.getvalue()
