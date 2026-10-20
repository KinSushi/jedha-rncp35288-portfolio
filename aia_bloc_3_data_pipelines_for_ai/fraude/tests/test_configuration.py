# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Configuration par environnement (critère 3.3) et gestion des secrets (critère 5.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from detection_fraude.configuration import (
    RACINE_PROJET,
    ErreurConfiguration,
    charger_configuration,
    lire_secret,
)


@pytest.fixture(autouse=True)
def environnement_propre(monkeypatch, tmp_path):
    for variable in (
        "FRAUDE_ENV",
        "FRAUDE_DSN_ENTREPOT",
        "FRAUDE_SEL_HACHAGE",
        "FRAUDE_SEL_HACHAGE_FICHIER",
        "FRAUDE_API_URL",
        "FRAUDE_TYPE_FILE",
        "FRAUDE_KAFKA_SERVEURS",
        "FRAUDE_SMTP_DESTINATAIRES",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("FRAUDE_DONNEES", str(tmp_path / "donnees"))


def test_dev_utilise_sqlite_et_prod_utilise_kafka():
    dev = charger_configuration("dev")
    prod = charger_configuration("prod")
    assert (dev.file.type, prod.file.type) == ("sqlite", "kafka")
    assert dev.entrepot.dsn.startswith("sqlite:///")
    assert prod.entrepot.dsn == ""  # en production le DSN est un secret
    assert dev.journal.console == "lisible" and prod.journal.console == "json"
    assert prod.mlflow.tracking_uri.startswith("http")


def test_meme_code_meme_parametres_metier_entre_environnements():
    dev, prod = charger_configuration("dev"), charger_configuration("prod")
    assert dev.modele.precision_cible == prod.modele.precision_cible
    assert dev.source.relances == prod.source.relances
    assert dev.file.sujet_valides == prod.file.sujet_valides


def test_environnement_choisi_par_variable(monkeypatch):
    monkeypatch.setenv("FRAUDE_ENV", "prod")
    assert charger_configuration().environnement == "prod"


def test_environnement_inconnu():
    with pytest.raises(ErreurConfiguration, match="inconnu"):
        charger_configuration("recette")


def test_variable_d_environnement_remplace_le_yaml(monkeypatch):
    monkeypatch.setenv("FRAUDE_KAFKA_SERVEURS", "courtier.exemple:9093")
    assert charger_configuration("prod").file.kafka_serveurs == "courtier.exemple:9093"


def test_le_sel_est_obligatoire_en_production():
    with pytest.raises(ErreurConfiguration, match="obligatoire"):
        charger_configuration("prod").sel_hachage()


def test_le_sel_vient_de_l_environnement(monkeypatch):
    monkeypatch.setenv("FRAUDE_SEL_HACHAGE", "valeur-de-test")
    assert charger_configuration("prod").sel_hachage() == b"valeur-de-test"


def test_le_secret_peut_venir_d_un_fichier_monte(monkeypatch, tmp_path):
    fichier = tmp_path / "secret_sel"
    fichier.write_text("sel-depuis-un-fichier\n", encoding="utf-8")
    monkeypatch.setenv("FRAUDE_SEL_HACHAGE_FICHIER", str(fichier))
    assert charger_configuration("prod").sel_hachage() == b"sel-depuis-un-fichier"


def test_fichier_de_secret_illisible(monkeypatch, tmp_path):
    monkeypatch.setenv("FRAUDE_SEL_HACHAGE_FICHIER", str(tmp_path / "absent"))
    with pytest.raises(ErreurConfiguration, match="illisible"):
        lire_secret("SEL_HACHAGE")


def test_en_dev_un_sel_local_est_genere_une_fois_puis_reutilise():
    config = charger_configuration("dev")
    premier = config.sel_hachage()
    assert len(premier) >= 32
    assert config.sel_hachage() == premier  # stable d'un appel à l'autre
    assert (config.racine_donnees / ".sel_hachage_dev").exists()


def test_le_dsn_secret_prime_sur_le_yaml(monkeypatch):
    monkeypatch.setenv("FRAUDE_DSN_ENTREPOT", "postgresql+psycopg://u:mot@h/b")
    assert charger_configuration("prod").dsn_entrepot() == "postgresql+psycopg://u:mot@h/b"


def test_dsn_absent_en_production():
    with pytest.raises(ErreurConfiguration, match="FRAUDE_DSN_ENTREPOT"):
        charger_configuration("prod").dsn_entrepot()


def test_les_secrets_n_apparaissent_pas_dans_la_representation(monkeypatch):
    monkeypatch.setenv("FRAUDE_SEL_HACHAGE", "sel-tres-secret")
    monkeypatch.setenv("FRAUDE_DSN_ENTREPOT", "postgresql://u:motdepasse@h/b")
    config = charger_configuration("prod")
    assert "sel-tres-secret" not in repr(config) and "motdepasse" not in repr(config)


def test_aucun_secret_dans_les_fichiers_yaml():
    for fichier in (RACINE_PROJET / "config").glob("*.yaml"):
        texte = fichier.read_text(encoding="utf-8").lower()
        contenu = yaml.safe_load(texte)
        assert contenu is not None
        for mot in ("password", "mot_de_passe:", "secret:", "token", "api_key"):
            assert mot not in texte.replace("# ", ""), f"{fichier.name} contient « {mot} »"


def test_prod_refuse_un_dsn_avec_identifiants_dans_le_yaml(tmp_path):
    dossier = tmp_path / "cfg"
    dossier.mkdir()
    (dossier / "prod.yaml").write_text(
        "file:\n  type: kafka\nsource:\n  api_url: https://exemple.test/x\n"
        "entrepot:\n  dsn: postgresql://admin:mdp@hote/base\n",
        encoding="utf-8",
    )
    with pytest.raises(ErreurConfiguration, match="identifiants"):
        charger_configuration("prod", dossier_config=dossier)


def test_prod_exige_https_pour_l_api(tmp_path):
    dossier = tmp_path / "cfg"
    dossier.mkdir()
    (dossier / "prod.yaml").write_text(
        "file:\n  type: kafka\nsource:\n  api_url: http://exemple.test/x\n", encoding="utf-8"
    )
    with pytest.raises(ErreurConfiguration, match="HTTPS"):
        charger_configuration("prod", dossier_config=dossier)


def test_prod_exige_kafka(tmp_path):
    dossier = tmp_path / "cfg"
    dossier.mkdir()
    (dossier / "prod.yaml").write_text(
        "file:\n  type: sqlite\nsource:\n  api_url: https://exemple.test/x\n", encoding="utf-8"
    )
    with pytest.raises(ErreurConfiguration, match="Kafka"):
        charger_configuration("prod", dossier_config=dossier)


def test_fichier_de_configuration_absent(tmp_path):
    with pytest.raises(ErreurConfiguration, match="introuvable"):
        charger_configuration("dev", dossier_config=Path(tmp_path))
