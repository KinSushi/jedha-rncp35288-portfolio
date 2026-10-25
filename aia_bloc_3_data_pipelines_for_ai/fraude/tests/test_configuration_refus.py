# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Configuration (critères 3.3 et 5.2) : chaque refus, chaque secret, chaque valeur par défaut.

Principes de ces tests :

* chaque ``ErreurConfiguration`` est vérifiée par son **message exact** (une exploitation ne
  doit pas avoir à deviner ce qui manque) ;
* le chargement reçoit un ``environ`` explicite : le résultat ne dépend jamais de la machine ;
* quand un refus est testé, on prouve aussi qu'une valeur voisine, juste au bon côté de la
  limite, est acceptée, et que rien n'a été écrit ni modifié.
"""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import replace
from pathlib import Path

import pytest

from detection_fraude.configuration import (
    RACINE_PROJET,
    Configuration,
    EntrepotConfig,
    ErreurConfiguration,
    FileConfig,
    JournalConfig,
    MlflowConfig,
    ModeleConfig,
    NotificationConfig,
    RapportConfig,
    Relances,
    SlaConfig,
    SourceConfig,
    charger_configuration,
    lire_secret,
    verifier_configuration,
)

MESSAGE_SEL_ABSENT = "Secret obligatoire absent : définir FRAUDE_SEL ou FRAUDE_SEL_FICHIER."
PROD_VALIDE = """\
file:
  type: kafka
source:
  api_url: "https://exemple.test/x"
notification:
  canaux: ["journal", "smtp"]
  exiger_tls: true
"""
DEV_LAXISTE = """\
file:
  type: sqlite
source:
  api_url: "http://exemple.test/x"
notification:
  canaux: ["journal", "smtp"]
  exiger_tls: false
entrepot:
  dsn: "postgresql://admin:mdp@hote/base"
"""
CONFIG_COMPLETE = """\
masquage:
  longueur_hash: 48
source:
  type: jeu
  api_url: "https://api.exemple.test/tx"
  intervalle_s: 30
  delai_attente_s: "12.5"
  relances:
    max_tentatives: 7
    attente_initiale_s: 0.5
    facteur: 3.0
    attente_max_s: 20.0
    gigue: 0.25
  jeu_chemin: "{donnees}/jeu/mon.csv"
  jeu_url: "https://exemple.test/jeu.csv"
  jeu_taille_octets: "1234"
  jeu_sha256: "abc123"
file:
  type: kafka
  chemin_sqlite: "{racine}/q/file.db"
  kafka_serveurs: "k1:9092,k2:9092"
  sujet_valides: "valides.test"
  sujet_rejets: "rejets.test"
  groupe_consommateur: "g-conso"
  groupe_rejets: "g-rejets"
  taille_lot: "250"
  attente_lecture_s: 2
  partitions_valides: 6
  replication: 3
entrepot:
  dsn: "sqlite:///{donnees}/e.db"
  retention_bronze_jours: 30
modele:
  dossier: "{donnees}/m"
  precision_cible: 0.95
  graine: 7
  fraction_entrainement: 0.6
  fraction_validation: 0.2
  candidats: ["a", "b", "c"]
  pr_auc_minimum: 0.4
  tolerance_regression: 0.05
mlflow:
  tracking_uri: "sqlite:///{donnees}/ml.db"
  experience: "exp-test"
  enregistrer_modele: true
notification:
  canaux: ["journal", "webhook"]
  fichier_alertes: "{donnees}/a.jsonl"
  max_tentatives: 3
  exiger_tls: true
  smtp_hote: "smtp.exemple.test"
  smtp_port: 2525
  smtp_expediteur: "fraude@exemple.test"
  smtp_destinataires: ["a@exemple.test", "b@exemple.test"]
rapport:
  dossier: "{donnees}/r"
  sla_minutes: 45
journal:
  niveau: debug
  console: json
  fichier: "{donnees}/j/a.jsonl"
sla:
  latence_notification_s: 30
  fraicheur_max_min: 5
  taux_rejets_max: 0.1
  alertes_en_attente_max_min: 2
"""


@pytest.fixture(autouse=True)
def environnement_propre(monkeypatch):
    """Aucune variable FRAUDE_* de la machine ne doit influencer un test."""
    for variable in list(os.environ):
        if variable.startswith("FRAUDE_") or variable == "MLFLOW_TRACKING_URI":
            monkeypatch.delenv(variable)


def ecrire_dossier(tmp_path: Path, nom: str = "cfg", **fichiers: str) -> Path:
    """Un dossier de configuration contenant ``<nom>.yaml`` pour chaque argument."""
    dossier = tmp_path / nom
    dossier.mkdir(parents=True, exist_ok=True)
    for fichier, contenu in fichiers.items():
        (dossier / f"{fichier}.yaml").write_text(contenu, encoding="utf-8", newline="\n")
    return dossier


def charger(
    tmp_path: Path, contenu: str = "{}\n", *, nom: str = "dev", base: str | None = None, **env: str
) -> Configuration:
    """Charge ``<nom>.yaml`` (et ``base.yaml``) avec un environnement explicite et isolé."""
    fichiers = {nom: contenu}
    if base is not None:
        fichiers["base"] = base
    dossier = ecrire_dossier(tmp_path, **fichiers)
    environ = {"FRAUDE_DONNEES": str(tmp_path / "donnees"), **env}
    return charger_configuration(nom, dossier_config=dossier, environ=environ)


# --------------------------------------------------------------------------------------------
# lire_secret
# --------------------------------------------------------------------------------------------
def test_un_secret_obligatoire_absent_est_refuse_avec_le_nom_des_deux_sources_possibles():
    """Sans variable ni fichier, un secret obligatoire arrête le démarrage, message exact."""
    for nom in ("SEL_HACHAGE", "DSN_ENTREPOT", "WEBHOOK_URL"):
        with pytest.raises(ErreurConfiguration) as refus:
            lire_secret(nom, obligatoire=True, environ={})
        assert str(refus.value) == (
            f"Secret obligatoire absent : définir FRAUDE_{nom} ou FRAUDE_{nom}_FICHIER."
        )
    assert lire_secret("SEL_HACHAGE", environ={}) is None
    assert lire_secret("SEL_HACHAGE", obligatoire=False, environ={}) is None


def test_un_secret_vide_ou_blanc_vaut_un_secret_absent_et_une_valeur_est_nettoyee():
    """Une variable vide ou blanche est absente ; une valeur réelle perd ses espaces de bord."""
    for valeur in ("", " ", "  \t\n "):
        environ = {"FRAUDE_SEL": valeur}
        assert lire_secret("SEL", environ=environ) is None
        with pytest.raises(ErreurConfiguration) as refus:
            lire_secret("SEL", obligatoire=True, environ=environ)
        assert str(refus.value) == MESSAGE_SEL_ABSENT
        assert environ == {"FRAUDE_SEL": valeur}
    assert lire_secret("SEL", environ={"FRAUDE_SEL": "  a b\n"}) == "a b"
    assert lire_secret("SEL", environ={"FRAUDE_SEL": "valeur"}) == "valeur"
    assert lire_secret("SEL", obligatoire=True, environ={"FRAUDE_SEL": " x "}) == "x"


def test_le_fichier_de_secret_prime_sur_la_variable_et_son_contenu_est_nettoye(tmp_path):
    """Un secret monté (Docker) l'emporte sur la variable ; retour à la ligne final retiré."""
    fichier = tmp_path / "secret"
    fichier.write_text("  du-fichier é\nsuite  \n", encoding="utf-8")
    contenu_avant = fichier.read_bytes()
    environ = {"FRAUDE_SEL_FICHIER": str(fichier), "FRAUDE_SEL": "de-la-variable"}
    assert lire_secret("SEL", environ=environ) == "du-fichier é\nsuite"
    assert fichier.read_bytes() == contenu_avant


