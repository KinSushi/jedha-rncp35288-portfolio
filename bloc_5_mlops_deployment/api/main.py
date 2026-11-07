# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""API de prix conseillé Getaround (certification RNCP35288, bloc 5 - industrialisation).

Ce que l'énoncé exige, et où c'est dans ce fichier
--------------------------------------------------
- au moins un endpoint **/predict** : POST d'un JSON ``{"input": [[...], ...]}`` qui renvoie
  ``{"prediction": [...]}`` → fonction ``predire`` ;
- une documentation à **/docs** avec un titre h1 et, pour chaque endpoint, son nom, sa méthode
  HTTP, l'entrée attendue et la sortie (avec exemple) → fonction ``documentation`` ;
- l'API en ligne (port 7860 dans le conteneur) → ``Dockerfile`` et bloc final.

Ce que le référentiel exige en plus (C5.1, normalisation via MLflow) : si ``MLFLOW_TRACKING_URI``
est défini (environnement local de ``requirements.txt``, où MLflow est installé), le modèle servi
est la version du registre MLflow portant l'alias ``champion`` ; sinon, c'est l'export
``models/model.joblib`` du même champion. L'image Docker et la production servent cet export :
MLflow n'est pas installé dans l'image.

Format d'entrée : chaque ligne de ``input`` décrit une voiture, avec 13 valeurs dans l'ordre de
``models/colonnes.json`` (rappelé sur /docs). En bonus de l'énoncé (« error handling »), l'API
accepte aussi des objets nommés (``{"model_key": "Renault", ...}``).

Validation de l'entrée (aucune entrée mal typée ne doit produire une erreur interne 500)
---------------------------------------------------------------------------------------
Le modèle est un pipeline scikit-learn : il accepte n'importe quel texte pour une catégorie
(une catégorie inconnue est encodée comme « neutre ») et plante, ou pire répond sans prévenir,
sur un type inattendu. L'API vérifie donc chaque colonne AVANT d'appeler le modèle :

- booléens : vrais booléens JSON (``true``/``false``), ni ``"true"``, ni ``"oui"``, ni 0/1 ;
- catégories : du texte (une marque numérique est refusée) ;
- ``fuel`` et ``car_type`` : valeur parmi celles de ``models/categories.json``, sinon erreur 422
  qui rappelle la liste admise (un carburant inventé fausserait la prédiction) ;
