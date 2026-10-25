# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Base transactionnelle de démonstration : création, données synthétiques,
capture des changements.

En production, l'OLTP est PostgreSQL (Aurora, multi-AZ) et la capture des changements (CDC) est
faite par Debezium, qui lit le journal de transactions (WAL) et publie chaque changement dans
Kafka. En démonstration locale, la base est SQLite et des **déclencheurs** écrivent chaque
insertion ou mise à jour de paiement dans `journal_changements` : même principe (un journal
ordonné de changements, lu par position), sans infrastructure à installer.

Les données sont **synthétiques** et reproductibles (graine fixe) : aucune donnée réelle de
client, conformément au RGPD et au PCI-DSS.

CHOIX
-----
- Retenu : SQLite en démonstration, avec les clés étrangères activées à chaque connexion (SQLite
  les ignore par défaut), et des déclencheurs qui tiennent le rôle du journal de transactions
  que Debezium lit en production. Écarté : exiger un PostgreSQL pour la démonstration.
- Retenu : un sel de démonstration pour hacher les courriels (SHA-256), le vrai sel devant être
  un secret de Secrets Manager. Aucune donnée réelle n'est jamais écrite.
- Retenu : une fraude simulée de base à 0,2 %, plus probable pour un pays d'IP différent
  (+25 %), la nuit avant 6 h (+4 %) et au-dessus de 30 000 unités mineures (+8 %) : des signaux
  qu'un modèle doit retrouver. Écarté : une fraude tirée sans lien avec les variables.
- Retenu : un lot de paiements est inséré dans UNE transaction ; un identifiant ou une clé
  d'idempotence déjà pris le refuse en entier.

OÙ
---
- Place : couche « base transactionnelle » de la démonstration. Appelants :
  `stripe_demo/pipeline.py`, `stripe_demo/vers_postgres.py` (qui réutilise `referentiels` et
  `paiements_synthetiques` avec un PostgreSQL), `stripe_demo/mesures.py`, les tests
  `tests/conftest.py` (fixture `mini_demo`), `tests/test_oltp.py`.
- Dépendances : SQLAlchemy, le modèle `stripe_demo/modele_oltp.py`, la bibliothèque standard.

COMMENT
-------
- `moteur` crée le moteur SQLite et branche l'activation des clés étrangères ; `creer` supprime
  puis recrée les tables et les deux déclencheurs (`cdc_paiements_insert`,
  `cdc_paiements_update`) ; `referentiels` charge pays, devises, taux, 40 marchands, 2 000
  clients, leurs moyens de paiement et 80 produits (graine 42) ; `paiements_synthetiques` tire des
  paiements (graine donnée), puis les litiges et remboursements qui en découlent, et rend le
  dernier identifiant.
