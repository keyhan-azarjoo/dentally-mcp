FROM python:3.12-slim

WORKDIR /app

# Dependencies in their own layer so a code change does not reinstall the world.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    DENTALLY_MCP_TRANSPORT=http \
    DENTALLY_MCP_HOST=0.0.0.0 \
    DENTALLY_MCP_PORT=8092

# Never run as root: this process holds credentials for entire patient databases.
RUN useradd --create-home --uid 10001 dentally \
    && mkdir -p /data && chown -R dentally:dentally /data /app
USER dentally

# Token store and audit log go in a volume, not the image layer.
VOLUME ["/data"]
ENV DENTALLY_TOKEN_STORE=/data/tokens.enc \
    DENTALLY_AUDIT_LOG=/data/audit.log

EXPOSE 8092

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8092/healthz', timeout=4).status==200 else 1)"

# The server refuses to start on this bind without DENTALLY_MCP_AUTH_TOKEN set.
CMD ["python", "-m", "dentally_mcp", "--http"]
