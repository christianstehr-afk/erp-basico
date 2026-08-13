"""
Sincronización con el SII: actualiza la base de datos con las facturas
recibidas y emitidas nuevas.

Se ejecuta al iniciar sesión (y puede dispararse manualmente). El guardado de
metadatos es rápido y sincrónico.

Los PDF se PRECARGAN al final de cada sync (decisión 2026-08-07, revirtiendo
la descarga al vuelo que resultó lenta: el SII tarda entre 3 y >45 s por
documento): se descargan en background los que aún no tengan copia local y se
guardan PERMANENTES en pdf_store (volumen /data en Railway). Los syncs
siguientes solo bajan lo nuevo. Ver _precargar_pdfs; el visor en main.py
sirve desde disco y solo cae al SII si un PDF aún no está (y ahí también lo
guarda, cache-on-view).
"""
from __future__ import annotations

import threading
from datetime import datetime

from . import db, pdf_store, sii_bhe, sii_docs, sii_rcv
from .sii_client import SIIClient, SIISessionExpirada

# --- Vigilancia de otras empresas (pedido de Christian, 2026-08-13) --------
#
# Con su RUT personal, Christian es apoderado en el SII de otras 2 empresas
# sin movimiento (aparte de E-Auto). El único riesgo real ahí es que alguien
# les emita una factura sin que él se entere (puede generarle problemas
# tributarios serios). Por eso, en cada sync se revisan también las facturas
# RECIBIDAS de esas 2 empresas — pero a propósito NUNCA se guardan en la BD:
# es solo informativo y no debe "contaminar" el resto del ERP. Se guardan
# solo en memoria (otras_empresas_cache, ver abajo) y se vuelven a consultar
# al SII en cada sync, igual que estado_sync.
OTRAS_EMPRESAS = [
    {"rut": "77204094-6", "nombre": "Christian Stehr Servicios SpA"},
    {"rut": "76178564-8", "nombre": "Christian Stehr Servicios Informáticos EIRL"},
]

otras_empresas_cache: dict = {
    "documentos": [],     # última lista buena conocida (nunca en BD, ver arriba)
    "actualizado": None,  # texto "dd-mm-YYYY HH:MM" del último sync que sí pudo actualizarla
    "error": None,        # si el último intento falló, el motivo (se sigue mostrando la lista anterior)
}

# Estado simple de sincronización (para mostrar en el panel)
estado_sync: dict = {
    "corriendo": False,
    "fase": "",           # texto de la etapa actual (para la barra de progreso)
    "recibidas": 0,
    "emitidas": 0,
    "boletas": 0,
    "error": None,
    # Distinto de un error cualquiera: la sesión con el SII se cerró de su
    # lado (típicamente por inactividad) y hay que volver a iniciar sesión
    # para seguir sincronizando. El panel usa esto para mandar al usuario a
    # /?relogin=1 en vez de solo mostrar "Error: ...".
    "sesion_perdida": False,
    # Las boletas de honorarios usan una sesión SII aparte (login "empresa").
    # Un problema ahí NUNCA bloquea el sync de facturas: se guarda acá y el
    # panel lo muestra como aviso, no como error duro.
    "boletas_error": None,
    # Progreso de la precarga de PDFs (el dashboard ya sabe pintar una barra
    # con porcentaje real cuando pdf_total > 0; ver dashboard.html).
    "pdf_total": 0,
    "pdf_hechos": 0,
    "pdf_fallidos": 0,
}


def _anios_a_sincronizar(anio_hasta: int, desde: str | None) -> list[int]:
    """Años del SII a consultar: desde el año de `desde` (YYYY-MM-DD) hasta
    `anio_hasta` inclusive, ambos por separado (el SII organiza recibidos,
    emitidos y boletas por año/mes, así que un `desde` de un año anterior
    exige una consulta extra por cada año de por medio).

    Sin `desde` (o si viene mal formado), se mantiene el comportamiento
    anterior: solo `anio_hasta`.
    """
    if not desde:
        return [anio_hasta]
    try:
        anio_desde = int(desde[:4])
    except (ValueError, IndexError):
        return [anio_hasta]
    if anio_desde > anio_hasta:
        return [anio_hasta]
    return list(range(anio_desde, anio_hasta + 1))


