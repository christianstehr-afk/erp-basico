"""
Boletas de Prestación de Servicios de Terceros Electrónica (BTE) emitidas por
E-Auto a terceros — módulo aparte porque usa otro sistema del SII, distinto
tanto de facturas (DTE, ver sii_docs.py) como de boletas de honorarios (BHE,
ver sii_bhe.py):

  Servicios online -> Boletas electrónicas -> Consultas BTEs emitidas
  (https://www.sii.cl/servicios_online/1040-1309.html)

Usa la MISMA sesión "empresa" que las boletas de honorarios: Christian
confirmó que las BTE también se consultan con el RUT y clave de la empresa,
no con el RUT personal de las facturas (pedido 2026-08-13). Por eso las
funciones de este módulo reciben el `session` de client_bhe en vez de crear
una sesión propia.

ESTADO DE ESTE MÓDULO (2026-08-13): a diferencia del primer intento (que se
armó solo con información pública, sin poder loguearse), esta versión SÍ está
confirmada contra el SII real — Christian navegó el flujo a mano en su
navegador (cuenta empresa, E-AUTO SPA) y pasó el HTML real de cada paso:

  1. Informe anual: POST a /cvc_cgi/bte/bte_indiv_cons2 con
     TIPO=anual, CNTR=1, AUTEN=RUTCLAVE, PAGINA=1, ANOA=<año>. Trae un
     resumen por mes (folios, boletas vigentes/anuladas, brutos, retenciones,
     total líquido). Los meses con boletas traen un link al detalle mensual;
     los meses en cero no. Se usa acá solo como chequeo de sesión antes de
     hacer hasta 12 consultas mensuales de más (mismo criterio que el
     informe anual de sii_bhe.py) — el detalle real se saca directo del
     paso 2, sin depender de parsear los links de esta página.
  2. Detalle mensual: GET a /cvc_cgi/bte/bte_indiv_cons2 con
     DIA=1, MESM=<01..12>, ANOM=<año>, TIPO=mensual, AUTEN=RUTCLAVE, CNTR=1,
     PAGINA=<página>. Trae una tabla con una fila por boleta: folio, estado
     (VIG/otro), fecha, RUT+nombre del emisor (E-Auto, no se usa), RUT+nombre
     del receptor (el tercero al que se le paga), y 3 montos: Brutos,
     Retenidos, Pagado. `parse_mes` lee esa tabla por POSICIÓN de columna
     (confirmada real, ver ejemplo abajo), anclándose en la fila que tenga el
     link "Ver boleta" (bte_indiv_cons3) para no depender de que el layout
     de encabezados no cambie.
  3. Ver boleta individual: el botón "Ver" de cada fila es un link (no
     POST) a /cvc_cgi/bte/bte_indiv_cons3?<código>, donde <código> es un
     identificador tipo código de barras (ej. "C777082150000142E1209"), NO
     un par clave=valor. El SII lo etiqueta "Ver boleta en formato html"
     (ícono html.gif) y esto quedó CONFIRMADO (2026-08-14): la respuesta es
     HTML, no un PDF descargable. `obtener_pdf_bytes` intenta igual por si el
     SII cambia de formato más adelante (si la respuesta no arranca con
     "%PDF-" devuelve None); el respaldo real es `obtener_html_boleta`, que
     trae ese mismo HTML (con un <base href> agregado) para mostrarlo
     embebido en la app — así "Ver PDF de la factura" en Movimientos CC /
     Pago a Proveedores igual muestra el documento, aunque no sea un PDF
     literal. `main.py` prueba primero el PDF y cae a este HTML si falla.

  Ejemplo real (E-Auto, agosto 2026, la primera BTE que emitió la empresa):
    folio=1, estado=VIG, fecha=11-08-2026, receptor=11802178-9 "JOSE
    HUMBERTO TROMILEN HUENULEF", Brutos=117.994, Retenidos=17.994,
    Pagado=100.000.

Semántica de montos (confirmada por Christian, 2026-08-13, y por la columna
real "Pagado" del SII): E-Auto emite la BTE a un tercero y le paga solo el
monto de la columna "Pagado" (bruto menos retención — mismo concepto que
honorariosliquidos en BHE). El "Retenido" NO se le paga a él, se declara al
SII aparte. Por eso `monto_pagado` en el dict devuelto por `parse_mes` es lo
que se guarda como monto a pagar/trackear (facturas.total, ver
db.upsert_bte); `honorario_bruto` e `impuesto_retenido` se devuelven solo
como referencia y no se persisten (mismo criterio que BHE: solo se guarda lo
que de verdad hay que pagar).
"""
from __future__ import annotations

