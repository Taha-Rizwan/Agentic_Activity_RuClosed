FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY frontend ./frontend
COPY tests.json .
# Hugging Face Spaces runs as a non-root user: make the SQLite fallback dir writable
RUN mkdir -p /app/data && chmod -R 777 /app/data
# Hugging Face Spaces expects 7860; Render/Railway set $PORT
EXPOSE 7860
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
