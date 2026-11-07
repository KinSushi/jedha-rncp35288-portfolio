# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
# Image de l'API de prix - tout hébergeur de conteneurs.
# Construction depuis la racine du dépôt :  docker build -f api/Dockerfile -t getaround-api .
FROM python:3.12-slim
# Utilisateur d'identifiant 1000 : on le crée et on travaille dans son dossier (bonne pratique :
# pas de service exécuté en root).
RUN useradd -m -u 1000 utilisateur
USER utilisateur
ENV PATH="/home/utilisateur/.local/bin:$PATH" PYTHONUNBUFFERED=1 PORT=7860
WORKDIR /home/utilisateur/app
COPY --chown=utilisateur requirements-api.txt .
RUN pip install --no-cache-dir --user -r requirements-api.txt
COPY --chown=utilisateur api/ api/
COPY --chown=utilisateur models/ models/
EXPOSE 7860
# --proxy-headers : derrière un mandataire HTTPS, les exemples de /docs gardent https.
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]