def test_un_fichier_de_secret_vide_ou_un_chemin_vide_laisse_la_main_a_la_variable(tmp_path):
    """Fichier vide, blanc ou chemin vide : la variable est lue ; sans elle, secret absent."""
    for contenu in ("", "  \n\t"):
        fichier = tmp_path / "secret"
        fichier.write_text(contenu, encoding="utf-8")
        environ = {"FRAUDE_SEL_FICHIER": str(fichier), "FRAUDE_SEL": " de-la-variable "}
        assert lire_secret("SEL", environ=environ) == "de-la-variable"
        sans_variable = {"FRAUDE_SEL_FICHIER": str(fichier)}
        assert lire_secret("SEL", environ=sans_variable) is None
        with pytest.raises(ErreurConfiguration) as refus:
            lire_secret("SEL", obligatoire=True, environ=sans_variable)
        assert str(refus.value) == MESSAGE_SEL_ABSENT
    chemin_vide = {"FRAUDE_SEL_FICHIER": "", "FRAUDE_SEL": "de-la-variable"}
    assert lire_secret("SEL", environ=chemin_vide) == "de-la-variable"
    assert lire_secret("SEL", environ={"FRAUDE_SEL_FICHIER": ""}) is None


def test_un_fichier_de_secret_illisible_arrete_tout_meme_si_la_variable_existe(tmp_path):
    """Le fichier indiqué mais introuvable est une erreur franche, jamais un repli silencieux."""
    environ = {"FRAUDE_SEL_HACHAGE_FICHIER": str(tmp_path / "absent"), "FRAUDE_SEL_HACHAGE": "x"}
    environ_avant = dict(environ)
    with pytest.raises(ErreurConfiguration) as refus:
        lire_secret("SEL_HACHAGE", environ=environ)
    assert str(refus.value) == (
        "Le fichier de secret indiqué par FRAUDE_SEL_HACHAGE_FICHIER est illisible "
        "(FileNotFoundError)."
    )
    assert isinstance(refus.value.__cause__, FileNotFoundError)
    assert str(tmp_path) not in str(refus.value)
    assert environ == environ_avant


def test_un_chemin_de_secret_qui_est_un_dossier_est_illisible(tmp_path):
    """Un dossier n'est pas un fichier de secret : le nom de l'erreur système est rapporté."""
    with pytest.raises(ErreurConfiguration) as refus:
        lire_secret("SEL", environ={"FRAUDE_SEL_FICHIER": str(tmp_path)})
    message = str(refus.value)
    debut = "Le fichier de secret indiqué par FRAUDE_SEL_FICHIER est illisible ("
    assert message.startswith(debut)
    assert re.fullmatch(r".*\((PermissionError|IsADirectoryError)\)\.", message)


def test_sans_environ_les_secrets_sont_lus_dans_l_environnement_du_processus(
    monkeypatch, tmp_path
):
    """Par défaut c'est ``os.environ`` ; un ``environ`` explicite, même vide, le remplace."""
    monkeypatch.setenv("FRAUDE_SEL", "du-processus")
    assert lire_secret("SEL") == "du-processus"
    assert lire_secret("SEL", environ={}) is None
    assert lire_secret("SEL", environ={"FRAUDE_SEL": "explicite"}) == "explicite"
    with pytest.raises(ErreurConfiguration):
        lire_secret("AUTRE", obligatoire=True)
    # L'URL du webhook embarque un jeton : c'est un secret, lu à la demande, jamais stocké.
    dev = charger(tmp_path)
    assert dev.webhook_url() is None
    monkeypatch.setenv("FRAUDE_WEBHOOK_URL", " https://hooks.exemple.test/jeton ")
    assert dev.webhook_url() == "https://hooks.exemple.test/jeton"
    assert "jeton" not in repr(dev)


# --------------------------------------------------------------------------------------------
# sel_hachage, dsn_entrepot, webhook_url
# --------------------------------------------------------------------------------------------
def test_en_production_le_sel_est_obligatoire_meme_si_un_sel_de_developpement_existe(
    tmp_path, monkeypatch
):
    """La production ne se rabat jamais sur le sel local : message exact, fichier non lu."""
    prod = charger(tmp_path, PROD_VALIDE, nom="prod")
    sel_local = prod.racine_donnees / ".sel_hachage_dev"
    sel_local.parent.mkdir(parents=True)
    sel_local.write_text("sel-de-dev", encoding="utf-8")
    contenu_avant = sel_local.read_bytes()
    monkeypatch.setenv("FRAUDE_SEL_HACHAGE", "  ")
    with pytest.raises(ErreurConfiguration) as refus:
        prod.sel_hachage()
    assert str(refus.value) == (
        "FRAUDE_SEL_HACHAGE (ou FRAUDE_SEL_HACHAGE_FICHIER) est obligatoire en production."
    )
    assert sel_local.read_bytes() == contenu_avant


