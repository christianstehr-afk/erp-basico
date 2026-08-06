"""
Base de datos SQLite del ERP Básico.

En este primer paso solo se crea el esquema. Las tablas quedan listas para los
módulos siguientes: facturas (RCV) y pagos de E-Auto.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

# --- Ubicación de la base de datos --------------------------------------
# IMPORTANTE: la BD VIVA no debe vivir en una carpeta sincronizada por Dropbox.
# SQLite y Dropbox no se llevan: Dropbox bloquea/revierte el archivo mientras
# sincroniza y se pierden las escrituras más recientes (por eso desaparecían los
# adjuntos). Por defecto la BD viva se guarda en una carpeta LOCAL del Mac,
# fuera de Dropbox. Se puede sobreescribir con la variable de entorno DB_PATH
# (p. ej. en Railway: /data/erp.db).
_DEFAULT_DB = Path.home() / "Library" / "Application Support" / "ERPBasico" / "erp.db"
DB_PATH = Path(os.environ.get("DB_PATH", _DEFAULT_DB))

# BD antigua dentro del proyecto (carpeta Dropbox). Se usa una sola vez para
# migrar los datos existentes a la nueva ubicación local.
_LEGACY_DB = Path(__file__).resolve().parent.parent / "data" / "erp.db"

# Carpeta de respaldos DENTRO del proyecto (Dropbox). Guardar aquí es seguro
# porque son COPIAS estáticas del archivo (no la BD viva): Dropbox las sincroniza
# sin riesgo. Configurable con DB_BACKUP_DIR.
BACKUP_DIR = Path(os.environ.get(
    "DB_BACKUP_DIR", Path(__file__).resolve().parent.parent / "data" / "backups"))
MAX_BACKUPS = 15  # cuántas copias conservar

# Módulo 5 · Movimientos CC: por ahora el espejo solo cubre desde esta fecha
# en adelante (coincide con el backfill de facturas/boletas ya hecho para
# nov-dic 2025; ver comentario de DESDE_SYNC en main.py).
DESDE_MOVIMIENTOS_CC = "2025-11-01"


def _migrar_desde_dropbox() -> None:
    """Si la BD local aún no existe pero sí la antigua en Dropbox, la copia una
    sola vez para no perder los datos ya cargados."""
    if DB_PATH.exists() or DB_PATH == _LEGACY_DB:
        return
    if _LEGACY_DB.exists():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_LEGACY_DB, DB_PATH)


def respaldar_db() -> Path | None:
    """Copia la BD viva a la carpeta de respaldos (Dropbox) con marca de tiempo
    y conserva solo las últimas MAX_BACKUPS. Devuelve la ruta del respaldo."""
    if DB_PATH == _LEGACY_DB or not DB_PATH.exists():
        return None
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        destino = BACKUP_DIR / f"erp_{datetime.now():%Y%m%d_%H%M%S}.db"
        shutil.copy2(DB_PATH, destino)
        copias = sorted(BACKUP_DIR.glob("erp_*.db"))
        for viejo in copias[:-MAX_BACKUPS]:
            viejo.unlink(missing_ok=True)
        return destino
    except Exception:
        return None  # un respaldo fallido nunca debe tumbar el arranque


def respaldo_bytes() -> bytes:
    """Genera un respaldo consistente de la BD viva y lo devuelve en memoria,
    listo para servir como descarga (ver GET /respaldo en main.py).

    Usa la API de backup de sqlite3 (Connection.backup) en vez de copiar el
    archivo directo: es segura aunque haya una escritura en curso en ese
    instante (a diferencia de shutil.copy2, que podría copiar el archivo a
    mitad de una transacción). No toca BACKUP_DIR ni el historial de
    respaldos periódicos de respaldar_db(); es una copia aparte, al vuelo.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        origen = sqlite3.connect(DB_PATH)
        try:
            destino = sqlite3.connect(tmp)
            try:
                origen.backup(destino)
            finally:
                destino.close()
        finally:
            origen.close()
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


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
    descripcion   TEXT,                        -- nota libre de la gestión del pago/cobro (una por factura)
    anulada_por   TEXT,                        -- codigo_sii de la Nota de Crédito que anuló esta factura (solo ventas)
    ref_procesada INTEGER DEFAULT 0,           -- 1 = ya se revisó el PDF de esta NC buscando "ANULA DOCUMENTO..."
    creado_en     TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tipo, tipo_dte, folio, rut_contraparte)
);

