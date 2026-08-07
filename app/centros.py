"""
Catálogo de centros de costo e ingreso.

Estructura en dos niveles:
  · Nivel 1 — Línea de negocio: MUE (mu-EVT, transporte de personas) y EAU
    (E-Auto: importación y venta de autos —incluye Las Gecko, que dejó de
    ser su propia línea el 2026-08-07— y también los costos de
    administración/corporativos, que antes tenían su propia línea ADM).
  · Nivel 2 — Categoría: distinta según el flujo. Los GASTOS usan las
    categorías de costo (IMP, DES, MKT, ...) y los INGRESOS su propio
    catálogo corto (VEH, CON, SRV, ...).

El código imputable es "LINEA-CATEGORIA", p. ej. EAU-MNT o MUE-SRV. Se guarda
como texto en la columna `centro_costo` de facturas, rendicion_items y
movimientos_cc (vacío/NULL = sin imputar). El catálogo vive solo acá: agregar
o quitar una línea/categoría es editar estas listas.

AGREGAR una categoría (como PST en gastos, 2026-08-07) no necesita migración:
nada quedó guardado antes con ese código. QUITAR o RENOMBRAR sí — los datos
ya imputados con el código viejo (p. ej. "AUT-...", "ADM-..." o "GEK-...") no
se arreglan solos; ver la migración `_renombrar_centros()` en db.py, que corre
una sola vez al arrancar la app.

PST existe en AMBOS catálogos (gasto e ingreso) a propósito: son las dos caras
de la postventa y comparten código para poder netearlas por línea. El flujo
del documento (compra o venta) es el que desambigua: cada select ofrece solo
las categorías de su flujo. Ojo con `etiqueta()`, que recibe el código suelto
sin saber el flujo: para "EAU-PST" devuelve el nombre del catálogo de
INGRESO ("Postventa"), porque _NOMBRES se arma con los ingresos al final.
"""
from __future__ import annotations

LINEAS: list[tuple[str, str]] = [
    ("MUE", "mu-EVT"),
    ("EAU", "E-Auto"),
]

CATEGORIAS_GASTO: list[tuple[str, str]] = [
    ("IMP", "Importación"),
    ("DES", "Desarrollo"),
    ("MKT", "Marketing y ventas"),
    ("OPE", "Operación"),
    ("MNT", "Mantención"),
    ("REM", "Remuneraciones"),
    ("TEC", "Tecnología"),
    ("FIN", "Administración y finanzas"),
    # Costo de atender a un cliente DESPUÉS de la venta: garantías asumidas
    # por E-Auto (p. ej. la reparación de un Gecko ya vendido, facturada por
    # un servicio técnico externo), repuestos de garantía, retrabajos.
    # Distinto de MNT (Mantención), que es mantener vehículos PROPIOS.
    # Agregada 2026-08-07 como contraparte del ingreso PST, para poder netear
    # postventa cobrada contra postventa asumida en cada línea.
    ("PST", "Postventa y garantías"),
]

CATEGORIAS_INGRESO: list[tuple[str, str]] = [
    ("VEH", "Venta de vehículos"),
    ("CON", "Comisiones por consignación"),
    ("SRV", "Servicios de transporte"),
    ("ARR", "Arriendo"),
    ("PST", "Postventa"),
    ("OTR", "Otros ingresos"),
]


def _categorias(flujo: str) -> list[tuple[str, str]]:
    """Catálogo de categorías según el flujo: 'ingreso' o 'gasto'."""
    return CATEGORIAS_INGRESO if flujo == "ingreso" else CATEGORIAS_GASTO


def grupos(flujo: str) -> list[dict]:
    """Opciones agrupadas por línea, listas para armar <optgroup> en un select.

    Cada grupo: {"codigo", "nombre", "opciones": [(codigo_completo, etiqueta)]}.
    """
    res = []
    for cod_l, nom_l in LINEAS:
        res.append({
            "codigo": cod_l,
            "nombre": nom_l,
            "opciones": [(f"{cod_l}-{cod_c}", nom_c) for cod_c, nom_c in _categorias(flujo)],
        })
    return res


_NOMBRES = dict(LINEAS + CATEGORIAS_GASTO + CATEGORIAS_INGRESO)
CODIGOS_GASTO = frozenset(
    f"{l}-{c}" for l, _ in LINEAS for c, _ in CATEGORIAS_GASTO
)
CODIGOS_INGRESO = frozenset(
    f"{l}-{c}" for l, _ in LINEAS for c, _ in CATEGORIAS_INGRESO
)


def es_valido(codigo: str, flujo: str) -> bool:
    """True si `codigo` existe en el catálogo del flujo ('ingreso'/'gasto')."""
    return codigo in (CODIGOS_INGRESO if flujo == "ingreso" else CODIGOS_GASTO)


def etiqueta(codigo: str) -> str:
    """Descripción legible de un código: 'EAU-MNT' -> 'E-Auto · Mantención'.

    Si el código no calza con el catálogo (o viene vacío), se devuelve tal cual.
    """
    if not codigo or "-" not in codigo:
        return codigo or ""
    linea, _, cat = codigo.partition("-")
    if linea in _NOMBRES and cat in _NOMBRES:
        return f"{_NOMBRES[linea]} · {_NOMBRES[cat]}"
    return codigo