def sincronizar(client: SIIClient, anio: int = 2026, desde: str | None = None,
                client_bhe: SIIClient | None = None, rut_empresa: str | None = None,
                empresa_rut: str | None = None) -> dict:
    """Actualiza la BD con recibidas, emitidas y boletas de honorarios.

    Consulta cada año entre el de `desde` (si viene) y `anio` inclusive,
    porque el SII lista los documentos y boletas año por año.

    `client_bhe`/`rut_empresa`: sesión y RUT de la cuenta "empresa", usada
    solo para boletas de honorarios recibidas (ver sii_bhe.py). Si vienen en
    None, simplemente no se sincronizan boletas (no es un error).

    `empresa_rut`: RUT de la empresa principal (E-Auto) que `client` ya tiene
    seleccionada al llamar esta función. Si viene, al final del sync se
    revisan también las OTRAS_EMPRESAS (ver _sincronizar_otras_empresas) y se
    vuelve a dejar esta empresa seleccionada en la sesión. Si viene None, no
    se revisan las otras empresas (comportamiento anterior).

    Devuelve un resumen con cuántos documentos se trajeron de cada tipo.
    """
    estado_sync.update(corriendo=True, error=None, fase="Consultando SII…", sesion_perdida=False,
                       pdf_total=0, pdf_hechos=0, pdf_fallidos=0)
    anios = _anios_a_sincronizar(anio, desde)
    try:
        recibidos: list[dict] = []
        emitidos: list[dict] = []
        for a in anios:
            recibidos.extend(sii_docs.obtener_documentos(client.session, "recibidos", anio=a))
            emitidos.extend(sii_docs.obtener_documentos(client.session, "emitidos", anio=a))
    except SIISessionExpirada as exc:
        estado_sync.update(corriendo=False, error=str(exc), fase="Sesión perdida", sesion_perdida=True)
        raise
    except Exception as exc:
        estado_sync.update(corriendo=False, error=str(exc), fase="Error")
        raise

    # Solo documentos desde `desde` (YYYY-MM-DD) en adelante: inicio en
    # producción del ERP (junio 2026). Antes de eso no se sincroniza nada.
    if desde:
        recibidos = [d for d in recibidos if (d.get("fecha") or "") >= desde]
        emitidos = [d for d in emitidos if (d.get("fecha") or "") >= desde]

    estado_sync["fase"] = "Guardando documentos…"
    conn = db.get_conn()
    try:
        for d in recibidos:
            db.upsert_documento(conn, d, tipo="compra")
        for d in emitidos:
            db.upsert_documento(conn, d, tipo="venta")
        conn.commit()

        # Estado de rechazo/acuse de las emitidas, cruzando con el RCV por periodo.
        if client.rut_empresa:
            periodos = sorted({
                (d["fecha"] or "")[:7].replace("-", "")
                for d in emitidos if d.get("fecha")
            })
            periodos = [p for p in periodos if len(p) == 6]
            try:
                for estado in sii_rcv.estados_de_venta(client.session, client.rut_empresa, periodos):
                    db.marcar_estado_venta(conn, estado)
                conn.commit()
            except Exception:
                pass  # si el RCV no responde, seguimos sin marcar rechazos

        # Notas de crédito de anulación: revisa el PDF de cada NC emitida aún no
        # procesada en busca de "ANULA DOCUMENTO..." y, si la encuentra, marca
        # como ANULADA la factura que referencia (folio + contraparte).
        # ref_procesada solo se pone en 1 cuando ya sabemos que no hay nada más
        # que hacer con esta NC: no encontró referencia de anulación en el PDF,
        # o sí la encontró y logró marcar la factura. Si encontró un folio_ref
        # pero marcar_anulada no pudo (p. ej. la factura referenciada todavía
        # no estaba sincronizada ese día, o hubo ambigüedad), NO se marca
        # procesada: se reintenta en el próximo sync en vez de quedar
        # silenciosamente sin marcar para siempre (bug reportado 2026-08-03:
        # una NC con referencia de anulación real no dejó su factura marcada
        # y no había forma de detectarlo sin mirar la BD a mano).
        pendientes_nc = db.notas_credito_sin_procesar(conn)
        if pendientes_nc:
            estado_sync["fase"] = "Revisando notas de crédito…"
            for nc in pendientes_nc:
                # Copia local primero (si ya se precargó en un sync anterior);
                # si no está, se baja del SII y se guarda de una vez.
                pdf_bytes = pdf_store.leer(nc["pdf_path"])
                if not pdf_bytes:
                    try:
                        pdf_bytes = sii_docs.obtener_pdf_bytes(client.session, "emitidos", nc["codigo_sii"])
                    except Exception:
                        pdf_bytes = None
                    if pdf_bytes:
                        pdf_store.guardar(conn, nc["codigo_sii"], "venta",
                                          nc["fecha_emision"], pdf_bytes)
                if not pdf_bytes:
                    continue  # falla de descarga: reintenta en el próximo sync
                folio_ref = sii_docs.folio_anulado_en_nc(pdf_bytes)
                if not folio_ref:
                    db.marcar_referencia_procesada(conn, nc["codigo_sii"])
                    continue
                if db.marcar_anulada(conn, folio_ref, nc["rut_contraparte"], nc["codigo_sii"]):
                    db.marcar_referencia_procesada(conn, nc["codigo_sii"])
                # si no logró marcarla, se deja ref_procesada en 0 a propósito
                # para reintentar en el próximo sync
            conn.commit()

        # Boletas de honorarios recibidas (BHE): requiere la sesión "empresa"
        # (login separado, ver main.py). Nunca bloquea el resto del sync: un
        # fallo acá se guarda en estado_sync["boletas_error"] y se sigue.
        estado_sync["boletas_error"] = None
        if client_bhe is not None and rut_empresa:
            estado_sync["fase"] = "Consultando boletas de honorarios…"
            try:
                boletas = []
                for a in anios:
                    boletas.extend(sii_bhe.obtener_boletas_recibidas(
                        client_bhe.session, rut_empresa, a, desde=desde))
                if desde:
                    boletas = [b for b in boletas if (b.get("fecha") or "") >= desde]
                for b in boletas:
                    db.upsert_boleta(conn, b)
                conn.commit()
            except Exception as exc:
                estado_sync["boletas_error"] = str(exc)

        estado_sync["recibidas"] = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo='compra'"
        ).fetchone()[0]
        estado_sync["emitidas"] = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo='venta'"
        ).fetchone()[0]
        estado_sync["boletas"] = conn.execute(
            "SELECT COUNT(*) FROM facturas WHERE tipo='compra' AND codigo_sii LIKE 'BHE-%'"
        ).fetchone()[0]
    finally:
        conn.close()

    # Precarga de PDFs: baja y guarda (permanente, ver pdf_store) los PDF de
    # facturas y boletas que aún no tengan copia local. Incremental: en el
    # primer sync los baja todos; en los siguientes, solo los documentos
    # nuevos. Nunca hace fallar el sync: lo que no se pudo bajar queda
    # pendiente y se reintenta en el próximo.
    _precargar_pdfs(client, client_bhe)

    # Vigilancia de otras empresas (ver comentario junto a OTRAS_EMPRESAS,
    # arriba). Va al final a propósito: cambia de empresa activa en la sesión
    # SII y, aunque siempre la restaura, mejor dejarlo después de todo lo que
    # sí depende de tener EMPRESA_RUT seleccionada.
    if empresa_rut:
        estado_sync["fase"] = "Revisando otras empresas…"
        _sincronizar_otras_empresas(client, anio, desde, empresa_rut)

    estado_sync["fase"] = "Listo"
    estado_sync["corriendo"] = False

    return {
        "recibidas": len(recibidos),
        "emitidas": len(emitidos),
        "total_recibidas": estado_sync["recibidas"],
        "total_emitidas": estado_sync["emitidas"],
        "total_boletas": estado_sync["boletas"],
    }


