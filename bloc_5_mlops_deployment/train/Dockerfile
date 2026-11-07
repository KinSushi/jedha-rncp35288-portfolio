# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
# Image d'entraînement du modèle de prix (compétence C5.1 : entraînement conteneurisé, suivi MLflow).
# Construction depuis la racine du dépôt :  docker build -f train/Dockerfile -t getaround-train .
# Les données et la base MLflow ne sont JAMAIS copiées dans l'image : elles sont montées en volume.
# Exemple d'entraînement (depuis la racine du dépôt) :
#   docker run --rm -v "$PWD/data:/home/utilisateur/app/data:ro" \
#     -v "$PWD/models:/home/utilisateur/app/models" -v "$PWD/suivi:/home/utilisateur/suivi" \
#     getaround-train
# data/ : get_around_pricing_project.csv (voir README) ; models/ : fichiers exportés ;
# suivi/ : base MLflow (mlflow.db) et ses artefacts.
FROM python:3.12-slim
RUN useradd -m -u 1000 utilisateur
USER utilisateur
ENV PATH="/home/utilisateur/.local/bin:$PATH" PYTHONUNBUFFERED=1 \
    MLFLOW_TRACKING_URI="sqlite:////home/utilisateur/suivi/mlflow.db"
WORKDIR /home/utilisateur/app
COPY --chown=utilisateur requirements-train.txt .
RUN pip install --no-cache-dir --user -r requirements-train.txt
COPY --chown=utilisateur train/train.py train/
# Dossiers de montage créés au nom de l'utilisateur non root (sinon le volume appartiendrait à root).
RUN mkdir -p data models /home/utilisateur/suivi
# Le dossier courant est celui du suivi : MLflow y écrit ses artefacts (./mlruns).
WORKDIR /home/utilisateur/suivi
CMD ["python", "/home/utilisateur/app/train/train.py"]
