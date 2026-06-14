# ---- Build stage (installs dependencies) ----
FROM python:3.11-slim AS builder

WORKDIR /app

# Install dependencies into a separate location to leverage layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---- Runtime stage ----
FROM python:3.11-slim

# Create a non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source code
COPY api/       ./api/
COPY models/    ./models/
COPY services/  ./services/
COPY static/    ./static/
COPY config.py  ./config.py
COPY main.py    ./main.py

# Hand off ownership of the app directory
RUN chown -R appuser:appgroup /app

USER appuser

EXPOSE 8088

# Healthcheck — relies on the root endpoint returning HTTP 200
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8088/')" || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8088"]
