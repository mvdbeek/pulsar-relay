# Configuration Guide

Pulsar Relay supports multiple configuration methods with a clear precedence hierarchy.

## Configuration Precedence

Configuration values are loaded in the following order (highest to lowest priority):

1. **Environment Variables** (highest priority) - `PULSAR_*` variables
2. **Config File** - `config.toml` or `config.yaml`
3. **Default Values** (lowest priority) - Built-in defaults

This means environment variables will always override config file settings, and config file settings will override defaults.

## Quick Start

### Option 1: Environment Variables (Recommended for Production)

```bash
# Set environment variables
export PULSAR_STORAGE_BACKEND=valkey
export PULSAR_VALKEY_HOST=valkey.example.com
export PULSAR_JWT_SECRET_KEY=your-secure-secret-key

# Start the server (port and workers controlled by uvicorn)
uvicorn app.main:app --host 0.0.0.0 --port 9000 --workers 4
```

### Option 2: .env File (Recommended for Development)

```bash
# Copy the example file
cp .env.example .env

# Edit .env with your settings
nano .env

# Start the server (will automatically load .env, specify port and workers via uvicorn)
uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 4
```

### Option 3: Config File (TOML)

```bash
# Copy the example file
cp config.toml.example config.toml

# Edit config.toml with your settings
nano config.toml

# Start the server (will automatically find config.toml, specify port and workers via uvicorn)
uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 4
```

### Option 4: Config File (YAML)

```bash
# Copy the example file
cp config.yaml.example config.yaml

# Edit config.yaml with your settings
nano config.yaml

# Start the server (will automatically find config.yaml, specify port and workers via uvicorn)
uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 4
```

## Environment Variable Reference

All environment variables are prefixed with `PULSAR_`:

### Server Configuration

- `PULSAR_APP_NAME` - Application name (default: "Pulsar Relay")

**Note:** HTTP port and worker count are controlled by uvicorn command-line arguments:
- `--port 8080` - HTTP server port
- `--workers 4` - Number of worker processes

### Storage Backend

- `PULSAR_STORAGE_BACKEND` - Storage backend: "memory" or "valkey" (default: "memory")

### Valkey Configuration

- `PULSAR_VALKEY_HOST` - Valkey server host (default: "localhost")
- `PULSAR_VALKEY_PORT` - Valkey server port (default: 6379)
- `PULSAR_VALKEY_USE_TLS` - Use TLS for Valkey (default: false)

### Storage Settings

- `PULSAR_PERSISTENT_TIER_RETENTION` - Persistent tier retention in seconds (default: 86400)
- `PULSAR_MAX_MESSAGES_PER_TOPIC` - Maximum messages per topic (default: 1000000)

### Logging

- `PULSAR_LOG_LEVEL` - Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL (default: "INFO")

### Authentication

- `PULSAR_JWT_SECRET_KEY` - JWT secret key for signing tokens (**CHANGE IN PRODUCTION!**)

### Config File Location

- `PULSAR_CONFIG_FILE` - Custom config file path (optional)

## Config File Formats

### TOML Format (config.toml)

```toml
# Server Configuration
app_name = "Pulsar Relay"

# Storage Backend
storage_backend = "valkey"

# Valkey Configuration
valkey_host = "localhost"
valkey_port = 6379
valkey_use_tls = false

# Logging
log_level = "INFO"

# Authentication
jwt_secret_key = "your-secure-secret-key-here"
```

### YAML Format (config.yaml)

```yaml
# Server Configuration
app_name: "Pulsar Relay"

# Storage Backend
storage_backend: "valkey"

# Valkey Configuration
valkey_host: "localhost"
valkey_port: 6379
valkey_use_tls: false

# Logging
log_level: "INFO"

# Authentication
jwt_secret_key: "your-secure-secret-key-here"
```

## Config File Search Locations

The application automatically searches for config files in the following locations:

1. `./config.toml` (current directory)
2. `./config.yaml` (current directory)
3. `./config.yml` (current directory)
4. `/etc/pulsar-relay/config.toml` (system-wide)
5. `/etc/pulsar-relay/config.yaml` (system-wide)

