"""
Boletas de Honorarios Electrónicas (BHE) recibidas — módulo aparte porque usa
un sistema del SII completamente distinto al de facturas (DTE):

  Servicios online -> Boletas de honorarios electrónicas -> Emisor de Boletas
  de Honorarios -> Consultar boletas recibidas -> elegir año -> elegir mes.

Es un sistema legado (cgi_IMT en loa.sii.cl), sin API ni HTML documentado, y
requiere iniciar sesión con el RUT de la EMPRESA (no con el RUT personal que
se usa para las facturas). Por eso vive en su propia sesión SII (ver
SII_SESSIONS_BHE en main.py) y su propio módulo.

IMPORTANTE — estado de este módulo (30-jul-2026): el informe anual construye
el link a cada mes con JavaScript puro (document.write), no con <a href>
planos, así que hubo que sacar el patrón real de la URL inspeccionando el
HTML real (ver /debug/bhe/inspeccionar en main.py) en vez de heurísticas:

  TMBCOC_InformeMensualBheRec.cgi?cbanoinformemensual=<año>
    &cbmesinformemensual=<MM, 2 dígitos>&dv_arrastre=<dv>
    &pagina_solicitada=<0,1,2,...>&rut_arrastre=<rut sin dv>

Esa parte ya está confirmada contra el SII real. Lo que SIGUE sin probar
contra datos reales es el parseo de las filas de boletas dentro de esa
página mensual (`parse_mes`): está escrito de forma defensiva (heurísticas
sobre RUT/fecha/montos, no nombres de columna fijos) porque aún no se vio el
HTML real de esa página. Es muy probable que necesite un ajuste una vez que
se vea (usar /debug/bhe/inspeccionar?url=<url de un mes con boletas>).
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
INFORME_MENSUAL_URL = "https://loa.sii.cl/cgi_IMT/TMBCOC_InformeMensualBheRec.cgi"

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


def _meses_a_revisar(anio: int, desde: str | None) -> range:
    """Meses (1-12) del año a consultar, recortado por `desde` (YYYY-MM-DD)
    cuando corresponde, para no pedir de más al SII."""
    if not desde:
        return range(1, 13)
    try:
        anio_desde, mes_desde = int(desde[:4]), int(desde[5:7])
    except (ValueError, IndexError):
        return range(1, 13)
    if anio_desde > anio:
        return range(0)  # nada que traer: el año pedido es anterior a `desde`
    if anio_desde == anio:
        return range(mes_desde, 13)
    return range(1, 13)


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
    session: requests.Session, rut_empresa: str, anio: int, desde: str | None = None
) -> list[dict]:
    """Devuelve las boletas de honorarios recibidas por la empresa en `anio`.

    Sigue el flujo: menú -> informe anual (valida la sesión) -> cada mes
    desde `desde` (YYYY-MM-DD) en adelante, con paginación -> parseo. Lanza
    BHEError si el SII no responde como se espera (sesión vencida, etc.) —
    nunca una excepción cruda de requests/bs4.

    La URL de cada mes se arma directamente (confirmada inspeccionando el
    HTML real del SII, ver comentario al inicio del módulo) en vez de
    scrapear el link: el informe anual lo arma con JavaScript puro
    (document.write), no hay <a href> que buscar.
    """
    try:
        numero, dv = normalizar_rut(rut_empresa)
    except Exception as exc:
        raise BHEError(f"RUT de empresa inválido: {exc}") from exc

    try:
        # Paso 1: menú (deja la navegación "posicionada"). Nunca bloquea: si
        # falla, se sigue igual al informe anual.
        session.get(MENU_URL, params={"dummy": int(time.time() * 1000)}, timeout=30)
    except requests.RequestException:
        pass

    try:
        # Paso 2: informe anual. Ya no se usa para sacar links de mes (ver
        # docstring), pero sirve para detectar sesión vencida ANTES de hacer
        # hasta 12 x paginación de consultas mensuales de más.
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

    boletas: list[dict] = []
    errores: list[str] = []
    alguna_pagina_ok = False
    for mes in _meses_a_revisar(anio, desde):
        pagina = 0
        while pagina < 20:  # tope de seguridad ante una paginación anómala
            params = {
                "cbanoinformemensual": anio,
                "cbmesinformemensual": f"{mes:02d}",
                "dv_arrastre": dv,
                "pagina_solicitada": pagina,
                "rut_arrastre": numero,
            }
            try:
                r = session.get(INFORME_MENSUAL_URL, params=params, timeout=30)
            except requests.RequestException as exc:
                errores.append(f"mes {mes:02d} pág {pagina}: {exc}")
                break
            html_mes = r.content.decode("iso-8859-1", "replace")
            if _es_pagina_de_login(html_mes):
                raise BHEError(
                    "La sesión con el SII (cuenta empresa) se perdió al consultar boletas de honorarios."
                )
            alguna_pagina_ok = True
            filas = parse_mes(html_mes, r.url, rut_empresa)
            if not filas:
                break  # página sin boletas: no hay más para este mes
            boletas.extend(filas)
            pagina += 1

    if not alguna_pagina_ok and errores:
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
    out.append("=== LINKS <a href> (href | texto) ===")
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

    # Muchas páginas legadas del SII arman la navegación con JavaScript
    # (onclick + document.write) en vez de <a href> planos. Se busca en TODO
    # el texto crudo, no solo en el HTML parseado.
    out.append("")
    out.append("=== Menciones de .cgi en el HTML crudo (fuera o dentro de tags) ===")
    cgis = sorted(set(re.findall(r"[\w/\.\-]+\.cgi[^\s\"'<>)]*", html)))
    if not cgis:
        out.append("(ninguna)")
    for c in cgis[:100]:
        out.append(c)

    out.append("")
    out.append("=== Atributos onclick ===")
    onclicks = sorted(set(re.findall(r'onclick\s*=\s*"([^"]*)"', html) + re.findall(r"onclick\s*=\s*'([^']*)'", html)))
    if not onclicks:
        out.append("(ninguno)")
    for oc in onclicks[:100]:
        out.append(oc)

    tablas = soup.find_all("table")
    out.append("")
    out.append(f"=== TABLAS ({len(tablas)}) ===")
    for i, t in enumerate(tablas):
        filas = t.find_all("tr")
        out.append(f"--- tabla {i}: {len(filas)} filas ---")
        for tr in filas[:6]:
            celdas = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            out.append(" | ".join(celdas))

    # Scripts locales (mismo dominio) cuyo nombre sugiere que arman el
    # link/click de cada mes (p. ej. "links" en el nombre). Se priorizan esos
    # por sobre utilitarios genéricos (validación, comunas, etc.) para no
    # inflar la respuesta con contenido poco probable de ser relevante.
    out.append("")
    out.append("=== <script src=...> relevantes (mismo dominio, contenido) ===")
    vistos_js: set[str] = set()
    candidatos = [
        s["src"] for s in soup.find_all("script", src=True)
        if "link" in s["src"].lower() or "detall" in s["src"].lower() or "mes" in s["src"].lower()
    ]
    for src in candidatos:
        if src in vistos_js:
            continue
        vistos_js.add(src)
        abs_src = urljoin(resp.url, src)
        try:
            r_js = session.get(abs_src, timeout=20)
            js_txt = r_js.content.decode("iso-8859-1", "replace")
        except requests.RequestException as exc:
            out.append(f"--- {abs_src}: error de red ({exc}) ---")
            continue
        out.append(f"--- {abs_src} ({len(js_txt)} caracteres) ---")
        out.append(js_txt[:8000])
        out.append("")
    if not candidatos:
        out.append("(ningún <script src> con nombre sugerente; ver lista completa de scripts abajo)")
        out.append(", ".join(s["src"] for s in soup.find_all("script", src=True)))

    out.append("")
    out.append(f"=== HTML crudo (primeros 20000 de {len(html)} caracteres) ===")
    out.append(html[:20000])
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
