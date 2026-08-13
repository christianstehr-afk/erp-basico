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

IMPORTANTE — estado de este módulo (creado 2026-08-13): a diferencia de
sii_bhe.py (que se terminó de ajustar mirando el HTML real del SII vía
/debug/bhe/inspeccionar en producción), esta primera versión se armó solo
con información pública (páginas de ayuda del SII y resultados de búsqueda),
SIN poder loguearse de verdad. Lo que sigue es la mejor estimación posible,
no un hecho confirmado:

  - Dominio: zeus.sii.cl/cvc_cgi/bte/ (distinto de zeusr.sii.cl del login y
    de loa.sii.cl que usa BHE).
  - Página de resultados: bte_indiv_cons4 ("Informe Boletas de Prestación de
    Servicios de Terceros") — la URL en sí SÍ quedó confirmada (responde
    200), pero solo se pudo ver sin sesión: mostró "Usted no se encuentra
    autenticado, o posiblemente expiró el tiempo de conexión". Esa frase
    exacta SÍ quedó confirmada real y es la que usa `_es_pagina_de_login`.
  - Parámetros de consulta (rut_arrastre/dv_arrastre/anio): sin confirmar.
    Se intenta con el mismo estilo que usa BHE en loa.sii.cl, por ser el
    patrón más común en los sistemas legados del SII — pero es un intento.
  - El parseo de la tabla de resultados (`parse_lista`) busca los
    encabezados por NOMBRE ("Folio", "Valor Total", "Impuesto Retenido",
    "RUT", "Fecha", ver _ENCABEZADOS) en vez de por posición fija de
    columna, para tolerar mejor si el orden real no coincide con lo
    esperado. Si la página no trae ninguna tabla reconocible (p. ej. porque
    en realidad hace falta un paso previo con un formulario de filtro, o el
    layout es otro), `obtener_bte_emitidas` lanza BTEError con un mensaje
    claro en vez de devolver una lista vacía silenciosa (que se confundiría
    con "no hay BTE este año").
  - `obtener_pdf_bytes`: el endpoint del PDF no se pudo determinar sin una
    sesión real. Por ahora devuelve None siempre (ver su docstring); las BTE
    quedan sincronizadas en la BD igual, solo sin PDF hasta confirmar esto.

  PRÓXIMO PASO para terminar de confirmar todo esto: una vez desplegado,
  entrar al Cockpit con sesión empresa activa y visitar
  /debug/bte/inspeccionar (mismo mecanismo que /debug/bhe/inspeccionar, ver
  main.py) — pasarle el volcado a Claude para ajustar este módulo a la
  estructura real (parse_lista, y el endpoint del PDF).