CREATE TABLE IF NOT EXISTS pagos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    direccion     TEXT NOT NULL,              -- 'emitido' (E-Auto paga) | 'recibido' (E-Auto cobra)
    factura_id    INTEGER,                     -- FK opcional a facturas.id
    rendicion_id  INTEGER,                     -- si el pago se hizo vía una rendición (no suma al export)
    externo       INTEGER DEFAULT 0,           -- 1 = pago externo: no fue desde la CC empresa ni vía
                                                -- rendición (no suma al export, sin rendición asociada)
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

-- Adjuntos de la gestión de una factura (pago a proveedores / ingresos):
-- documentos de respaldo aparte de la descripción libre. Mismo patrón que
-- rendicion_adjuntos, pero colgando de facturas.
CREATE TABLE IF NOT EXISTS factura_adjuntos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    factura_id     INTEGER NOT NULL,
    nombre_archivo TEXT NOT NULL,            -- nombre original
    path           TEXT NOT NULL,            -- ruta local del archivo guardado
    creado_en      TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (factura_id) REFERENCES facturas(id)
);

CREATE INDEX IF NOT EXISTS idx_factura_adj ON factura_adjuntos(factura_id);

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

-- Módulo 5 · Cartola del banco (para comparar contra los movimientos de la app).
-- Cada "Agregar CC" reemplaza por completo el contenido de esta tabla: solo se
-- guarda la última cartola subida (no se acumula historial).
CREATE TABLE IF NOT EXISTS cc_banco (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha      TEXT NOT NULL,                  -- YYYY-MM-DD
    detalle    TEXT,                            -- texto de "Detalle Movimiento"
    flujo      TEXT NOT NULL,                   -- 'Ingreso' (abono) | 'Egreso' (cargo)
    monto      INTEGER NOT NULL,
    canal      TEXT,                            -- INTERNET, CENTRAL, ...
    creado_en  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cc_banco_fecha ON cc_banco(fecha);

-- Módulo 5 · Movimientos CC: espejo editable de la cuenta corriente de la
-- empresa. Se alimenta solo de los pagos/cobros de facturas y boletas y de
-- los pagos de rendiciones (mismo criterio que antes calculaba
-- movimientos_en_rango al vuelo), más los movimientos que se agregan a mano
-- (origen='manual') para todo lo que la CC del banco muestra pero no nace de
-- una factura/boleta (comisiones, transferencias sueltas, etc.).
-- `pago_id` / `rendicion_pago_id` marcan el origen automático de la fila (uno
-- de los dos, o ninguno si es manual) y permiten mantenerla sincronizada sin
-- duplicar: se recalcula con sincronizar_movimientos_cc() cada vez que se
-- agrega o borra un pago, y también al iniciar la app.
CREATE TABLE IF NOT EXISTS movimientos_cc (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha             TEXT NOT NULL,             -- YYYY-MM-DD
    flujo             TEXT NOT NULL,              -- 'Ingreso' | 'Egreso'
    descripcion       TEXT,
    monto             INTEGER NOT NULL,
    origen            TEXT NOT NULL,              -- 'factura' | 'rendicion' | 'manual'
    ref               TEXT,                        -- codigo_sii (factura) o código de rendición, solo para mostrar/enlazar
    pago_id           INTEGER,                     -- FK pagos.id, si origen='factura'
    rendicion_pago_id INTEGER,                     -- FK rendicion_pagos.id, si origen='rendicion'
    creado_en         TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (pago_id) REFERENCES pagos(id),
    FOREIGN KEY (rendicion_pago_id) REFERENCES rendicion_pagos(id)
);

CREATE INDEX IF NOT EXISTS idx_mov_cc_fecha ON movimientos_cc(fecha);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mov_cc_pago
    ON movimientos_cc(pago_id) WHERE pago_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_mov_cc_rend_pago
    ON movimientos_cc(rendicion_pago_id) WHERE rendicion_pago_id IS NOT NULL;

-- Módulo 6 · Log de auditoría: registra fecha, hora y una descripción de cada
-- operación relevante hecha en la app (crear/editar/eliminar), para poder
-- reconstruir qué pasó si algo se borra por accidente (p. ej. una rendición).
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha      TEXT NOT NULL,              -- YYYY-MM-DD
    hora       TEXT NOT NULL,              -- HH:MM:SS
    accion     TEXT NOT NULL,              -- descripción de la operación realizada
    usuario    TEXT,                        -- RUT de quien tenía la sesión activa
    creado_en  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_logs_fecha ON logs(fecha, hora);
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
        "descripcion": "TEXT", "anulada_por": "TEXT", "ref_procesada": "INTEGER DEFAULT 0",
        # Para boletas de honorarios (codigo_sii que empieza con "BHE-"): el
        # código de barras de la boleta, NO una URL ni una ruta local. El SII
        # no tiene un link directo al PDF de una boleta; su código de barras
        # es lo que hay que mandarle (con la sesión "empresa") para pedirlo
        # al momento de verlo (ver sii_bhe.obtener_pdf_bytes). En facturas
        # normales (DTE) esta columna queda NULL.
        "pdf_href_bhe": "TEXT",
    }
    for col, ddl in nuevas.items():
        if col not in existentes:
            conn.execute(f"ALTER TABLE facturas ADD COLUMN {col} {ddl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_facturas_codigo ON facturas(codigo_sii)")
    # pagos.rendicion_id: pago de factura hecho vía una rendición
    cols_pagos = {r[1] for r in conn.execute("PRAGMA table_info(pagos)")}
    if "rendicion_id" not in cols_pagos:
        conn.execute("ALTER TABLE pagos ADD COLUMN rendicion_id INTEGER")
    if "externo" not in cols_pagos:
        conn.execute("ALTER TABLE pagos ADD COLUMN externo INTEGER DEFAULT 0")
    # Rellena la fecha tope (de pago/cobro) que aún no exista con su fecha de emisión.
    # Aplica a recibidas (pago a proveedores) y emitidas (ingresos).
    conn.execute(
        "UPDATE facturas SET fecha_pago_tope = fecha_emision "
        "WHERE fecha_pago_tope IS NULL OR fecha_pago_tope = ''"
    )


