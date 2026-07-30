"""
Boletas de Honorarios Electrónicas (BHE) recibidas — módulo aparte porque usa
un sistema del SII completamente distinto al de facturas (DTE):

  Servicios online -> Boletas de honorarios electrónicas -> Emisor de Boletas
  de Honorarios -> Consultar boletas recibidas -> elegir año -> elegir mes.

Es un sistema legado (cgi_IMT en loa.sii.cl), sin API ni HTML documentado, y
requiere iniciar sesión con el RUT de la EMPRESA (no con el RUT personal que
se usa para las facturas). Por eso vive en su propia sesión SII (ver
SII_SESSIONS_BHE en main.py) y su propio módulo.

IMPORTANTE — estado de este módulo: el parseo de las tablas de boletas está
escrito de forma defensiva (heurísticas sobre RUT/fecha/montos, no nombres de
columna fijos) porque no fue posible probarlo contra una sesión real del SII
antes de desplegarlo (requiere credenciales de producción). Es muy probable
que el primer sync real necesite un ajuste fino una vez que se vea el HTML
real que devuelve el SII. Si falla, `obtener_boletas_recibidas` lanza
BHEError con el detalle más específico posible para facilitar ese ajuste.
"""
from __future__ import annotations

import re
import time
import unicodedata
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .sii_client import normalizar_rut

MENU_URL = "https://loa.sii.cl/cgi_IMT/TMBCOC_MenuConsultasContribRec.cgi"
INFORME_ANUAL_URL = "https://loa.sii.cl/cgi_IMT/TMBCOC_InformeAnualBheRec.cgi"

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

_RUT_RE = re.compile(r"\b(\d{1,2}\.?\d{3}\.?\d{3})-?([\dkK])\b")
_FECHA_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})\b")
_MONTO_RE = re.compile(r"\b\d{1,3}(?:\.\d{3})+\b|\b\d{4,}\b")


class BHEError(Exception):
    """Error al consultar boletas de honorarios recibidas (login, red, o
    parseo). Nunca debe tumbar el sync general de facturas: quien la capture
    (sync.py) debe tratarla como no bloqueante."""


