# Copyright © 2026 · voir le fichier LICENSE du dépôt
# Image applicative : producteur, consommateur, entraînement initial.
# Construction : docker compose build (contexte = dossier fraude/).

FROM python:3.12-slim

ENV GIT_PYTHON_REFRESH=quiet \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    FRAUDE_ENV=prod \
    FRAUDE_DONNEES=/donnees \
    FRAUDE_CONFIG_DIR=/app/config

WORKDIR /app

# 1. Dépendances d'abord : cette couche ne change que si les fichiers de dépendances changent.
#    Mêmes contraintes que l'image Airflow (numpy, pandas, SQLAlchemy…) plus nos versions exactes :
#    le modèle entraîné par Airflow est chargé ici avec exactement les mêmes bibliothèques.
COPY requirements.txt requirements-prod.txt docker/contraintes-ml.txt ./
RUN pip install \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-2.10.5/constraints-3.12.txt" \
    --constraint contraintes-ml.txt \
    -r requirements-prod.txt

# 2. Code et configuration (aucun secret : ils arrivent par /run/secrets à l'exécution).
COPY detection_fraude ./detection_fraude
COPY config ./config

# 3. Utilisateur non privilégié (uid 50000, comme l'image Airflow) et dossier de données partagé.
RUN useradd --uid 50000 --gid 0 --no-create-home --shell /usr/sbin/nologin fraude \
    && mkdir -p /donnees \
    && chown -R 50000:0 /donnees \
    && chmod -R g=u /donnees
USER 50000

VOLUME ["/donnees"]
CMD ["python", "-m", "detection_fraude.consommateur"]
