# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Démonstration de bout en bout de l'architecture Stripe (OLTP → CDC → OLAP + NoSQL + ML).

Étapes (chacune affichée, pour la vidéo de démonstration) :
1. OLTP : création du schéma normalisé, référentiels, 60 jours de paiements synthétiques ;
2. batch : chargement complet de l'entrepôt en étoile et des agrégats (rôle d'Airflow) ;
3. ML : entraînement du modèle de fraude sur l'entrepôt (découpage temporel) ;
4. flux : arrivée de nouveaux paiements en 5 micro-lots ; la CDC (journal des changements)
   alimente l'entrepôt de façon incrémentale, le magasin de caractéristiques en ligne (NoSQL)
   et le score de fraude en temps réel ; alertes au-delà d'un seuil ;
5. surveillance : dérive des scores (PSI) ;
6. requêtes métier SQL (entrepôt) et NoSQL (documents) ; rapport dans `sorties/`.

Usage : ``python -m stripe_demo.pipeline`` (aucune infrastructure à installer).

CHOIX
-----
- Retenu : une démonstration autonome (SQLite, DuckDB, `mongomock`, scikit-learn) qui remplace
  chaque service AWS par un équivalent local : `journal_changements` + position tiennent lieu de
  WAL + offset Kafka, `INSERT OR REPLACE` de `MERGE`, DuckDB de Redshift. Écarté : exiger
  Docker pour voir fonctionner l'architecture.
- Retenu : cinq micro-lots de 200 paiements fixes pour le flux, de graines 9000 à 9004, et un
  seuil d'alerte de 0,8 ; ils font les chiffres de la note (1 000 paiements notés).
- Retenu : une décision de réentraînement quand le PSI est STRICTEMENT supérieur à 0,2.
- Écarté : journaliser par `logging` ; la démonstration est une narration pour la vidéo, ses
  étapes sont affichées par `titre` et chaque résultat par `print`.

OÙ
---
- Place : point d'entrée de la démonstration, il relie toutes les couches. Appelants : la
  commande `python -m stripe_demo.pipeline`, `stripe_demo/mesures.py` (`mesurer_demo` appelle
  `executer`), les tests `tests/test_pipeline.py` et `tests/test_olap.py` (`requetes_sql`).
- Dépendances : `stripe_demo/oltp.py`, `olap.py`, `fraude.py`, `nosql.py`, `console.py` ; le
  fichier `sql/requetes_olap.sql` (sept requêtes métier) ; numpy, SQLAlchemy.

COMMENT
-------
- `executer` enchaîne : OLTP (schéma, référentiels, `jours` x `par_jour` paiements de graines 1000
  et suivantes), batch OLAP, entraînement, flux (cinq micro-lots : paiements, CDC, caractéristiques
  en ligne, score, signal écrit), PSI, requêtes SQL et NoSQL ; il écrit `rapport_demo.json` et
  rend le rapport. `requetes_sql` découpe `sql/requetes_olap.sql` en (question, requête) grâce aux
  commentaires `-- @ `. `titre` encadre chaque étape.
- Ce qui le prouve (`tests/test_pipeline.py`) : une exécution en petit, avec horloge et modèle
  factices, dont le texte affiché est comparé à une transcription de référence, ligne à ligne,
  ainsi que le rapport, les fichiers, les graines et dates des lots, le seuil d'alerte et le seuil
  du PSI ; puis une exécution avec le vrai modèle.

POURQUOI
--------
- Les six étapes, les équivalences local / production et les chiffres constatés :
  `livrables/aia2/05_pipeline.md:131-157`.
- Le service de score, les alertes à 0,8 et la latence :
  `livrables/aia2/07_integration_ml.md:75-98`.
- Les sept requêtes métier : `sql/requetes_olap.sql:5-46`.

JOURNALISATION
--------------
- Pas de module `logging` : la sortie standard EST le compte rendu (titres, comptes, micro-lots,
  PSI, résultats des requêtes), pour la vidéo de démonstration. Le rapport complet est écrit en
  JSON dans `rapport_demo.json`. Aucune exception n'est avalée.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import text

from stripe_demo import fraude, nosql, olap, oltp
from stripe_demo.console import forcer_utf8

RACINE = Path(__file__).resolve().parent.parent
SORTIES = RACINE / "sorties"
SEUIL_ALERTE = 0.8


def titre(texte: str) -> None:
    print(f"\n=== {texte} ===")


def requetes_sql(chemin: Path) -> list[tuple[str, str]]:
    """Découpe le fichier SQL en (question, requête) grâce aux commentaires « -- @ »."""
    blocs = []
    for morceau in chemin.read_text(encoding="utf-8").split("-- @ ")[1:]:
        question, _, requete = morceau.partition("\n")
        if requete.strip():  # un bloc sans requête (texte d'en-tête) est ignoré
            blocs.append((question.strip(), requete.strip().rstrip(";")))
    return blocs


