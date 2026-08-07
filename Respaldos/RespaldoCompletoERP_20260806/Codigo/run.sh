#!/bin/bash
# Levanta el ERP en local vigilando SOLO el código (app/).
# IMPORTANTE: no vigilar toda la carpeta, porque data/ recibe escrituras
# (PDFs, adjuntos, respaldos) y --reload reiniciaría el server en pleno guardado,
# perdiendo la última escritura (p.ej. adjuntos al gestionar una rendición).
cd "$(dirname "$0")"
exec uvicorn app.main:app --reload --reload-dir app
