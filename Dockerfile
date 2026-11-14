FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr libgomp1 libglib2.0-0 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN useradd -m -u 10001 appuser && mkdir -p /app/data && chown -R appuser:appuser /app/data
COPY app/ /app/app/
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
