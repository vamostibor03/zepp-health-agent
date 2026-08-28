# Container image for the WatchData MCP server (streamable-http transport).
# Serves the read-only health tools + ChatGPT-compatible search/fetch.
FROM python:3.11-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code + the committed SQLite history (data/health.db).
COPY . .

# Bind on all interfaces; the platform provides the real port via $PORT.
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DATABASE_PATH=data/health.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# --host/--port fall back to $HOST/$PORT. DNS-rebinding protection is relaxed
# unless MCP_ALLOWED_HOSTS is set (recommended: your public domain).
CMD ["python", "-m", "watchdata.mcp_server", "--http"]