def executer(dossier: Path = SORTIES, jours: int = 60, par_jour: int = 800) -> dict[str, Any]:
    forcer_utf8()
    dossier.mkdir(exist_ok=True)
    chemin_oltp, chemin_olap = dossier / "oltp.sqlite", dossier / "olap.duckdb"
    for f in (chemin_oltp, chemin_olap):
        f.unlink(missing_ok=True)
    rapport: dict[str, Any] = {}

    titre("1. OLTP : schéma normalisé et données synthétiques")
    moteur = oltp.moteur(str(chemin_oltp))
    oltp.creer(moteur)
    oltp.referentiels(moteur, jours=jours + 2)
    dernier = 0
    debut = datetime(2026, 8, 1, tzinfo=UTC)
    for j in range(jours):
        dernier = oltp.paiements_synthetiques(moteur, debut + timedelta(days=j), par_jour,
                                              graine=1000 + j, premier_id=dernier + 1)
    with moteur.connect() as cx:
        comptes = {t: cx.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
                   for t in ("marchands", "clients", "paiements", "litiges", "remboursements",
                             "journal_changements")}
    print(comptes)
    rapport["oltp"] = comptes

    titre("2. Batch : entrepôt en étoile et agrégats")
    entrepot = olap.connecter(str(chemin_olap))
    t0 = time.perf_counter()
    rapport["batch"] = olap.charger_batch(entrepot, str(chemin_oltp))
    print(rapport["batch"], f"en {time.perf_counter() - t0:.1f} s")
    with moteur.connect() as cx:
        position = cx.execute(text("SELECT max(position) FROM journal_changements")).scalar_one()

    titre("3. ML : modèle de fraude entraîné sur l'entrepôt")
    modele, mesures, scores_reference = fraude.entrainer(entrepot)
    print(mesures)
    rapport["modele"] = {"version": fraude.VERSION_MODELE, **mesures}

    titre("4. Flux : nouveaux paiements → CDC → entrepôt, NoSQL, score en temps réel")
    db = nosql.base()
    nosql.evenements_synthetiques(db, 5000)
    nosql.retours_synthetiques(db, 400)
    clients = {r[0]: (r[1], r[2]) for r in entrepot.execute(
        "SELECT cle_client, code_pays, anciennete_jours FROM dim_client").fetchall()}
    scores_production, latences, alertes = [], [], 0  # `alertes` : nombre d'alertes (un compteur)
    jour_flux = debut + timedelta(days=jours)
    for lot in range(5):
        dernier_avant = dernier
        dernier = oltp.paiements_synthetiques(moteur, jour_flux + timedelta(hours=4 * lot), 200,
                                              graine=9000 + lot, premier_id=dernier + 1)
        ancienne = position
        position, n = olap.appliquer_changements(entrepot, str(chemin_oltp), position)
        nouveaux = entrepot.execute(
            "SELECT id_paiement, cle_client, montant_usd, heure, pays_ip, appareil "
            "FROM faits_paiements WHERE id_paiement > ? ORDER BY id_paiement",
            [dernier_avant]).fetchall()
        for id_p, id_client, montant, heure, pays_ip, appareil in nouveaux:
            t = time.perf_counter()
            paiement = {"id_client": id_client, "montant_usd": montant, "heure": heure,
                        "pays_ip": pays_ip, "appareil": appareil}
            carac = nosql.mettre_a_jour_caracteristiques(db, paiement)
            pays_client, anciennete = clients[id_client]
            score = fraude.noter(modele, paiement, carac, anciennete, pays_client)
            db.signaux_fraude.insert_one({"id_paiement": id_p, "score": round(score, 4),
                                          "version_modele": fraude.VERSION_MODELE,
                                          "horodatage": datetime.now(UTC)})
            latences.append((time.perf_counter() - t) * 1000)
            scores_production.append(score)
            if score >= SEUIL_ALERTE:
                alertes += 1
        print(f"micro-lot {lot + 1} : journal {ancienne} → {position}, {n} faits insérés, "
              f"{len(nouveaux)} paiements notés, alertes cumulées : {alertes}")
    rapport["flux"] = {
        "paiements_notes": len(scores_production), "alertes": alertes,
        "latence_mediane_ms": round(float(np.median(latences)), 1),
        "documents_caracteristiques": db.caracteristiques_clients.count_documents({}),
    }
    print(rapport["flux"])

    titre("5. Surveillance du modèle : dérive des scores")
    valeur_psi = fraude.psi(scores_reference, np.array(scores_production))
    rapport["surveillance"] = {"psi": round(valeur_psi, 3),
                               "decision": "réentraînement" if valeur_psi > 0.2 else "stable"}
    print(rapport["surveillance"])

    titre("6. Requêtes métier")
    rapport["requetes_sql"] = {}
    for question, requete in requetes_sql(RACINE / "sql" / "requetes_olap.sql"):
        tableau = entrepot.execute(requete).df()
        print(f"\n-- {question}\n{tableau.to_string(index=False)}")
        rapport["requetes_sql"][question] = tableau.astype(str).to_dict("records")
    rapport["requetes_nosql"] = {}
    for question, pipeline_mongo in nosql.REQUETES.items():
        documents = list(db.evenements.aggregate(pipeline_mongo))
        print(f"\n-- {question}\n{documents}")
        rapport["requetes_nosql"][question] = documents
    for question, pipeline_mongo in nosql.REQUETES_RETOURS.items():
        documents = list(db.retours_clients.aggregate(pipeline_mongo))
        print(f"\n-- {question}\n{documents}")
        rapport["requetes_nosql"][question] = documents
    (dossier / "rapport_demo.json").write_text(
        json.dumps(rapport, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    entrepot.close()
    moteur.dispose()  # sous Windows, la connexion gardée en réserve verrouille `oltp.sqlite`
    return rapport


if __name__ == "__main__":
    executer()