def test_le_sel_vient_d_abord_de_l_environnement_et_laisse_le_dossier_de_donnees_inchange(
    tmp_path, monkeypatch
):
    """Avec un secret fourni, le sel local n'est pas généré : aucun dossier n'est créé."""
    dev = charger(tmp_path)
    existait_avant = dev.racine_donnees.exists()
    monkeypatch.setenv("FRAUDE_SEL_HACHAGE", "  sél-de-test  ")
    assert dev.sel_hachage() == "sél-de-test".encode()
    fichier = tmp_path / "sel.txt"
    fichier.write_text("sel-du-fichier\n", encoding="utf-8")
    monkeypatch.setenv("FRAUDE_SEL_HACHAGE_FICHIER", str(fichier))
    assert dev.sel_hachage() == b"sel-du-fichier"
    assert dev.racine_donnees.exists() == existait_avant


def test_le_sel_local_est_genere_une_fois_en_64_hexadecimaux_puis_jamais_ecrase(tmp_path):
    """Sans secret : sel aléatoire (32 octets) rangé dans les données, relu, jamais écrasé."""
    profonde = tmp_path / "a" / "b" / "donnees"
    dev = charger(tmp_path, FRAUDE_DONNEES=str(profonde))
    assert not profonde.parent.exists()
    premier = dev.sel_hachage()
    fichier = profonde / ".sel_hachage_dev"
    contenu = fichier.read_text(encoding="utf-8")
    assert re.fullmatch(r"[0-9a-f]{64}", contenu)
    assert premier == contenu.encode()
    assert dev.sel_hachage() == premier
    assert fichier.read_text(encoding="utf-8") == contenu
    existante = tmp_path / "existante"
    existante.mkdir()
    autre = charger(tmp_path, FRAUDE_DONNEES=str(existante)).sel_hachage()
    assert re.fullmatch(r"[0-9a-f]{64}", autre.decode())
    assert autre != premier
    assert (existante / ".sel_hachage_dev").read_bytes() == autre
    # Un sel déjà présent n'est pas régénéré : relu tel quel, espaces de bord retirés.
    ancien = tmp_path / "ancien" / "donnees"
    ancien.mkdir(parents=True)
    (ancien / ".sel_hachage_dev").write_text("  sél-existant \n", encoding="utf-8")
    contenu_avant = (ancien / ".sel_hachage_dev").read_bytes()
    assert charger(tmp_path, FRAUDE_DONNEES=str(ancien)).sel_hachage() == "sél-existant".encode()
    assert (ancien / ".sel_hachage_dev").read_bytes() == contenu_avant


def test_le_dsn_de_l_entrepot_absent_partout_est_refuse_et_le_secret_prime_sur_le_yaml(
    tmp_path, monkeypatch
):
    """Ni secret ni DSN du YAML : refus exact ; sinon secret (variable ou fichier) puis YAML."""
    prod = charger(tmp_path / "p", PROD_VALIDE, nom="prod")
    assert prod.entrepot.dsn == ""
    with pytest.raises(ErreurConfiguration) as refus:
        prod.dsn_entrepot()
    assert str(refus.value) == "Entrepôt non configuré : définir FRAUDE_DSN_ENTREPOT."
    dev = charger(tmp_path / "d", "entrepot:\n  dsn: 'sqlite:///{donnees}/e.db'\n")
    posix = (tmp_path / "d" / "donnees").as_posix()
    assert dev.dsn_entrepot() == f"sqlite:///{posix}/e.db"
    monkeypatch.setenv("FRAUDE_DSN_ENTREPOT", " postgresql://h/b ")
    assert dev.dsn_entrepot() == "postgresql://h/b"
    assert prod.dsn_entrepot() == "postgresql://h/b"
    fichier = tmp_path / "dsn"
    fichier.write_text("postgresql://f/b\n", encoding="utf-8")
    monkeypatch.setenv("FRAUDE_DSN_ENTREPOT_FICHIER", str(fichier))
    assert dev.dsn_entrepot() == "postgresql://f/b"


def test_la_configuration_est_immuable_a_chaque_niveau(tmp_path):
    """Modifier un réglage après chargement est refusé : la configuration reste inchangée."""
    config = charger(tmp_path)
    configuration_avant = dataclasses.asdict(config)
    with pytest.raises(dataclasses.FrozenInstanceError) as refus:
        config.environnement = "prod"  # type: ignore[misc]
    assert str(refus.value) == "cannot assign to field 'environnement'"
    with pytest.raises(dataclasses.FrozenInstanceError) as refus:
        config.file.type = "kafka"  # type: ignore[misc]
    assert str(refus.value) == "cannot assign to field 'type'"
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.source.relances.max_tentatives = 99  # type: ignore[misc]
    assert dataclasses.asdict(config) == configuration_avant


# --------------------------------------------------------------------------------------------
# charger_configuration : environnement et fichiers
# --------------------------------------------------------------------------------------------
def test_un_environnement_inconnu_est_refuse_mais_l_argument_prime_sur_la_variable(tmp_path):
    """Seuls « dev » et « prod » existent ; l'argument l'emporte sur FRAUDE_ENV, casse ignorée."""
    dossier = ecrire_dossier(tmp_path, dev="{}\n", prod=PROD_VALIDE)
    for nom in ("recette", "PROD2", "production"):
        with pytest.raises(ErreurConfiguration) as refus:
            charger_configuration(nom, dossier_config=dossier, environ={})
        assert str(refus.value) == (
            f"Environnement inconnu : {nom.strip().lower()!r}. Valeurs admises : dev, prod."
        )
    with pytest.raises(ErreurConfiguration) as refus:
        charger_configuration(dossier_config=dossier, environ={"FRAUDE_ENV": "staging"})
    assert str(refus.value) == "Environnement inconnu : 'staging'. Valeurs admises : dev, prod."
    voisin = charger_configuration(" Dev ", dossier_config=dossier, environ={"FRAUDE_ENV": "x"})
    assert voisin.environnement == "dev"
    prod = charger_configuration(dossier_config=dossier, environ={"FRAUDE_ENV": " PROD "})
    assert prod.environnement == "prod"
    vide = charger_configuration("", dossier_config=dossier, environ={"FRAUDE_ENV": "prod"})
    assert vide.environnement == "prod"
    defaut = charger_configuration(dossier_config=dossier, environ={})
    assert defaut.environnement == "dev"


