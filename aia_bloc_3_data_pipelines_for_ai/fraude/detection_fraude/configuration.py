# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Configuration par environnement (dev / prod) et lecture des secrets.

* Critère 3.3 (reproductibilité) : le même code tourne en développement et en production ;
  seul le fichier ``config/<environnement>.yaml`` change, choisi par ``FRAUDE_ENV``.
* Critère 5.2 (gestion des secrets) : les fichiers YAML ne contiennent **que** des réglages non
  secrets. Mots de passe, jetons, sel de hachage et chaîne de connexion à la base sont lus dans
  l'environnement (``FRAUDE_<NOM>``) ou dans un fichier monté par l'orchestrateur
  (``FRAUDE_<NOM>_FICHIER``, convention des secrets Docker et Kubernetes).

CHOIX
-----
- Retenu : des sections immuables (``dataclass(frozen=True)``) construites une fois, puis une
  vérification qui refuse les combinaisons dangereuses avant tout traitement (échec rapide).
- Retenu : les secrets ne sont jamais stockés dans l'objet de configuration. Ils sont lus à la
  demande par ``lire_secret`` : aucun secret dans un ``repr`` ni dans un journal.
- Retenu : priorité variables d'environnement > ``<environnement>.yaml`` > ``base.yaml``.
- Retenu : en développement seulement, un sel de hachage aléatoire est généré au premier usage et
  rangé dans le dossier de données (jamais versionné) ; en production il est obligatoire.
- Écarté : mettre les secrets dans les fichiers YAML ou dans le code (refusé par un test qui
  cherche « password » ou « token » dans ``config``).
- Écarté : un coffre de secrets dédié pour ce projet ; il reste une suite proposée dans
  livrables/aia3/03_securite_conformite.md:170-180.

OÙ
---
- Place : socle du paquet ; tout outil en ligne de commande commence par ``charger_configuration``.
- Appelants : tous les modules exécutables (producteur, consommateur, entraînement, rapport,
  supervision, retraitement, tâches Airflow, mode local) et les tests.
- Dépendances : PyYAML pour la lecture des fichiers ; bibliothèque standard pour le reste.
- Fichiers lus : ``base.yaml``, ``dev.yaml``, ``prod.yaml`` du dossier ``config``.

COMMENT
-------
1. ``charger_configuration`` choisit l'environnement (argument, sinon ``FRAUDE_ENV``, sinon
   ``dev``), fusionne récursivement ``base.yaml`` puis le fichier de l'environnement, applique les
   variables d'environnement non secrètes, convertit chaque valeur (float, int, tuple, Path) et
   appelle ``verifier_configuration``.
2. ``lire_secret`` lit d'abord ``FRAUDE_<NOM>_FICHIER`` (prioritaire, retour à la ligne final
   retiré), puis ``FRAUDE_<NOM>`` ; absent et obligatoire : ``ErreurConfiguration``.
3. ``verifier_configuration`` refuse : file ou source de type inconnu, fractions
   entraînement + validation qui ne laissent aucun jeu de test ; en production, une file autre que
   Kafka, une API hors HTTPS, le canal SMTP sans TLS, un DSN à identifiants écrit dans le YAML.
Tests qui le prouvent : test_environnement_inconnu (tests/test_configuration.py:57-59),
test_le_sel_est_obligatoire_en_production (tests/test_configuration.py:67-69),
test_prod_exige_https_pour_l_api (tests/test_configuration.py:136-143),
test_les_secrets_n_apparaissent_pas_dans_la_representation (tests/test_configuration.py:108-112),
test_la_production_exige_une_source_api_en_https (tests/test_configuration_refus.py:892-906) et
test_un_secret_obligatoire_absent_est_refuse_avec_le_nom_des_deux_sources_possibles
(tests/test_configuration_refus.py:165-174).

POURQUOI
--------
- Environnements identiques sauf la configuration, preuves de reproductibilité :
  livrables/aia3/02_pipeline_et_qualite.md:189-208.
- Mécanisme unique de lecture des secrets, rotation, absence de secret dans le dépôt :
  livrables/aia3/03_securite_conformite.md:129-156.
- HTTPS et TLS imposés en production : livrables/aia3/03_securite_conformite.md:113-127.
- Production figée sur les contraintes d'Airflow 2.10.5 : docker/contraintes-ml.txt:6.
- Indicateurs 3.3 et 5.2 du référentiel AIA RNCP41993 : texte du référentiel à joindre au dossier.