You can also specify a custom location:

```bash
export PULSAR_CONFIG_FILE=/path/to/my-config.toml
```

## Production Deployment

### Security Best Practices

1. **NEVER commit `.env` or `config.toml`/`config.yaml` to git**
   - Only commit `.env.example`, `config.toml.example`, `config.yaml.example`

2. **Always change the JWT secret key in production**
   ```bash
   # Generate a secure key
   python -c "import secrets; print(secrets.token_urlsafe(32))"

   # Set it as environment variable
   export PULSAR_JWT_SECRET_KEY="generated-key-here"
   ```

3. **Use environment variables for secrets**
   - Never store secrets in config files
   - Use environment variables or secret management systems

4. **Enable TLS for Valkey in production**
   ```bash
   export PULSAR_VALKEY_USE_TLS=true
   ```

### Example Production Setup

```bash
# Set all production settings via environment variables
export PULSAR_APP_NAME="Pulsar Relay Production"
export PULSAR_STORAGE_BACKEND=valkey
export PULSAR_VALKEY_HOST=valkey.prod.example.com
export PULSAR_VALKEY_PORT=6379
export PULSAR_VALKEY_USE_TLS=true
export PULSAR_LOG_LEVEL=WARNING
export PULSAR_JWT_SECRET_KEY="${JWT_SECRET}"  # From secret manager

# Start with production settings (specify workers and port via uvicorn/gunicorn)
gunicorn app.main:app -w 8 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8080
```

## Docker Deployment

```bash
# Using environment variables
docker run -d \
  -e PULSAR_STORAGE_BACKEND=valkey \
  -e PULSAR_VALKEY_HOST=valkey \
  -e PULSAR_JWT_SECRET_KEY=secret \
  -p 8080:8080 \
  pulsar-relay \
  uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 4

# Using config file
docker run -d \
  -v /path/to/config.toml:/app/config.toml \
  -p 8080:8080 \
  pulsar-relay

# Using environment file
docker run -d \
  --env-file .env.prod \
  -p 8080:8080 \
  pulsar-relay
```

## Kubernetes Deployment

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: pulsar-relay-config
data:
  config.toml: |
    storage_backend = "valkey"
    valkey_host = "valkey-service"
    log_level = "WARNING"
---
apiVersion: v1
kind: Secret
metadata:
  name: pulsar-relay-secrets
type: Opaque
stringData:
  jwt-secret: "your-secret-key-here"
  valkey-password: "your-valkey-password"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pulsar-relay
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: pulsar-relay
        image: pulsar-relay:latest
        env:
        - name: PULSAR_JWT_SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: pulsar-relay-secrets
              key: jwt-secret
        volumeMounts:
        - name: config
          mountPath: /app/config.toml
          subPath: config.toml
      volumes:
      - name: config
        configMap:
          name: pulsar-relay-config
```

## Validation and Debugging

### Check Current Configuration

The application logs its configuration on startup:

```
INFO:app.config:Configuration loaded successfully
INFO:app.config:  Storage backend: valkey
INFO:app.config:  Log level: INFO
```

### Common Issues

1. **Config file not found**
   - Check file exists in search locations
   - Set `PULSAR_CONFIG_FILE` to specify location

2. **Environment variables not working**
   - Ensure variables are prefixed with `PULSAR_`
   - Check variable names match exactly (case-insensitive)

3. **Values not overriding**
   - Remember precedence: ENV > Config File > Defaults
   - Check environment variables are actually set

4. **JWT secret warning**
   ```
   WARNING: Using default JWT secret key!
   ```
   - Set `PULSAR_JWT_SECRET_KEY` environment variable

## Testing Configuration

```python
from app.config import load_settings

# Test loading from specific file
settings = load_settings(config_path="config.test.toml")

# Check values
print(f"Storage Backend: {settings.storage_backend}")
print(f"Log Level: {settings.log_level}")
print(f"App Name: {settings.app_name}")
```