def test_un_fichier_d_environnement_absent_est_refuse_meme_si_base_yaml_existe(tmp_path):
    """Le fichier de l'environnement est obligatoire ; base.yaml seul ne suffit pas."""
    dossier = ecrire_dossier(tmp_path, base="source:\n  intervalle_s: 10\n")
    contenu_avant = (dossier / "base.yaml").read_bytes()
    with pytest.raises(ErreurConfiguration) as refus:
        charger_configuration("dev", dossier_config=dossier, environ={})
    assert str(refus.value) == f"Fichier de configuration introuvable : {dossier / 'dev.yaml'}"
    with pytest.raises(ErreurConfiguration) as refus:
        charger_configuration("prod", dossier_config=dossier, environ={})
    assert str(refus.value) == f"Fichier de configuration introuvable : {dossier / 'prod.yaml'}"
    assert (dossier / "base.yaml").read_bytes() == contenu_avant
    assert sorted(p.name for p in dossier.iterdir()) == ["base.yaml"]


def test_un_dossier_de_configuration_inexistant_est_refuse_et_l_argument_prime_sur_la_variable(
    tmp_path,
):
    """Dossier introuvable : refus avec le chemin ; l'argument prime sur FRAUDE_CONFIG_DIR."""
    dossier = tmp_path / "nulle_part"
    with pytest.raises(ErreurConfiguration) as refus:
        charger_configuration("dev", dossier_config=dossier, environ={})
    assert str(refus.value) == f"Fichier de configuration introuvable : {dossier / 'dev.yaml'}"
    avec_variable = {"FRAUDE_CONFIG_DIR": str(dossier)}
    with pytest.raises(ErreurConfiguration) as refus:
        charger_configuration("dev", environ=avec_variable)
    assert str(refus.value) == f"Fichier de configuration introuvable : {dossier / 'dev.yaml'}"
    assert not dossier.exists()
    par_argument = ecrire_dossier(tmp_path, "a", dev="journal:\n  console: json\n")
    par_variable = ecrire_dossier(tmp_path, "b", dev="journal:\n  console: texte\n")
    environ = {"FRAUDE_CONFIG_DIR": str(par_variable), "FRAUDE_DONNEES": str(tmp_path / "d")}
    assert charger_configuration("dev", environ=environ).journal.console == "texte"
    config = charger_configuration("dev", dossier_config=par_argument, environ=environ)
    assert config.journal.console == "json"
    # Sans argument ni variable, c'est le dossier « config » livré avec le projet.
    livres = {"FRAUDE_DONNEES": str(tmp_path / "d")}
    assert (RACINE_PROJET / "config" / "dev.yaml").is_file()
    assert charger_configuration("dev", environ=livres).file.type == "sqlite"
    assert charger_configuration("prod", environ=livres).file.type == "kafka"


def test_un_environ_explicite_vide_ignore_les_variables_du_processus(tmp_path, monkeypatch):
    """``environ={}`` isole le chargement de ``os.environ``, qui reste lui-même inchangé."""
    dossier = ecrire_dossier(tmp_path, dev="{}\n", prod=PROD_VALIDE)
    monkeypatch.setenv("FRAUDE_ENV", "prod")
    monkeypatch.setenv("FRAUDE_DONNEES", str(tmp_path / "ailleurs"))
    monkeypatch.setenv("FRAUDE_API_URL", "https://processus.test/x")
    monkeypatch.setenv("FRAUDE_CONFIG_DIR", str(tmp_path / "inconnu"))
    processus_avant = dict(os.environ)
    config = charger_configuration(dossier_config=dossier, environ={})
    assert config.environnement == "dev"
    assert config.racine_donnees == RACINE_PROJET / "donnees"
    assert config.source.api_url == ""
    processus = charger_configuration(dossier_config=dossier)
    assert processus.environnement == "prod"
    assert processus.racine_donnees == tmp_path / "ailleurs"
    assert processus.source.api_url == "https://processus.test/x"
    assert dict(os.environ) == processus_avant
    # Racine de données absente ou vide : le dossier « donnees » du projet.
    for environ in ({}, {"FRAUDE_DONNEES": ""}):
        vide = charger_configuration("dev", dossier_config=dossier, environ=environ)
        assert vide.racine_donnees == RACINE_PROJET / "donnees"
        assert vide.file.chemin_sqlite == RACINE_PROJET / "donnees" / "file_messages.db"


# --------------------------------------------------------------------------------------------
# charger_configuration : valeurs
# --------------------------------------------------------------------------------------------
def test_un_fichier_vide_donne_chaque_valeur_par_defaut_exacte_et_ne_modifie_pas_le_yaml(
    tmp_path,
):
    """Sans aucun réglage, chaque champ prend sa valeur par défaut documentée."""
    for contenu in ("{}\n", "", "# rien\n"):
        dossier = ecrire_dossier(tmp_path, dev=contenu)
        contenu_avant = (dossier / "dev.yaml").read_bytes()
        config = charger_configuration(
            "dev", dossier_config=dossier, environ={"FRAUDE_DONNEES": str(tmp_path / "donnees")}
        )
        racine = tmp_path / "donnees"
        assert config == Configuration(
            environnement="dev",
            racine_donnees=racine,
            longueur_hash=32,
            source=SourceConfig(
                type="api",
                api_url="",
                intervalle_s=60.0,
                delai_attente_s=30.0,
                relances=Relances(5, 1.0, 2.0, 60.0, 0.1),
                jeu_chemin=Path(str(RACINE_PROJET) + "/data/fraudTest.csv"),
                jeu_url="",
                jeu_taille_octets=0,
                jeu_sha256="",
            ),
            file=FileConfig(
                type="sqlite",
                chemin_sqlite=racine / "file_messages.db",
                kafka_serveurs="localhost:9092",
                sujet_valides="transactions.valides",
                sujet_rejets="transactions.rejets",
                groupe_consommateur="consommateur-fraude",
                groupe_rejets="archiveur-rejets",
                taille_lot=500,
                attente_lecture_s=1.0,
                partitions_valides=3,
                replication=1,
            ),
            entrepot=EntrepotConfig(dsn="", retention_bronze_jours=90),
            modele=ModeleConfig(
                dossier=racine / "modeles",
                precision_cible=0.9,
                graine=42,
                fraction_entrainement=0.7,
                fraction_validation=0.15,
                candidats=("regression_logistique", "gradient_boosting"),
                pr_auc_minimum=0.3,
                tolerance_regression=0.02,
            ),
            mlflow=MlflowConfig(
                tracking_uri=f"sqlite:///{racine.as_posix()}/mlflow.db",
                experience="detection-fraude",
                enregistrer_modele=False,
            ),
            notification=NotificationConfig(
                canaux=("journal", "fichier"),
                fichier_alertes=racine / "alertes.jsonl",
                max_tentatives=5,
                exiger_tls=False,
                smtp_hote="",
                smtp_port=587,
                smtp_expediteur="",
                smtp_destinataires=(),
            ),
            rapport=RapportConfig(dossier=racine / "rapports", sla_minutes=60),
            journal=JournalConfig(
                niveau="INFO", console="lisible", fichier=racine / "journaux" / "audit.jsonl"
            ),
            sla=SlaConfig(
                latence_notification_s=60.0,
                fraicheur_max_min=15.0,
                taux_rejets_max=0.02,
                alertes_en_attente_max_min=10.0,
            ),
        )
        assert (dossier / "dev.yaml").read_bytes() == contenu_avant
        assert not racine.exists()


