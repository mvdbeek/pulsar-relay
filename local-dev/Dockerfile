FROM python:3.12-slim

WORKDIR /app

# Install only what's needed; the runtime deps all have pre-built wheels.
COPY pyproject.toml README.md /app/
COPY pulsar_relay /app/pulsar_relay
RUN pip install --no-cache-dir -e .

EXPOSE 9000

# --reload watches the bind-mounted source so edits on the host are picked up.
CMD ["uvicorn", "pulsar_relay.main:app", \
     "--host", "0.0.0.0", "--port", "9000", \
     "--reload", "--reload-dir", "/app/pulsar_relay"]
