# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Modèle transactionnel (OLTP) de Stripe, défini UNE fois, en SQLAlchemy Core.

Pourquoi en Python plutôt qu'en SQL écrit à la main : le même modèle sert (1) à créer la base de
démonstration locale (SQLite) et (2) à produire le DDL PostgreSQL de production
(`python -m stripe_demo.generer_ddl`) ; la documentation, la démonstration et la production ne
peuvent donc pas diverger.

Principes de modélisation (3e forme normale) :
- une table par entité ; aucune donnée répétée (le pays, la devise sont des références) ;
- montants en **unités mineures entières** (centimes) : pas d'arrondi flottant sur de l'argent ;
- **aucun numéro de carte** : seul un jeton du prestataire de paiement et les 4 derniers chiffres
  sont stockés (réduction du périmètre PCI-DSS) ; l'e-mail du client est **haché**
  (pseudonymisation, RGPD) ;
- contraintes d'intégrité (clés étrangères, CHECK, UNIQUE) : l'intégrité est garantie par la
  base, pas seulement par le code (propriété C d'ACID) ;
- clé d'idempotence unique sur les paiements : un paiement rejoué après une coupure réseau
  n'est pas débité deux fois.

CHOIX
-----
- Retenu : SQLAlchemy Core (des `Table` déclaratives), pas l'ORM : le but est un schéma, non des
  objets métier. Le même modèle produit la base SQLite de la démonstration ET le DDL PostgreSQL.
- Retenu : montants en unités mineures entières, devise séparée ; jeton du prestataire et quatre
  derniers chiffres, jamais un numéro de carte ; courriel haché.
- Retenu : les domaines de valeurs en contraintes `CHECK` et les règles métier en `UNIQUE` (un
  litige par paiement, une clé d'idempotence par paiement), portées par la base.
- Écarté : un champ `numero_carte` ou une date d'expiration (périmètre PCI-DSS le plus lourd).

OÙ
---
- Place : source unique du modèle transactionnel. Appelants : `stripe_demo/oltp.py` (création de
  la base, insertions), `stripe_demo/generer_ddl.py` (DDL PostgreSQL), les tests
  `tests/test_oltp_contraintes.py` et `tests/test_generer_ddl.py`.
- Dépendances : SQLAlchemy seul. Le module ne contient aucune fonction : que des déclarations.

COMMENT
-------
- Chaque `Table` est rattachée à `schema` (un `MetaData`) ; les clés étrangères, les `CHECK`
  (`ck_paiement_statut`, `ck_paiement_montant`...) et les `UNIQUE` sont déclarés sur les colonnes ;
  deux index servent l'historique d'un client et le chiffre d'un marchand.
- Ce qui le prouve : `tests/test_oltp_contraintes.py` (la base refuse un montant nul, un statut
  inconnu, une référence inexistante, un doublon d'idempotence, un second litige), et
  `tests/test_generer_ddl.py` (le DDL reprend toutes les tables et contraintes).

POURQUOI
--------
- Treize tables, 18 clés étrangères, 7 contraintes `CHECK` et le choix de modélisation :
  `livrables/aia2/02_oltp_erd.md:130-147`.
- Propriétés ACID et leur preuve : `livrables/aia2/02_oltp_erd.md:149-156`.

JOURNALISATION
--------------
- Aucune : le module ne contient que des déclarations, sans code exécuté au-delà de la
  construction des objets `Table`.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
)

schema = MetaData()

pays = Table(
    "pays", schema,
    Column("code_pays", String(2), primary_key=True),  # ISO 3166-1 alpha-2
    Column("nom", String(80), nullable=False),
    Column("region", String(20), nullable=False),  # EEE, Amérique du Nord, Asie-Pacifique...
)

devises = Table(
    "devises", schema,
    Column("code_devise", String(3), primary_key=True),  # ISO 4217
    Column("nom", String(40), nullable=False),
    Column("decimales", Integer, nullable=False),
)

taux_change = Table(
    "taux_change", schema,
    Column("code_devise", String(3), ForeignKey("devises.code_devise"), primary_key=True),
    Column("date_taux", Date, primary_key=True),
    Column("taux_vers_usd", Numeric(18, 8), nullable=False),
)

marchands = Table(
    "marchands", schema,
    Column("id_marchand", BigInteger, primary_key=True),
    Column("raison_sociale", String(120), nullable=False),
    Column("code_pays", String(2), ForeignKey("pays.code_pays"), nullable=False),
    Column("code_mcc", String(4), nullable=False),  # catégorie d'activité (Merchant Category Code)
    Column("statut", String(12), nullable=False),
    Column("cree_le", DateTime(timezone=True), nullable=False),
    CheckConstraint("statut IN ('actif', 'suspendu', 'ferme')", name="ck_marchand_statut"),
)

clients = Table(
    "clients", schema,
    Column("id_client", BigInteger, primary_key=True),
    Column("id_marchand", BigInteger, ForeignKey("marchands.id_marchand"), nullable=False),
    Column("email_hache", String(64), nullable=False),  # SHA-256 salé : pseudonymisation
    Column("code_pays", String(2), ForeignKey("pays.code_pays"), nullable=False),
    Column("cree_le", DateTime(timezone=True), nullable=False),
)

moyens_paiement = Table(
    "moyens_paiement", schema,
    Column("id_moyen", BigInteger, primary_key=True),
    Column("id_client", BigInteger, ForeignKey("clients.id_client"), nullable=False),
    Column("type", String(12), nullable=False),
    Column("jeton_psp", String(40), nullable=False, unique=True),  # jamais le numéro de carte
    Column("marque", String(12)),
    Column("quatre_derniers", String(4)),
    Column("pays_emission", String(2), ForeignKey("pays.code_pays")),
    CheckConstraint("type IN ('carte', 'sepa', 'portefeuille')", name="ck_moyen_type"),
)

produits = Table(
    "produits", schema,
    Column("id_produit", BigInteger, primary_key=True),
    Column("id_marchand", BigInteger, ForeignKey("marchands.id_marchand"), nullable=False),
    Column("nom", String(120), nullable=False),
    Column("prix_mineur", BigInteger, nullable=False),
    Column("code_devise", String(3), ForeignKey("devises.code_devise"), nullable=False),
    Column("periodicite", String(10)),  # NULL : achat unique ; 'mois' ou 'an' : abonnement
)

abonnements = Table(
    "abonnements", schema,
    Column("id_abonnement", BigInteger, primary_key=True),
    Column("id_client", BigInteger, ForeignKey("clients.id_client"), nullable=False),
    Column("id_produit", BigInteger, ForeignKey("produits.id_produit"), nullable=False),
    Column("statut", String(12), nullable=False),
    Column("debut", DateTime(timezone=True), nullable=False),
    Column("fin", DateTime(timezone=True)),
    CheckConstraint("statut IN ('actif', 'impaye', 'annule')", name="ck_abonnement_statut"),
)

paiements = Table(
    "paiements", schema,
    Column("id_paiement", BigInteger, primary_key=True),
    Column("id_marchand", BigInteger, ForeignKey("marchands.id_marchand"), nullable=False),
    Column("id_client", BigInteger, ForeignKey("clients.id_client"), nullable=False),
    Column("id_moyen", BigInteger, ForeignKey("moyens_paiement.id_moyen"), nullable=False),
    Column("id_abonnement", BigInteger, ForeignKey("abonnements.id_abonnement")),
    Column("montant_mineur", BigInteger, nullable=False),
    Column("code_devise", String(3), ForeignKey("devises.code_devise"), nullable=False),
    Column("statut", String(12), nullable=False),
    Column("cree_le", DateTime(timezone=True), nullable=False),
    Column("pays_ip", String(2), ForeignKey("pays.code_pays")),  # géolocalisation par adresse IP
    Column("appareil", String(10), nullable=False),
    Column("score_risque", Numeric(5, 4)),  # score de fraude attribué à la volée (0 à 1)
    Column("cle_idempotence", String(64), nullable=False, unique=True),
    CheckConstraint("montant_mineur > 0", name="ck_paiement_montant"),
    CheckConstraint("statut IN ('reussi', 'echoue', 'rembourse', 'conteste')",
                    name="ck_paiement_statut"),
    CheckConstraint("appareil IN ('mobile', 'ordinateur', 'tablette', 'api')",
                    name="ck_paiement_appareil"),
)
# Index des requêtes les plus fréquentes : historique d'un client, chiffre d'un marchand.
Index("ix_paiements_client_date", paiements.c.id_client, paiements.c.cree_le)
Index("ix_paiements_marchand_date", paiements.c.id_marchand, paiements.c.cree_le)

remboursements = Table(
    "remboursements", schema,
    Column("id_remboursement", BigInteger, primary_key=True),
    Column("id_paiement", BigInteger, ForeignKey("paiements.id_paiement"), nullable=False),
    Column("montant_mineur", BigInteger, nullable=False),
    Column("motif", String(30), nullable=False),
    Column("cree_le", DateTime(timezone=True), nullable=False),
    CheckConstraint("montant_mineur > 0", name="ck_remboursement_montant"),
)

litiges = Table(
    "litiges", schema,
    Column("id_litige", BigInteger, primary_key=True),
    Column("id_paiement", BigInteger, ForeignKey("paiements.id_paiement"), nullable=False,
           unique=True),
    Column("motif", String(30), nullable=False),
    Column("statut", String(12), nullable=False),
    Column("cree_le", DateTime(timezone=True), nullable=False),
)

journal_audit = Table(
    "journal_audit", schema,
    Column("id_evenement", BigInteger, primary_key=True),
    Column("acteur", String(60), nullable=False),
    Column("action", String(30), nullable=False),
    Column("entite", String(30), nullable=False),
    Column("id_entite", String(40), nullable=False),
    Column("horodatage", DateTime(timezone=True), nullable=False),
    Column("details", Text),
)

# Journal des changements (« outbox ») : alimenté par des déclencheurs, il joue le rôle du
# journal de transactions que Debezium lit en production (capture des changements, CDC).
journal_changements = Table(
    "journal_changements", schema,
    Column("position", Integer, primary_key=True, autoincrement=True),
    Column("table_source", String(30), nullable=False),
    Column("operation", String(6), nullable=False),
    Column("id_ligne", BigInteger, nullable=False),
    Column("horodatage", DateTime(timezone=True), nullable=False),
)