def test_chaque_cle_du_yaml_est_lue_convertie_et_les_jetons_sont_remplaces(tmp_path):
    """Chaque réglage est repris avec le bon type ; {donnees} et {racine} sont résolus."""
    dossier = ecrire_dossier(tmp_path, dev=CONFIG_COMPLETE)
    contenu_avant = (dossier / "dev.yaml").read_bytes()
    environ = {"FRAUDE_DONNEES": str(tmp_path / "donnees")}
    config = charger_configuration("dev", dossier_config=dossier, environ=environ)
    racine = tmp_path / "donnees"
    posix = racine.as_posix()
    assert config == Configuration(
        environnement="dev",
        racine_donnees=racine,
        longueur_hash=48,
        source=SourceConfig(
            type="jeu",
            api_url="https://api.exemple.test/tx",
            intervalle_s=30.0,
            delai_attente_s=12.5,
            relances=Relances(7, 0.5, 3.0, 20.0, 0.25),
            jeu_chemin=racine / "jeu" / "mon.csv",
            jeu_url="https://exemple.test/jeu.csv",
            jeu_taille_octets=1234,
            jeu_sha256="abc123",
        ),
        file=FileConfig(
            type="kafka",
            chemin_sqlite=Path(str(RACINE_PROJET) + "/q/file.db"),
            kafka_serveurs="k1:9092,k2:9092",
            sujet_valides="valides.test",
            sujet_rejets="rejets.test",
            groupe_consommateur="g-conso",
            groupe_rejets="g-rejets",
            taille_lot=250,
            attente_lecture_s=2.0,
            partitions_valides=6,
            replication=3,
        ),
        entrepot=EntrepotConfig(dsn=f"sqlite:///{posix}/e.db", retention_bronze_jours=30),
        modele=ModeleConfig(
            dossier=racine / "m",
            precision_cible=0.95,
            graine=7,
            fraction_entrainement=0.6,
            fraction_validation=0.2,
            candidats=("a", "b", "c"),
            pr_auc_minimum=0.4,
            tolerance_regression=0.05,
        ),
        mlflow=MlflowConfig(
            tracking_uri=f"sqlite:///{posix}/ml.db", experience="exp-test", enregistrer_modele=True
        ),
        notification=NotificationConfig(
            canaux=("journal", "webhook"),
            fichier_alertes=racine / "a.jsonl",
            max_tentatives=3,
            exiger_tls=True,
            smtp_hote="smtp.exemple.test",
            smtp_port=2525,
            smtp_expediteur="fraude@exemple.test",
            smtp_destinataires=("a@exemple.test", "b@exemple.test"),
        ),
        rapport=RapportConfig(dossier=racine / "r", sla_minutes=45),
        journal=JournalConfig(niveau="DEBUG", console="json", fichier=racine / "j" / "a.jsonl"),
        sla=SlaConfig(
            latence_notification_s=30.0,
            fraicheur_max_min=5.0,
            taux_rejets_max=0.1,
            alertes_en_attente_max_min=2.0,
        ),
    )
    entiers = (config.source.jeu_taille_octets, config.file.taille_lot)
    assert [type(valeur) for valeur in (*entiers, config.notification.smtp_port)] == [int] * 3
    assert (dossier / "dev.yaml").read_bytes() == contenu_avant


def test_un_chemin_vide_ou_nul_retombe_sur_son_repli_et_le_yaml_reste_inchange(tmp_path):
    """Chemin vide : repli relatif ; fichier de journal vide : pas de fichier d'audit."""
    dossier = ecrire_dossier(
        tmp_path,
        dev="source:\n  jeu_chemin: ''\nfile:\n  chemin_sqlite:\nmodele:\n  dossier: ''\n"
        "notification:\n  fichier_alertes: ''\nrapport:\n  dossier:\njournal:\n  fichier: ''\n"
        "entrepot:\n  dsn:\nmlflow:\n  tracking_uri: ''\n",
    )
    contenu_avant = (dossier / "dev.yaml").read_bytes()
    environ = {"FRAUDE_DONNEES": str(tmp_path / "donnees")}
    config = charger_configuration("dev", dossier_config=dossier, environ=environ)
    assert config.source.jeu_chemin == Path("data/fraudTest.csv")
    assert config.file.chemin_sqlite == Path("file_messages.db")
    assert config.modele.dossier == Path("modeles")
    assert config.notification.fichier_alertes == Path("alertes.jsonl")
    assert config.rapport.dossier == Path("rapports")
    assert config.journal.fichier is None
    assert config.entrepot.dsn == ""
    assert config.mlflow.tracking_uri == ""
    assert (dossier / "dev.yaml").read_bytes() == contenu_avant