import re
import unicodedata

import requests
from bs4 import BeautifulSoup

from .sii_client import normalizar_rut

CONSULTA_URL = "https://zeus.sii.cl/cvc_cgi/bte/bte_indiv_cons2"
VER_URL = "https://zeus.sii.cl/cvc_cgi/bte/bte_indiv_cons3"

# Link de "Ver boleta" de una fila: /cvc_cgi/bte/bte_indiv_cons3?<código>
# (el código no es un par clave=valor, va pegado tal cual tras el "?").
_VER_HREF_RE = re.compile(r"bte_indiv_cons3\?(.+)$")


class BTEError(Exception):
    """Error al consultar BTE emitidas (login, red, o parseo). Nunca debe
    tumbar el sync general de facturas/boletas: quien la capture (sync.py)
    debe tratarla como no bloqueante, igual que BHEError en sii_bhe.py."""


def _norm(texto: str) -> str:
    s = unicodedata.normalize("NFKD", texto or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


def _es_pagina_de_login(html: str) -> bool:
    texto = _norm(html)
    marcadores = (
        "no se encuentra autenticado",
        "expiro el tiempo de conexion",
        "clave tributaria",
        "debe iniciar sesion",
    )
    return any(m in texto for m in marcadores)


def _solo_digitos(texto: str) -> int:
    n = re.sub(r"[^0-9]", "", texto or "")
    return int(n) if n else 0


def _fecha_a_iso(fecha_raw: str) -> str | None:
    """Convierte 'DD-MM-YYYY' (formato real de esta página) a 'YYYY-MM-DD'.
    También acepta 'DD/MM/YYYY' o ISO por si acaso."""
    if not fecha_raw:
        return None
    fecha_raw = fecha_raw.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", fecha_raw):
        return fecha_raw
    m = re.match(r"^(\d{2})[/-](\d{2})[/-](\d{4})$", fecha_raw)
    if m:
        dia, mes, anio = m.groups()
        return f"{anio}-{mes}-{dia}"
    return None


def parse_mes(html: str) -> list[dict]:
    """Extrae las BTE de una página de detalle mensual (ver docstring del
    módulo, paso 2).

    Ancla en la fila que trae el link "Ver boleta" (bte_indiv_cons3) para no
    depender de la fila de encabezados ni de la de totales. Columnas
    confirmadas contra el SII real, en orden:
      0 Ver (link, se extrae el código) · 1 N° (folio) · 2 Estado
      3 Fecha (de la boleta) · 4 Rut emisor · 5 Nombre emisor (no se usan:
      siempre son los de la empresa) · 6 Fecha (emisión, no se usa)
      7 Rut receptor · 8 Nombre receptor · 9 Honorarios Brutos
      10 Honorarios Retenidos · 11 Honorarios Pagado

    Excluye las boletas no vigentes (Estado != "VIG": anuladas u otro
    estado) — no corresponde pagarlas, mismo criterio que una boleta de
    honorarios anulada en sii_bhe.py.
    """
    soup = BeautifulSoup(html, "html.parser")
    boletas: list[dict] = []
    for tr in soup.find_all("tr"):
        enlace = tr.find("a", href=_VER_HREF_RE)
        if not enlace:
            continue  # encabezado, fila de totales, u otra fila sin boleta
        celdas = tr.find_all("td")
        if len(celdas) < 12:
            continue
        textos = [c.get_text(" ", strip=True) for c in celdas]

        estado = textos[2].strip().upper()
        if estado != "VIG":
            continue  # anulada u otro estado: no se paga

        folio_raw = textos[1]
        folio = _solo_digitos(folio_raw)
        if not folio:
            continue

        m = _VER_HREF_RE.search(enlace.get("href", ""))
        ver_href = m.group(1) if m else None

        rut_contraparte = textos[7].strip()
        boletas.append({
            "folio": folio,
            "estado": estado,
            "fecha": _fecha_a_iso(textos[3]),
            "rut_contraparte": rut_contraparte,
            "razon_social": textos[8].strip() or rut_contraparte,
            "honorario_bruto": _solo_digitos(textos[9]),
            "impuesto_retenido": _solo_digitos(textos[10]),
            "monto_pagado": _solo_digitos(textos[11]),
            "ver_href": ver_href,
        })
    return boletas


def obtener_bte_emitidas(
    session: requests.Session, rut_empresa: str, anio: int, desde: str | None = None
) -> list[dict]:
    """Devuelve las BTE emitidas por la empresa en `anio`.

    Usa la sesión "empresa" ya autenticada (la misma de boletas de
    honorarios, ver sii_bhe.py) — no hace login propio. Sigue el flujo:
    informe anual (valida la sesión) -> cada mes desde `desde` (o todo el
    año) en adelante, con paginación -> parseo (ver docstring del módulo).
    Lanza BTEError si el SII no responde como se espera (sesión vencida).
    """
    try:
        numero, dv = normalizar_rut(rut_empresa)
    except Exception as exc:
        raise BTEError(f"RUT de empresa inválido: {exc}") from exc

    try:
        resp = session.post(
            CONSULTA_URL,
            data={"TIPO": "anual", "CNTR": "1", "AUTEN": "RUTCLAVE", "PAGINA": "1", "ANOA": anio},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise BTEError(f"No se pudo conectar al SII (informe anual BTE): {exc}") from exc

    html = resp.content.decode("iso-8859-1", "replace")
    if _es_pagina_de_login(html):
        raise BTEError(
            "La sesión con el SII (cuenta empresa) se perdió o no se pudo autenticar "
            "para consultar BTE emitidas."
        )

    boletas: list[dict] = []
    for mes in _meses_a_revisar(anio, desde):
        pagina = 1
        while pagina < 20:  # tope de seguridad ante una paginación anómala
            params = {
                "DIA": "1", "MESM": f"{mes:02d}", "ANOM": anio,
                "TIPO": "mensual", "AUTEN": "RUTCLAVE", "CNTR": "1", "PAGINA": pagina,
            }
            try:
                r = session.get(CONSULTA_URL, params=params, timeout=30)
            except requests.RequestException as exc:
                raise BTEError(f"No se pudo conectar al SII (BTE mes {mes:02d}): {exc}") from exc
            html_mes = r.content.decode("iso-8859-1", "replace")
            if _es_pagina_de_login(html_mes):
                raise BTEError(
                    "La sesión con el SII (cuenta empresa) se perdió al consultar BTE emitidas."
                )
            filas = parse_mes(html_mes)
            if not filas:
                break  # página sin boletas: no hay más para este mes
            boletas.extend(filas)
            pagina += 1

    if desde:
        boletas = [b for b in boletas if (b.get("fecha") or "") >= desde]
    return boletas


def _meses_a_revisar(anio: int, desde: str | None) -> range:
    """Meses (1-12) del año a consultar, recortado por `desde` (YYYY-MM-DD)
    cuando corresponde, para no pedir de más al SII. Misma lógica que
    sii_bhe._meses_a_revisar."""
    if not desde:
        return range(1, 13)
    try:
        anio_desde, mes_desde = int(desde[:4]), int(desde[5:7])
    except (ValueError, IndexError):
        return range(1, 13)
    if anio_desde > anio:
        return range(0)
    if anio_desde == anio:
        return range(mes_desde, 13)
    return range(1, 13)


def obtener_pdf_bytes(session: requests.Session, ver_href: str | None) -> bytes | None:
    """Descarga el "Ver boleta" de una BTE.

    CONFIRMADO (2026-08-14, Christian probó "Ver PDF" desde la app): el SII
    devuelve HTML para este link, no un PDF descargable — la etiqueta "Ver
    boleta en formato html" resultó correcta. Esta función igual intenta
    primero por si el SII cambia de formato más adelante; si la respuesta no
    arranca con "%PDF-" devuelve None (nunca invalida la sesión guardada). El
    llamador debe caer a `obtener_html_boleta` como respaldo — ver main.py."""
    if not ver_href:
        return None
    try:
        resp = session.get(f"{VER_URL}?{ver_href}", timeout=60)
    except requests.RequestException:
        return None
    if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
        return resp.content
    return None


def _con_base_href(html: str) -> str:
    """Inserta <base href="https://zeus.sii.cl/"> justo después de <head>,
    para que las rutas relativas del HTML del SII (imágenes, css, p.ej.
    "/cvc/comun/html.gif") sigan resolviendo bien cuando este HTML se sirve
    embebido en nuestra propia app, fuera del dominio del SII."""
    base_tag = '<base href="https://zeus.sii.cl/">'
    if re.search(r"<head[^>]*>", html, re.IGNORECASE):
        return re.sub(r"(<head[^>]*>)", r"\1" + base_tag, html, count=1, flags=re.IGNORECASE)
    return base_tag + html


def obtener_html_boleta(session: requests.Session, ver_href: str | None) -> str | None:
    """Respaldo de `obtener_pdf_bytes`: descarga el HTML del "Ver boleta" tal
    cual lo muestra el SII (confirmado que es HTML, no PDF — ver arriba), con
    un <base href> agregado para que cargue bien embebido en un iframe/pestaña
    de la app. Se usa para poder mostrar igual el documento aunque no haya un
    PDF real que ofrecer.

    Devuelve None si no hay `ver_href`, si falla la conexión, si la página
    parece de login (sesión vencida), o si viene vacía — el llamador debe
    tratar eso igual que un PDF no disponible (nunca invalida la sesión
    guardada: una BTE puntual fallando no dice nada sobre el resto)."""
    if not ver_href:
        return None
    try:
        resp = session.get(f"{VER_URL}?{ver_href}", timeout=60)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or resp.content[:5] == b"%PDF-":
        return None
    html = resp.content.decode("iso-8859-1", "replace")
    if not html.strip() or _es_pagina_de_login(html):
        return None
    return _con_base_href(html)


def diagnostico(session: requests.Session, url: str, params: dict) -> str:
    """Vuelca links, tablas y formularios de una página del SII (BTE) con la
    sesión ya autenticada, para poder seguir ajustando el parser sin acceso
    directo al SII. Ver /debug/bte/inspeccionar en main.py. Nunca lanza:
    cualquier error de red queda como texto en el resultado.

    (Implementación equivalente a sii_bhe.diagnostico — se duplica en vez de
    compartir código porque hablan con sistemas del SII distintos que
    pueden divergir con el tiempo.)
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
    out.append("=== TABLAS ===")
    tablas = soup.find_all("table")
    if not tablas:
        out.append("(ninguna)")
    for i, t in enumerate(tablas):
        filas = t.find_all("tr")
        out.append(f"--- tabla {i}: {len(filas)} filas ---")
        for tr in filas[:6]:
            celdas = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            out.append(" | ".join(celdas))

    out.append("")
    out.append(f"=== HTML crudo (primeros 20000 de {len(html)} caracteres) ===")
    out.append(html[:20000])
    return "\n".join(out)
