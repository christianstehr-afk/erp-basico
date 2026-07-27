#!/usr/bin/env python3
"""
Migración: pasa rendiciones, pagos/cobros de facturas y fechas tope editadas
desde la app LOCAL (tu Mac) a la app en RAILWAY (producción).

Es SEGURO correr este script varias veces: antes de crear nada, revisa qué
rendiciones ya existen en Railway (por nombre+fecha) y no las vuelve a crear
ni les vuelve a subir los adjuntos; solo reintenta los pagos/cobros de
factura que aún no se hayan migrado con éxito en una corrida anterior
-- pero OJO: si un pago YA se migró bien, volver a correr el script lo
migraría de nuevo (quedaría duplicado). Este script no lleva registro de qué
pagos individuales ya se migraron, así que solo vuelve a correrlo si la
corrida anterior reportó pagos "sin factura" (no migrados) y ya sincronizaste
esas facturas — no lo corras "por si acaso" una vez que ya haya reportado
pagos migrados con éxito.

Requisitos antes de correrlo:
  1. La app local debe estar corriendo (./run.sh) en http://127.0.0.1:8000
  2. La variable ADMIN_SECRET debe estar seteada IGUAL en ambos lados
     (local y Railway).
  3. En la web de Railway ya debiste haber iniciado sesión y dado
     "Actualizar con SII", para que las facturas (recibidas/emitidas) que
     tienen pagos/cobros asociados ya existan ahí.

Uso:
    ADMIN_SECRET="el-secreto" python3 migrar_a_railway.py
"""
import os
import sys

import requests

LOCAL_URL = os.environ.get("LOCAL_URL", "http://127.0.0.1:8000")
RAILWAY_URL = os.environ.get("RAILWAY_URL", "https://erp-basico-production.up.railway.app")
SECRET = os.environ.get("ADMIN_SECRET", "")


def main() -> int:
    if not SECRET:
        print("ERROR: falta la variable de entorno ADMIN_SECRET.")
        print('Corre:  ADMIN_SECRET="el-secreto" python3 migrar_a_railway.py')
        return 1

    print(f"1) Exportando datos de {LOCAL_URL} ...")
    r = requests.get(f"{LOCAL_URL}/admin/export", params={"secret": SECRET}, timeout=30)
    if r.status_code == 404:
        print("ERROR: la app local respondió 404. ¿Reiniciaste con ./run.sh después del último")
        print("       cambio de código, y seteaste ADMIN_SECRET antes de arrancarla?")
        return 1
    r.raise_for_status()
    data = r.json()
    n_rend = len(data.get("rendiciones", []))
    n_pagos = len(data.get("pagos_facturas", []))
    n_tope = len(data.get("fecha_pago_tope", []))
    print(f"   {n_rend} rendición(es), {n_pagos} pago(s)/cobro(s) de factura, {n_tope} fecha(s) tope editada(s).")

    if n_rend == 0 and n_pagos == 0 and n_tope == 0:
        print("No hay nada que migrar. Fin.")
        return 0

    # --- Evita duplicar rendiciones ya migradas: mira qué existe hoy en Railway ---
    print(f"2) Revisando qué ya existe en {RAILWAY_URL} ...")
    r0 = requests.get(f"{RAILWAY_URL}/admin/export", params={"secret": SECRET}, timeout=30)
    if r0.status_code == 404:
        print("ERROR: Railway respondió 404 en /admin/export. ¿Ya terminó el redeploy con el")
        print("       código nuevo? ¿ADMIN_SECRET está seteada en Railway con el mismo valor?")
        return 1
    r0.raise_for_status()
    existentes = {(rr["nombre"], rr["fecha"]): rr["local_id"] for rr in r0.json().get("rendiciones", [])}

    a_crear = []
    id_map_previo = {}
    ya_migradas = 0
    for rend in data.get("rendiciones", []):
        clave = (rend["nombre"], rend["fecha"])
        if clave in existentes:
            id_map_previo[str(rend["local_id"])] = existentes[clave]
            ya_migradas += 1
        else:
            a_crear.append(rend)
    if ya_migradas:
        print(f"   {ya_migradas} rendición(es) ya estaban migradas (por nombre+fecha) — no se duplican.")
    print(f"   {len(a_crear)} rendición(es) nueva(s) por crear.")

    payload = {
        "rendiciones": a_crear,
        "pagos_facturas": data.get("pagos_facturas", []),
        "fecha_pago_tope": data.get("fecha_pago_tope", []),
        "rendicion_id_map": id_map_previo,
    }

    print(f"3) Subiendo a {RAILWAY_URL} ...")
    r2 = requests.post(f"{RAILWAY_URL}/admin/import", params={"secret": SECRET}, json=payload, timeout=60)
    r2.raise_for_status()
    resumen = r2.json()
    print(f"   Rendiciones creadas en Railway: {resumen['rendiciones_creadas']}")
    print(f"   Pagos/cobros de factura migrados: {resumen['pagos_facturas_ok']}")
    if resumen["pagos_facturas_sin_factura"]:
        pendientes = resumen["pagos_facturas_sin_factura"]
        print(f"   AVISO: {len(pendientes)} pago(s) no se migraron porque la factura aún no existe")
        print("          en Railway (falta sincronizar con el SII):")
        for cod in sorted(set(pendientes)):
            print(f"            - {cod}")
        print("          Sincroniza en la web y vuelve a correr este script para esos casos.")

    # --- Adjuntos: solo de las rendiciones NUEVAS (las ya existentes no se tocan) ---
    id_map = resumen["id_map"]  # incluye las nuevas + las pasadas en rendicion_id_map
    total_adj = sum(len(r["adjuntos"]) for r in a_crear)
    if total_adj:
        print(f"4) Subiendo {total_adj} adjunto(s) de las rendiciones nuevas ...")
        subidos = 0
        for rend in a_crear:
            nuevo_rid = id_map.get(str(rend["local_id"]))
            if nuevo_rid is None:
                continue
            for adj in rend["adjuntos"]:
                fr = requests.get(
                    f"{LOCAL_URL}/admin/export/adjunto/{adj['local_id']}",
                    params={"secret": SECRET}, timeout=30,
                )
                if fr.status_code != 200:
                    print(f"   AVISO: no se pudo leer localmente el adjunto {adj['nombre_archivo']!r}")
                    continue
                fu = requests.post(
                    f"{RAILWAY_URL}/admin/import/adjunto",
                    data={"rendicion_id": nuevo_rid, "secret": SECRET},
                    files={"archivo": (adj["nombre_archivo"], fr.content)},
                    timeout=60,
                )
                if fu.status_code == 200:
                    subidos += 1
                else:
                    print(f"   AVISO: falló la subida de {adj['nombre_archivo']!r} ({fu.status_code})")
        print(f"   Adjuntos subidos: {subidos}/{total_adj}")

    print()
    print("Listo. Verifica en la web de Railway que rendiciones, pagos e ingresos aparezcan bien.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