def test_les_variables_d_environnement_remplacent_le_yaml_sans_modifier_leur_mapping(tmp_path):
    """FRAUDE_* > fichier YAML > défaut : chacune des onze variables prend le dessus."""
    yaml_contenu = (
        "source:\n  api_url: https://yaml.test/x\n  type: api\n"
        "  jeu_chemin: '{donnees}/yaml.csv'\n"
        "file:\n  type: sqlite\n  kafka_serveurs: yaml:9092\n"
        "journal:\n  niveau: INFO\n"
        "notification:\n  smtp_hote: yaml.test\n  smtp_port: 25\n  smtp_expediteur: yaml@x.test\n"
        "  smtp_destinataires: [yaml@x.test]\n"
        "mlflow:\n  tracking_uri: http://yaml:5000\n"
    )
    dossier = ecrire_dossier(tmp_path, dev=yaml_contenu)
    environ = {
        "FRAUDE_DONNEES": str(tmp_path / "donnees"),
        "FRAUDE_API_URL": "https://env.test/api",
        "FRAUDE_KAFKA_SERVEURS": "env:9093",
        "FRAUDE_TYPE_FILE": "kafka",
        "FRAUDE_TYPE_SOURCE": "jeu",
        "FRAUDE_JEU_CHEMIN": "{donnees}/env.csv",
        "FRAUDE_NIVEAU_JOURNAL": "warning",
        "FRAUDE_SMTP_HOTE": "smtp.env.test",
        "FRAUDE_SMTP_PORT": "2526",
        "FRAUDE_SMTP_EXPEDITEUR": "env@x.test",
        "FRAUDE_SMTP_DESTINATAIRES": "a@x.test, b@x.test ,, c@x.test,",
        "MLFLOW_TRACKING_URI": "http://env:5000",
    }
    environ_avant = dict(environ)
    config = charger_configuration("dev", dossier_config=dossier, environ=environ)
    assert environ == environ_avant
    assert config.source.api_url == "https://env.test/api"
    assert config.file.kafka_serveurs == "env:9093"
    assert config.file.type == "kafka"
    assert config.source.type == "jeu"
    assert config.source.jeu_chemin == tmp_path / "donnees" / "env.csv"
    assert config.journal.niveau == "WARNING"
    assert config.notification.smtp_hote == "smtp.env.test"
    assert config.notification.smtp_port == 2526
    assert config.notification.smtp_expediteur == "env@x.test"
    assert config.notification.smtp_destinataires == ("a@x.test", "b@x.test", "c@x.test")
    assert config.mlflow.tracking_uri == "http://env:5000"


def test_une_variable_d_environnement_vide_laisse_le_yaml_inchange(tmp_path):
    """Une variable définie mais vide n'écrase rien : le réglage du fichier est conservé."""
    yaml_contenu = (
        "source:\n  api_url: https://yaml.test/x\n  type: jeu\n"
        "file:\n  type: kafka\n  kafka_serveurs: yaml:9092\n"
        "journal:\n  niveau: error\n"
        "notification:\n  smtp_hote: yaml.test\n  smtp_port: 25\n"
        "  smtp_destinataires: [y@x.test]\n"
        "mlflow:\n  tracking_uri: http://yaml:5000\n"
    )
    vides = dict.fromkeys(
        [
            "FRAUDE_API_URL",
            "FRAUDE_KAFKA_SERVEURS",
            "FRAUDE_TYPE_FILE",
            "FRAUDE_TYPE_SOURCE",
            "FRAUDE_JEU_CHEMIN",
            "FRAUDE_NIVEAU_JOURNAL",
            "FRAUDE_SMTP_HOTE",
            "FRAUDE_SMTP_PORT",
            "FRAUDE_SMTP_EXPEDITEUR",
            "FRAUDE_SMTP_DESTINATAIRES",
            "MLFLOW_TRACKING_URI",
        ],
        "",
    )
    sans_variables = charger(tmp_path / "a", yaml_contenu)
    avec_variables_vides = charger(tmp_path / "a", yaml_contenu, **vides)
    assert avec_variables_vides == sans_variables
    assert avec_variables_vides.source.api_url == "https://yaml.test/x"
    assert avec_variables_vides.file.kafka_serveurs == "yaml:9092"
    assert avec_variables_vides.journal.niveau == "ERROR"
    assert avec_variables_vides.notification.smtp_port == 25
    assert avec_variables_vides.notification.smtp_destinataires == ("y@x.test",)


def test_destinataires_sections_partielles_et_niveau_sont_derives_du_yaml_qui_reste_inchange(
    tmp_path,
):
    """Destinataires nettoyés, sections à moitié remplies complétées, niveau en majuscules."""
    dossier = ecrire_dossier(
        tmp_path,
        dev="notification:\n  smtp_destinataires: ' a@x.test ,b@x.test, ,'\n"
        "sla:\n  taux_rejets_max: 1\nsource:\n  relances:\n    max_tentatives: 2\n"
        "journal:\n  niveau: Warning\n",
    )
    contenu_avant = (dossier / "dev.yaml").read_bytes()
    environ = {"FRAUDE_DONNEES": str(tmp_path / "donnees")}
    config = charger_configuration("dev", dossier_config=dossier, environ=environ)
    assert config.notification.smtp_destinataires == ("a@x.test", "b@x.test")
    assert config.sla == SlaConfig(
        latence_notification_s=60.0,
        fraicheur_max_min=15.0,
        taux_rejets_max=1.0,
        alertes_en_attente_max_min=10.0,
    )
    assert type(config.sla.taux_rejets_max) is float
    assert config.source.relances == Relances(2, 1.0, 2.0, 60.0, 0.1)
    assert config.journal.niveau == "WARNING"
    assert (dossier / "dev.yaml").read_bytes() == contenu_avant
    liste = charger(tmp_path / "l", "notification:\n  smtp_destinataires: [a@x.test, b@x.test]\n")
    assert liste.notification.smtp_destinataires == ("a@x.test", "b@x.test")
    vide = charger(tmp_path / "v", "notification:\n  smtp_destinataires: '  , '\n")
    assert vide.notification.smtp_destinataires == ()
    absent = charger(tmp_path / "x", "notification:\n  smtp_hote: h\n")
    assert absent.notification.smtp_destinataires == ()
    unique = charger(tmp_path / "u", "notification:\n  smtp_destinataires: seul@x.test\n")
    assert unique.notification.smtp_destinataires == ("seul@x.test",)
    assert charger(tmp_path / "a", "journal:\n  niveau: debug\n").journal.niveau == "DEBUG"
    assert charger(tmp_path / "c", "journal:\n  niveau: ERROR\n").journal.niveau == "ERROR"


def test_la_fusion_de_base_yaml_et_de_l_environnement_est_recursive_et_laisse_les_fichiers_intacts(
    tmp_path,
):
    """L'environnement remplace clé à clé ; une liste est remplacée en bloc, jamais concaténée."""
    base = (
        "source:\n  intervalle_s: 10\n  delai_attente_s: 5\n"
        "  relances:\n    max_tentatives: 9\n    gigue: 0.5\n"
        "modele:\n  candidats: [a, b]\n  graine: 1\n"
        "journal:\n  console: json\n"
    )
    dev = (
        "source:\n  intervalle_s: 99\n  relances:\n    max_tentatives: 2\n"
        "modele:\n  candidats: [c]\n"
        "file:\n  taille_lot: 12\n"
    )
    dossier = ecrire_dossier(tmp_path, base=base, dev=dev)
    contenus_avant = {p.name: p.read_bytes() for p in dossier.iterdir()}
    config = charger_configuration(
        "dev", dossier_config=dossier, environ={"FRAUDE_DONNEES": str(tmp_path / "d")}
    )
    assert config.source.intervalle_s == 99.0
    assert config.source.delai_attente_s == 5.0
    assert config.source.relances == Relances(max_tentatives=2, gigue=0.5)
    assert config.modele.candidats == ("c",)
    assert config.modele.graine == 1
    assert config.journal.console == "json"
    assert config.file.taille_lot == 12
    assert {p.name: p.read_bytes() for p in dossier.iterdir()} == contenus_avant
    prod_seul = charger(tmp_path / "p", PROD_VALIDE, nom="prod", base=base)
    assert prod_seul.source.intervalle_s == 10.0
    assert prod_seul.environnement == "prod"