def init_db() -> None:
    _migrar_desde_dropbox()
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrar(conn)
        # Backfill + red de seguridad: deja movimientos_cc al día con lo que
        # ya hay en pagos/rendicion_pagos (idempotente, no duplica en cada
        # arranque).
        sincronizar_movimientos_cc(conn)
    respaldar_db()


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


def upsert_boleta(conn: sqlite3.Connection, doc: dict) -> None:
    """Inserta o actualiza una boleta de honorarios recibida en `facturas`
    (tipo='compra'), reutilizando la misma tabla que las facturas del SII.

    `doc` esperado (de sii_bhe.parse_mes):
      codigo, folio, rut_contraparte, razon_social, fecha, monto, pdf_href.

    `codigo` debe venir ya prefijado como "BHE-..." (único, ver sii_bhe.py) y
    es el que identifica el registro (ON CONFLICT(codigo_sii)). No se toca
    tipo_dte: las boletas de honorarios no son DTE, no tienen ese código.
    """
    params = {
        "codigo": doc["codigo"],
        "folio": doc.get("folio"),
        "rut_contraparte": doc.get("rut_contraparte"),
        "razon_social": doc.get("razon_social"),
        "fecha": doc.get("fecha"),
        "monto": doc.get("monto", 0),
        "estado": doc.get("estado") or "Vigente",
        "pdf_href": doc.get("pdf_href"),
    }
    conn.execute(
        """
        INSERT INTO facturas
            (tipo, codigo_sii, tipo_dte, documento, folio,
             rut_contraparte, razon_social, fecha_emision, total, estado,
             fecha_pago_tope, pdf_href_bhe)
        VALUES
            ('compra', :codigo, NULL, 'Boleta de honorarios electrónica', :folio,
             :rut_contraparte, :razon_social, :fecha, :monto, :estado,
             :fecha, :pdf_href)
        ON CONFLICT(codigo_sii) DO UPDATE SET
            estado          = excluded.estado,
            total           = excluded.total,
            rut_contraparte = excluded.rut_contraparte,
            razon_social    = excluded.razon_social,
            folio           = excluded.folio,
            fecha_emision   = excluded.fecha_emision,
            pdf_href_bhe    = excluded.pdf_href_bhe
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
               f.pdf_path, f.descripcion, f.anulada_por,
               COALESCE((SELECT SUM(p.monto) FROM pagos p WHERE p.factura_id = f.id), 0) AS pagado
        FROM facturas f
        WHERE f.tipo = ?
          AND (f.tipo_dte IS NULL OR f.tipo_dte NOT IN ({marcadores}))
        ORDER BY f.fecha_pago_tope IS NULL, f.fecha_pago_tope ASC, f.folio DESC
        """,
        (tipo, *TIPOS_NO_PAGABLES),
    ).fetchall()


