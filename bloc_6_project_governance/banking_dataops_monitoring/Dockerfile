# Image of the `app` service of docker-compose.yml (ADR-0005). A single stage: one FROM, no
# multi-stage build. It holds the Python dependencies and the banking_dataops package; the work
# itself is done by docker/entrypoint.sh, which runs the pipeline and then serves the API (8000)
# and the Streamlit dashboard (8501).
#
# docker-compose.yml bind-mounts the checkout over /app, so the code copied below is hidden when
# the stack is started through compose.

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copied and installed before the rest of the code: this layer is rebuilt only when
# requirements.txt changes. The versions there are lower bounds (>=), so a rebuild can pull newer
# releases: build days ahead, never on presentation day (scripts/preflight.sh).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Editable: banking_dataops is imported from /app/src rather than copied into site-packages. With
# the bind mount of docker-compose.yml, that is the host's src/, so edits need no rebuild.
RUN pip install -e .

# Informative only: the ports actually published are set in docker-compose.yml.
EXPOSE 8000 8501

# Run through `sh`, so the script needs no executable bit (git stores it as 100644).
CMD ["sh", "docker/entrypoint.sh"]