# --------------------------------------------------------------------------------------------
# verifier_configuration : un refus par règle
# --------------------------------------------------------------------------------------------
def test_un_type_de_file_inconnu_est_refuse_par_la_verification_et_par_le_chargement(tmp_path):
    """Seules les files « sqlite » et « kafka » existent ; le nom fautif est cité."""
    config = charger(tmp_path / "a")
    for nom in ("rabbit", "", "SQLITE", "Kafka"):
        inconnue = replace(config, file=replace(config.file, type=nom))
        with pytest.raises(ErreurConfiguration) as refus:
            verifier_configuration(inconnue)
        assert str(refus.value) == f"Type de file inconnu : {nom!r}."
    for connu in ("sqlite", "kafka"):
        connue = replace(config, file=replace(config.file, type=connu))
        assert verifier_configuration(connue) is None
    with pytest.raises(ErreurConfiguration) as refus:
        charger(tmp_path / "b", "file:\n  type: rabbit\n")
    assert str(refus.value) == "Type de file inconnu : 'rabbit'."
    with pytest.raises(ErreurConfiguration) as refus:
        charger(tmp_path / "c", "{}\n", FRAUDE_TYPE_FILE="amqp")
    assert str(refus.value) == "Type de file inconnu : 'amqp'."


def test_un_type_de_source_inconnu_est_refuse_par_la_verification_et_par_le_chargement(tmp_path):
    """Seules les sources « api » et « jeu » existent ; l'autre nom est cité entre guillemets."""
    config = charger(tmp_path / "a")
    for nom in ("fichier", "", "API", "Jeu"):
        inconnue = replace(config, source=replace(config.source, type=nom))
        with pytest.raises(ErreurConfiguration) as refus:
            verifier_configuration(inconnue)
        assert str(refus.value) == f"Type de source inconnu : {nom!r}."
    for connu in ("api", "jeu"):
        connue = replace(config, source=replace(config.source, type=connu))
        assert verifier_configuration(connue) is None
    with pytest.raises(ErreurConfiguration) as refus:
        charger(tmp_path / "b", "source:\n  type: csv\n")
    assert str(refus.value) == "Type de source inconnu : 'csv'."
    with pytest.raises(ErreurConfiguration) as refus:
        charger(tmp_path / "c", "{}\n", FRAUDE_TYPE_SOURCE="ftp")
    assert str(refus.value) == "Type de source inconnu : 'ftp'."


def test_le_type_de_file_est_verifie_avant_le_type_de_source(tmp_path):
    """Quand les deux sont faux, c'est la file qui est signalée en premier."""
    config = charger(tmp_path)
    double = replace(
        config, file=replace(config.file, type="x"), source=replace(config.source, type="y")
    )
    with pytest.raises(ErreurConfiguration) as refus:
        verifier_configuration(double)
    assert str(refus.value) == "Type de file inconnu : 'x'."


def fractions(config: Configuration, entrainement: float, validation: float) -> Configuration:
    modele = replace(
        config.modele, fraction_entrainement=entrainement, fraction_validation=validation
    )
    return replace(config, modele=modele)


MESSAGE_FRACTIONS = "Les fractions d'entraînement et de validation laissent 0 test."


def test_des_fractions_dont_la_somme_vaut_exactement_1_ne_laissent_aucun_test_et_sont_refusees(
    tmp_path,
):
    """Entraînement + validation = 1 : plus rien pour le test. 0,99 reste accepté."""
    config = charger(tmp_path)
    for entrainement, validation in ((0.5, 0.5), (1.0, 0.0), (0.0, 1.0), (0.75, 0.25)):
        with pytest.raises(ErreurConfiguration) as refus:
            verifier_configuration(fractions(config, entrainement, validation))
        assert str(refus.value) == MESSAGE_FRACTIONS
    assert verifier_configuration(fractions(config, 0.5, 0.49)) is None
    assert verifier_configuration(fractions(config, 0.98, 0.0)) is None
    with pytest.raises(ErreurConfiguration) as refus:
        yaml_somme_un = "modele:\n  fraction_entrainement: 0.5\n  fraction_validation: 0.5\n"
        charger(tmp_path / "yaml", yaml_somme_un)
    assert str(refus.value) == MESSAGE_FRACTIONS


def test_des_fractions_dont_la_somme_depasse_1_sont_refusees(tmp_path):
    """Entraînement + validation > 1 : impossible de réserver un jeu de test."""
    config = charger(tmp_path)
    for entrainement, validation in ((0.8, 0.3), (1.0, 0.5), (2.0, 0.0), (0.5, 0.5000001)):
        with pytest.raises(ErreurConfiguration) as refus:
            verifier_configuration(fractions(config, entrainement, validation))
        assert str(refus.value) == MESSAGE_FRACTIONS
    assert verifier_configuration(fractions(config, 0.9, 0.0999)) is None


def test_des_fractions_dont_la_somme_est_nulle_ou_negative_sont_refusees(tmp_path):
    """Entraînement + validation <= 0 : rien pour entraîner ni valider."""
    config = charger(tmp_path)
    for entrainement, validation in ((0.0, 0.0), (-0.2, 0.1), (-1.0, 0.0), (0.1, -0.1)):
        with pytest.raises(ErreurConfiguration) as refus:
            verifier_configuration(fractions(config, entrainement, validation))
        assert str(refus.value) == MESSAGE_FRACTIONS
    assert verifier_configuration(fractions(config, 0.001, 0.0)) is None
    assert verifier_configuration(fractions(config, 0.0, 0.001)) is None


