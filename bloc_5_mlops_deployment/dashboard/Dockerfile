# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
# Image du tableau de bord.
# Construction depuis la racine du dépôt :  docker build -f dashboard/Dockerfile -t getaround-dashboard .
# Variable à définir au lancement du conteneur : GETAROUND_API_URL (adresse publique de l'API).
FROM python:3.12-slim
RUN useradd -m -u 1000 utilisateur
USER utilisateur
ENV PATH="/home/utilisateur/.local/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /home/utilisateur/app
COPY --chown=utilisateur requirements-dashboard.txt .
RUN pip install --no-cache-dir --user -r requirements-dashboard.txt
COPY --chown=utilisateur retards.py .
COPY --chown=utilisateur dashboard/ dashboard/
# Le classeur de l'énoncé n'est pas copié dans l'image : le tableau de bord le télécharge au
# démarrage depuis l'adresse officielle (voir dashboard/app.py).
# Marques, couleurs, carburants et carrosseries du formulaire de prix : listes lues dans
# models/categories.json (versionné), pas recopiées à la main dans le code.
COPY --chown=utilisateur models/categories.json models/
EXPOSE 7860
CMD ["streamlit", "run", "dashboard/app.py", "--server.port=7860", "--server.address=0.0.0.0", "--browser.gatherUsageStats=false"]
