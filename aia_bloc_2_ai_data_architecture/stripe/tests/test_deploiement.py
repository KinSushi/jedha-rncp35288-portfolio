# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Fichiers de déploiement : docker-compose, connecteur Debezium, secrets, Dockerfile.

Ces fichiers ne sont pas exécutables sans Docker et sans téléchargement d'images. Les tests
vérifient donc ce qui peut l'être sans eux : syntaxe, cohérence entre les fichiers (tables
capturées, noms de secrets, ports), et les règles de sécurité qui ne doivent pas régresser
(aucun mot de passe écrit, images épinglées, conteneurs sans privilèges superflus).

Une règle qui ne sait rien refuser serait toujours verte : chaque règle est donc écrite comme une
fonction `exiger_...` (elle lève `AssertionError` quand le fichier la viole), appliquée au vrai
fichier, puis à une copie VOLONTAIREMENT FAUTIVE du même fichier, qu'elle doit refuser.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import yaml
from deploiement_chargeur import charger_generer_secrets

from stripe_demo import generer_ddl as g

RACINE = Path(__file__).resolve().parent.parent
COMPOSE = RACINE / "docker-compose.yml"
CONNECTEUR = RACINE / "deploiement" / "debezium_connecteur.json"
COPYRIGHT = "Copyright © 2026 · voir le fichier LICENSE du dépôt"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def connecteur() -> dict:
    return json.loads(CONNECTEUR.read_text(encoding="utf-8"))


def _sans_cle(dictionnaire: dict, cle: str) -> dict:
    copie = copy.deepcopy(dictionnaire)
    del copie[cle]
    return copie


# ---------------------------------------------------------------- règles du docker-compose


def exiger_services(compose: dict) -> None:
    assert set(compose["services"]) == {
        "postgres", "mongodb", "redpanda", "connect", "enregistrer_connecteur", "amorcage",
        "demonstration"}


def exiger_images_epinglees(compose: dict) -> None:
    for nom, service in compose["services"].items():
        image = service["image"]
        assert ":" in image and not image.endswith(":latest"), (nom, image)


def exiger_postgres_prepare_pour_la_cdc(compose: dict) -> None:
    commande = compose["services"]["postgres"]["command"]
    assert "wal_level=logical" in commande
    volumes = " ".join(compose["services"]["postgres"]["volumes"])
    assert "sql/oltp_schema_postgresql.sql:/docker-entrypoint-initdb.d/01_" in volumes
    assert "02_comptes_service.sh" in volumes


def exiger_aucun_mot_de_passe(compose: dict, texte: str) -> None:
    for service in compose["services"].values():
        for cle, valeur in (service.get("environment") or {}).items():
            # Les variables qui portent un mot de passe désignent un fichier de secret ; la classe
            # du fournisseur de secrets (CLASS) est un nom de classe Java, pas un secret.
            if re.search(r"(?i)pass|token", cle):
                assert str(valeur).startswith("/run/secrets/"), cle
    assert not re.search(r"(?m)^[ 	]*[A-Z_]*PASSWORD[ 	]*[:=][ 	]*[^/\s]", texte)


def exiger_secrets_en_fichiers_ignores_par_git(compose: dict, gitignore: str) -> None:
    assert "stripe/secrets/" in gitignore.splitlines()
    for nom, secret in compose["secrets"].items():
        assert secret["file"].startswith("./secrets/"), nom
        assert Path(secret["file"]).stem == nom


def exiger_secrets_declares(compose: dict) -> None:
    declares = set(compose["secrets"])
    for nom, service in compose["services"].items():
        for secret in service.get("secrets", []):
            assert secret in declares, (nom, secret)
        for valeur in (service.get("environment") or {}).values():
            for utilise in re.findall(r"/run/secrets/(\w+)", str(valeur)):
                assert utilise in service.get("secrets", []), (nom, utilise)


def exiger_conteneurs_durcis(compose: dict) -> None:
    for nom, service in compose["services"].items():
        assert "no-new-privileges:true" in service["security_opt"], nom
        assert service["cap_drop"] == ["ALL"], nom
        assert {"mem_limit", "cpus"} <= set(service) or nom == "enregistrer_connecteur", nom
    for nom in ("amorcage", "demonstration"):
        assert compose["services"][nom]["read_only"] is True


