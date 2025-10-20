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
COPY pyproject.toml ./
COPY app/ ./app/

# Install Python dependencies and application
RUN pip3 install --no-cache-dir --break-system-packages --ignore-installed .

# Copy configuration files
COPY valkey.conf /etc/valkey/valkey.conf
COPY .env.example /app/.env

# Create supervisor and log directories
RUN mkdir -p /etc/supervisor/conf.d /var/log/valkey /var/log/supervisor && \
    chown -R valkey:valkey /var/log/valkey
COPY <<'EOF' /etc/supervisor/conf.d/supervisord.conf
[supervisord]
nodaemon=true
user=root
logfile=/var/log/supervisor/supervisord.log
pidfile=/var/run/supervisord.pid

[program:valkey]
command=/usr/local/bin/valkey-server /etc/valkey/valkey.conf
autostart=true
autorestart=true
user=valkey
stdout_logfile=/var/log/valkey/valkey.log
stderr_logfile=/var/log/valkey/valkey-error.log
priority=1

[program:pulsar-relay]
command=/usr/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 4
directory=/app
autostart=true
autorestart=true
user=root
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
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
