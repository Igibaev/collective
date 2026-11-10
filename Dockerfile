FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -r -u 10001 app && mkdir -p /data && chown app /data
COPY app.py .
COPY templates templates
COPY static static
USER app
EXPOSE 8000
# 1 воркер: ограничитель попыток входа хранится в памяти процесса
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:8000", "--timeout", "300", "--access-logfile", "-", "app:app"]
