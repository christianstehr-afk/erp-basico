"""
Base de datos SQLite del ERP Básico.

En este primer paso solo se crea el esquema. Las tablas quedan listas para los
módulos siguientes: facturas (RCV) y pagos de E-Auto.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Ruta de la base de datos. Configurable con la variable de entorno DB_PATH;
# por defecto se guarda en data/erp.db dentro del proyecto.
DB_PATH = Path(
    os.environ.get("DB_PATH", Path(__file__).resolve().parent.parent / "data" / "erp.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS facturas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo          TEXT NOT NULL,              -- 'venta' (emitida) | 'compra' (recibida)
    codigo_sii    TEXT,                        -- CODIGO del documento en el SII (clave única)
    tipo_dte      INTEGER,                    -- código tipo de documento SII (33, 34, 61, ...)
    documento     TEXT,                        -- texto del tipo de documento
    folio         INTEGER,
    rut_contraparte TEXT,
    razon_social  TEXT,
    fecha_emision TEXT,                        -- YYYY-MM-DD
    neto          INTEGER DEFAULT 0,
    iva           INTEGER DEFAULT 0,
    total         INTEGER DEFAULT 0,
    estado        TEXT,                        -- estado SII del documento
    pdf_path      TEXT,                        -- ruta local del PDF descargado
    fecha_reclamo TEXT,                        -- fecha/hora de rechazo (RCV); no-null = rechazada
    fecha_acuse   TEXT,                        -- fecha/hora de acuse de recibo (RCV)
    fecha_pago_tope TEXT,                      -- fecha tope de pago (default = fecha_emision); editable
    creado_en     TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tipo, tipo_dte, folio, rut_contraparte)
);

CREATE TABLE IF NOT EXISTS pagos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    direccion     TEXT NOT NULL,              -- 'emitido' (E-Auto paga) | 'recibido' (E-Auto cobra)
    factura_id    INTEGER,                     -- FK opcional a facturas.id
    rendicion_id  INTEGER,                     -- si el pago se hizo vía una rendición (no suma al export)
    fecha         TEXT,                        -- YYYY-MM-DD
    monto         INTEGER NOT NULL,
    medio         TEXT,                        -- transferencia, cheque, efectivo, ...
    referencia    TEXT,
    nota          TEXT,
    creado_en     TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (factura_id) REFERENCES facturas(id)
);

CREATE INDEX IF NOT EXISTS idx_facturas_fecha ON facturas(fecha_emision);
CREATE INDEX IF NOT EXISTS idx_pagos_fecha ON pagos(fecha);

-- Módulo 4 · Rendiciones (gastos pagados por la empresa)
CREATE TABLE IF NOT EXISTS rendiciones (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre     TEXT NOT NULL,
    fecha      TEXT NOT NULL,               -- fecha de la rendición (YYYY-MM-DD)
    creado_en  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rendicion_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rendicion_id  INTEGER NOT NULL,
    descripcion   TEXT NOT NULL,
    numero_doc    TEXT,                      -- número de boleta o factura
    monto         INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (rendicion_id) REFERENCES rendiciones(id)
);

CREATE TABLE IF NOT EXISTS rendicion_adjuntos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    rendicion_id   INTEGER NOT NULL,
    nombre_archivo TEXT NOT NULL,            -- nombre original
    path           TEXT NOT NULL,            -- ruta local del archivo guardado
    creado_en      TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (rendicion_id) REFERENCES rendiciones(id)
);

CREATE TABLE IF NOT EXISTS rendicion_pagos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rendicion_id  INTEGER NOT NULL,
    fecha         TEXT NOT NULL,
    monto         INTEGER NOT NULL,
    creado_en     TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (rendicion_id) REFERENCES rendiciones(id)
);

CREATE INDEX IF NOT EXISTS idx_rend_items ON rendicion_items(rendicion_id);
CREATE INDEX IF NOT EXISTS idx_rend_adj ON rendicion_adjuntos(rendicion_id);
CREATE INDEX IF NOT EXISTS idx_rend_pagos ON rendicion_pagos(rendicion_id);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrar(conn: sqlite3.Connection) -> None:
    """Añade columnas nuevas si la BD viene de una versión anterior."""
    existentes = {r[1] for r in conn.execute("PRAGMA table_info(facturas)")}
    nuevas = {
        "codigo_sii": "TEXT", "documento": "TEXT", "pdf_path": "TEXT",
        "fecha_reclamo": "TEXT", "fecha_acuse": "TEXT", "fecha_pago_tope": "TEXT",
    }
    for col, ddl in nuevas.items():
        if col not in existentes:
            conn.execute(f"ALTER TABLE facturas ADD COLUMN {col} {ddl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_facturas_codigo ON facturas(codigo_sii)")
    # pagos.rendicion_id: pago de factura hecho vía una rendición
    cols_pagos = {r[1] for r in conn.execute("PRAGMA table_info(pagos)")}
    if "rendicion_id" not in cols_pagos:
        conn.execute("ALTER TABLE pagos ADD COLUMN rendicion_id INTEGER")
    # Rellena la fecha tope (de pago/cobro) que aún no exista con su fecha de emisión.
    # Aplica a recibidas (pago a proveedores) y emitidas (ingresos).
    conn.execute(
        "UPDATE facturas SET fecha_pago_tope = fecha_emision "
        "WHERE fecha_pago_tope IS NULL OR fecha_pago_tope = ''"
    )


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrar(conn)


def upsert_documento(conn: sqlite3.Connection, doc: dict, tipo: str) -> None:
    """Inserta o actualiza un documento identificado por su codigo_sii.

    `doc` proviene de sii_docs.parse_lista. `tipo` es 'compra' (recibida) o
    'venta' (emitida).
    """
    params = {**doc, "tipo": tipo}
    conn.execute(
        """
        INSERT INTO facturas
            (tipo, codigo_sii, tipo_dte, documento, folio,
             rut_contraparte, razon_social, fecha_emision, total, estado,
             fecha_pago_tope)
        VALUES
            (:tipo, :codigo, :tipo_dte, :documento, :folio,
             :rut_contraparte, :razon_social, :fecha, :monto, :estado,
             :fecha)
        ON CONFLICT(codigo_sii) DO UPDATE SET
            estado          = excluded.estado,
            total           = excluded.total,
            rut_contraparte = excluded.rut_contraparte,
            razon_social    = excluded.razon_social,
            documento       = excluded.documento,
            tipo_dte        = excluded.tipo_dte,
            folio           = excluded.folio,
            fecha_emision   = excluded.fecha_emision
        """,
        params,
    )


def marcar_pdf(conn: sqlite3.Connection, codigo: str, pdf_path: str) -> None:
    conn.execute("UPDATE facturas SET pdf_path = ? WHERE codigo_sii = ?", (pdf_path, codigo))


def marcar_estado_venta(conn: sqlite3.Connection, estado: dict) -> None:
    """Marca en una emitida (tipo='venta') su fecha de reclamo/acuse del RCV.

    `estado` viene de sii_rcv.estados_de_venta. El cruce es por tipo_dte + folio.
    """
    conn.execute(
        "UPDATE facturas SET fecha_reclamo = :fecha_reclamo, fecha_acuse = :fecha_acuse "
        "WHERE tipo = 'venta' AND tipo_dte = :tipo_dte AND folio = :folio",
        estado,
    )


def pendientes_de_pdf(conn: sqlite3.Connection, tipo: str = "compra") -> list[str]:
    """Códigos de documentos de un tipo sin PDF descargado aún."""
    cur = conn.execute(
        "SELECT codigo_sii FROM facturas WHERE tipo = ? AND codigo_sii IS NOT NULL "
        "AND (pdf_path IS NULL OR pdf_path = '')",
        (tipo,),
    )
    return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Módulo 4 · Pago a proveedores (facturas recibidas, tipo='compra')
# ---------------------------------------------------------------------------

# Tipos DTE que no se pagan y no deben salir en pago a proveedores:
# 52 = guía de despacho, 61 = nota de crédito (montos cero / no pagables).
TIPOS_NO_PAGABLES = (52, 61)


def facturas_con_pago(conn: sqlite3.Connection, tipo: str = "compra") -> list[sqlite3.Row]:
    """Facturas de un tipo con su total pagado agregado y fecha tope.

    Excluye guías de despacho y notas de crédito (ver TIPOS_NO_PAGABLES).
    Devuelve, por factura: datos base + `pagado` (suma de pagos asociados).
    """
    marcadores = ",".join("?" * len(TIPOS_NO_PAGABLES))
    return conn.execute(
        f"""
        SELECT f.id, f.codigo_sii, f.documento, f.folio, f.rut_contraparte,
               f.razon_social, f.fecha_emision, f.total, f.fecha_pago_tope, f.fecha_reclamo,
               f.pdf_path,
               COALESCE((SELECT SUM(p.monto) FROM pagos p WHERE p.factura_id = f.id), 0) AS pagado
        FROM facturas f
        WHERE f.tipo = ?
          AND (f.tipo_dte IS NULL OR f.tipo_dte NOT IN ({marcadores}))
        ORDER BY f.fecha_pago_tope IS NULL, f.fecha_pago_tope ASC, f.folio DESC
        """,
        (tipo, *TIPOS_NO_PAGABLES),
    ).fetchall()


def factura_pago_por_codigo(conn: sqlite3.Connection, codigo: str) -> sqlite3.Row | None:
    """Una factura por su codigo_sii con el total pagado agregado."""
    return conn.execute(
        """
        SELECT f.id, f.codigo_sii, f.documento, f.folio, f.rut_contraparte,
               f.razon_social, f.fecha_emision, f.total, f.fecha_pago_tope, f.fecha_reclamo, f.pdf_path,
               COALESCE((SELECT SUM(p.monto) FROM pagos p WHERE p.factura_id = f.id), 0) AS pagado
        FROM facturas f
        WHERE f.codigo_sii = ?
        """,
        (codigo,),
    ).fetchone()


def set_fecha_tope(conn: sqlite3.Connection, codigo: str, fecha: str) -> None:
    conn.execute(
        "UPDATE facturas SET fecha_pago_tope = ? WHERE codigo_sii = ?", (fecha, codigo)
    )


def pagos_de_factura(conn: sqlite3.Connection, factura_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.id, p.fecha, p.monto, p.rendicion_id, r.nombre AS rendicion_nombre "
        "FROM pagos p LEFT JOIN rendiciones r ON r.id = p.rendicion_id "
        "WHERE p.factura_id = ? ORDER BY p.fecha ASC, p.id ASC",
        (factura_id,),
    ).fetchall()


def rendicion_asociada_a_factura(conn: sqlite3.Connection, factura_id: int) -> int | None:
    """Devuelve el id de la rendición asociada a la factura (si hay), o None.

    Regla del negocio: una factura se asocia a UNA sola rendición. Se toma la de
    su primer pago con rendicion_id.
    """
    row = conn.execute(
        "SELECT rendicion_id FROM pagos "
        "WHERE factura_id = ? AND rendicion_id IS NOT NULL "
        "ORDER BY id ASC LIMIT 1",
        (factura_id,),
    ).fetchone()
    return row[0] if row else None


def agregar_pago(conn: sqlite3.Connection, factura_id: int, fecha: str, monto: int,
                 direccion: str = "emitido", rendicion_id: int | None = None) -> None:
    """Registra un pago parcial de una factura.

    `direccion='emitido'` = pago que E-Auto realiza (pago a proveedores).
    `rendicion_id` no-null = el pago se hizo vía esa rendición; ese monto NO se
    suma al listado de export (la rendición ya aporta el movimiento de caja).
    """
    conn.execute(
        "INSERT INTO pagos (direccion, factura_id, fecha, monto, rendicion_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (direccion, factura_id, fecha, monto, rendicion_id),
    )


def eliminar_pago(conn: sqlite3.Connection, pago_id: int, factura_id: int) -> None:
    conn.execute(
        "DELETE FROM pagos WHERE id = ? AND factura_id = ?", (pago_id, factura_id)
    )


# ---------------------------------------------------------------------------
# Módulo 4 · Rendiciones
# ---------------------------------------------------------------------------

def crear_rendicion(conn: sqlite3.Connection, nombre: str, fecha: str,
                    items: list[dict]) -> int:
    """Crea una rendición con sus ítems. Cada ítem: descripcion, numero_doc, monto.

    Devuelve el id de la rendición creada.
    """
    cur = conn.execute(
        "INSERT INTO rendiciones (nombre, fecha) VALUES (?, ?)", (nombre, fecha)
    )
    rid = cur.lastrowid
    for it in items:
        conn.execute(
            "INSERT INTO rendicion_items (rendicion_id, descripcion, numero_doc, monto) "
            "VALUES (?, ?, ?, ?)",
            (rid, it["descripcion"], it.get("numero_doc") or None, int(it["monto"])),
        )
    return rid


def listar_rendiciones(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rendiciones con su total (suma de ítems), pagado y número de adjuntos."""
    return conn.execute(
        """
        SELECT r.id, r.nombre, r.fecha,
               COALESCE((SELECT SUM(i.monto) FROM rendicion_items i WHERE i.rendicion_id = r.id), 0) AS total,
               COALESCE((SELECT SUM(p.monto) FROM rendicion_pagos p WHERE p.rendicion_id = r.id), 0) AS pagado,
               (SELECT COUNT(*) FROM rendicion_adjuntos a WHERE a.rendicion_id = r.id) AS n_adjuntos
        FROM rendiciones r
        ORDER BY r.fecha DESC, r.id DESC
        """
    ).fetchall()