def facturas_con_pago_en_rango(conn: sqlite3.Connection, tipo: str, desde: str, hasta: str) -> list[sqlite3.Row]:
    """Igual que facturas_con_pago(), pero filtrando por la fecha de emisión
    de la factura (f.fecha_emision), no por la fecha de sus pagos."""
    marcadores = ",".join("?" * len(TIPOS_NO_PAGABLES))
    return conn.execute(
        f"""
        SELECT f.id, f.codigo_sii, f.documento, f.folio, f.rut_contraparte,
               f.razon_social, f.fecha_emision, f.total, f.fecha_pago_tope, f.fecha_reclamo,
               f.pdf_path, f.descripcion, f.anulada_por,
               COALESCE((SELECT SUM(p.monto) FROM pagos p WHERE p.factura_id = f.id), 0) AS pagado
        FROM facturas f
        WHERE f.tipo = ?
          AND (f.tipo_dte IS NULL OR f.tipo_dte NOT IN ({marcadores}))
          AND f.fecha_emision >= ? AND f.fecha_emision <= ?
        ORDER BY f.fecha_pago_tope IS NULL, f.fecha_pago_tope ASC, f.folio DESC
        """,
        (tipo, *TIPOS_NO_PAGABLES, desde, hasta),
    ).fetchall()


