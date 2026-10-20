# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Aide aux tests : rejouer la démonstration en petit, avec horloge et modèle factices.

Pourquoi : la démonstration complète traite 48 000 paiements (une minute). Pour la tester, on la
rejoue en petit (quelques jours de quelques centaines de paiements, puis les 5 micro-lots fixes
de 200 paiements du flux) avec deux simplifications qui la rendent exactement reproductible :
l'horloge est un compteur (les durées affichées deviennent des constantes) et le modèle de
fraude est un faux dont les scores sont connus. `test_fraude.py` prouve le vrai modèle.
"""

from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import mongomock
import numpy as np
import pandas as pd
from sqlalchemy import event

from stripe_demo import nosql, olap, oltp, pipeline

PAS_D_HORLOGE = 0.123456  # secondes ajoutées à chaque lecture de l'horloge factice
INSTANT_FIGE = datetime(2026, 10, 1, 12, 34, 56, tzinfo=UTC)  # « maintenant » de la démonstration


class DatetimeFige(datetime):
    """`datetime` dont « maintenant » est fixe et exigé en UTC (jamais dans le fuseau local)."""

    @classmethod
    def now(cls, tz=None):
        assert tz is UTC, "l'heure doit être demandée en UTC, pas dans le fuseau de la machine"
        return INSTANT_FIGE


def maintenant_utc_naif() -> datetime:
    """Instant présent en UTC sans fuseau : ce que mongomock lit (voir `tests/conftest.py`)."""
    return datetime.now(UTC).replace(tzinfo=None)


def faux_entrainer(cx):
    """Entraînement factice : mesures fixes, scores de référence répartis de 0 à 1."""
    lignes = cx.execute("SELECT count(*) FROM faits_paiements").fetchall()[0][0]
    mesures = {"taux_fraude": 0.025, "pr_auc_test": 0.5, "roc_auc_test": 0.9,
               "lignes_apprentissage": lignes, "lignes_test": 0}
    return object(), mesures, np.linspace(0.0, 1.0, 101)


def noter_selon_le_montant(modele, paiement, caracteristiques, anciennete_jours, pays_client):
    """Score connu : 0,9 (alerte) au-dessus de 100 USD, sinon 0,123456 (arrondi à 0,1235)."""
    return 0.9 if paiement["montant_usd"] > 100 else 0.123456


class ConnexionEspion:
    """Enveloppe une connexion DuckDB : transmet les appels et note chaque instruction.

    Pourquoi : DuckDB et SQLite acceptent les mots-clés SQL en majuscules comme en minuscules ;
    un test qui ne regarde que le résultat laisserait passer un `select` écrit autrement. Ici, on
    note le TEXTE exact de chaque instruction (espaces normalisés), dans l'ordre.
    """

    def __init__(self, connexion):
        self.connexion = connexion
        self.instructions: list[tuple[str, tuple]] = []

    def execute(self, sql, parametres=None):
        self.instructions.append((" ".join(sql.split()), tuple(parametres or ())))
        if parametres is None:
            return self.connexion.execute(sql)
        return self.connexion.execute(sql, parametres)

    def register(self, nom, objet):
        self.instructions.append((f"register({nom})", ()))
        return self.connexion.register(nom, objet)

    def begin(self):
        self.instructions.append(("begin()", ()))
        self.connexion.begin()

    def commit(self):
        self.instructions.append(("commit()", ()))
        self.connexion.commit()

    def rollback(self):
        self.instructions.append(("rollback()", ()))
        self.connexion.rollback()

    def close(self):
        self.instructions.append(("close()", ()))
        self.connexion.close()

    def textes(self) -> list[str]:
        return [sql for sql, _ in self.instructions]

    def __getattr__(self, nom):
        return getattr(self.connexion, nom)


def noter_les_requetes_sqlalchemy(moteur, requetes: list[str] | None = None) -> list[str]:
    """Note, dans l'ordre, le texte exact (espaces normalisés) des instructions du moteur.

    Rend la liste où elles s'accumulent : celle donnée, sinon une nouvelle.
    """
    requetes = [] if requetes is None else requetes

    def noter(_connexion, _curseur, sql, _parametres, _contexte, _plusieurs):
        requetes.append(" ".join(sql.split()))

    event.listen(moteur, "before_cursor_execute", noter)
    return requetes


def noter_les_appels_json(mp) -> list[dict]:
    """Note les arguments nommés de chaque `json.dumps`, sans rien changer à son résultat.

    Pourquoi : `ensure_ascii=None` et `ensure_ascii=False` donnent le même texte (seule la valeur
    de vérité compte) ; la sortie ne dit donc pas lequel des deux est écrit dans le code.
    """
    appels: list[dict] = []
    vrai_dumps = json.dumps

    def dumps(objet, *arguments, **options):
        appels.append(options)
        return vrai_dumps(objet, *arguments, **options)

    mp.setattr(json, "dumps", dumps)
    return appels


def noter_les_appels_to_string(mp) -> list[dict]:
    """Note les arguments nommés de chaque `DataFrame.to_string` (même raison que ci-dessus)."""
    appels: list[dict] = []
    vrai_to_string = pd.DataFrame.to_string

    def to_string(tableau, *arguments, **options):
        appels.append(options)
        return vrai_to_string(tableau, *arguments, **options)

    mp.setattr(pd.DataFrame, "to_string", to_string)
    return appels


def noter_les_appels_to_dict(mp) -> list[tuple]:
    """Note les arguments (positionnels puis nommés) de chaque `DataFrame.to_dict`.

    Pourquoi : pandas lit l'orientation sans tenir compte de la casse (« RECORDS » donne le même
    résultat que « records ») ; la sortie ne dit donc pas laquelle des deux est écrite.
    """
    appels: list[tuple] = []
    vrai_to_dict = pd.DataFrame.to_dict

    def to_dict(tableau, *arguments, **options):
        appels.append((arguments, options))
        return vrai_to_dict(tableau, *arguments, **options)

    mp.setattr(pd.DataFrame, "to_dict", to_dict)
    return appels


def espionner_ouvertures_utf8(mp) -> list[dict]:
    """Fait échouer toute ouverture de fichier texte qui ne déclare pas l'encodage « utf-8 ».

    `pathlib` ouvre ses fichiers par `io.open` : on le remplace, avec `mp` (un MonkeyPatch), par
    une version qui refuse l'absence d'encodage ou tout autre encodage que « utf-8 ». Rend la
    liste des ouvertures de fichiers texte (chemin, mode, encodage, fin de ligne demandée), que
    le test peut interroger. Les fichiers binaires ne sont pas concernés.
    """
    ouvrir_reel = io.open
    ouvertures: list[dict] = []

    def ouvrir_en_exigeant_utf8(fichier, mode="r", buffering=-1, encoding=None, errors=None,
                                newline=None, *args, **kwargs):
        if "b" not in mode:
            # Sans `encoding`, `pathlib` transmet None (la page de code de la machine) : refusé.
            assert encoding == "utf-8", f"{fichier} ouvert en {encoding!r}, pas en « utf-8 »"
            ouvertures.append({"fichier": str(fichier), "mode": mode, "encodage": encoding,
                               "fin_de_ligne": newline})
        return ouvrir_reel(fichier, mode, buffering, encoding, errors, newline, *args, **kwargs)

    mp.setattr(io, "open", ouvrir_en_exigeant_utf8)
    return ouvertures


class Execution:
    """Tout ce qu'une exécution en petit a produit, pour que les tests l'interrogent."""

    def __init__(self, rapport, texte, dossier, base, appels_noter, appels_paiements,
                 ouvertures, entrepot, requetes_oltp, appels_json, appels_to_string,
                 appels_to_dict):
        self.rapport = rapport
        self.texte = texte
        self.dossier = dossier
        self.base = base
        self.appels_noter = appels_noter
        self.appels_paiements = appels_paiements
        self.ouvertures = ouvertures
        self.entrepot = entrepot  # la connexion DuckDB de la démonstration, avec son journal
        self.requetes_oltp = requetes_oltp  # les instructions envoyées à la base OLTP
        self.appels_json = appels_json  # arguments nommés de chaque json.dumps
        self.appels_to_string = appels_to_string  # arguments nommés de chaque to_string
        self.appels_to_dict = appels_to_dict  # arguments de chaque to_dict


def executer_en_petit(mp, dossier: Path, *, jours=3, par_jour=100, noter=noter_selon_le_montant,
                      psi=None, avec_documents=True, appels_referentiels=None) -> Execution:
    """Lance `pipeline.executer` avec horloge et modèle factices (`mp` : un MonkeyPatch)."""
    mp.setattr(mongomock, "utcnow", maintenant_utc_naif)
    ouvertures = espionner_ouvertures_utf8(mp)
    appels_json = noter_les_appels_json(mp)
    appels_to_string = noter_les_appels_to_string(mp)
    appels_to_dict = noter_les_appels_to_dict(mp)
    lectures = {"n": 0}

    def horloge():
        lectures["n"] += 1
        return lectures["n"] * PAS_D_HORLOGE

    bases = []
    vraie_base = nosql.base

    def base_gardee():
        bases.append(vraie_base())
        return bases[-1]

    appels_noter, appels_paiements = [], []

    def noter_et_garder(modele, paiement, caracteristiques, anciennete_jours, pays_client):
        appels_noter.append((dict(paiement), dict(caracteristiques), anciennete_jours,
                             pays_client))
        return noter(modele, paiement, caracteristiques, anciennete_jours, pays_client)

    vraie_generation = oltp.paiements_synthetiques

    def generation_gardee(moteur, debut, nombre, graine, premier_id):
        appels_paiements.append((debut, nombre, graine, premier_id))
        return vraie_generation(moteur, debut, nombre, graine=graine, premier_id=premier_id)

    vrais_referentiels = oltp.referentiels

    def referentiels_gardes(moteur, jours, **autres):
        if appels_referentiels is not None:
            appels_referentiels.append(jours)
        return vrais_referentiels(moteur, jours=jours, **autres)

    entrepots, requetes_oltp = [], []
    vrai_connecter = olap.connecter
    vrai_moteur = oltp.moteur

    def connecter_espionne(chemin):
        entrepots.append(ConnexionEspion(vrai_connecter(chemin)))
        return entrepots[-1]

    def moteur_espionne(chemin):
        engine = vrai_moteur(chemin)
        noter_les_requetes_sqlalchemy(engine, requetes_oltp)
        return engine

    mp.setattr(time, "perf_counter", horloge)
    mp.setattr(pipeline, "datetime", DatetimeFige)
    mp.setattr(pipeline.olap, "connecter", connecter_espionne)
    mp.setattr(pipeline.oltp, "moteur", moteur_espionne)
    mp.setattr(pipeline.nosql, "base", base_gardee)
    mp.setattr(pipeline.fraude, "entrainer", faux_entrainer)
    mp.setattr(pipeline.fraude, "noter", noter_et_garder)
    mp.setattr(pipeline.oltp, "paiements_synthetiques", generation_gardee)
    mp.setattr(pipeline.oltp, "referentiels", referentiels_gardes)
    if psi is not None:
        mp.setattr(pipeline.fraude, "psi", lambda reference, production: psi)
    if not avec_documents:
        mp.setattr(pipeline.nosql, "evenements_synthetiques", lambda db, nombre: None)
        mp.setattr(pipeline.nosql, "retours_synthetiques", lambda db, nombre: None)
    sortie = io.StringIO()
    with redirect_stdout(sortie):
        rapport = pipeline.executer(dossier, jours=jours, par_jour=par_jour)
    return Execution(rapport, sortie.getvalue(), dossier, bases[-1], appels_noter,
                     appels_paiements, ouvertures, entrepots[-1], requetes_oltp,
                     appels_json, appels_to_string, appels_to_dict)