def test_la_production_exige_une_file_kafka(tmp_path):
    """En production une file SQLite locale est refusée, message exact ; Kafka est accepté."""
    prod = charger(tmp_path, PROD_VALIDE, nom="prod")
    sqlite = replace(prod, file=replace(prod.file, type="sqlite"))
    with pytest.raises(ErreurConfiguration) as refus:
        verifier_configuration(sqlite)
    assert str(refus.value) == "La production exige une file Kafka (file.type: kafka)."
    assert verifier_configuration(prod) is None
    with pytest.raises(ErreurConfiguration) as refus:
        charger(
            tmp_path / "yaml",
            PROD_VALIDE.replace("type: kafka", "type: sqlite"),
            nom="prod",
        )
    assert str(refus.value) == "La production exige une file Kafka (file.type: kafka)."


def test_la_production_exige_une_source_api_en_https(tmp_path):
    """Une URL http://, vide ou mal écrite est refusée en production ; https:// est accepté."""
    prod = charger(tmp_path, PROD_VALIDE, nom="prod")
    sans_https = ["http://exemple.test/x", "", "ftp://exemple.test", "exemple.test"]
    sans_https += [" https://x", "https:/x"]
    for url in sans_https:
        en_http = replace(prod, source=replace(prod.source, api_url=url))
        with pytest.raises(ErreurConfiguration) as refus:
            verifier_configuration(en_http)
        assert str(refus.value) == "La source API doit être jointe en HTTPS en production."
    en_https = replace(prod, source=replace(prod.source, api_url="https://x"))
    assert verifier_configuration(en_https) is None
    with pytest.raises(ErreurConfiguration) as refus:
        charger(tmp_path / "env", PROD_VALIDE, nom="prod", FRAUDE_API_URL="http://env.test/x")
    assert str(refus.value) == "La source API doit être jointe en HTTPS en production."


def test_la_production_refuse_le_canal_smtp_sans_tls(tmp_path):
    """Le canal « smtp » sans TLS est refusé en production ; sans « smtp », TLS est facultatif."""
    prod = charger(tmp_path, PROD_VALIDE, nom="prod")
    sans_tls = replace(prod, notification=replace(prod.notification, exiger_tls=False))
    with pytest.raises(ErreurConfiguration) as refus:
        verifier_configuration(sans_tls)
    assert str(refus.value) == "Le canal SMTP exige TLS en production (exiger_tls: true)."
    sans_smtp = replace(
        sans_tls, notification=replace(sans_tls.notification, canaux=("journal", "fichier"))
    )
    assert verifier_configuration(sans_smtp) is None
    seul_smtp = replace(prod, notification=replace(prod.notification, canaux=("smtp",)))
    assert verifier_configuration(seul_smtp) is None
    with pytest.raises(ErreurConfiguration) as refus:
        sans_tls_yaml = PROD_VALIDE.replace("exiger_tls: true", "exiger_tls: false")
        charger(tmp_path / "yaml", sans_tls_yaml, nom="prod")
    assert str(refus.value) == "Le canal SMTP exige TLS en production (exiger_tls: true)."


def test_la_production_refuse_un_dsn_avec_identifiants_dans_le_yaml(tmp_path):
    """Un DSN contenant « @ » (identifiants) est refusé ; sans « @ » ou vide, il est accepté."""
    prod = charger(tmp_path, PROD_VALIDE, nom="prod")
    for dsn in ("postgresql://admin:mdp@hote/base", "postgresql://admin@hote/base", "@"):
        avec_identifiants = replace(prod, entrepot=replace(prod.entrepot, dsn=dsn))
        with pytest.raises(ErreurConfiguration) as refus:
            verifier_configuration(avec_identifiants)
        assert str(refus.value) == (
            "Le DSN du fichier YAML contient des identifiants : utiliser FRAUDE_DSN_ENTREPOT."
        )
    for dsn in ("", "postgresql://hote/base", "sqlite:///x.db"):
        sans_identifiants = replace(prod, entrepot=replace(prod.entrepot, dsn=dsn))
        assert verifier_configuration(sans_identifiants) is None
    with pytest.raises(ErreurConfiguration) as refus:
        charger(
            tmp_path / "yaml",
            PROD_VALIDE + "entrepot:\n  dsn: 'postgresql://admin:mdp@hote/base'\n",
            nom="prod",
        )
    assert str(refus.value) == (
        "Le DSN du fichier YAML contient des identifiants : utiliser FRAUDE_DSN_ENTREPOT."
    )


def test_les_regles_de_production_sont_verifiees_dans_l_ordre_kafka_https_tls_dsn(tmp_path):
    """Une configuration qui les viole toutes révèle une règle à la fois, dans cet ordre."""
    prod = charger(tmp_path, PROD_VALIDE, nom="prod")
    courante = replace(
        prod,
        file=replace(prod.file, type="sqlite"),
        source=replace(prod.source, api_url="http://exemple.test"),
        notification=replace(prod.notification, exiger_tls=False),
        entrepot=replace(prod.entrepot, dsn="postgresql://u:p@h/b"),
    )
    attendus = [
        "La production exige une file Kafka (file.type: kafka).",
        "La source API doit être jointe en HTTPS en production.",
        "Le canal SMTP exige TLS en production (exiger_tls: true).",
        "Le DSN du fichier YAML contient des identifiants : utiliser FRAUDE_DSN_ENTREPOT.",
    ]
    corrections = [
        lambda c: replace(c, file=replace(c.file, type="kafka")),
        lambda c: replace(c, source=replace(c.source, api_url="https://exemple.test")),
        lambda c: replace(c, notification=replace(c.notification, exiger_tls=True)),
        lambda c: replace(c, entrepot=replace(c.entrepot, dsn="postgresql://h/b")),
    ]
    for message, corriger in zip(attendus, corrections, strict=True):
        with pytest.raises(ErreurConfiguration) as refus:
            verifier_configuration(courante)
        assert str(refus.value) == message
        courante = corriger(courante)
    assert verifier_configuration(courante) is None


def test_le_developpement_tolere_ce_que_la_production_refuse(tmp_path):
    """Les règles de production ne s'appliquent qu'en production : même réglage, autre verdict."""
    dev = charger(tmp_path, DEV_LAXISTE)
    assert dev.environnement == "dev"
    assert verifier_configuration(dev) is None
    assert dev.file.type == "sqlite"
    assert dev.source.api_url == "http://exemple.test/x"
    en_production = replace(dev, environnement="prod")
    with pytest.raises(ErreurConfiguration) as refus:
        verifier_configuration(en_production)
    assert str(refus.value) == "La production exige une file Kafka (file.type: kafka)."
    with pytest.raises(ErreurConfiguration) as refus:
        charger(tmp_path / "prod", DEV_LAXISTE, nom="prod")
    assert str(refus.value) == "La production exige une file Kafka (file.type: kafka)."