- ``model_key`` et ``paint_color`` : une valeur inconnue est ACCEPTÉE (le parc évolue : une
  marque rare absente de l'entraînement reste une voiture réelle), et la prédiction se fait
  alors sans cette information ; la réponse n'a qu'une clé, ``prediction`` ;
- kilométrage de 0 à 1 500 000 et puissance de 1 à 500 ch : bornes métier larges (le jeu
  d'entraînement va jusqu'à 1 000 376 km et 423 ch) qui écartent les valeurs impossibles ;
- au plus 1 000 voitures par requête (erreur 413 au-delà) ;
- un texte contenant un caractère Unicode isolé (``"\\ud800"``, une moitié de paire de
  substituts) est un JSON valide mais ne s'encode pas en UTF-8 : il est refusé en 422, car il
  ferait échouer l'écriture de la réponse elle-même.

Format d'erreur unique, quelle que soit l'origine : ``{"error": "<texte>"}``, complété d'une
liste ``details`` quand plusieurs problèmes sont signalés (ligne, colonne, message). Les textes
de l'API sont en français, y compris pour un corps de requête illisible (422) ou un chemin
inconnu (404).

CHOIX
-----
- Retenu : une validation écrite à la main, colonne par colonne, AVANT d'appeler le modèle, avec
  tous les problèmes rassemblés dans une seule réponse. Le client corrige tout en un aller-retour.
- Écarté : décrire l'entrée par un modèle pydantic. Une voiture est une liste de 13 valeurs
  positionnelles ou un objet nommé (deux formes), et pydantic répondrait en anglais, au format
  ``{"detail": [...]}`` éclaté ligne par ligne : ce n'est pas le format unique de l'API.
- Écarté : laisser le pipeline scikit-learn juger l'entrée. Il accepte n'importe quel texte pour
  une catégorie et plante, ou répond sans prévenir, sur un type inattendu (une marque numérique).
- Retenu : carburant et carrosserie inconnus refusés (422), marque et couleur inconnues acceptées
  sans autre signalement : un carburant inventé n'a aucun sens, une marque rare est bien réelle.
- Retenu : démarrer sans modèle (``/health`` et ``/docs`` répondent, ``/predict`` renvoie 503).
  Écarté : planter au démarrage, qui empêcherait même d'afficher la documentation.
- Écarté : la documentation interactive générée par FastAPI : l'énoncé exige une page propre à
  ``/docs``, et le schéma d'erreur 422 de cette documentation contredit le format réel de l'API.
  Documentation générée, ReDoc et schéma OpenAPI sont désactivés.
- Retenu : 422 pour un corps non décodable, comme pour un JSON illisible (annoncé par ``/docs``) ;
  FastAPI répondrait 400 en anglais.

OÙ
---
- Racine ``api/`` du dépôt. Lancé par uvicorn (``api/Dockerfile`` : ``uvicorn api.main:app``) ou
  par ``python api/main.py``.
- Appelant en production : dashboard/app.py, qui envoie un POST sur ``/predict``.
- Dépendances : FastAPI, pandas, joblib ; MLflow seulement si ``MLFLOW_TRACKING_URI`` est défini
  (import tardif). Fichiers lus au chargement du module : models/colonnes.json,
  models/exemple.json, models/categories.json ; au démarrage : models/model.joblib.
  Ces fichiers sont écrits ou contrôlés par train/train.py.

COMMENT
-------
1. Au chargement du module, les trois fichiers JSON de ``models/`` sont lus et recoupés : si
   ``categories.json`` et ``colonnes.json`` ne décrivent pas les mêmes colonnes, le module refuse
   de démarrer (``RuntimeError``) plutôt que de répondre faux plus tard.
2. Au démarrage du serveur (``cycle_de_vie``), ``charger_modele`` lit le registre MLflow (alias
   ``champion``) ou le fichier ``model.joblib``.
3. ``/predict`` : ``vers_tableau`` appelle ``valider_ligne`` pour chaque voiture, qui appelle
   ``verifier_valeur`` pour chaque colonne ; les problèmes sont rassemblés (20 détaillés au plus),
   ``ErreurEntree`` porte le code HTTP (422, ou 413 au-delà de 1 000 voitures). Puis
   ``calculer_prix`` interroge le modèle. Une panne du modèle donne 500 sans fuite d'information.
4. Les gestionnaires d'exceptions ramènent tout au format ``{"error": ..., "details": [...]}``.

POURQUOI
--------
- Contrat de l'énoncé (POST ``/predict`` en ``{"input": [[...]]}`` vers ``{"prediction": [...]}``,
  documentation à ``/docs`` avec un titre h1) : énoncé du projet ; résumé dans README.md.
- Les 4 843 voitures, 3 874 en apprentissage et 969 en test (graine 42) : issues de
  data/get_around_pricing_project.csv, séparées par train/train.py.
- Erreur absolue moyenne de 10,5 € affichée par ``/docs`` : colonne ``mae_test`` du gradient
  boosting dans models/comparaison_modeles.csv.

JOURNALISATION
--------------
Logger ``journal`` (``logging.getLogger(__name__)``). INFO : source du modèle chargé. WARNING :
aucun modèle trouvé au démarrage ; entrée refusée (code HTTP et nombre de problèmes, jamais le
contenu de la requête). ERROR avec trace : exception imprévue d'un gestionnaire général, échec
imprévu de la validation, panne du modèle, échec du calcul de l'exemple de ``/docs``. La
réponse au client ne contient jamais le détail interne. Aucun ``print`` : c'est une bibliothèque
de service.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as ErreurHTTP

RACINE = Path(__file__).resolve().parent.parent  # racine du dépôt (ou de l'image Docker)
DOSSIER_MODELE = Path(os.getenv("MODEL_DIR", RACINE / "models"))
NOM_REGISTRE = os.getenv("MODEL_NAME", "getaround-pricing")
ALIAS = os.getenv("MODEL_ALIAS", "champion")
URI_MLFLOW = os.getenv("MLFLOW_TRACKING_URI")