def _precargar_pdfs(client: SIIClient, client_bhe: SIIClient | None) -> None:
    """Descarga y guarda los PDF de todos los documentos sin copia local.

    · Qué falta se decide contra el DISCO (pdf_store.tiene_copia), no contra
      pdf_path: si la BD trae rutas de otra máquina o el archivo se perdió,
      se vuelve a bajar.
    · Un commit por PDF: el avance queda confirmado aunque el proceso muera
      a mitad de la cola (el próximo sync retoma donde quedó), y las
      transacciones cortas no bloquean a un usuario navegando la app.
    · Boletas (BHE-*) requieren la sesión "empresa": si no vino client_bhe o
      la boleta no trae su código de barras (pdf_href_bhe), quedan como
      fallidas para reintentar en el próximo sync.
    · El progreso real (pdf_hechos/pdf_total) se publica en estado_sync y el
      dashboard lo pinta como barra con porcentaje.
    """
    conn = db.get_conn()
    try:
        filas = db.facturas_para_precarga_pdf(conn)
        pendientes = [f for f in filas if not pdf_store.tiene_copia(f["pdf_path"])]
        estado_sync.update(pdf_total=len(pendientes), pdf_hechos=0, pdf_fallidos=0)
        if not pendientes:
            return
        estado_sync["fase"] = "Descargando PDFs…"
        nuevos = 0
        for f in pendientes:
            codigo = f["codigo_sii"]
            data = None
            try:
                if codigo.startswith("BHE-"):
                    if client_bhe is not None and f["pdf_href_bhe"]:
                        data = sii_bhe.obtener_pdf_bytes(client_bhe.session, f["pdf_href_bhe"])
                else:
                    fuente = "recibidos" if f["tipo"] == "compra" else "emitidos"
                    data = sii_docs.obtener_pdf_bytes(client.session, fuente, codigo)
            except Exception:
                # BHEError, timeout, etc.: falla puntual de ESTE documento;
                # nunca aborta la cola ni invalida sesiones.
                data = None
            if data and pdf_store.guardar(conn, codigo, f["tipo"], f["fecha_emision"], data):
                conn.commit()
                nuevos += 1
            else:
                estado_sync["pdf_fallidos"] += 1
            estado_sync["pdf_hechos"] += 1
        if nuevos or estado_sync["pdf_fallidos"]:
            db.registrar_log(
                conn,
                f"Precarga de PDFs: {nuevos} guardado(s), "
                f"{estado_sync['pdf_fallidos']} pendiente(s) de reintento",
                usuario="sync",
            )
            conn.commit()
    finally:
        conn.close()