JOURNALISATION
--------------
Ce module ne journalise rien : il lève ``ErreurConfiguration`` et laisse l'appelant décider. Les
outils en ligne de commande journalisent l'échec (``configuration_invalide`` pour le producteur,
``demarrage_impossible`` pour le consommateur, niveau ERROR) et retournent le code 2. Les valeurs
des secrets ne sont jamais écrites, ni dans le message d'erreur ni dans un événement.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RACINE_PROJET = Path(__file__).resolve().parent.parent
ENVIRONNEMENTS = ("dev", "prod")
PREFIXE_ENV = "FRAUDE_"


class ErreurConfiguration(Exception):
    """Configuration absente, incomplète ou incohérente : on s'arrête avant de traiter."""


# --------------------------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------------------------
def lire_secret(
    nom: str, *, obligatoire: bool = False, environ: Mapping[str, str] | None = None
) -> str | None:
    """Lit un secret depuis ``FRAUDE_<NOM>``, sinon depuis le fichier ``FRAUDE_<NOM>_FICHIER``.

    Le fichier est prioritaire sur la variable : c'est le cas d'un secret Docker monté sous
    ``/run/secrets``. La valeur lue est nettoyée de ses espaces de bord (retour à la ligne final).
    """
    env = os.environ if environ is None else environ
    chemin = env.get(f"{PREFIXE_ENV}{nom}_FICHIER")
    if chemin:
        try:
            valeur = Path(chemin).read_text(encoding="utf-8").strip()
        except OSError as erreur:
            raise ErreurConfiguration(
                f"Le fichier de secret indiqué par {PREFIXE_ENV}{nom}_FICHIER est illisible "
                f"({erreur.__class__.__name__})."
            ) from erreur
        if valeur:
            return valeur
    valeur = env.get(f"{PREFIXE_ENV}{nom}", "").strip()
    if valeur:
        return valeur
    if obligatoire:
        raise ErreurConfiguration(
            f"Secret obligatoire absent : définir {PREFIXE_ENV}{nom} "
            f"ou {PREFIXE_ENV}{nom}_FICHIER."
        )
    return None


# --------------------------------------------------------------------------------------------
# Sections de configuration (objets immuables)
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Relances:
    """Politique de relance avec attente exponentielle (critère 4.3)."""

    max_tentatives: int = 5
    attente_initiale_s: float = 1.0
    facteur: float = 2.0
    attente_max_s: float = 60.0
    gigue: float = 0.1  # fraction aléatoire ajoutée à l'attente, pour désynchroniser les clients


@dataclass(frozen=True)
class SourceConfig:
    type: str = "api"  # "api" (temps réel) ou "jeu" (rejeu du fichier)
    api_url: str = ""
    intervalle_s: float = 60.0
    delai_attente_s: float = 30.0
    relances: Relances = field(default_factory=Relances)
    jeu_chemin: Path = Path("data/fraudTest.csv")
    jeu_url: str = ""
    jeu_taille_octets: int = 0
    jeu_sha256: str = ""


@dataclass(frozen=True)
class FileConfig:
    type: str = "sqlite"  # "sqlite" (local, sans Docker) ou "kafka" (production)
    chemin_sqlite: Path = Path("donnees/file_messages.db")
    kafka_serveurs: str = "localhost:9092"
    sujet_valides: str = "transactions.valides"
    sujet_rejets: str = "transactions.rejets"
    groupe_consommateur: str = "consommateur-fraude"
    groupe_rejets: str = "archiveur-rejets"
    taille_lot: int = 500
    attente_lecture_s: float = 1.0
    partitions_valides: int = 3  # clé de partition = carte hachée : l'ordre par carte est gardé
    replication: int = 1  # un seul courtier en local ; 3 en production réelle


@dataclass(frozen=True)
class EntrepotConfig:
    dsn: str = ""  # vide : en production, il vient du secret FRAUDE_DSN_ENTREPOT
    retention_bronze_jours: int = 90


@dataclass(frozen=True)
class ModeleConfig:
    dossier: Path = Path("donnees/modeles")
    precision_cible: float = 0.90
    graine: int = 42
    fraction_entrainement: float = 0.70
    fraction_validation: float = 0.15
    candidats: tuple[str, ...] = ("regression_logistique", "gradient_boosting")
    pr_auc_minimum: float = 0.30
    tolerance_regression: float = 0.02


@dataclass(frozen=True)
class MlflowConfig:
    tracking_uri: str = "sqlite:///donnees/mlflow.db"
    experience: str = "detection-fraude"
    enregistrer_modele: bool = False