COLONNES: list[str] = json.loads((DOSSIER_MODELE / "colonnes.json").read_text(encoding="utf-8"))
EXEMPLE: dict[str, Any] = json.loads((DOSSIER_MODELE / "exemple.json").read_text(encoding="utf-8"))
# Valeurs vues à l'entraînement, par colonne catégorielle. Le fichier est versionné avec le
# dépôt. Les clés qui commencent par « _ »
# (copyright, source) sont des métadonnées, pas des colonnes.
CATEGORIES: dict[str, list[str]] = {
    colonne: valeurs
    for colonne, valeurs in json.loads(
        (DOSSIER_MODELE / "categories.json").read_text(encoding="utf-8")
    ).items()
    if not colonne.startswith("_")
}

# Colonnes dont une valeur inconnue est refusée (422) : un carburant ou une carrosserie
# inventés n'ont aucun sens métier et fausseraient la prédiction sans que personne s'en aperçoive.
CATEGORIES_STRICTES = ("fuel", "car_type")
# Colonnes dont une valeur inconnue est acceptée : le parc évolue.
CATEGORIES_SOUPLES = ("model_key", "paint_color")
# Bornes métier (incluses). Larges à dessein : elles ne servent qu'à écarter l'impossible.
BORNES = {"mileage": (0, 1_500_000), "engine_power": (1, 500)}
COLONNES_BOOLEENNES = [c for c in COLONNES if c not in CATEGORIES and c not in BORNES]
# Au-delà, la requête est refusée : on borne le travail d'un seul appel (413).
MAX_LIGNES = 1000
# Nombre maximal de problèmes détaillés dans une réponse d'erreur (la réponse reste lisible).
MAX_PROBLEMES = 20

# Effectifs de l'entraînement (train/train.py) : 4 843 voitures dans le fichier, séparées en
# 80 % pour ajuster le modèle exporté et 20 % pour l'évaluer, graine 42. Les trois nombres sont
# issus de ce fichier de données.
N_VOITURES, N_APPRENTISSAGE, N_TEST = 4843, 3874, 969

# Contrôle de cohérence au chargement du module : si models/ était régénéré avec d'autres
# colonnes, mieux vaut échouer ici, avec un message clair, que répondre faux plus tard.
_attendues = set(CATEGORIES) | set(BORNES)
if not _attendues <= set(COLONNES) or set(CATEGORIES_STRICTES + CATEGORIES_SOUPLES) != set(
    CATEGORIES
):
    raise RuntimeError(
        "models/categories.json et models/colonnes.json ne décrivent pas les mêmes colonnes "
        f"(catégories : {sorted(CATEGORIES)} ; colonnes : {COLONNES})"
    )

journal = logging.getLogger(__name__)

# Le modèle reste None tant qu'il n'est pas chargé : l'API démarre quand même (la documentation
# et /health répondent) et /predict renvoie 503. Une API qui plante au démarrage faute de modèle
# ne peut même pas afficher sa documentation.
etat: dict[str, Any] = {"modele": None, "source": None}


def charger_modele() -> None:
    """Charge le champion : depuis le registre MLflow si configuré, sinon depuis le fichier."""
    if URI_MLFLOW:
        import mlflow  # import tardif : MLflow est lourd et inutile en mode fichier

        mlflow.set_tracking_uri(URI_MLFLOW)
        etat["modele"] = mlflow.sklearn.load_model(f"models:/{NOM_REGISTRE}@{ALIAS}")
        etat["source"] = f"registre MLflow : {NOM_REGISTRE}@{ALIAS}"
    elif (DOSSIER_MODELE / "model.joblib").exists():
        import joblib

        etat["modele"] = joblib.load(DOSSIER_MODELE / "model.joblib")
        etat["source"] = "fichier models/model.joblib"
    else:
        journal.warning("aucun modèle chargé : ni registre MLflow, ni %s", DOSSIER_MODELE)
        return
    journal.info("modèle chargé : %s", etat["source"])


@asynccontextmanager
async def cycle_de_vie(_application: FastAPI) -> AsyncIterator[None]:
    """Au démarrage du serveur : chargement du modèle (forme « lifespan » de FastAPI)."""
    charger_modele()
    yield