Semántica de montos (confirmada por Christian, 2026-08-13): E-Auto emite la
BTE a un tercero y le paga solo el "Valor total" del documento — el
"Impuesto retenido" NO se le paga a él, se declara al SII aparte (mismo
criterio que la retención de las BHE, ver sii_bhe.py). Por eso `valor_total`
en el dict devuelto es lo que se guarda como monto a pagar/trackear
(facturas.total, ver db.upsert_bte); `impuesto_retenido` se devuelve solo
como referencia y no se persiste.
"""
from __future__ import annotations

import re
import unicodedata

import requests
from bs4 import BeautifulSoup

from .sii_client import normalizar_rut

MENU_URL = "https://zeus.sii.cl/cvc/bte/menu.html"
CONSULTA_URL = "https://zeus.sii.cl/cvc_cgi/bte/bte_indiv_cons4"


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
    """Convierte 'DD/MM/YYYY' o 'DD-MM-YYYY' a 'YYYY-MM-DD'. Si ya viene en
    ISO, la deja igual."""
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


# Encabezados esperados (normalizados, sin tilde/mayúsculas) -> nombre de
# campo interno. Varias variantes por columna porque el texto exacto que usa
# el SII en esta página no está confirmado (ver docstring del módulo).
_ENCABEZADOS = {
    "folio": "folio",
    "nro boleta": "folio",
    "n boleta": "folio",
    "n de boleta": "folio",
    "rut receptor": "rut_contraparte",
    "rut": "rut_contraparte",
    "receptor": "razon_social",
    "nombre receptor": "razon_social",
    "razon social": "razon_social",
    "fecha": "fecha",
    "fecha emision": "fecha",
    "fecha boleta": "fecha",
    "valor total": "valor_total",
    "monto total": "valor_total",
    "impuesto retenido": "impuesto_retenido",
    "retencion": "impuesto_retenido",
    "estado": "estado",
}


def parse_lista(html: str) -> list[dict]:
    """Extrae las BTE emitidas de la tabla de resultados.

    Busca los encabezados por nombre (ver _ENCABEZADOS), no por posición
    fija, porque la estructura real de esta página no está confirmada (ver
    docstring del módulo). Devuelve [] si no encuentra ninguna tabla con
    encabezados reconocibles — es responsabilidad de quien llama (ver
    obtener_bte_emitidas) decidir si eso significa "no hay datos" o "no se
    pudo interpretar la página".
    """
    soup = BeautifulSoup(html, "html.parser")
    for tabla in soup.find_all("table"):
        filas = tabla.find_all("tr")
        if not filas:
            continue
        encabezado = [_norm(c.get_text(" ", strip=True)) for c in filas[0].find_all(["th", "td"])]
        mapa: dict[int, str] = {}
        for i, texto in enumerate(encabezado):
            for clave, campo in _ENCABEZADOS.items():
                if clave in texto:
                    mapa[i] = campo
                    break
        if "folio" not in mapa.values() or "valor_total" not in mapa.values():
            continue  # esta tabla no es la que buscamos

        boletas: list[dict] = []
        for tr in filas[1:]:
            celdas = tr.find_all("td")
            if not celdas:
                continue
            datos: dict[str, str] = {}
            for i, celda in enumerate(celdas):
                campo = mapa.get(i)
                if campo:
                    datos[campo] = celda.get_text(" ", strip=True)
            folio_raw = datos.get("folio")
            if not folio_raw:
                continue
            folio = _solo_digitos(folio_raw)
            if not folio:
                continue
            rut_contraparte = (datos.get("rut_contraparte") or "").strip()
            boletas.append({
                "folio": folio,
                "rut_contraparte": rut_contraparte,
                "razon_social": (datos.get("razon_social") or "").strip() or rut_contraparte,
                "fecha": _fecha_a_iso(datos.get("fecha") or ""),
                "valor_total": _solo_digitos(datos.get("valor_total") or "0"),
                "impuesto_retenido": _solo_digitos(datos.get("impuesto_retenido") or "0"),
                "estado": (datos.get("estado") or "").strip() or "Vigente",
            })
        return boletas
    return []


def obtener_bte_emitidas(
    session: requests.Session, rut_empresa: str, anio: int, desde: str | None = None
) -> list[dict]:
    """Devuelve las BTE emitidas por la empresa en `anio`.

    Usa la sesión "empresa" ya autenticada (la misma de boletas de
    honorarios, ver sii_bhe.py) — no hace login propio. Lanza BTEError si el
    SII no responde como se espera: sesión vencida, o página con una
    estructura que este módulo todavía no sabe interpretar (ver docstring
    del módulo sobre su estado sin confirmar contra el SII real).
    """
    try:
        numero, dv = normalizar_rut(rut_empresa)
    except Exception as exc:
        raise BTEError(f"RUT de empresa inválido: {exc}") from exc

    try:
        session.get(MENU_URL, timeout=30)
    except requests.RequestException:
        pass  # solo warm-up de navegación; nunca bloquea

    try:
        resp = session.get(
            CONSULTA_URL,
            params={"rut_arrastre": numero, "dv_arrastre": dv, "anio": anio},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise BTEError(f"No se pudo conectar al SII (BTE emitidas): {exc}") from exc

    html = resp.content.decode("iso-8859-1", "replace")
    if _es_pagina_de_login(html):
        raise BTEError(
            "La sesión con el SII (cuenta empresa) se perdió o no se pudo autenticar "
            "para consultar BTE emitidas."
        )

    boletas = parse_lista(html)
    if not boletas:
        # Ninguna fila reconocida: más probable que la estructura real de la
        # página sea distinta de lo esperado (ver _ENCABEZADOS) que que de
        # verdad no haya ninguna BTE ese año — mejor avisar fuerte que
        # quedar silenciosamente en cero para siempre.
        raise BTEError(
            "No se pudo interpretar la página de BTE emitidas del SII (0 filas "
            "reconocidas; puede ser que de verdad no haya BTE este año, o que la "
            "estructura de la página no coincida con lo esperado). Revisar con "
            "/debug/bte/inspeccionar y ajustar sii_bte.py si corresponde."
        )

    if desde:
        boletas = [b for b in boletas if (b.get("fecha") or "") >= desde]
    return boletas


def obtener_pdf_bytes(session: requests.Session, folio: int | None, rut_contraparte: str | None) -> bytes | None:
    """Descarga el PDF de una BTE.

    AÚN NO IMPLEMENTADO: no se pudo determinar el endpoint del PDF de una
    BTE sin una sesión real contra el SII (ver docstring del módulo).
    Devuelve None a propósito — igual que sii_docs/sii_bhe cuando fallan,
    esto NO invalida ninguna sesión guardada; la BTE queda sincronizada en
    la BD (folio, monto, fecha, etc.) pero sin PDF hasta que se confirme el
    endpoint real y se complete esta función.
    """
    return None


def diagnostico(session: requests.Session, url: str, params: dict) -> str:
    """Vuelca links, tablas y formularios de una página del SII (BTE) con la
    sesión ya autenticada, para poder ajustar el parser sin acceso directo
    al SII. Ver /debug/bte/inspeccionar en main.py. Nunca lanza: cualquier
    error de red queda como texto en el resultado.

    (Implementación equivalente a sii_bhe.diagnostico — mismo propósito y
    formato de salida — se duplica en vez de compartir código porque en el
    fondo hablan con sistemas del SII distintos que pueden divergir con el
    tiempo.)
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
