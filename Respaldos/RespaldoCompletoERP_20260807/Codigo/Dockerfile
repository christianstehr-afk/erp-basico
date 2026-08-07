# ERP Básico · e-auto — imagen para Railway
FROM python:3.12-slim

WORKDIR /app

# Dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código
COPY . .

# Datos persistentes (Railway monta un volumen en /data)
ENV DB_PATH=/data/erp.db \
    PDF_DIR=/data/pdfs \
    ADJUNTOS_DIR=/data/adjuntos/rendiciones \
    PORT=8000

EXPOSE 8000

# Railway inyecta $PORT en runtime
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