# Documentation générée, ReDoc et schéma OpenAPI sont désactivés : par défaut, FastAPI publierait
# sa page interactive à /docs, ce qui masquerait la page exigée par l'énoncé, et son schéma
# d'erreur 422 n'est pas le format réel de l'API. La page /docs est écrite à la main
# (``documentation``).
app = FastAPI(
    title="Getaround - API de prix conseillé",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=cycle_de_vie,
)


# --------------------------------------------------------------------------------------------
# Erreurs : un seul format, ``{"error": "...", "details": [...]}``, pour tous les codes d'erreur
# --------------------------------------------------------------------------------------------
def encodable(contenu: Any) -> Any:
    """Copie du contenu où tout caractère Unicode isolé est remplacé par son écriture ``\\udXXX``.

    Un JSON peut contenir ``"\\ud800"`` : Python l'accepte en lecture (c'est une chaîne valide),
    mais ne sait plus l'écrire en UTF-8. Sans ce nettoyage, citer une telle valeur dans un message
    d'erreur ferait échouer l'écriture de la réponse, donc une erreur 500 en texte brut.
    """
    if isinstance(contenu, str):
        return contenu.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(contenu, list):
        return [encodable(element) for element in contenu]
    if isinstance(contenu, dict):
        return {encodable(cle): encodable(valeur) for cle, valeur in contenu.items()}
    return contenu


def reponse_erreur(
    statut: int, message: str, details: list[str] | None = None
) -> JSONResponse:
    """Réponse d'erreur au format unique de l'API (``details`` seulement s'il y en a)."""
    corps: dict[str, Any] = {"error": message}
    if details:
        corps["details"] = details
    return JSONResponse(status_code=statut, content=encodable(corps))


FORME_ATTENDUE = 'il doit être un objet JSON de la forme {"input": [[...]]}'
# Traduction des codes d'erreur de pydantic pour le corps de la requête.
DETAILS_CORPS = {
    "json_invalid": "JSON illisible (erreur de syntaxe)",
    "dict_type": "le corps doit être un objet JSON (pas une liste, un nombre ou un texte)",
    # « missing » : corps absent, vide ou valant null (aucun objet JSON exploitable).
    "missing": "le corps de la requête est vide ou vaut null",
}


@app.exception_handler(RequestValidationError)
async def corps_invalide(_requete: Request, erreur: RequestValidationError) -> JSONResponse:
    """JSON illisible ou corps qui n'est pas un objet : sans ce gestionnaire, FastAPI répondrait
    au format ``{"detail": [...]}``, différent de celui de tout le reste de l'API."""
    # Les messages de pydantic sont en anglais : on traduit les cas connus par leur code.
    details = list(
        dict.fromkeys(
            DETAILS_CORPS.get(e.get("type"), "corps de requête non conforme")
            for e in erreur.errors()
        )
    )
    return reponse_erreur(422, f"corps de requête invalide : {FORME_ATTENDUE}", details)


# Messages français des erreurs HTTP que FastAPI produit lui-même, en anglais.
MESSAGES_HTTP = {404: "chemin inconnu", 405: "méthode non autorisée"}


@app.exception_handler(ErreurHTTP)
async def erreur_http(_requete: Request, erreur: ErreurHTTP) -> JSONResponse:
    """Chemin inconnu (404), méthode interdite (405), corps illisible (400)… : même format que
    les autres erreurs, et en français."""
    if erreur.status_code == 400:
        # FastAPI répond 400 « There was an error parsing the body » quand le corps n'est pas
        # décodable (octets qui ne sont pas de l'UTF-8, par exemple). C'est, comme un JSON
        # illisible, une entrée mal formée : 422, comme l'annonce /docs.
        return reponse_erreur(
            422,
            f"corps de requête illisible : {FORME_ATTENDUE}",
            ["le corps n'a pas pu être décodé (JSON en UTF-8 attendu)"],
        )
    message = MESSAGES_HTTP.get(erreur.status_code, str(erreur.detail))
    return reponse_erreur(erreur.status_code, message)


@app.exception_handler(Exception)
async def erreur_inattendue(_requete: Request, erreur: Exception) -> JSONResponse:
    """Dernier filet, pour tout ce qu'aucune autre règle n'a prévu : réponse JSON au format
    commun plutôt que le texte brut « Internal Server Error ». Le détail reste dans le journal."""
    journal.error("erreur inattendue : %r", erreur, exc_info=erreur)
    return reponse_erreur(500, "erreur interne du serveur")