def _norm(texto: str) -> str:
    s = unicodedata.normalize("NFKD", texto or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


def _es_pagina_de_login(html: str) -> bool:
    texto = _norm(html)
    marcadores = (
        "clave tributaria", "ingrese su rut y clave", "sesion ha expirado",
        "sesion no valida", "sesion invalida", "debe iniciar sesion",
        "usuario y/o clave", "clave incorrecta",
    )
    return any(m in texto for m in marcadores)


def _fecha_a_iso(fecha_raw: str) -> str | None:
    """Convierte 'DD/MM/YYYY' a 'YYYY-MM-DD'. Si ya viene en ISO, la deja igual."""
    if not fecha_raw:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", fecha_raw):
        return fecha_raw
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", fecha_raw)
    if m:
        dia, mes, anio = m.groups()
        return f"{anio}-{mes}-{dia}"
    return None


def _solo_digitos(texto: str) -> int:
    n = re.sub(r"[^0-9]", "", texto or "")
    return int(n) if n else 0


def _links_de_mes(html: str, base_url: str) -> list[str]:
    """Busca en el informe anual los links a cada mes (detalle con boletas).

    No conocemos el nombre exacto del .cgi de detalle mensual, así que se
    toma cualquier link a un script bajo cgi_IMT que NO sea el informe anual
    mismo (heurística: son los únicos otros links "de negocio" en esa
    página, aparte de navegación general del portal SII).
    """
    soup = BeautifulSoup(html, "html.parser")
    vistos: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "cgi_IMT" not in href:
            continue
        if "InformeAnualBheRec" in href or "MenuConsultasContribRec" in href:
            continue
        abs_href = urljoin(base_url, href)
        if abs_href not in vistos:
            vistos.add(abs_href)
            links.append(abs_href)
    return links


def _pdf_link_de_fila(tr, base_url: str) -> str | None:
    """Busca dentro de una fila un link que probablemente descargue el PDF de
    la boleta (heurística: contiene 'pdf' en el href, o el texto del link es
    'Ver'/'PDF'/un ícono sin texto)."""
    for a in tr.find_all("a", href=True):
        href = a["href"]
        texto = _norm(a.get_text(" ", strip=True))
        if "pdf" in href.lower() or "pdf" in texto or texto in ("ver", "visualizar", ""):
            return urljoin(base_url, href)
    return None


def parse_mes(html: str, base_url: str, rut_empresa: str) -> list[dict]:
    """Extrae las boletas de una página de detalle mensual.

    Heurística por fila (sin asumir nombres/orden de columna fijos):
      - RUT del emisor: primer patrón de RUT en la fila que NO sea el RUT de
        la empresa (que puede aparecer repetido en cada fila del listado).
      - Folio: primer número "suelto" (sin puntos de miles) de la fila.
      - Fecha: primer patrón de fecha (DD/MM/YYYY o YYYY-MM-DD).
      - Monto: el mayor número con formato de miles (1.234) de la fila — en
        general el bruto es el monto más alto de la boleta.
      - Razón social / nombre: el texto de la celda más larga que no sea
        puramente numérica ni el RUT.
    """
    soup = BeautifulSoup(html, "html.parser")
    rut_emp_norm, dv_emp = normalizar_rut(rut_empresa)
    rut_emp_fmt = f"{rut_emp_norm}-{dv_emp}"

    boletas: list[dict] = []
    for tabla in soup.find_all("table"):
        filas = tabla.find_all("tr")
        if len(filas) < 2:
            continue
        for tr in filas:
            celdas = tr.find_all("td")
            if len(celdas) < 3:
                continue
            textos = [c.get_text(" ", strip=True) for c in celdas]

            # Identifica las celdas que SON un RUT (para no confundir sus
            # dígitos con folio/monto más abajo: p. ej. "98.765.432-1" calza
            # con el patrón de monto con puntos de miles si no se excluye).
            rut_cells: set[int] = set()
            ruts_en_fila: list[str] = []
            for i, c in enumerate(textos):
                m = _RUT_RE.search(c)
                if m:
                    rut_cells.add(i)
                    ruts_en_fila.append(f"{m.group(1).replace('.', '')}-{m.group(2).upper()}")

            ruts_ajenos = [r for r in ruts_en_fila if r != rut_emp_fmt]
            if not ruts_ajenos:
                continue  # fila sin RUT de un tercero: no es una boleta (cabecera, totales, etc.)
            rut_emisor = ruts_ajenos[0]

            resto = [c for i, c in enumerate(textos) if i not in rut_cells]
            resto_txt = " | ".join(resto)

            fecha_m = _FECHA_RE.search(resto_txt)
            fecha_iso = _fecha_a_iso(fecha_m.group(1)) if fecha_m else None

            montos = [_solo_digitos(m.group(0)) for m in _MONTO_RE.finditer(resto_txt)]
            montos = [m for m in montos if m > 0]
            if not montos:
                continue
            monto = max(montos)

            # Folio: primer entero "suelto" (1-6 dígitos) entre las celdas que
            # no son RUT.
            folio = None
            for c in resto:
                c_limpio = c.strip()
                if re.fullmatch(r"\d{1,6}", c_limpio):
                    folio = int(c_limpio)
                    break

            # Nombre: celda no numérica más larga entre las que no son RUT.
            candidatos_nombre = [c for c in resto if c and not re.fullmatch(r"[\d.\-/ ]+", c)]
            razon_social = max(candidatos_nombre, key=len) if candidatos_nombre else rut_emisor

            pdf_href = _pdf_link_de_fila(tr, base_url)

            if folio is None:
                # Sin folio no hay forma confiable de armar un codigo_sii único
                # y estable; se descarta la fila en vez de arriesgar duplicados.
                continue

            boletas.append({
                "codigo": f"BHE-{rut_emisor}-{folio}",
                "folio": folio,
                "rut_contraparte": rut_emisor,
                "razon_social": razon_social,
                "fecha": fecha_iso,
                "monto": monto,
                "pdf_href": pdf_href,
            })
    return boletas


def obtener_boletas_recibidas(
    session: requests.Session, rut_empresa: str, anio: int
) -> list[dict]:
    """Devuelve las boletas de honorarios recibidas por la empresa en `anio`.

    Sigue el flujo: menú -> informe anual (elige año) -> cada mes -> parseo.
    Lanza BHEError si el SII no responde como se espera (sesión vencida,
    HTML irreconocible, etc.) — nunca una excepción cruda de requests/bs4.
    """
    try:
        numero, dv = normalizar_rut(rut_empresa)
    except Exception as exc:
        raise BHEError(f"RUT de empresa inválido: {exc}") from exc

    try:
        # Paso 1: menú (deja la navegación "posicionada"; algunos sistemas
        # legados del SII validan que se venga de ahí). Nunca bloquea: si
        # falla, se sigue igual al informe anual.
        session.get(MENU_URL, params={"dummy": int(time.time() * 1000)}, timeout=30)
    except requests.RequestException:
        pass

    try:
        resp = session.get(
            INFORME_ANUAL_URL,
            params={"rut_arrastre": numero, "dv_arrastre": dv, "cbanoinformeanual": anio},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise BHEError(f"No se pudo conectar al SII (informe anual BHE): {exc}") from exc

    html = resp.content.decode("iso-8859-1", "replace")
    if _es_pagina_de_login(html):
        raise BHEError(
            "La sesión con el SII (cuenta empresa) se perdió o no se pudo autenticar "
            "para consultar boletas de honorarios."
        )

    links_mes = _links_de_mes(html, resp.url)
    if not links_mes:
        raise BHEError(
            "No se encontraron links a meses en el informe anual de boletas de honorarios. "
            "Es posible que el SII haya cambiado el HTML de esa página (revisar sii_bhe.py)."
        )

    boletas: list[dict] = []
    errores: list[str] = []
    for href in links_mes:
        try:
            r = session.get(href, timeout=30)
            html_mes = r.content.decode("iso-8859-1", "replace")
            if _es_pagina_de_login(html_mes):
                raise BHEError("Sesión perdida al abrir el detalle de un mes.")
            boletas.extend(parse_mes(html_mes, r.url, rut_empresa))
        except BHEError:
            raise
        except Exception as exc:
            # Un mes que falla no debe tumbar los demás.
            errores.append(f"{href}: {exc}")

    if not boletas and errores:
        raise BHEError("No se pudo leer ningún mes: " + "; ".join(errores[:3]))

    return boletas


def diagnostico(session: requests.Session, url: str, params: dict) -> str:
    """Vuelca links, tablas y formularios de una página del SII (con la
    sesión ya autenticada) en texto plano, para poder ajustar el parser sin
    tener acceso directo al SII. Ver /debug/bhe/inspeccionar en main.py.

    Nunca lanza: cualquier error de red queda como texto en el resultado.
    """
    try:
        resp = session.get(url, params=params, timeout=30)
    except requests.RequestException as exc:
        return f"Error de red pidiendo {url} (params={params}): {exc}"

    html = resp.content.decode("iso-8859-1", "replace")
    soup = BeautifulSoup(html, "html.parser")

    out = [
        f"URL pedida: {url}",
        f"Params:     {params}",
        f"URL final:  {resp.url}",
        f"Status:     {resp.status_code}  ·  bytes: {len(resp.content)}",
        f"¿Pantalla de login?: {_es_pagina_de_login(html)}",
        "",
        "=== FORMULARIOS (action | method | inputs) ===",
    ]
    forms = soup.find_all("form")
    if not forms:
        out.append("(ninguno)")
    for f in forms:
        inputs = [
            f"{i.get('name')}={i.get('value', '')!r}"
            for i in f.find_all(["input", "select"])
            if i.get("name")
        ]
        out.append(f"{f.get('action')}  |  {f.get('method', 'get')}  |  {', '.join(inputs)}")

    out.append("")
    out.append("=== LINKS (href | texto) ===")
    vistos: set[tuple[str, str]] = set()
    n = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        texto = a.get_text(" ", strip=True)
        key = (href, texto)
        if key in vistos:
            continue
        vistos.add(key)
        out.append(f"{href}  |  {texto}")
        n += 1
        if n >= 300:
            out.append("… (cortado en 300 links)")
            break

    tablas = soup.find_all("table")
    out.append("")
    out.append(f"=== TABLAS ({len(tablas)}) ===")
    for i, t in enumerate(tablas):
        filas = t.find_all("tr")
        out.append(f"--- tabla {i}: {len(filas)} filas ---")
        for tr in filas[:6]:
            celdas = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            out.append(" | ".join(celdas))

    out.append("")
    out.append("=== HTML crudo (primeros 6000 caracteres) ===")
    out.append(html[:6000])
    return "\n".join(out)


def obtener_pdf_bytes(session: requests.Session, pdf_href: str) -> bytes | None:
    """Descarga el PDF de una boleta desde el href guardado al parsear el mes."""
    if not pdf_href:
        return None
    try:
        resp = session.get(pdf_href, timeout=60)
    except requests.RequestException:
        return None
    if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
        return resp.content
    if _es_pagina_de_login(resp.content.decode("iso-8859-1", "replace")):
        raise BHEError("La sesión con el SII (cuenta empresa) se perdió al pedir el PDF de la boleta.")
    return None