- Ce qui le prouve : `tests/test_oltp_contraintes.py` (la base refuse l'incohérence) et
  `tests/test_oltp.py` (forme des déclencheurs, volumes, empreinte SHA-256 des référentiels et
  d'un lot de paiements, bornes des tirages par un faux générateur, seuils exacts de la
  probabilité de fraude, refus d'un identifiant déjà pris, d'un client absent ou sans devise).

POURQUOI
--------
- Choix de modélisation, treize tables, contraintes : `livrables/aia2/02_oltp_erd.md:130-147`.
- Écarts entre la démonstration (SQLite) et la production :
  `livrables/aia2/02_oltp_erd.md:282-290`.
- Fraude simulée et signaux que le modèle doit retrouver :
  `livrables/aia2/07_integration_ml.md:71`.

JOURNALISATION
--------------
- Rien n'est journalisé (pas de module `logging`) ; le journal des changements de la base
  (`journal_changements`) est une table de données, non un journal d'exécution. Les violations
  de contrainte lèvent `IntegrityError` et annulent le lot.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine, event, insert, text
from sqlalchemy.engine import Engine

from stripe_demo import modele_oltp as m

PAYS = [("FR", "France", "EEE"), ("DE", "Allemagne", "EEE"), ("IE", "Irlande", "EEE"),
        ("US", "États-Unis", "Amérique du Nord"), ("GB", "Royaume-Uni", "Europe hors EEE"),
        ("SG", "Singapour", "Asie-Pacifique"), ("BR", "Brésil", "Amérique latine"),
        ("NG", "Nigeria", "Afrique")]
DEVISES = [("EUR", "euro", 2), ("USD", "dollar américain", 2), ("GBP", "livre sterling", 2),
           ("SGD", "dollar de Singapour", 2), ("BRL", "réal", 2)]
DEVISE_PAR_PAYS = {"FR": "EUR", "DE": "EUR", "IE": "EUR", "US": "USD", "GB": "GBP",
                   "SG": "SGD", "BR": "BRL", "NG": "USD"}
TAUX = {"EUR": 1.08, "USD": 1.0, "GBP": 1.27, "SGD": 0.74, "BRL": 0.18}
MCC = ["5411", "5732", "5812", "5999", "7372", "4121"]


def moteur(chemin: str) -> Engine:
    """Moteur SQLite avec clés étrangères activées (désactivées par défaut dans SQLite)."""
    engine = create_engine(f"sqlite:///{chemin}")

    @event.listens_for(engine, "connect")
    def _activer_cles_etrangeres(connexion: sqlite3.Connection, _: object) -> None:
        connexion.execute("PRAGMA foreign_keys = ON")

    return engine


def creer(engine: Engine) -> None:
    """Crée les tables et les déclencheurs de capture des changements."""
    m.schema.drop_all(engine)
    m.schema.create_all(engine)
    with engine.begin() as cx:
        for operation, moment in (("INSERT", "INSERT"), ("UPDATE", "UPDATE OF statut")):
            cx.execute(text(f"""
                CREATE TRIGGER IF NOT EXISTS cdc_paiements_{operation.lower()}
                AFTER {moment} ON paiements
                BEGIN
                    INSERT INTO journal_changements (table_source, operation, id_ligne, horodatage)
                    VALUES ('paiements', '{operation}', NEW.id_paiement, CURRENT_TIMESTAMP);
                END"""))


def _hacher(valeur: str) -> str:
    """Pseudonymisation : SHA-256 salé (le sel serait un secret de Secrets Manager en prod)."""
    return hashlib.sha256(("sel-demo:" + valeur).encode()).hexdigest()


def referentiels(engine: Engine, jours: int = 60, debut: date = date(2026, 8, 1)) -> None:
    """Pays, devises, taux de change quotidiens, marchands, clients, moyens, produits."""
    alea = random.Random(42)
    with engine.begin() as cx:
        cx.execute(insert(m.pays),
                   [dict(zip(("code_pays", "nom", "region"), p, strict=True)) for p in PAYS])
        cx.execute(insert(m.devises), [
            dict(zip(("code_devise", "nom", "decimales"), d, strict=True)) for d in DEVISES])
        cx.execute(insert(m.taux_change), [
            {"code_devise": d, "date_taux": debut + timedelta(days=j),
             "taux_vers_usd": round(t * (1 + alea.uniform(-0.01, 0.01)), 6)}
            for d, t in TAUX.items() for j in range(jours)])
        marchands = [{"id_marchand": i, "raison_sociale": f"Marchand {i:03d}",
                      "code_pays": alea.choice(PAYS[:6])[0], "code_mcc": alea.choice(MCC),
                      "statut": "actif", "cree_le": datetime(2025, 1, 1, tzinfo=UTC)}
                     for i in range(1, 41)]
        cx.execute(insert(m.marchands), marchands)
        clients, moyens = [], []
        for i in range(1, 2001):
            pays_client = alea.choices([p[0] for p in PAYS],
                                       weights=[20, 15, 5, 30, 10, 8, 7, 5])[0]
            clients.append({"id_client": i, "id_marchand": alea.randint(1, 40),
                            "email_hache": _hacher(f"client{i}@exemple.org"),
                            "code_pays": pays_client,
                            "cree_le": datetime(2026, 1, 1, tzinfo=UTC)
                            + timedelta(days=alea.randint(0, 240))})
            moyens.append({"id_moyen": i, "id_client": i,
                           "type": alea.choices(["carte", "sepa", "portefeuille"],
                                                [80, 10, 10])[0],
                           "jeton_psp": f"pm_{_hacher(str(i))[:24]}",
                           "marque": alea.choice(["visa", "mastercard", "amex"]),
                           "quatre_derniers": f"{alea.randint(0, 9999):04d}",
                           "pays_emission": pays_client})
        cx.execute(insert(m.clients), clients)
        cx.execute(insert(m.moyens_paiement), moyens)
        cx.execute(insert(m.produits), [
            {"id_produit": i, "id_marchand": (i - 1) % 40 + 1, "nom": f"Offre {i}",
             "prix_mineur": alea.choice([499, 999, 1999, 4999]), "code_devise": "EUR",
             "periodicite": alea.choice([None, "mois", "an"])} for i in range(1, 81)])


def paiements_synthetiques(engine: Engine, debut: datetime, nombre: int, graine: int,
                           premier_id: int) -> int:
    """Insère des paiements réalistes, avec un schéma de fraude connu ; renvoie le dernier id.

    Fraude simulée (environ 2 %) : plus probable la nuit, pour un pays IP différent du pays du
    client, pour un montant élevé et un compte récent : signaux qu'un modèle doit retrouver.
    """
    alea = random.Random(graine)
    lignes = []
    with engine.connect() as cx:
        clients = cx.execute(text(
            "SELECT id_client, id_marchand, code_pays, cree_le FROM clients")).fetchall()
    for k in range(nombre):
        id_client, id_marchand, pays_client, _cree_le = alea.choice(clients)
        instant = debut + timedelta(seconds=alea.randint(0, 86_399))
        devise = DEVISE_PAR_PAYS[pays_client]
        montant = int(alea.lognormvariate(7.8, 1.0))  # médiane ≈ 24 unités
        pays_ip = pays_client if alea.random() > 0.06 else alea.choice(PAYS)[0]
        nuit = instant.hour < 6
        risque = (0.002 + 0.25 * (pays_ip != pays_client) + 0.04 * nuit
                  + 0.08 * (montant > 30_000))
        fraude = alea.random() < risque
        statut = "conteste" if fraude else alea.choices(
            ["reussi", "echoue", "rembourse"], [92, 5, 3])[0]
        lignes.append({
            "id_paiement": premier_id + k, "id_marchand": id_marchand, "id_client": id_client,
            "id_moyen": id_client, "montant_mineur": max(montant, 50), "code_devise": devise,
            "statut": statut, "cree_le": instant, "pays_ip": pays_ip,
            "appareil": alea.choices(["mobile", "ordinateur", "tablette", "api"],
                                     [55, 35, 5, 5])[0],
            "cle_idempotence": f"idem-{graine}-{k}",
        })
    with engine.begin() as cx:
        cx.execute(insert(m.paiements), lignes)
        litiges = [{"id_litige": lg["id_paiement"], "id_paiement": lg["id_paiement"],
                    "motif": "fraude", "statut": "ouvert", "cree_le": lg["cree_le"]}
                   for lg in lignes if lg["statut"] == "conteste"]
        if litiges:
            cx.execute(insert(m.litiges), litiges)
        remb = [{"id_remboursement": lg["id_paiement"], "id_paiement": lg["id_paiement"],
                 "montant_mineur": lg["montant_mineur"], "motif": "demande_client",
                 "cree_le": lg["cree_le"] + timedelta(days=2)}
                for lg in lignes if lg["statut"] == "rembourse"]
        if remb:
            cx.execute(insert(m.remboursements), remb)
    return premier_id + nombre - 1
