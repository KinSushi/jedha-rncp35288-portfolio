# Copyright © 2026 · voir le fichier LICENSE du dépôt
#
# Image du service de démonstration (Python). Principes :
# - version de Python paramétrable (3.12 par défaut, celle des règles du dépôt) ;
# - dépendances installées avant le code : la couche reste en cache tant que requirements.txt
#   ne change pas ;
# - exécution par un utilisateur sans privilège, système de fichiers en lecture seule (voir
#   docker-compose.yml) ;
# - l'extension « sqlite » de DuckDB est téléchargée à la construction, pas à l'exécution : le
#   conteneur n'a besoin d'aucun accès réseau pour lancer la démonstration autonome.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appli
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY stripe_demo ./stripe_demo
COPY sql ./sql
COPY nosql ./nosql

USER appli
RUN python -c "import duckdb; duckdb.sql('INSTALL sqlite')"

CMD ["python", "-m", "stripe_demo.pipeline"]
