"""
Boletas de Honorarios Electrónicas (BHE) recibidas — módulo aparte porque usa
un sistema del SII completamente distinto al de facturas (DTE):

  Servicios online -> Boletas de honorarios electrónicas -> Emisor de Boletas
  de Honorarios -> Consultar boletas recibidas -> elegir año -> elegir mes.

Es un sistema legado (cgi_IMT en loa.sii.cl), sin API ni HTML documentado, y
requiere iniciar sesión con el RUT de la EMPRESA (no con el RUT personal que
se usa para las facturas). Por eso vive en su propia sesión SII (ver
SII_SESSIONS_BHE en main.py) y su propio módulo.

IMPORTANTE — estado de este módulo (30-jul-2026), confirmado contra el SII
real vía /debug/bhe/inspeccionar en main.py:

- El informe anual y el mensual arman TODO con JavaScript puro
  (document.write); no hay <a href> ni <table> en el HTML crudo.
- La URL de cada mes se arma directamente (no se scrapea un link):

    TMBCOC_InformeMensualBheRec.cgi?cbanoinformemensual=<año>
      &cbmesinformemensual=<MM, 2 dígitos>&dv_arrastre=<dv>
      &pagina_solicitada=<0,1,2,...>&rut_arrastre=<rut sin dv>

- Esa página mensual trae los datos de cada boleta como asignaciones
  directas a un array JS, una por campo e índice de fila (1..CantidadFilas):

    arr_informe_mensual['nroboleta_1']          = "23";
    arr_informe_mensual['rutemisor_1']          = "10971552";
    arr_informe_mensual['dvemisor_1']           = "2";
    arr_informe_mensual['nombre_emisor_1']      = "JAIME ARTURO MONSALVE VERA ";
    arr_informe_mensual['fecha_boleta_1']       = "01/06/2026";
    arr_informe_mensual['totalhonorarios_1']    = formatMiles("764700",'.');
    arr_informe_mensual['honorariosliquidos_1'] = formatMiles("648083",'.');
    arr_informe_mensual['retencion_receptor_1'] = formatMiles("116617",'.');
    arr_informe_mensual['estado_1']             = "N";
    arr_informe_mensual['fechaanulacion_1']     = " ";
    arr_informe_mensual['codigobarras_1']       = "10971552000239966A55";

  `parse_mes` lee esas asignaciones directamente con regex (no busca tablas).

- El PDF NO es un link: el botón "Ver" hace un POST a
  TMBCOT_ConsultaBoletaPdf.cgi con (al menos) txt_codigobarras=<codigobarras>.
  El JS también manda txt_cod_39 (código de barras Code39, calculado con una
  función JS que no se replicó en Python) y txt_descr_comuna (nombre de
  comuna). AÚN NO CONFIRMADO si el SII los exige o si basta con
  txt_codigobarras: `obtener_pdf_bytes` es un intento best-effort, revisar
  si falla siempre (ver tarea pendiente en el repo).
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
PDF_URL = "https://loa.sii.cl/cgi_IMT/TMBCOT_ConsultaBoletaPdf.cgi"

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# arr_informe_mensual['campo_N'] = "valor";  o  = formatMiles("valor", '.');
_ARR_ITEM_RE = re.compile(
    r"arr_informe_mensual\[\s*'(\w+?)_(\d+)'\s*\]\s*=\s*"
    r"(?:formatMiles\(\s*\"([^\"]*)\"|\"([^\"]*)\")"
)


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


def parse_mes(html: str, base_url: str, rut_empresa: str) -> list[dict]:
    """Extrae las boletas de una página de detalle mensual.

    Esta página no trae una tabla HTML: arma todo con JavaScript
    (arr_informe_mensual['campo_N'] = ...; luego un for que hace
    document.write). Se leen directamente esas asignaciones, agrupadas por
    índice N (una boleta por índice, de 1 a CantidadFilas).

    `base_url` y `rut_empresa` no se usan para el parseo en sí (no hace
    falta resolver links relativos ni comparar contra el RUT de la empresa,
    a diferencia del intento anterior); se mantienen en la firma para no
    tener que tocar el resto del módulo.
    """
    campos: dict[int, dict[str, str]] = {}
    for m in _ARR_ITEM_RE.finditer(html):
        campo, idx_str, val_formateado, val_plano = m.groups()
        valor = val_formateado if val_formateado is not None else val_plano
        campos.setdefault(int(idx_str), {})[campo] = valor

    boletas: list[dict] = []
    for idx in sorted(campos):
        c = campos[idx]
        folio_raw = c.get("nroboleta")
        rut_raw = c.get("rutemisor")
        dv_raw = c.get("dvemisor")
        if not (folio_raw and rut_raw and dv_raw):
            continue
        try:
            folio = int(folio_raw)
        except ValueError:
            continue

        # Boleta anulada: no corresponde pagarla, se excluye del listado.
        if (c.get("fechaanulacion") or "").strip():
            continue

        rut_emisor = f"{rut_raw}-{dv_raw.upper()}"
        fecha_iso = _fecha_a_iso(c.get("fecha_boleta") or "")
        # Lo que la empresa efectivamente le paga al emisor es el líquido
        # (bruto menos la retención, que se entera al SII como PPM del
        # emisor, no se le paga a él) — es lo que corresponde trackear como
        # "monto" en Pago a proveedores.
        monto = _solo_digitos(c.get("honorariosliquidos") or "0")
        codigo_barras = (c.get("codigobarras") or "").strip()
        codigo = f"BHE-{codigo_barras}" if codigo_barras else f"BHE-{rut_emisor}-{folio}"

        boletas.append({
            "codigo": codigo,
            "folio": folio,
            "rut_contraparte": rut_emisor,
            "razon_social": (c.get("nombre_emisor") or "").strip() or rut_emisor,
            "fecha": fecha_iso,
            "monto": monto,
            # No es una URL: es el código de barras de la boleta, que es lo
            # que exige el POST a TMBCOT_ConsultaBoletaPdf.cgi para pedir el
            # PDF (ver obtener_pdf_bytes más abajo).
            "pdf_href": codigo_barras or None,
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
    """Descarga el PDF de una boleta.

    `pdf_href` es en realidad el código de barras de la boleta (ver
    parse_mes) — esta página NO tiene un link directo al PDF: el botón "Ver"
    del SII hace un POST a TMBCOT_ConsultaBoletaPdf.cgi con el código de
    barras. El JS del SII también manda txt_cod_39 (el código de barras
    codificado en Code39, calculado ahí mismo con una función JS que no se
    replicó en Python) y txt_descr_comuna (nombre de la comuna); todavía no
    está confirmado si el SII los exige o si basta con txt_codigobarras —
    esto es un intento best-effort, no una implementación verificada.
    """
    if not pdf_href:
        return None
    try:
        resp = session.post(
            PDF_URL,
            data={
                "origen": "RECIBIDOS",
                "veroriginal": "si",
                "nro_boleta": "0",
                "txt_codigobarras": pdf_href,
                "txt_cod_39": "",
                "txt_descr_comuna": "",
            },
            timeout=60,
        )
    except requests.RequestException:
        return None
    if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
        return resp.content
    if _es_pagina_de_login(resp.content.decode("iso-8859-1", "replace")):
        raise BHEError("La sesión con el SII (cuenta empresa) se perdió al pedir el PDF de la boleta.")
    return None