@dataclass(frozen=True)
class NotificationConfig:
    canaux: tuple[str, ...] = ("journal", "fichier")
    fichier_alertes: Path = Path("donnees/alertes.jsonl")
    max_tentatives: int = 5
    exiger_tls: bool = False
    smtp_hote: str = ""
    smtp_port: int = 587
    smtp_expediteur: str = ""
    smtp_destinataires: tuple[str, ...] = ()


@dataclass(frozen=True)
class RapportConfig:
    dossier: Path = Path("donnees/rapports")
    # Le rapport planifié à 07:00 doit être livré dans ce délai (donc avant 08:00).
    sla_minutes: int = 60


@dataclass(frozen=True)
class JournalConfig:
    niveau: str = "INFO"
    console: str = "lisible"  # "lisible" (démonstration) ou "json" (production)
    fichier: Path | None = Path("donnees/journaux/audit.jsonl")


@dataclass(frozen=True)
class SlaConfig:
    """Seuils de supervision : au-delà, une alerte est émise (critère 6.1)."""

    latence_notification_s: float = 60.0
    fraicheur_max_min: float = 15.0
    taux_rejets_max: float = 0.02
    alertes_en_attente_max_min: float = 10.0


@dataclass(frozen=True)
class Configuration:
    environnement: str
    racine_donnees: Path
    source: SourceConfig
    file: FileConfig
    entrepot: EntrepotConfig
    modele: ModeleConfig
    mlflow: MlflowConfig
    notification: NotificationConfig
    rapport: RapportConfig
    journal: JournalConfig
    sla: SlaConfig
    longueur_hash: int = 32

    # ----- Accès aux valeurs secrètes (lues à la demande, jamais conservées) -----
    def dsn_entrepot(self) -> str:
        """Chaîne de connexion à l'entrepôt ; en production elle est un secret."""
        secret = lire_secret("DSN_ENTREPOT")
        if secret:
            return secret
        if self.entrepot.dsn:
            return self.entrepot.dsn
        raise ErreurConfiguration("Entrepôt non configuré : définir FRAUDE_DSN_ENTREPOT.")

    def sel_hachage(self) -> bytes:
        """Clé secrète du hachage des numéros de carte (HMAC).

        En production la clé est obligatoire. En développement, à défaut, on en génère une au
        hasard au premier usage et on la range dans le dossier de données (jamais versionné) :
        les empreintes restent stables d'un lancement à l'autre sans qu'aucune clé ne figure
        dans le code.
        """
        secret = lire_secret("SEL_HACHAGE")
        if secret:
            return secret.encode("utf-8")
        if self.environnement == "prod":
            raise ErreurConfiguration(
                "FRAUDE_SEL_HACHAGE (ou FRAUDE_SEL_HACHAGE_FICHIER) est obligatoire en production."
            )
        chemin = self.racine_donnees / ".sel_hachage_dev"
        if not chemin.exists():
            chemin.parent.mkdir(parents=True, exist_ok=True)
            chemin.write_text(secrets.token_hex(32), encoding="utf-8")
        return chemin.read_text(encoding="utf-8").strip().encode("utf-8")

    def webhook_url(self) -> str | None:
        """URL du webhook : elle embarque souvent un jeton, donc c'est un secret."""
        return lire_secret("WEBHOOK_URL")


# --------------------------------------------------------------------------------------------
# Chargement
# --------------------------------------------------------------------------------------------
def _fusionner(base: dict[str, Any], surcharge: Mapping[str, Any]) -> dict[str, Any]:
    """Fusion récursive : les clés de ``surcharge`` remplacent celles de ``base``."""
    resultat = dict(base)
    for cle, valeur in surcharge.items():
        if isinstance(valeur, Mapping) and isinstance(resultat.get(cle), dict):
            resultat[cle] = _fusionner(resultat[cle], valeur)
        else:
            resultat[cle] = valeur
    return resultat


def _chemin(valeur: str | None, racine_donnees: Path) -> Path | None:
    """Résout les jetons ``{donnees}`` et ``{racine}`` d'un chemin du fichier YAML."""
    if valeur in (None, ""):
        return None
    texte = str(valeur).replace("{donnees}", str(racine_donnees))
    texte = texte.replace("{racine}", str(RACINE_PROJET))
    return Path(texte)


def _texte(valeur: str | None, racine_donnees: Path) -> str:
    if not valeur:
        return ""
    return str(valeur).replace("{donnees}", racine_donnees.as_posix())