def exiger_ports_sur_la_boucle_locale(compose: dict) -> None:
    for nom, service in compose["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (nom, port)


def exiger_ordre_de_demarrage(compose: dict) -> None:
    s = compose["services"]
    assert s["connect"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert s["demonstration"]["depends_on"]["enregistrer_connecteur"]["condition"] == (
        "service_completed_successfully")
    assert s["demonstration"]["depends_on"]["amorcage"]["condition"] == (
        "service_completed_successfully")


# ------------------------------------------------------------ docker-compose : règles et fautes


def test_services_attendus(compose):
    exiger_services(compose)

    sans_demonstration = copy.deepcopy(compose)
    del sans_demonstration["services"]["demonstration"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_services(sans_demonstration)


def test_images_epinglees_sans_latest(compose):
    exiger_images_epinglees(compose)

    fautif = copy.deepcopy(compose)
    fautif["services"]["postgres"]["image"] = "postgres:latest"
    with pytest.raises((AssertionError, KeyError)):
        exiger_images_epinglees(fautif)
    fautif["services"]["postgres"]["image"] = "postgres"  # sans version du tout
    with pytest.raises((AssertionError, KeyError)):
        exiger_images_epinglees(fautif)


def test_postgres_prepare_pour_la_capture_des_changements(compose):
    exiger_postgres_prepare_pour_la_cdc(compose)

    sans_wal_logique = copy.deepcopy(compose)
    sans_wal_logique["services"]["postgres"]["command"] = "postgres -c max_wal_senders=4"
    with pytest.raises((AssertionError, KeyError)):
        exiger_postgres_prepare_pour_la_cdc(sans_wal_logique)
    sans_schema = copy.deepcopy(compose)
    sans_schema["services"]["postgres"]["volumes"] = ["donnees_postgres:/var/lib/postgresql/data"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_postgres_prepare_pour_la_cdc(sans_schema)


def test_aucun_mot_de_passe_dans_le_compose(compose):
    texte = COMPOSE.read_text(encoding="utf-8")
    exiger_aucun_mot_de_passe(compose, texte)

    en_clair = copy.deepcopy(compose)
    en_clair["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"] = "azerty"
    with pytest.raises((AssertionError, KeyError)):
        exiger_aucun_mot_de_passe(en_clair, texte)
    with pytest.raises((AssertionError, KeyError)):
        exiger_aucun_mot_de_passe(compose, texte + "\n    POSTGRES_PASSWORD: azerty\n")


def test_secrets_sont_des_fichiers_ignores_par_git(compose):
    gitignore = (RACINE.parent / ".gitignore").read_text(encoding="utf-8")
    exiger_secrets_en_fichiers_ignores_par_git(compose, gitignore)

    with pytest.raises((AssertionError, KeyError)):
        exiger_secrets_en_fichiers_ignores_par_git(
            compose,
            gitignore.replace("stripe/secrets/", "stripe/autre/"))
    hors_dossier = copy.deepcopy(compose)
    hors_dossier["secrets"]["postgres_password"]["file"] = "/etc/postgres_password.txt"
    with pytest.raises((AssertionError, KeyError)):
        exiger_secrets_en_fichiers_ignores_par_git(hors_dossier, gitignore)
    mal_nomme = copy.deepcopy(compose)
    mal_nomme["secrets"]["postgres_password"]["file"] = "./secrets/autre_nom.txt"
    with pytest.raises((AssertionError, KeyError)):
        exiger_secrets_en_fichiers_ignores_par_git(mal_nomme, gitignore)


def test_chaque_service_utilisant_un_secret_le_declare(compose):
    exiger_secrets_declares(compose)

    secret_inconnu = copy.deepcopy(compose)
    secret_inconnu["services"]["postgres"]["secrets"].append("secret_non_declare")
    with pytest.raises((AssertionError, KeyError)):
        exiger_secrets_declares(secret_inconnu)
    secret_non_monte = copy.deepcopy(compose)
    secret_non_monte["services"]["postgres"]["secrets"] = []  # le chemin /run/secrets/... reste
    with pytest.raises((AssertionError, KeyError)):
        exiger_secrets_declares(secret_non_monte)


def test_conteneurs_durcis(compose):
    exiger_conteneurs_durcis(compose)

    sans_garde = copy.deepcopy(compose)
    sans_garde["services"]["redpanda"]["security_opt"] = []
    with pytest.raises((AssertionError, KeyError)):
        exiger_conteneurs_durcis(sans_garde)
    avec_privileges = copy.deepcopy(compose)
    avec_privileges["services"]["mongodb"]["cap_drop"] = []
    with pytest.raises((AssertionError, KeyError)):
        exiger_conteneurs_durcis(avec_privileges)
    sans_limite = copy.deepcopy(compose)
    del sans_limite["services"]["connect"]["mem_limit"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_conteneurs_durcis(sans_limite)
    inscriptible = copy.deepcopy(compose)
    inscriptible["services"]["demonstration"]["read_only"] = False
    with pytest.raises((AssertionError, KeyError)):
        exiger_conteneurs_durcis(inscriptible)


def test_ports_publies_sur_la_boucle_locale_seulement(compose):
    exiger_ports_sur_la_boucle_locale(compose)

    expose = copy.deepcopy(compose)
    expose["services"]["postgres"]["ports"] = ["0.0.0.0:5432:5432"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_ports_sur_la_boucle_locale(expose)
    sans_adresse = copy.deepcopy(compose)
    sans_adresse["services"]["mongodb"]["ports"] = ["27017:27017"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_ports_sur_la_boucle_locale(sans_adresse)


def test_ordre_de_demarrage(compose):
    exiger_ordre_de_demarrage(compose)

    sans_sante = copy.deepcopy(compose)
    sans_sante["services"]["connect"]["depends_on"]["postgres"]["condition"] = "service_started"
    with pytest.raises((AssertionError, KeyError)):
        exiger_ordre_de_demarrage(sans_sante)
    sans_amorcage = copy.deepcopy(compose)
    sans_amorcage["services"]["demonstration"]["depends_on"]["amorcage"]["condition"] = (
        "service_started")
    with pytest.raises((AssertionError, KeyError)):
        exiger_ordre_de_demarrage(sans_amorcage)


# ---------------------------------------------------------------- règles du connecteur Debezium


def exiger_connecteur_pgoutput_sur_publication_existante(connecteur: dict) -> None:
    assert connecteur["connector.class"] == "io.debezium.connector.postgresql.PostgresConnector"
    assert connecteur["plugin.name"] == "pgoutput"
    assert connecteur["publication.name"] == g.NOM_PUBLICATION
    # Droits minimaux : Debezium ne crée pas la publication, le DDL s'en charge.
    assert connecteur["publication.autocreate.mode"] == "disabled"
    assert connecteur["database.user"] == "svc_debezium"
    assert connecteur["snapshot.mode"] == "initial"


def exiger_tables_capturees_identiques_a_la_publication(connecteur: dict) -> None:
    tables = connecteur["table.include.list"].split(",")
    assert tables == [f"public.{t}" for t in g.TABLES_CDC]


def exiger_donnees_personnelles_exclues(connecteur: dict) -> None:
    assert "public.clients.email_hache" in connecteur["column.exclude.list"]


def exiger_mot_de_passe_du_connecteur_secret(connecteur: dict, compose: dict) -> None:
    assert connecteur["database.password"] == "${secrets:/run/secrets:postgres_cdc_password}"
    assert "postgres_cdc_password" in compose["services"]["connect"]["secrets"]
    env = compose["services"]["connect"]["environment"]
    assert env["CONNECT_CONFIG_PROVIDERS"] == "secrets"
    assert env["CONNECT_CONFIG_PROVIDERS_SECRETS_CLASS"].endswith("DirectoryConfigProvider")


def exiger_mention_de_droits_retiree_a_l_enregistrement(connecteur: dict, compose: dict) -> None:
    assert connecteur["_copyright"] == COPYRIGHT
    commande = " ".join(compose["services"]["enregistrer_connecteur"]["command"])
    assert "sed '/\"_copyright\"/d'" in commande
    assert "-X PUT" in commande and "/connectors/stripe-postgres/config" in commande
    # Sans cette clé, ce qui reste est un JSON valide de propriétés toutes textuelles.
    nettoye = {k: v for k, v in connecteur.items() if k != "_copyright"}
    assert all(isinstance(v, str) for v in nettoye.values())


def exiger_sujets_kafka_selon_la_convention(connecteur: dict) -> None:
    """Sujet = <topic.prefix>.<schéma>.<table> : stripe.public.paiements, etc."""
    assert connecteur["topic.prefix"] == "stripe"


# ---------------------------------------------------------------- connecteur : règles et fautes


def test_connecteur_postgres_pgoutput_sur_publication_existante(connecteur):
    exiger_connecteur_pgoutput_sur_publication_existante(connecteur)

    for cle, valeur in (("plugin.name", "decoderbufs"), ("publication.name", "autre"),
                        ("publication.autocreate.mode", "all_tables"),
                        ("database.user", "postgres"), ("snapshot.mode", "never")):
        fautif = {**connecteur, cle: valeur}
        with pytest.raises((AssertionError, KeyError)):
            exiger_connecteur_pgoutput_sur_publication_existante(fautif)


def test_tables_capturees_identiques_a_la_publication(connecteur):
    exiger_tables_capturees_identiques_a_la_publication(connecteur)

    en_trop = {**connecteur, "table.include.list": connecteur["table.include.list"]
               + ",public.journal_audit"}
    with pytest.raises((AssertionError, KeyError)):
        exiger_tables_capturees_identiques_a_la_publication(en_trop)
    en_moins = {**connecteur, "table.include.list": "public.paiements"}
    with pytest.raises((AssertionError, KeyError)):
        exiger_tables_capturees_identiques_a_la_publication(en_moins)


def test_donnees_personnelles_exclues_du_flux(connecteur):
    exiger_donnees_personnelles_exclues(connecteur)

    with pytest.raises((AssertionError, KeyError)):
        exiger_donnees_personnelles_exclues({**connecteur, "column.exclude.list": ""})


def test_mot_de_passe_du_connecteur_renvoie_a_un_secret(connecteur, compose):
    exiger_mot_de_passe_du_connecteur_secret(connecteur, compose)

    en_clair = {**connecteur, "database.password": "azerty"}
    with pytest.raises((AssertionError, KeyError)):
        exiger_mot_de_passe_du_connecteur_secret(en_clair, compose)
    sans_fournisseur = copy.deepcopy(compose)
    sans_fournisseur["services"]["connect"]["environment"]["CONNECT_CONFIG_PROVIDERS"] = "env"
    with pytest.raises((AssertionError, KeyError)):
        exiger_mot_de_passe_du_connecteur_secret(connecteur, sans_fournisseur)


def test_mention_de_droits_retiree_a_l_enregistrement(connecteur, compose):
    """La clé _copyright n'est pas une propriété Debezium : l'enregistrement la retire."""
    exiger_mention_de_droits_retiree_a_l_enregistrement(connecteur, compose)

    sans_mention = _sans_cle(connecteur, "_copyright")
    with pytest.raises((AssertionError, KeyError)):
        exiger_mention_de_droits_retiree_a_l_enregistrement(sans_mention, compose)
    propriete_non_textuelle = {**connecteur, "tasks.max": 1}
    with pytest.raises((AssertionError, KeyError)):
        exiger_mention_de_droits_retiree_a_l_enregistrement(propriete_non_textuelle, compose)


def test_les_sujets_kafka_suivent_la_convention_documentee(connecteur):
    exiger_sujets_kafka_selon_la_convention(connecteur)

    with pytest.raises((AssertionError, KeyError)):
        exiger_sujets_kafka_selon_la_convention({**connecteur, "topic.prefix": "autre"})


# ---------------------------------------------------------------- secrets et Dockerfile


def test_generation_des_secrets_ne_remplace_jamais_un_secret_existant(tmp_path):
    module = charger_generer_secrets()
    crees = module.generer(tmp_path)
    assert set(crees) == set(module.NOMS)
    valeurs_avant = {n: (tmp_path / f"{n}.txt").read_text(encoding="utf-8") for n in module.NOMS}
    assert len(set(valeurs_avant.values())) == len(valeurs_avant)  # tous distincts
    assert all(len(v) >= 32 for v in valeurs_avant.values())
    assert module.generer(tmp_path) == []  # deuxième appel : rien n'est recréé
    assert {n: (tmp_path / f"{n}.txt").read_text(encoding="utf-8")
            for n in module.NOMS} == valeurs_avant


def test_les_noms_de_secrets_du_compose_sont_ceux_du_generateur(compose):
    module = charger_generer_secrets()
    assert set(compose["secrets"]) == set(module.NOMS)

    sans_un_secret = copy.deepcopy(compose)
    del sans_un_secret["secrets"]["mongo_root_password"]
    assert set(sans_un_secret["secrets"]) != set(module.NOMS)  # l'écart serait vu


def test_dockerfile_sans_privilege_et_sans_secret_et_script_d_initialisation_sans_mot_de_passe():
    dockerfile = (RACINE / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER appli$", dockerfile, re.M)
    assert not re.search(r"(?i)password|secret|token", dockerfile.replace("# ", ""))
    assert "ARG PYTHON_VERSION=3.12" in dockerfile
    script = (RACINE / "deploiement" / "02_comptes_service.sh").read_text(encoding="utf-8")
    assert "/run/secrets/postgres_app_password" in script
    assert not re.search(r"PASSWORD\s+'", script)  # toujours une variable psql :'nom'
    assert script.splitlines()[1].startswith("# Copyright")

    # Les mêmes motifs détectent un Dockerfile qui passerait en root ou écrirait un secret.
    assert not re.search(r"^USER appli$", dockerfile.replace("USER appli", "USER root"), re.M)
    assert re.search(r"(?i)password|secret|token", dockerfile + "\nENV PASSWORD=azerty\n")
    assert re.search(r"PASSWORD\s+'", script + "\nCREATE ROLE x PASSWORD 'azerty';\n")
