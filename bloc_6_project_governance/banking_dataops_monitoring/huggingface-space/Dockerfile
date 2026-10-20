# Space Hugging Face — tableau de bord live, lecture seule.
# Image minimale volontairement : moins de surface = moins de risque de build
# cassé un jour de démonstration publique.

FROM python:3.12-slim

# UID 1000 : convention Hugging Face Spaces pour l'utilisateur non-root.
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY --chown=user app.py .

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 7860

CMD ["streamlit", "run", "app.py", "--server.port=7860", "--server.address=0.0.0.0"]