def _surcharges_environnement(brut: dict[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    """Applique les variables d'environnement *non secrètes* qui remplacent le YAML."""
    cartes = {
        "FRAUDE_API_URL": ("source", "api_url"),
        "FRAUDE_KAFKA_SERVEURS": ("file", "kafka_serveurs"),
        "FRAUDE_TYPE_FILE": ("file", "type"),
        "FRAUDE_TYPE_SOURCE": ("source", "type"),
        "FRAUDE_JEU_CHEMIN": ("source", "jeu_chemin"),
        "FRAUDE_NIVEAU_JOURNAL": ("journal", "niveau"),
        "FRAUDE_SMTP_HOTE": ("notification", "smtp_hote"),
        "FRAUDE_SMTP_PORT": ("notification", "smtp_port"),
        "FRAUDE_SMTP_EXPEDITEUR": ("notification", "smtp_expediteur"),
        "FRAUDE_SMTP_DESTINATAIRES": ("notification", "smtp_destinataires"),
        "MLFLOW_TRACKING_URI": ("mlflow", "tracking_uri"),
    }
    for variable, (section, cle) in cartes.items():
        valeur = environ.get(variable)
        if valeur:
            brut.setdefault(section, {})
            brut[section][cle] = valeur
    return brut


def charger_configuration(
    environnement: str | None = None,
    *,
    dossier_config: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Configuration:
    """Construit la configuration de l'environnement demandé.

    Ordre de priorité : variables d'environnement > fichier ``<environnement>.yaml`` >
    ``base.yaml``. L'environnement vient de l'argument, sinon de ``FRAUDE_ENV``, sinon ``dev``.
    """
    env = os.environ if environ is None else environ
    nom = (environnement or env.get("FRAUDE_ENV") or "dev").strip().lower()
    if nom not in ENVIRONNEMENTS:
        raise ErreurConfiguration(
            f"Environnement inconnu : {nom!r}. Valeurs admises : {', '.join(ENVIRONNEMENTS)}."
        )
    dossier = dossier_config or Path(env.get("FRAUDE_CONFIG_DIR") or RACINE_PROJET / "config")
    brut: dict[str, Any] = {}
    for fichier in ("base.yaml", f"{nom}.yaml"):
        chemin = dossier / fichier
        if not chemin.exists():
            if fichier == f"{nom}.yaml":
                raise ErreurConfiguration(f"Fichier de configuration introuvable : {chemin}")
            continue
        contenu = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
        brut = _fusionner(brut, contenu)
    brut = _surcharges_environnement(brut, env)

    racine_donnees = Path(env.get("FRAUDE_DONNEES") or RACINE_PROJET / "donnees")
    source = brut.get("source", {})
    file_ = brut.get("file", {})
    entrepot = brut.get("entrepot", {})
    modele = brut.get("modele", {})
    mlflow_ = brut.get("mlflow", {})
    notif = brut.get("notification", {})
    rapport = brut.get("rapport", {})
    journal = brut.get("journal", {})
    sla = brut.get("sla", {})

    destinataires = notif.get("smtp_destinataires", ())
    if isinstance(destinataires, str):
        destinataires = [d.strip() for d in destinataires.split(",") if d.strip()]

    configuration = Configuration(
        environnement=nom,
        racine_donnees=racine_donnees,
        longueur_hash=int(brut.get("masquage", {}).get("longueur_hash", 32)),
        source=SourceConfig(
            type=source.get("type", "api"),
            api_url=source.get("api_url", ""),
            intervalle_s=float(source.get("intervalle_s", 60)),
            delai_attente_s=float(source.get("delai_attente_s", 30)),
            relances=Relances(**source.get("relances", {})),
            jeu_chemin=_chemin(
                source.get("jeu_chemin", "{racine}/data/fraudTest.csv"), racine_donnees
            )
            or Path("data/fraudTest.csv"),
            jeu_url=source.get("jeu_url", ""),
            jeu_taille_octets=int(source.get("jeu_taille_octets", 0)),
            jeu_sha256=source.get("jeu_sha256", ""),
        ),
        file=FileConfig(
            type=file_.get("type", "sqlite"),
            chemin_sqlite=_chemin(
                file_.get("chemin_sqlite", "{donnees}/file_messages.db"), racine_donnees
            )
            or Path("file_messages.db"),
            kafka_serveurs=file_.get("kafka_serveurs", "localhost:9092"),
            sujet_valides=file_.get("sujet_valides", "transactions.valides"),
            sujet_rejets=file_.get("sujet_rejets", "transactions.rejets"),
            groupe_consommateur=file_.get("groupe_consommateur", "consommateur-fraude"),
            groupe_rejets=file_.get("groupe_rejets", "archiveur-rejets"),
            taille_lot=int(file_.get("taille_lot", 500)),
            attente_lecture_s=float(file_.get("attente_lecture_s", 1.0)),
            partitions_valides=int(file_.get("partitions_valides", 3)),
            replication=int(file_.get("replication", 1)),
        ),
        entrepot=EntrepotConfig(
            dsn=_texte(entrepot.get("dsn"), racine_donnees),
            retention_bronze_jours=int(entrepot.get("retention_bronze_jours", 90)),
        ),
        modele=ModeleConfig(
            dossier=_chemin(modele.get("dossier", "{donnees}/modeles"), racine_donnees)
            or Path("modeles"),
            precision_cible=float(modele.get("precision_cible", 0.90)),
            graine=int(modele.get("graine", 42)),
            fraction_entrainement=float(modele.get("fraction_entrainement", 0.70)),
            fraction_validation=float(modele.get("fraction_validation", 0.15)),
            candidats=tuple(modele.get("candidats", ModeleConfig.candidats)),
            pr_auc_minimum=float(modele.get("pr_auc_minimum", 0.30)),
            tolerance_regression=float(modele.get("tolerance_regression", 0.02)),
        ),
        mlflow=MlflowConfig(
            tracking_uri=_texte(
                mlflow_.get("tracking_uri", "sqlite:///{donnees}/mlflow.db"), racine_donnees
            ),
            experience=mlflow_.get("experience", "detection-fraude"),
            enregistrer_modele=bool(mlflow_.get("enregistrer_modele", False)),
        ),
        notification=NotificationConfig(
            canaux=tuple(notif.get("canaux", ("journal", "fichier"))),
            fichier_alertes=_chemin(
                notif.get("fichier_alertes", "{donnees}/alertes.jsonl"), racine_donnees
            )
            or Path("alertes.jsonl"),
            max_tentatives=int(notif.get("max_tentatives", 5)),
            exiger_tls=bool(notif.get("exiger_tls", False)),
            smtp_hote=notif.get("smtp_hote", ""),
            smtp_port=int(notif.get("smtp_port", 587)),
            smtp_expediteur=notif.get("smtp_expediteur", ""),
            smtp_destinataires=tuple(destinataires),
        ),
        rapport=RapportConfig(
            dossier=_chemin(rapport.get("dossier", "{donnees}/rapports"), racine_donnees)
            or Path("rapports"),
            sla_minutes=int(rapport.get("sla_minutes", 60)),
        ),
        journal=JournalConfig(
            niveau=str(journal.get("niveau", "INFO")).upper(),
            console=journal.get("console", "lisible"),
            fichier=_chemin(
                journal.get("fichier", "{donnees}/journaux/audit.jsonl"), racine_donnees
            ),
        ),
        sla=SlaConfig(**{k: float(v) for k, v in sla.items()}),
    )
    verifier_configuration(configuration)
    return configuration


def verifier_configuration(config: Configuration) -> None:
    """Refuse les combinaisons dangereuses avant tout traitement (échec rapide)."""
    if config.file.type not in ("sqlite", "kafka"):
        raise ErreurConfiguration(f"Type de file inconnu : {config.file.type!r}.")
    if config.source.type not in ("api", "jeu"):
        raise ErreurConfiguration(f"Type de source inconnu : {config.source.type!r}.")
    if not 0 < config.modele.fraction_entrainement + config.modele.fraction_validation < 1:
        raise ErreurConfiguration("Les fractions d'entraînement et de validation laissent 0 test.")
    if config.environnement == "prod":
        if config.file.type != "kafka":
            raise ErreurConfiguration("La production exige une file Kafka (file.type: kafka).")
        if not config.source.api_url.startswith("https://"):
            raise ErreurConfiguration("La source API doit être jointe en HTTPS en production.")
        if "smtp" in config.notification.canaux and not config.notification.exiger_tls:
            raise ErreurConfiguration("Le canal SMTP exige TLS en production (exiger_tls: true).")
        # Le DSN de production est un secret : il ne doit pas être écrit dans le fichier YAML.
        if config.entrepot.dsn and "@" in config.entrepot.dsn:
            raise ErreurConfiguration(
                "Le DSN du fichier YAML contient des identifiants : utiliser FRAUDE_DSN_ENTREPOT."
            )