class ErreurEntree(Exception):
    """Entrée refusée : porte le code HTTP, un résumé et la liste détaillée des problèmes."""

    def __init__(self, statut: int, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.statut, self.message, self.details = statut, message, details or []


# --------------------------------------------------------------------------------------------
# Validation colonne par colonne
# --------------------------------------------------------------------------------------------
def nombre(n: int) -> str:
    """Entier avec espace comme séparateur de milliers (1 500 000), à la française."""
    return f"{n:,}".replace(",", " ")


LIMITE_CITATION = 40  # longueur maximale d'une valeur reçue citée dans un message


def abreger(texte: str) -> str:
    """Texte raccourci à ``LIMITE_CITATION`` caractères : une valeur démesurée envoyée par un
    client ne doit pas gonfler le message d'erreur qui la cite."""
    if len(texte) <= LIMITE_CITATION:
        return texte
    return texte[: LIMITE_CITATION - 3] + "..."


def montrer(valeur: Any) -> str:
    """Valeur reçue, raccourcie, pour la citer dans un message d'erreur.

    Un caractère Unicode isolé (``"\\ud800"``), valide en JSON, n'est pas écrivable en UTF-8 : il
    est remplacé par son écriture ``\\ud800`` pour que le message reste encodable.
    """
    texte = json.dumps(valeur, ensure_ascii=False, default=str)
    return abreger(texte.encode("utf-8", "backslashreplace").decode("utf-8"))


def verifier_valeur(colonne: str, valeur: Any) -> Any:
    """Contrôle une valeur ; renvoie la valeur propre ou lève ValueError (message en français)."""
    if colonne in BORNES:
        mini, maxi = BORNES[colonne]
        # bool est une sous-classe de int en Python : sans ce test, ``true`` passerait pour 1.
        if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
            raise ValueError(f"doit être un nombre (reçu {montrer(valeur)})")
        if isinstance(valeur, float) and not math.isfinite(valeur):
            raise ValueError(f"doit être un nombre fini (reçu {montrer(valeur)})")
        if not mini <= valeur <= maxi:
            raise ValueError(
                f"doit être compris entre {nombre(mini)} et {nombre(maxi)} "
                f"(reçu {montrer(valeur)})"
            )
        return float(valeur)
    if colonne in CATEGORIES:
        if not isinstance(valeur, str) or not valeur.strip():
            raise ValueError(f"doit être un texte non vide (reçu {montrer(valeur)})")
        try:
            valeur.encode("utf-8")
        except UnicodeEncodeError:
            # Caractère Unicode isolé : JSON valide, mais impossible à écrire en UTF-8, donc à
            # citer dans la réponse ou à confier à pandas. Refusé ici, proprement.
            raise ValueError("texte invalide : caractère Unicode isolé") from None
        if colonne in CATEGORIES_STRICTES and valeur not in CATEGORIES[colonne]:
            raise ValueError(
                f"valeur inconnue {montrer(valeur)} ; valeurs admises : "
                + ", ".join(CATEGORIES[colonne])
            )
        return valeur
    # Dernier cas : les sept colonnes booléennes. Strict : « true », « oui » ou 1 sont refusés.
    if not isinstance(valeur, bool):
        raise ValueError(f"doit être un booléen JSON, true ou false (reçu {montrer(valeur)})")
    return valeur


# Un problème : {"ligne": n, "colonne": nom (facultatif), "message": texte}.
Probleme = dict[str, Any]


def valider_ligne(numero: int, ligne: Any) -> tuple[list[Any], list[Probleme]]:
    """Valide une voiture (liste de 13 valeurs ou objet nommé).

    Renvoie ``(valeurs dans l'ordre des colonnes, problèmes)`` : ``problemes`` est la liste des
    erreurs (vide si la ligne est valide).
    """
    if isinstance(ligne, dict):
        manquantes = [c for c in COLONNES if c not in ligne]
        if manquantes:
            message = f"colonnes manquantes : {manquantes}"
            return [], [{"ligne": numero, "message": message}]
        valeurs = [ligne[c] for c in COLONNES]
    elif isinstance(ligne, list) and len(ligne) == len(COLONNES):
        valeurs = ligne
    else:
        message = (
            f"doit contenir {len(COLONNES)} valeurs dans l'ordre {COLONNES}, ou être un objet "
            "nommé portant ces colonnes"
        )
        return [], [{"ligne": numero, "message": message}]
    propres, problemes = [], []
    for position, colonne in enumerate(COLONNES):
        valeur = valeurs[position]
        try:
            propre = verifier_valeur(colonne, valeur)
        except ValueError as erreur:
            problemes.append({"ligne": numero, "colonne": colonne, "message": str(erreur)})
            continue
        propres.append(propre)
    return propres, problemes


def vers_tableau(charge: dict[str, Any]) -> pd.DataFrame:
    """Transforme le corps de la requête en tableau typé comme à l'entraînement.

    Renvoie le tableau ou lève ``ErreurEntree`` (422, ou 413 si la requête
    contient trop de voitures). Tous les problèmes sont rassemblés avant de répondre : le
    client corrige tout en une fois au lieu de découvrir les erreurs une par une.
    """
    lignes = charge.get("input")
    if not isinstance(lignes, list) or not lignes:
        raise ErreurEntree(422, "le champ « input » doit être une liste non vide de voitures")
    if len(lignes) > MAX_LIGNES:
        raise ErreurEntree(
            413,
            f"trop de voitures dans la requête : {nombre(len(lignes))} reçues, "
            f"{nombre(MAX_LIGNES)} au plus ; découpez la requête en plusieurs appels",
        )
    valeurs_lignes, problemes = [], []
    for numero, ligne in enumerate(lignes):
        valeurs, erreurs = valider_ligne(numero, ligne)
        valeurs_lignes.append(valeurs)
        problemes.extend(erreurs)
    if problemes:
        details = [
            f"ligne {p['ligne']}"
            + (f", colonne « {p['colonne']} »" if "colonne" in p else "")
            + f" : {p['message']}"
            for p in problemes
        ]
        # Le résumé cite le premier problème : un client qui ne lit que « error » (cas courant)
        # voit déjà de quoi corriger, par exemple la liste des carburants admis.
        resume = f"entrée invalide : {details[0]}"
        if len(details) > 1:
            resume += f" (et {len(details) - 1} autre(s) problème(s), voir « details »)"
        reste = len(details) - MAX_PROBLEMES
        if reste > 0:
            details = details[:MAX_PROBLEMES] + [f"... et {reste} autre(s) problème(s)"]
        raise ErreurEntree(422, resume, details)
    tableau = pd.DataFrame(valeurs_lignes, columns=COLONNES)
    # Mêmes types qu'à l'entraînement (train/train.py) : nombres décimaux (déjà le cas, car
    # ``verifier_valeur`` rend des ``float``), booléens en 0/1.
    tableau[COLONNES_BOOLEENNES] = tableau[COLONNES_BOOLEENNES].astype(int)
    return tableau


def calculer_prix(tableau: pd.DataFrame) -> list[float]:
    """Prix conseillé de chaque ligne (euros par jour, arrondis au centime)."""
    prix = [round(float(p), 2) for p in etat["modele"].predict(tableau)]
    if not all(math.isfinite(p) for p in prix):  # un modèle qui répond NaN est en panne
        raise ValueError("le modèle a renvoyé une valeur non finie")
    return prix


@app.get("/health")
def sante() -> JSONResponse:
    """État de l'API : 200 si un modèle est chargé (et sa source), 503 sinon."""
    if etat["modele"] is None:
        return JSONResponse(
            status_code=503, content={"status": "indisponible", "model_loaded": False}
        )
    return JSONResponse(content={"status": "ok", "model_loaded": True, "source": etat["source"]})


@app.post("/predict")
def predire(charge: dict[str, Any]) -> JSONResponse:
    """Prix de location conseillé (euros par jour) pour chaque voiture de ``charge["input"]``."""
    if etat["modele"] is None:
        return reponse_erreur(503, "aucun modèle chargé")
    try:
        tableau = vers_tableau(charge)
    except ErreurEntree as erreur:
        journal.warning(
            "entrée refusée : code %s, %s problème(s) détaillé(s)",
            erreur.statut,
            len(erreur.details),
        )
        return reponse_erreur(erreur.statut, erreur.message, erreur.details)
    except Exception:
        # Filet de sécurité : la validation prévoit chaque cas connu ; si un cas imprévu la fait
        # échouer, mieux vaut une réponse JSON au format commun qu'un texte brut.
        journal.exception("échec imprévu de la validation de l'entrée")
        return reponse_erreur(500, "erreur interne : la validation de l'entrée a échoué")
    # Dernier filet : l'entrée est valide, donc une exception ici est une panne du modèle, pas
    # une faute du client. On la journalise (traceback complet côté serveur) et on répond 500 au
    # format commun, sans exposer les détails internes.
    try:
        prix = calculer_prix(tableau)
    except Exception:
        journal.exception("échec de la prédiction")
        return reponse_erreur(500, "erreur interne : la prédiction a échoué")
    return JSONResponse(content=encodable({"prediction": prix}))


# --------------------------------------------------------------------------------------------
# Page de documentation /docs
# --------------------------------------------------------------------------------------------
DESCRIPTIONS = {
    "model_key": "marque (texte, ex. « Renault », « Citroën »)",
    "mileage": "kilométrage (nombre)",
    "engine_power": "puissance du moteur en chevaux (nombre)",
    "fuel": "carburant",
    "paint_color": "couleur (texte, ex. « black », « grey »)",
    "car_type": "carrosserie",
    "private_parking_available": "place de parking privée",
    "has_gps": "GPS",
    "has_air_conditioning": "climatisation",
    "automatic_car": "boîte automatique",
    "has_getaround_connect": "équipée Getaround Connect",
    "has_speed_regulator": "régulateur de vitesse",
    "winter_tires": "pneus hiver",
}


def contrainte(colonne: str) -> str:
    """Valeurs admises pour une colonne, en texte (mêmes règles que la validation)."""
    if colonne in BORNES:
        mini, maxi = BORNES[colonne]
        return f"nombre de {nombre(mini)} à {nombre(maxi)}"
    if colonne in CATEGORIES_STRICTES:
        return "texte, parmi : " + ", ".join(CATEGORIES[colonne])
    if colonne in CATEGORIES_SOUPLES:
        return (
            "texte ; connues : " + ", ".join(CATEGORIES[colonne])
            + " (une valeur inconnue est acceptée : la prédiction se fait sans cette information)"
        )
    return "booléen JSON : true ou false (ni « true » entre guillemets, ni 0/1)"


def prediction_de_l_exemple() -> str:
    """Résultat réel de l'appel d'exemple, pour l'afficher à l'identique dans la documentation.

    Calculé avec le modèle chargé : la page ne peut pas afficher un nombre périmé. Sans modèle,
    on le dit plutôt que d'inventer une valeur.
    """
    if etat["modele"] is None:
        return "(modèle non chargé : exemple de réponse indisponible)"
    try:
        tableau = vers_tableau({"input": EXEMPLE["input"][:1]})
        return repr({"prediction": calculer_prix(tableau)})
    except Exception:  # la page de documentation doit s'afficher même si l'exemple échoue
        journal.exception("échec du calcul de l'exemple de la documentation")
        return "(exemple de réponse indisponible)"


def page_documentation(url_de_base: str = "http://localhost:7860") -> str:
    """Page HTML de /docs : titre h1, chaque endpoint (nom, méthode, entrée, sortie, exemple)."""
    ligne_exemple = EXEMPLE["input"][0]
    exemple = json.dumps({"input": [ligne_exemple]}, ensure_ascii=False)
    # Exemple Python : ``repr`` écrit des littéraux Python (True/False), alors que ``json.dumps``
    # écrirait true/false, noms indéfinis en Python. L'exemple se copie donc tel quel et s'exécute.
    exemple_python = (
        "import requests\n\n"
        '# Adresse de l\'API : à remplacer (en local : "http://localhost:7860").\n'
        f'URL = "{url_de_base}"\n'
        f"voiture = {ligne_exemple!r}\n"
        'reponse = requests.post(f"{URL}/predict", json={"input": [voiture]}, timeout=20)\n'
        f"print(reponse.status_code, reponse.json())   # 200 {prediction_de_l_exemple()}"
    )
    lignes_colonnes = "".join(
        f"<tr><td>{i + 1}</td><td><code>{escape(c)}</code></td>"
        f"<td>{escape(DESCRIPTIONS[c])}</td><td>{escape(contrainte(c))}</td></tr>"
        for i, c in enumerate(COLONNES)
    )
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Getaround - documentation de l'API</title>
<style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;
line-height:1.5}}code,pre{{background:#f3f3f3;padding:.15rem .3rem;border-radius:4px}}
pre{{padding:.8rem;white-space:pre-wrap;overflow-wrap:anywhere}}table{{border-collapse:collapse}}
td,th{{border:1px solid #ccc;padding:.3rem .6rem;vertical-align:top}}</style></head>
<body>
<h1>Getaround - API de prix conseillé</h1>
<p>Cette API recommande un <strong>prix de location par jour</strong> (en euros) pour une
voiture, à partir de ses caractéristiques. Modèle : gradient boosting ajusté sur
{nombre(N_APPRENTISSAGE)} voitures (80 % des {nombre(N_VOITURES)} du jeu de données), puis évalué
sur les {nombre(N_TEST)} autres, jamais vues à l'entraînement : erreur absolue moyenne d'environ
10,5 € sur ces voitures. Le modèle est suivi et versionné avec MLflow. Toutes les réponses sont
en JSON.</p>

<h2>POST /predict</h2>
<ul>
<li><strong>Rôle</strong> : prédire le prix conseillé de chaque voiture décrite.</li>
<li><strong>Méthode HTTP</strong> : POST, corps JSON
(<code>Content-Type: application/json</code>).</li>
<li><strong>Entrée</strong> : <code>{{"input": [[...], ...]}}</code> - une liste de 1 à
{nombre(MAX_LIGNES)} voitures ; chaque voiture est une liste de 13 valeurs dans l'ordre du
tableau ci-dessous (les objets nommés <code>{{"model_key": ..., ...}}</code> sont aussi
acceptés). Chaque valeur est contrôlée avant la prédiction.</li>
<li><strong>Sortie</strong> : <code>{{"prediction": [...]}}</code> - un prix par voiture, dans le
même ordre. La réponse n'a qu'une clé, <code>prediction</code>. Si une marque ou une couleur est
inconnue du modèle, la requête est acceptée et la prédiction est calculée sans cette
information.</li>
<li><strong>Erreurs</strong>, toujours au format
<code>{{"error": "...", "details": [...]}}</code> :
422 entrée mal formée (JSON illisible, nombre de valeurs, type, valeur hors bornes, carburant ou
carrosserie inconnus avec la liste admise) ; 413 plus de {nombre(MAX_LIGNES)} voitures ; 503
aucun modèle chargé ; 500 panne interne du modèle.</li>
</ul>
<table><tr><th>#</th><th>Variable</th><th>Description</th><th>Valeurs admises</th></tr>
{lignes_colonnes}</table>
<p><strong>Exemple</strong> :</p>
<pre>curl -i -H "Content-Type: application/json" -X POST \\
  -d '{escape(exemple)}' {url_de_base}/predict</pre>
<pre>{escape(exemple_python)}</pre>

<h2>GET /health</h2>
<ul>
<li><strong>Rôle</strong> : vérifier que l'API est prête et connaître la source du modèle.</li>
<li><strong>Méthode HTTP</strong> : GET - <strong>Entrée</strong> : aucune.</li>
<li><strong>Sortie</strong> :
<code>{{"status": "ok", "model_loaded": true, "source": "..."}}</code>, ou une erreur 503 si
aucun modèle n'est chargé.</li>
</ul>

<h2>GET /docs</h2>
<ul>
<li><strong>Rôle</strong> : afficher cette documentation.</li>
<li><strong>Méthode HTTP</strong> : GET - <strong>Entrée</strong> : aucune.</li>
<li><strong>Sortie</strong> : cette page HTML.</li>
</ul>
<hr>
<p><small>Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC</small></p>
</body></html>"""


@app.get("/docs", response_class=HTMLResponse)
def documentation(requete: Request) -> HTMLResponse:
    """Page de documentation exigée par l'énoncé, adresse dérivée de la requête."""
    return HTMLResponse(page_documentation(str(requete.base_url).rstrip("/")))


if __name__ == "__main__":
    import uvicorn

    # 7860 : port de l'application dans le conteneur ; modifiable par la variable PORT.
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
