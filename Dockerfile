# Use official Valkey image as base
FROM valkey/valkey:9.0-trixie

# Install Python and build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    gcc \
    g++ \
    curl \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy application files
COPY pyproject.toml README.md MANIFEST.in ./
COPY pulsar_relay/ ./pulsar_relay/

# Install Python dependencies and application
RUN pip3 install --no-cache-dir --break-system-packages --ignore-installed .

# Copy configuration files. ``.env.example`` is intentionally NOT shipped
# into the image: a real deployment must inject its own secrets via
# environment variables, and the example file used to bake in default
# values that the startup guard now refuses to boot with.
COPY valkey.conf /etc/valkey/valkey.conf

# Create a dedicated unprivileged user for the relay process and ensure the
# code directory is owned by it. Valkey continues to run as the existing
# ``valkey`` user provided by the base image.
RUN useradd -r -s /sbin/nologin -d /app relay && \
    chown -R relay:relay /app

# Create supervisor and log directories
RUN mkdir -p /etc/supervisor/conf.d /var/log/valkey /var/log/supervisor /var/log/relay && \
    chown -R valkey:valkey /var/log/valkey && \
    chown -R relay:relay /var/log/relay
COPY <<'EOF' /etc/supervisor/conf.d/supervisord.conf
[supervisord]
nodaemon=true
user=root
logfile=/var/log/supervisor/supervisord.log
pidfile=/var/run/supervisord.pid

[program:valkey]
# ``--requirepass`` is templated from the container environment so the
# password never lands in the on-disk config. Supervisord substitutes
# %(ENV_X)s before launching the child; if PULSAR_VALKEY_PASSWORD is
# unset, valkey refuses to start with a clear error.
command=/usr/local/bin/valkey-server /etc/valkey/valkey.conf --requirepass %(ENV_PULSAR_VALKEY_PASSWORD)s
autostart=true
autorestart=true
user=valkey
stdout_logfile=/var/log/valkey/valkey.log
stderr_logfile=/var/log/valkey/valkey-error.log
priority=1

[program:pulsar-relay]
command=/usr/bin/python3 -m uvicorn pulsar_relay.main:app --host 0.0.0.0 --port 8080 --workers 4
directory=/app
autostart=true
autorestart=true
user=relay
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
# Storage backend pinned. All secrets (JWT, bootstrap admin password,
# Valkey password) come from the container env and are inherited by
# supervisord's children — do not list them here.
environment=PULSAR_STORAGE_BACKEND="valkey",PULSAR_VALKEY_HOST="localhost",PULSAR_VALKEY_PORT="6379"
priority=2
EOF

# Expose only the Pulsar Relay API port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Set environment variables
ENV PULSAR_STORAGE_BACKEND=valkey \
    PULSAR_VALKEY_HOST=localhost \
    PULSAR_VALKEY_PORT=6379 \
    PULSAR_LOG_LEVEL=INFO

# Use supervisor to run both services
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