def rendicion_por_id(conn: sqlite3.Connection, rid: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT r.id, r.nombre, r.fecha,
               COALESCE((SELECT SUM(i.monto) FROM rendicion_items i WHERE i.rendicion_id = r.id), 0) AS total,
               COALESCE((SELECT SUM(p.monto) FROM rendicion_pagos p WHERE p.rendicion_id = r.id), 0) AS pagado
        FROM rendiciones r
        WHERE r.id = ?
        """,
        (rid,),
    ).fetchone()


def items_de_rendicion(conn: sqlite3.Connection, rid: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, descripcion, numero_doc, monto FROM rendicion_items "
        "WHERE rendicion_id = ? ORDER BY id ASC",
        (rid,),
    ).fetchall()


def adjuntos_de_rendicion(conn: sqlite3.Connection, rid: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, nombre_archivo, path FROM rendicion_adjuntos "
        "WHERE rendicion_id = ? ORDER BY id ASC",
        (rid,),
    ).fetchall()


def agregar_adjunto(conn: sqlite3.Connection, rid: int, nombre_archivo: str, path: str) -> None:
    conn.execute(
        "INSERT INTO rendicion_adjuntos (rendicion_id, nombre_archivo, path) VALUES (?, ?, ?)",
        (rid, nombre_archivo, path),
    )


def adjunto_por_id(conn: sqlite3.Connection, adj_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, rendicion_id, nombre_archivo, path FROM rendicion_adjuntos WHERE id = ?",
        (adj_id,),
    ).fetchone()


def eliminar_adjunto(conn: sqlite3.Connection, adj_id: int, rid: int) -> None:
    conn.execute(
        "DELETE FROM rendicion_adjuntos WHERE id = ? AND rendicion_id = ?", (adj_id, rid)
    )


def pagos_de_rendicion(conn: sqlite3.Connection, rid: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, fecha, monto FROM rendicion_pagos WHERE rendicion_id = ? "
        "ORDER BY fecha ASC, id ASC",
        (rid,),
    ).fetchall()


def agregar_pago_rendicion(conn: sqlite3.Connection, rid: int, fecha: str, monto: int) -> None:
    conn.execute(
        "INSERT INTO rendicion_pagos (rendicion_id, fecha, monto) VALUES (?, ?, ?)",
        (rid, fecha, monto),
    )


def eliminar_pago_rendicion(conn: sqlite3.Connection, pago_id: int, rid: int) -> None:
    conn.execute(
        "DELETE FROM rendicion_pagos WHERE id = ? AND rendicion_id = ?", (pago_id, rid)
    )


def eliminar_rendicion(conn: sqlite3.Connection, rid: int) -> list[str]:
    """Elimina la rendición y todo lo asociado. Devuelve las rutas de adjuntos
    para que la capa superior borre los archivos del disco."""
    paths = [r["path"] for r in adjuntos_de_rendicion(conn, rid)]
    conn.execute("DELETE FROM rendicion_items WHERE rendicion_id = ?", (rid,))
    conn.execute("DELETE FROM rendicion_adjuntos WHERE rendicion_id = ?", (rid,))
    conn.execute("DELETE FROM rendicion_pagos WHERE rendicion_id = ?", (rid,))
    conn.execute("DELETE FROM rendiciones WHERE id = ?", (rid,))
    return paths


# ---------------------------------------------------------------------------
# Módulo 5 · Export compras/ventas y Rendiciones
#
# Consolida los movimientos reales de caja (cada pago/cobro registrado) para
# que el contador los pueda cruzar con la cartola bancaria. Tres orígenes:
#   · pagos de facturas recibidas   -> EGRESO  (E-Auto paga a un proveedor)
#   · cobros de facturas emitidas   -> INGRESO (E-Auto cobra a un cliente)
#   · pagos de rendiciones          -> EGRESO  (gasto rendido)
# ---------------------------------------------------------------------------

def codigo_rendicion(rid: int) -> str:
    """Código legible y único de una rendición, derivado de su id: R-0001."""
    return f"R-{int(rid):04d}"


def movimientos_en_rango(conn: sqlite3.Connection, desde: str, hasta: str) -> list[dict]:
    """Movimientos de caja entre `desde` y `hasta` (YYYY-MM-DD, ambos inclusive),
    ordenados por fecha. Cada movimiento es un dict con:
    fecha, flujo ('Ingreso'|'Egreso'), descripcion, monto, origen, ref.
    """
    movs: list[dict] = []

    # Pagos/cobros asociados a facturas.
    for p in conn.execute(
        """
        SELECT p.fecha AS fecha, p.direccion AS direccion, p.monto AS monto,
               f.tipo AS ftipo, f.documento AS documento, f.folio AS folio,
               f.razon_social AS razon_social, f.codigo_sii AS codigo_sii
        FROM pagos p
        JOIN facturas f ON f.id = p.factura_id
        WHERE p.fecha >= ? AND p.fecha <= ?
          AND p.rendicion_id IS NULL
        """,
        (desde, hasta),
    ).fetchall():
        # 'recibido' = E-Auto cobra (ingreso); 'emitido' = E-Auto paga (egreso).
        ingreso = p["direccion"] == "recibido"
        doc = (p["documento"] or "Factura").strip()
        folio = p["folio"]
        rs = (p["razon_social"] or "").strip()
        desc = doc + (f" N° {folio}" if folio else "")
        if rs:
            desc += f" · {rs}"
        movs.append({
            "fecha": p["fecha"],
            "flujo": "Ingreso" if ingreso else "Egreso",
            "descripcion": desc,
            "monto": p["monto"],
            "origen": "factura",
            "ref": p["codigo_sii"],
        })

    # Pagos de rendiciones (siempre egreso).
    for p in conn.execute(
        """
        SELECT rp.fecha AS fecha, rp.monto AS monto,
               r.id AS rid, r.nombre AS nombre
        FROM rendicion_pagos rp
        JOIN rendiciones r ON r.id = rp.rendicion_id
        WHERE rp.fecha >= ? AND rp.fecha <= ?
        """,
        (desde, hasta),
    ).fetchall():
        movs.append({
            "fecha": p["fecha"],
            "flujo": "Egreso",
            "descripcion": f"Rendición {codigo_rendicion(p['rid'])}: {p['nombre']}",
            "monto": p["monto"],
            "origen": "rendicion",
            "ref": p["rid"],
        })

    # Orden estable: por fecha; a igual fecha, ingresos antes que egresos.
    movs.sort(key=lambda m: (m["fecha"] or "", 0 if m["flujo"] == "Ingreso" else 1))
    return movs


def rendiciones_con_pago_en_rango(conn: sqlite3.Connection, desde: str,
                                  hasta: str) -> list[sqlite3.Row]:
    """Rendiciones con al menos un pago registrado dentro del rango.

    Se usa para el export de PDFs: solo se exportan las rendiciones que
    aparecen en el listado de movimientos de ese rango.
    """
    return conn.execute(
        """
        SELECT DISTINCT r.id, r.nombre, r.fecha,
               COALESCE((SELECT SUM(i.monto) FROM rendicion_items i
                         WHERE i.rendicion_id = r.id), 0) AS total,
               COALESCE((SELECT SUM(pp.monto) FROM rendicion_pagos pp
                         WHERE pp.rendicion_id = r.id), 0) AS pagado
        FROM rendiciones r
        JOIN rendicion_pagos rp ON rp.rendicion_id = r.id
        WHERE rp.fecha >= ? AND rp.fecha <= ?
        ORDER BY r.fecha ASC, r.id ASC
        """,
        (desde, hasta),
    ).fetchall()


# ---------------------------------------------------------------------------
# Cockpit (pantalla inicial)
# ---------------------------------------------------------------------------

def facturas_rechazadas(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Facturas con reclamo (rechazadas en el RCV), ordenadas por fecha desc.

    Devuelve folio, contraparte (razon_social), fecha de emisión, monto y datos
    para enlazar el PDF.
    """
    return conn.execute(
        """
        SELECT codigo_sii, folio, razon_social, rut_contraparte,
               fecha_emision, total, pdf_path, fecha_reclamo
        FROM facturas
        WHERE fecha_reclamo IS NOT NULL AND fecha_reclamo != ''
        ORDER BY fecha_emision DESC, folio DESC
        """
    ).fetchall()


def documentos_vencidos(conn: sqlite3.Connection, tipo: str, hoy: str) -> list[dict]:
    """Facturas de `tipo` con saldo pendiente y fecha tope YA vencida (< hoy).

    Sirve para el cockpit: 'compra' = pagos vencidos a proveedores,
    'venta' = cobranzas vencidas a clientes. Excluye guías/NC (vía
    facturas_con_pago) y las rechazadas. Devuelve dicts con
    codigo_sii, folio, razon_social, fecha_pago_tope, pendiente, pdf_path.
    Ordenado por fecha tope ascendente (lo más vencido primero).
    """
    res: list[dict] = []
    for f in facturas_con_pago(conn, tipo=tipo):
        if f["fecha_reclamo"]:
            continue  # una rechazada no se paga ni se cobra
        pendiente = (f["total"] or 0) - (f["pagado"] or 0)
        if pendiente <= 0:
            continue  # ya pagada/cobrada al 100%
        tope = f["fecha_pago_tope"]
        if not tope or tope >= hoy:
            continue  # sin tope o aún no vence
        res.append({
            "codigo_sii": f["codigo_sii"],
            "folio": f["folio"],
            "razon_social": f["razon_social"],
            "fecha_pago_tope": tope,
            "pendiente": pendiente,
            "pdf_path": f["pdf_path"],
        })
    res.sort(key=lambda d: d["fecha_pago_tope"])
    return res


def rendiciones_pendientes(conn: sqlite3.Connection) -> list[dict]:
    """Rendiciones que no están pagadas al 100%.

    Devuelve dicts con id, nombre, fecha y saldo (lo que queda por pagar),
    ordenados por fecha ascendente.
    """
    res: list[dict] = []
    for r in listar_rendiciones(conn):
        saldo = (r["total"] or 0) - (r["pagado"] or 0)
        if saldo <= 0:
            continue
        res.append({
            "id": r["id"], "nombre": r["nombre"],
            "fecha": r["fecha"], "saldo": saldo,
        })
    res.sort(key=lambda d: d["fecha"] or "")
    return res