def _sincronizar_otras_empresas(
    client: SIIClient, anio: int, desde: str | None, empresa_rut: str
) -> None:
    """Revisa las facturas RECIBIDAS de OTRAS_EMPRESAS y actualiza
    otras_empresas_cache (ver comentario junto a OTRAS_EMPRESAS, arriba).

    Nunca persiste en BD ni hace fallar el resto del sync: cualquier error
    queda solo en otras_empresas_cache["error"], conservando la última lista
    buena conocida (mejor mostrar información vieja que borrar todo y que
    parezca, erróneamente, que no hay nada que revisar).

    Selecciona cada empresa por turno en la MISMA sesión SII (comparte el
    login personal de Christian, que es apoderado de las tres) y, pase lo que
    pase, siempre vuelve a dejar seleccionada `empresa_rut` (E-Auto) al
    final: el resto del ERP asume que la sesión queda apuntando a esa
    empresa.
    """
    anios = _anios_a_sincronizar(anio, desde)
    documentos: list[dict] = []
    try:
        for empresa in OTRAS_EMPRESAS:
            client.seleccionar_empresa(empresa["rut"])
            docs: list[dict] = []
            for a in anios:
                docs.extend(sii_docs.obtener_documentos(client.session, "recibidos", anio=a))
            if desde:
                docs = [d for d in docs if (d.get("fecha") or "") >= desde]
            for d in docs:
                d["empresa_rut"] = empresa["rut"]
                d["empresa_nombre"] = empresa["nombre"]
            documentos.extend(docs)
    except Exception as exc:
        otras_empresas_cache["error"] = str(exc)
        return
    finally:
        try:
            client.seleccionar_empresa(empresa_rut)
        except Exception:
            pass  # si esto falla hay un problema mayor; no lo escondemos acá

    documentos.sort(key=lambda d: (d.get("fecha") or "", d.get("codigo") or ""), reverse=True)
    otras_empresas_cache["documentos"] = documentos
    otras_empresas_cache["actualizado"] = datetime.now().strftime("%d-%m-%Y %H:%M")
    otras_empresas_cache["error"] = None


def sincronizar_async(client: SIIClient, anio: int = 2026, desde: str | None = None,
                      client_bhe: SIIClient | None = None, rut_empresa: str | None = None,
                      empresa_rut: str | None = None) -> None:
    """Dispara la sincronización completa en segundo plano.

    Marca corriendo=True de inmediato (antes de devolver) para que el panel
    muestre la barra de progreso apenas se pulsa el botón; el trabajo
    (consulta al SII y guardado) corre en un hilo aparte.
    """
    if estado_sync.get("corriendo"):
        return  # ya hay una sincronización en curso
    estado_sync.update(corriendo=True, error=None, fase="Iniciando…", sesion_perdida=False)

    def _worker() -> None:
        try:
            sincronizar(client, anio=anio, desde=desde, client_bhe=client_bhe,
                       rut_empresa=rut_empresa, empresa_rut=empresa_rut)
        except SIISessionExpirada:
            pass  # ya quedó reflejado en estado_sync (sesion_perdida=True)
        except Exception as exc:
            estado_sync.update(corriendo=False, error=str(exc), fase="Error")

    threading.Thread(target=_worker, daemon=True).start()
