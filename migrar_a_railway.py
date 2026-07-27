#!/usr/bin/env python3
"""
Migración ÚNICA: pasa rendiciones, pagos/cobros de facturas y fechas tope
editadas desde la app LOCAL (tu Mac) a la app en RAILWAY (producción).

Requisitos antes de correrlo:
  1. La app local debe estar corriendo (./run.sh) en http://127.0.0.1:8000
  2. La variable ADMIN_SECRET debe estar seteada IGUAL en ambos lados
     (local y Railway). Pide el valor a Claude si no lo tienes a mano.
  3. En la web de Railway ya debiste haber iniciado sesión al menos una vez
     y darle "Actualizar con SII", para que las facturas (recibidas/emitidas)
     ya existan ahí — si no, los pagos que apuntan a una factura que aún no
     existe en Railway se reportan al final como "sin factura" y no se
     pierden: puedes volver a correr este script después de sincronizar.

Uso:
    ADMIN_SECRET="el-secreto" python3 migrar_a_railway.py

Es seguro correrlo más de una vez para los pagos de facturas y fechas tope
(se vuelven a insertar como pagos nuevos, así que si ya migraste algo NO lo
vuelvas a correr sin avisar — duplicaría esos pagos). Las rendiciones SIEMPRE
se crean nuevas cada vez que corres el script, así que solo debe ejecutarse
UNA vez por rendición.
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

    print(f"2) Subiendo datos a {RAILWAY_URL} ...")
    r2 = requests.post(f"{RAILWAY_URL}/admin/import", params={"secret": SECRET}, json=data, timeout=60)
    if r2.status_code == 404:
        print("ERROR: Railway respondió 404. ¿Ya hiciste git push y terminó el redeploy con el")
        print("       código nuevo? ¿ADMIN_SECRET está seteada en Railway con el mismo valor?")
        return 1
    r2.raise_for_status()
    resumen = r2.json()
    print(f"   Rendiciones creadas en Railway: {resumen['rendiciones_creadas']}")
    print(f"   Pagos/cobros de factura migrados: {resumen['pagos_facturas_ok']}")
    if resumen["pagos_facturas_sin_factura"]:
        print(f"   AVISO: {len(resumen['pagos_facturas_sin_factura'])} pago(s) no se migraron porque")
        print("          la factura aún no existe en Railway (falta sincronizar con el SII):")
        for cod in resumen["pagos_facturas_sin_factura"]:
            print(f"            - {cod}")
        print("          Sincroniza en la web y vuelve a correr este script para esos casos.")

    # --- Adjuntos: uno por uno, usando el mapeo de ids que devolvió /admin/import ---
    id_map = resumen["id_map"]  # {"local_id_str": nuevo_id_en_railway}
    total_adj = sum(len(r["adjuntos"]) for r in data.get("rendiciones", []))
    if total_adj:
        print(f"3) Subiendo {total_adj} adjunto(s) ...")
        subidos = 0
        for rend in data.get("rendiciones", []):
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