def factura_pago_por_codigo(conn: sqlite3.Connection, codigo: str) -> sqlite3.Row | None:
    """Una factura por su codigo_sii con el total pagado agregado."""
    return conn.execute(
        """
        SELECT f.id, f.codigo_sii, f.documento, f.folio, f.rut_contraparte,
               f.razon_social, f.fecha_emision, f.total, f.fecha_pago_tope, f.fecha_reclamo, f.pdf_path,
               f.descripcion, f.anulada_por, f.pdf_href_bhe,
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


def set_descripcion(conn: sqlite3.Connection, codigo: str, descripcion: str) -> None:
    """Guarda la nota de la gestión (una por factura, no por pago parcial)."""
    conn.execute(
        "UPDATE facturas SET descripcion = ? WHERE codigo_sii = ?", (descripcion, codigo)
    )


# ---------------------------------------------------------------------------
# Notas de crédito de anulación (facturas emitidas)
#
# Algunas Notas de Crédito Electrónicas (tipo_dte=61) dejan sin efecto una
# factura emitida completa ("ANULA DOCUMENTO DE LA REFERENCIA..." en el PDF).
# Esa factura pasa a estado ANULADA: no se cobra, no se gestiona y no entra al
# export de movimientos.
# ---------------------------------------------------------------------------

def notas_credito_sin_procesar(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Notas de crédito emitidas cuyo PDF aún no se revisó buscando la
    referencia de anulación."""
    return conn.execute(
        "SELECT codigo_sii, folio, rut_contraparte FROM facturas "
        "WHERE tipo = 'venta' AND tipo_dte = 61 AND codigo_sii IS NOT NULL "
        "AND (ref_procesada IS NULL OR ref_procesada = 0)"
    ).fetchall()


def marcar_referencia_procesada(conn: sqlite3.Connection, codigo_sii: str) -> None:
    """Marca que ya se intentó leer el PDF de esta NC (se logró o no encontrar
    la referencia de anulación); evita volver a descargarlo en cada sync."""
    conn.execute("UPDATE facturas SET ref_procesada = 1 WHERE codigo_sii = ?", (codigo_sii,))


def marcar_anulada(conn: sqlite3.Connection, folio: int, rut_contraparte: str, nc_codigo: str) -> bool:
    """Marca como ANULADA la factura de venta (folio + contraparte) referenciada
    por una Nota de Crédito de anulación. Solo actúa si hay una única
    coincidencia (si no encuentra ninguna o hay ambigüedad, no hace nada).
    Devuelve True si se marcó."""
    filas = conn.execute(
        "SELECT id FROM facturas WHERE tipo = 'venta' AND folio = ? AND rut_contraparte = ? "
        "AND tipo_dte != 61",
        (folio, rut_contraparte),
    ).fetchall()
    if len(filas) != 1:
        return False
    conn.execute("UPDATE facturas SET anulada_por = ? WHERE id = ?", (nc_codigo, filas[0]["id"]))
    return True


def pagos_de_factura(conn: sqlite3.Connection, factura_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.id, p.fecha, p.monto, p.rendicion_id, p.externo, r.nombre AS rendicion_nombre "
        "FROM pagos p LEFT JOIN rendiciones r ON r.id = p.rendicion_id "
        "WHERE p.factura_id = ? ORDER BY p.fecha ASC, p.id ASC",
        (factura_id,),
    ).fetchall()


def adjuntos_de_factura(conn: sqlite3.Connection, factura_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, nombre_archivo, path FROM factura_adjuntos "
        "WHERE factura_id = ? ORDER BY id ASC",
        (factura_id,),
    ).fetchall()


def agregar_adjunto_factura(conn: sqlite3.Connection, factura_id: int,
                            nombre_archivo: str, path: str) -> None:
    conn.execute(
        "INSERT INTO factura_adjuntos (factura_id, nombre_archivo, path) VALUES (?, ?, ?)",
        (factura_id, nombre_archivo, path),
    )


def adjunto_factura_por_id(conn: sqlite3.Connection, adj_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, factura_id, nombre_archivo, path FROM factura_adjuntos WHERE id = ?",
        (adj_id,),
    ).fetchone()


def eliminar_adjunto_factura(conn: sqlite3.Connection, adj_id: int, factura_id: int) -> None:
    conn.execute(
        "DELETE FROM factura_adjuntos WHERE id = ? AND factura_id = ?", (adj_id, factura_id)
    )


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
                 direccion: str = "emitido", rendicion_id: int | None = None,
                 externo: bool = False) -> None:
    """Registra un pago parcial de una factura.

    `direccion='emitido'` = pago que E-Auto realiza (pago a proveedores).
    `rendicion_id` no-null = el pago se hizo vía esa rendición; ese monto NO se
    suma al listado de export (la rendición ya aporta el movimiento de caja).
    `externo=True` = el pago NO se hizo desde la CC empresa y tampoco está
    asociado a una rendición; tampoco suma al listado de export. Es
    mutuamente excluyente con `rendicion_id` (se valida en main.py).
    """
    conn.execute(
        "INSERT INTO pagos (direccion, factura_id, fecha, monto, rendicion_id, externo) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (direccion, factura_id, fecha, monto, rendicion_id, 1 if externo else 0),
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


def rendiciones_en_rango(conn: sqlite3.Connection, desde: str, hasta: str) -> list[sqlite3.Row]:
    """Igual que listar_rendiciones(), pero filtrando por la fecha propia de
    la rendición (r.fecha), no por la fecha de sus pagos."""
    return conn.execute(
        """
        SELECT r.id, r.nombre, r.fecha,
               COALESCE((SELECT SUM(i.monto) FROM rendicion_items i WHERE i.rendicion_id = r.id), 0) AS total,
               COALESCE((SELECT SUM(p.monto) FROM rendicion_pagos p WHERE p.rendicion_id = r.id), 0) AS pagado,
               (SELECT COUNT(*) FROM rendicion_adjuntos a WHERE a.rendicion_id = r.id) AS n_adjuntos
        FROM rendiciones r
        WHERE r.fecha >= ? AND r.fecha <= ?
        ORDER BY r.fecha DESC, r.id DESC
        """,
        (desde, hasta),
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
          AND (p.externo IS NULL OR p.externo = 0)
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
# Módulo 5 · Movimientos CC (espejo editable de la cuenta corriente)
# ---------------------------------------------------------------------------

def sincronizar_movimientos_cc(conn: sqlite3.Connection) -> None:
    """Mantiene movimientos_cc al día con pagos/cobros de facturas y con pagos
    de rendiciones (desde DESDE_MOVIMIENTOS_CC en adelante), sin tocar las
    filas manuales. Idempotente: se puede llamar tantas veces como se quiera
    (al iniciar la app y después de cada alta/baja de un pago).

    Mismo criterio que antes usaba movimientos_en_rango(): un pago de factura
    solo cuenta si no fue vía rendición ni externo (esos movimientos de caja
    ya los aporta, o no los aporta, la rendición/lo externo).
    """
    # 1) Agrega los pagos de facturas que califican y todavía no están.
    conn.execute(
        """
        INSERT INTO movimientos_cc (fecha, flujo, descripcion, monto, origen, ref, pago_id)
        SELECT
            p.fecha,
            CASE WHEN p.direccion = 'recibido' THEN 'Ingreso' ELSE 'Egreso' END,
            TRIM(
                COALESCE(f.documento, 'Factura')
                || CASE WHEN f.folio IS NOT NULL THEN ' N° ' || f.folio ELSE '' END
                || CASE WHEN f.razon_social IS NOT NULL AND f.razon_social <> ''
                        THEN ' · ' || f.razon_social ELSE '' END
            ),
            p.monto, 'factura', f.codigo_sii, p.id
        FROM pagos p
        JOIN facturas f ON f.id = p.factura_id
        WHERE (p.rendicion_id IS NULL) AND (p.externo IS NULL OR p.externo = 0)
          AND p.fecha >= ?
          AND p.id NOT IN (
              SELECT pago_id FROM movimientos_cc WHERE pago_id IS NOT NULL
          )
        """,
        (DESDE_MOVIMIENTOS_CC,),
    )
    # 2) Quita las filas "factura" cuyo pago ya no existe o dejó de calificar
    #    (p. ej. se borró el pago, o pasó a ser vía rendición/externo).
    conn.execute(
        """
        DELETE FROM movimientos_cc
        WHERE origen = 'factura' AND pago_id IS NOT NULL
          AND pago_id NOT IN (
              SELECT id FROM pagos
              WHERE (rendicion_id IS NULL) AND (externo IS NULL OR externo = 0)
          )
        """
    )
    # 3) Agrega los pagos de rendiciones que todavía no están.
    conn.execute(
        """
        INSERT INTO movimientos_cc (fecha, flujo, descripcion, monto, origen, ref, rendicion_pago_id)
        SELECT rp.fecha, 'Egreso',
               'Rendición R-' || printf('%04d', r.id) || ': ' || r.nombre,
               rp.monto, 'rendicion', 'R-' || printf('%04d', r.id), rp.id
        FROM rendicion_pagos rp
        JOIN rendiciones r ON r.id = rp.rendicion_id
        WHERE rp.fecha >= ?
          AND rp.id NOT IN (
              SELECT rendicion_pago_id FROM movimientos_cc WHERE rendicion_pago_id IS NOT NULL
          )
        """,
        (DESDE_MOVIMIENTOS_CC,),
    )
    # 4) Quita las filas "rendicion" cuyo pago ya no existe.
    conn.execute(
        """
        DELETE FROM movimientos_cc
        WHERE origen = 'rendicion' AND rendicion_pago_id IS NOT NULL
          AND rendicion_pago_id NOT IN (SELECT id FROM rendicion_pagos)
        """
    )


def movimientos_cc_en_rango(conn: sqlite3.Connection, desde: str, hasta: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM movimientos_cc WHERE fecha >= ? AND fecha <= ? "
        "ORDER BY fecha ASC, id ASC",
        (desde, hasta),
    ).fetchall()


def movimiento_cc_por_id(conn: sqlite3.Connection, mid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM movimientos_cc WHERE id = ?", (mid,)).fetchone()


def agregar_movimiento_manual(conn: sqlite3.Connection, fecha: str, flujo: str,
                              descripcion: str, monto: int) -> int:
    cur = conn.execute(
        "INSERT INTO movimientos_cc (fecha, flujo, descripcion, monto, origen) "
        "VALUES (?, ?, ?, ?, 'manual')",
        (fecha, flujo, descripcion, monto),
    )
    return cur.lastrowid


def editar_movimiento_manual(conn: sqlite3.Connection, mid: int, fecha: str, flujo: str,
                             descripcion: str, monto: int) -> bool:
    """Solo actualiza si la fila es manual (nunca toca una fila automática).
    Devuelve True si se actualizó algo."""
    cur = conn.execute(
        "UPDATE movimientos_cc SET fecha = ?, flujo = ?, descripcion = ?, monto = ? "
        "WHERE id = ? AND origen = 'manual'",
        (fecha, flujo, descripcion, monto, mid),
    )
    return cur.rowcount > 0


def eliminar_movimiento_manual(conn: sqlite3.Connection, mid: int) -> bool:
    """Solo borra si la fila es manual. Devuelve True si se borró algo."""
    cur = conn.execute(
        "DELETE FROM movimientos_cc WHERE id = ? AND origen = 'manual'", (mid,)
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Módulo 5 · Cartola del banco (comparación CC)
# ---------------------------------------------------------------------------

def reemplazar_cc_banco(conn: sqlite3.Connection, movimientos: list[dict]) -> int:
    """Reemplaza por completo la cartola guardada por la nueva ('Agregar CC'
    siempre sobrescribe la anterior). Devuelve la cantidad de filas insertadas."""
    conn.execute("DELETE FROM cc_banco")
    conn.executemany(
        "INSERT INTO cc_banco (fecha, detalle, flujo, monto, canal) "
        "VALUES (:fecha, :detalle, :flujo, :monto, :canal)",
        movimientos,
    )
    return len(movimientos)


def cc_banco_en_rango(conn: sqlite3.Connection, desde: str, hasta: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cc_banco WHERE fecha >= ? AND fecha <= ? ORDER BY fecha ASC, id ASC",
        (desde, hasta),
    ).fetchall()


def cc_banco_resumen(conn: sqlite3.Connection) -> dict | None:
    """Info de la cartola actualmente cargada (cantidad y rango de fechas), o
    None si no hay ninguna cargada. Se usa para habilitar 'Exportar Comparación'."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(fecha) AS min_fecha, MAX(fecha) AS max_fecha FROM cc_banco"
    ).fetchone()
    if not row or not row["n"]:
        return None
    return {"cantidad": row["n"], "desde": row["min_fecha"], "hasta": row["max_fecha"]}


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
        if f["anulada_por"]:
            continue  # una anulada (NC de anulación) tampoco se cobra
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


# ---------------------------------------------------------------------------
# Módulo 6 · Log de auditoría
# ---------------------------------------------------------------------------

def registrar_log(conn: sqlite3.Connection, accion: str, usuario: str | None = None) -> None:
    """Guarda una fila en el log: fecha, hora, la acción (texto libre) y,
    si hay sesión activa, el RUT del usuario que la ejecutó."""
    ahora = datetime.now()
    conn.execute(
        "INSERT INTO logs (fecha, hora, accion, usuario) VALUES (?, ?, ?, ?)",
        (ahora.strftime("%Y-%m-%d"), ahora.strftime("%H:%M:%S"), accion, usuario),
    )


def listar_logs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Todo el historial de operaciones registradas, más recientes primero."""
    return conn.execute(
        "SELECT fecha, hora, accion, usuario FROM logs ORDER BY id DESC"
    ).fetchall()
