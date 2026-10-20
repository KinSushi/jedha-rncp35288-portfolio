# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Contrôles statiques du code Terraform, sans AWS ni binaire Terraform.

Pourquoi : `terraform validate` et `terraform plan` exigent le binaire Terraform (et, pour `plan`,
un compte AWS). Ces tests attrapent ce qu'ils attraperaient le plus souvent, sur le texte HCL
analysé par `python-hcl2` : appel de module avec un argument inconnu ou une variable obligatoire
oubliée, référence à une variable ou à une sortie qui n'existe pas, et surtout les garanties de
sécurité qui ne doivent jamais régresser (chiffrement, absence d'accès public, pas de secret en
clair, pas de droit IAM « * »). Ils ne remplacent pas `terraform validate`, qu'il faut lancer.

Une garantie qui ne sait rien refuser serait toujours verte : chaque règle est écrite comme une
fonction `exiger_...` (elle lève `AssertionError` quand le code la viole), appliquée au vrai code,
puis à une copie VOLONTAIREMENT FAUTIVE, qu'elle doit refuser.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

hcl2 = pytest.importorskip("hcl2", reason="python-hcl2 est installé avec checkov")
LarkError = pytest.importorskip("lark.exceptions", reason="lark accompagne python-hcl2").LarkError

RACINE = Path(__file__).resolve().parent.parent
TERRAFORM = RACINE / "terraform"
MODULES = sorted(p for p in (TERRAFORM / "modules").iterdir() if p.is_dir())
ENVIRONNEMENTS = sorted(p for p in (TERRAFORM / "environnements").iterdir() if p.is_dir())
FICHIERS_TF = sorted(TERRAFORM.rglob("*.tf"))
COPYRIGHT = "Copyright © 2026 · voir le fichier LICENSE du dépôt"
META_ARGUMENTS = {"source", "count", "for_each", "depends_on", "providers", "version"}


def _un(valeur):
    """python-hcl2 enveloppe chaque valeur dans une liste : on la déballe."""
    while isinstance(valeur, list) and len(valeur) == 1:
        valeur = valeur[0]
    return valeur


def _arguments(bloc: dict) -> set[str]:
    """Noms d'arguments d'un bloc, sans les clés de position ajoutées par python-hcl2."""
    return {cle for cle in bloc if not cle.startswith("__")}


def _charger(fichier: Path) -> dict:
    with fichier.open(encoding="utf-8") as flux:
        return hcl2.load(flux)


def _blocs(dossier: Path, nature: str) -> dict[str, dict]:
    """Tous les blocs d'une nature (variable, output, module…) d'un dossier, par nom."""
    resultat: dict[str, dict] = {}
    for fichier in sorted(dossier.glob("*.tf")):
        for bloc in _charger(fichier).get(nature, []):
            resultat.update(bloc)
    return resultat


def _texte(dossier: Path) -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in sorted(dossier.glob("*.tf")))


def _sans_commentaires(texte: str) -> str:
    return "\n".join(ligne for ligne in texte.splitlines() if not ligne.lstrip().startswith("#"))


def _sans_chaines_ni_commentaires(texte: str) -> str:
    """Texte sans commentaires ni littéraux entre guillemets (descriptions comprises)."""
    sans = _sans_commentaires(texte)
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', sans)


def _ressources(dossier: Path, type_ressource: str) -> dict[str, dict]:
    resultat = {}
    for fichier in sorted(dossier.glob("*.tf")):
        for bloc in _charger(fichier).get("resource", []):
            if type_ressource in bloc:
                resultat.update(bloc[type_ressource])
    return resultat


def _vrai(valeur) -> bool:
    return str(_un(valeur)).lower() == "true"


# ---------------------------------------------------------------- hygiène des fichiers


def exiger_mention_d_auteur(texte: str) -> None:
    entete = "\n".join(texte.splitlines()[:3])
    assert COPYRIGHT in entete


def exiger_quatre_fichiers(noms: set[str]) -> None:
    assert noms >= {"main.tf", "variables.tf", "outputs.tf", "versions.tf"}


def exiger_variables_et_sorties_documentees(variables: dict, sorties: dict) -> None:
    for nom, bloc in variables.items():
        assert _un(bloc.get("description")), f"variable {nom} sans description"
        assert "type" in bloc, f"variable {nom} sans type"
    for nom, bloc in sorties.items():
        assert _un(bloc.get("description")), f"sortie {nom} sans description"


def test_il_y_a_des_fichiers_terraform():
    assert len(FICHIERS_TF) >= 40
    assert {p.name for p in MODULES} >= {
        "kms", "reseau", "bucket_s3", "lac_s3", "aurora_postgresql", "documentdb",
        "msk_serverless", "redshift_serverless", "secrets", "iam", "sauvegarde", "calcul",
        "supervision"}
    assert {p.name for p in ENVIRONNEMENTS} == {"dev", "prod"}

    # Contre-épreuve : un module sans ses quatre fichiers est refusé.
    with pytest.raises((AssertionError, KeyError)):
        exiger_quatre_fichiers({"main.tf", "variables.tf"})


@pytest.mark.parametrize("fichier", FICHIERS_TF + sorted(TERRAFORM.rglob("*.exemple")),
                         ids=lambda f: str(f.relative_to(TERRAFORM)))
def test_mention_de_droits_d_auteur_presente(fichier):
    texte = fichier.read_text(encoding="utf-8")
    exiger_mention_d_auteur(texte)

    with pytest.raises((AssertionError, KeyError)):
        exiger_mention_d_auteur(texte.replace(COPYRIGHT, ""))


@pytest.mark.parametrize("fichier", FICHIERS_TF, ids=lambda f: str(f.relative_to(TERRAFORM)))
def test_le_fichier_est_analysable(fichier):
    assert _charger(fichier) is not None

    # Un bloc jamais refermé n'est pas du HCL : l'analyseur le refuse.
    with pytest.raises(LarkError):  # l'analyseur de python-hcl2 est écrit avec lark
        hcl2.loads(fichier.read_text(encoding="utf-8") + '\nresource "x" "y" {\n')


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_chaque_module_a_ses_quatre_fichiers(module):
    noms = {f.name for f in module.glob("*.tf")}
    exiger_quatre_fichiers(noms)

    with pytest.raises((AssertionError, KeyError)):
        exiger_quatre_fichiers(noms - {"outputs.tf"})


@pytest.mark.parametrize("dossier", MODULES + ENVIRONNEMENTS, ids=lambda p: p.name)
def test_variables_et_sorties_sont_documentees(dossier):
    variables, sorties = _blocs(dossier, "variable"), _blocs(dossier, "output")
    exiger_variables_et_sorties_documentees(variables, sorties)

    sans_description = {"v": {"type": ["string"]}}
    with pytest.raises((AssertionError, KeyError)):
        exiger_variables_et_sorties_documentees(sans_description, {})
    sans_type = {"v": {"description": ["x"]}}
    with pytest.raises((AssertionError, KeyError)):
        exiger_variables_et_sorties_documentees(sans_type, {})
    with pytest.raises((AssertionError, KeyError)):
        exiger_variables_et_sorties_documentees({}, {"s": {}})


# ---------------------------------------------------------------- cohérence des appels


def _appels_de_modules():
    for dossier in MODULES + ENVIRONNEMENTS:
        for nom, bloc in _blocs(dossier, "module").items():
            yield dossier, nom, bloc


def exiger_appel_de_module_coherent(dossier: Path, nom: str, bloc: dict) -> None:
    cible = (dossier / _un(bloc["source"])).resolve()
    assert cible.is_dir(), f"{dossier.name}: module {nom} : source introuvable"
    declarees = _blocs(cible, "variable")
    fournies = _arguments(bloc) - META_ARGUMENTS
    inconnues = fournies - set(declarees)
    assert not inconnues, f"{dossier.name}: module {nom} : arguments inconnus {inconnues}"
    obligatoires = {n for n, v in declarees.items() if "default" not in v}
    manquantes = obligatoires - fournies
    assert not manquantes, f"{dossier.name}: module {nom} : variables obligatoires {manquantes}"


@pytest.mark.parametrize("dossier, nom, bloc", list(_appels_de_modules()),
                         ids=lambda x: x.name if isinstance(x, Path) else None)
def test_appel_de_module_coherent_avec_ses_variables(dossier, nom, bloc):
    exiger_appel_de_module_coherent(dossier, nom, bloc)

    avec_argument_inconnu = {**bloc, "argument_qui_n_existe_pas": ["x"]}
    with pytest.raises((AssertionError, KeyError)):
        exiger_appel_de_module_coherent(dossier, nom, avec_argument_inconnu)
    sans_source = {**bloc, "source": ["./dossier_qui_n_existe_pas"]}
    with pytest.raises((AssertionError, KeyError)):
        exiger_appel_de_module_coherent(dossier, nom, sans_source)
    cible = (dossier / _un(bloc["source"])).resolve()
    obligatoires = [n for n, v in _blocs(cible, "variable").items() if "default" not in v]
    if obligatoires:  # retirer une variable obligatoire est refusé
        with pytest.raises((AssertionError, KeyError)):
            exiger_appel_de_module_coherent(
                dossier,
                nom,
                {cle: valeur for cle, valeur in bloc.items() if cle != obligatoires[0]})


def exiger_references_resolues(dossier: Path, texte: str) -> None:
    variables = set(_blocs(dossier, "variable"))
    for nom in set(re.findall(r"\bvar\.(\w+)", texte)):
        assert nom in variables, f"{dossier.name}: var.{nom} non déclarée"
    locaux: set[str] = set()
    for fichier in dossier.glob("*.tf"):
        for bloc in _charger(fichier).get("locals", []):
            locaux |= set(bloc)
    for nom in set(re.findall(r"\blocal\.(\w+)", texte)):
        assert nom in locaux, f"{dossier.name}: local.{nom} non défini"
    modules = _blocs(dossier, "module")
    for nom_module, sortie in set(re.findall(r"\bmodule\.(\w+)\.(\w+)", texte)):
        assert nom_module in modules, f"{dossier.name}: module.{nom_module} inexistant"
        cible = (dossier / _un(modules[nom_module]["source"])).resolve()
        assert sortie in _blocs(cible, "output"), (
            f"{dossier.name}: module.{nom_module}.{sortie} : sortie inexistante")


@pytest.mark.parametrize("dossier", MODULES + ENVIRONNEMENTS, ids=lambda p: p.name)
def test_references_resolues(dossier):
    """Toute var.x, local.x et module.m.sortie utilisée est déclarée quelque part."""
    texte = _sans_commentaires(_texte(dossier))
    exiger_references_resolues(dossier, texte)

    with pytest.raises((AssertionError, KeyError)):
        exiger_references_resolues(dossier, texte + "\nx = var.variable_inexistante\n")
    with pytest.raises((AssertionError, KeyError)):
        exiger_references_resolues(dossier, texte + "\nx = local.local_inexistant\n")
    with pytest.raises((AssertionError, KeyError)):
        exiger_references_resolues(dossier, texte + "\nx = module.module_inexistant.sortie\n")


def exiger_meme_structure(dev: dict, prod: dict, fichiers_dev: set[str],
                          fichiers_prod: set[str]) -> None:
    assert dev.keys() == prod.keys()
    for nom in dev:
        assert _arguments(dev[nom]) == _arguments(prod[nom]), nom
    assert fichiers_dev == fichiers_prod


def test_dev_et_prod_ont_la_meme_structure():
    """Mêmes modules, mêmes arguments : seules les valeurs (tailles, protections) diffèrent."""
    dev, prod = (_blocs(TERRAFORM / "environnements" / e, "module") for e in ("dev", "prod"))
    fichiers = [{f.name for f in (TERRAFORM / "environnements" / e).iterdir()}
                for e in ("dev", "prod")]
    exiger_meme_structure(dev, prod, fichiers[0], fichiers[1])

    sans_un_module = {cle: valeur for cle, valeur in prod.items() if cle != "kms"}
    with pytest.raises((AssertionError, KeyError)):
        exiger_meme_structure(dev, sans_un_module, fichiers[0], fichiers[1])
    argument_en_plus = copy.deepcopy(prod)
    argument_en_plus["kms"]["argument_en_plus"] = ["x"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_meme_structure(dev, argument_en_plus, fichiers[0], fichiers[1])
    with pytest.raises((AssertionError, KeyError)):
        exiger_meme_structure(dev, prod, fichiers[0], fichiers[1] | {"fichier_en_plus.tf"})


def exiger_protections_de_production(texte: str, variables: dict) -> None:
    assert re.search(r"protection_suppression\s*=\s*true", texte)
    assert re.search(r"autoriser_destruction\s*=\s*false", texte)
    assert 'check "protections_de_production"' in texte
    assert "default" not in variables["fonction_rotation_arn"]


def test_prod_exige_la_rotation_du_secret_et_la_protection():
    prod_dossier = TERRAFORM / "environnements" / "prod"
    prod = prod_dossier.joinpath("main.tf").read_text(encoding="utf-8")
    variables = _blocs(prod_dossier, "variable")
    exiger_protections_de_production(prod, variables)

    with pytest.raises((AssertionError, KeyError)):
        exiger_protections_de_production(
            re.sub(r"protection_suppression\s*=\s*true", "protection_suppression = false", prod),
            variables)
    with pytest.raises((AssertionError, KeyError)):
        exiger_protections_de_production(
            re.sub(r"autoriser_destruction\s*=\s*false", "autoriser_destruction = true", prod),
            variables)
    with pytest.raises((AssertionError, KeyError)):
        exiger_protections_de_production(
            prod.replace(
        'check "protections_de_production"', 'check "autre"'),
            variables)
    avec_defaut = copy.deepcopy(variables)
    avec_defaut["fonction_rotation_arn"]["default"] = ["arn:aws:lambda:eu-west-1:0:function:x"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_protections_de_production(prod, avec_defaut)


# ---------------------------------------------------------------- garanties de sécurité


def exiger_bases_chiffrees_sans_mot_de_passe(aurora: dict, docdb: dict, namespace: dict) -> None:
    assert _vrai(aurora["storage_encrypted"]) and aurora["kms_key_id"]
    assert _vrai(aurora["manage_master_user_password"])
    assert _vrai(aurora["iam_database_authentication_enabled"])
    assert "master_password" not in aurora
    assert _vrai(docdb["storage_encrypted"]) and docdb["kms_key_id"]
    assert _vrai(docdb["manage_master_user_password"]) and "master_password" not in docdb
    assert namespace["kms_key_id"] and _vrai(namespace["manage_admin_password"])
    assert "admin_user_password" not in namespace


def test_bases_de_donnees_chiffrees_et_sans_mot_de_passe_dans_le_code():
    aurora = _ressources(TERRAFORM / "modules" / "aurora_postgresql", "aws_rds_cluster")["this"]
    docdb = _ressources(TERRAFORM / "modules" / "documentdb", "aws_docdb_cluster")["this"]
    namespace = _ressources(TERRAFORM / "modules" / "redshift_serverless",
                            "aws_redshiftserverless_namespace")["this"]
    exiger_bases_chiffrees_sans_mot_de_passe(aurora, docdb, namespace)

    non_chiffree = {**aurora, "storage_encrypted": ["false"]}
    with pytest.raises((AssertionError, KeyError)):
        exiger_bases_chiffrees_sans_mot_de_passe(non_chiffree, docdb, namespace)
    mot_de_passe_ecrit = {**docdb, "master_password": ["azerty"]}
    with pytest.raises((AssertionError, KeyError)):
        exiger_bases_chiffrees_sans_mot_de_passe(aurora, mot_de_passe_ecrit, namespace)
    sans_cle = {cle: valeur for cle, valeur in namespace.items() if cle != "kms_key_id"}
    with pytest.raises((AssertionError, KeyError)):
        exiger_bases_chiffrees_sans_mot_de_passe(aurora, docdb, sans_cle)


def exiger_aucune_ressource_publique(textes: dict[str, str], groupe: dict) -> None:
    for nom, texte in textes.items():
        texte = _sans_commentaires(texte)
        assert not re.search(r"publicly_accessible\s*=\s*true", texte), nom
        assert "0.0.0.0/0" not in texte and "::/0" not in texte, nom
        assert not re.search(
            r"aws_internet_gateway|aws_nat_gateway|map_public_ip_on_launch\s*=\s*true",
            texte), nom
    assert _vrai(groupe["enhanced_vpc_routing"]) and not _vrai(groupe["publicly_accessible"])


def test_aucune_ressource_publique():
    textes = {f.name: f.read_text(encoding="utf-8") for f in FICHIERS_TF}
    groupe = _ressources(TERRAFORM / "modules" / "redshift_serverless",
                         "aws_redshiftserverless_workgroup")["this"]
    exiger_aucune_ressource_publique(textes, groupe)

    for faute in ("publicly_accessible = true", 'cidr_blocks = ["0.0.0.0/0"]',
                  'resource "aws_internet_gateway" "x" {}', "map_public_ip_on_launch = true"):
        with pytest.raises((AssertionError, KeyError)):
            exiger_aucune_ressource_publique({**textes, "faute.tf": faute}, groupe)
    with pytest.raises((AssertionError, KeyError)):
        exiger_aucune_ressource_publique(textes, {**groupe, "publicly_accessible": ["true"]})


def exiger_buckets_proteges(blocage: dict, versioning: dict, chiffrement: dict,
                            texte: str) -> None:
    for option in ("block_public_acls", "block_public_policy", "ignore_public_acls",
                   "restrict_public_buckets"):
        assert _vrai(blocage[option]), option
    assert versioning
    assert chiffrement
    assert "aws:SecureTransport" in texte  # TLS obligatoire


def test_buckets_sans_acces_public_versionnes_et_chiffres():
    module = TERRAFORM / "modules" / "bucket_s3"
    blocage = _ressources(module, "aws_s3_bucket_public_access_block")["this"]
    versioning = _ressources(module, "aws_s3_bucket_versioning")
    chiffrement = _ressources(module, "aws_s3_bucket_server_side_encryption_configuration")
    texte = _texte(module)
    exiger_buckets_proteges(blocage, versioning, chiffrement, texte)

    ouvert = {**blocage, "block_public_policy": ["false"]}
    with pytest.raises((AssertionError, KeyError)):
        exiger_buckets_proteges(ouvert, versioning, chiffrement, texte)
    with pytest.raises((AssertionError, KeyError)):
        exiger_buckets_proteges(blocage, {}, chiffrement, texte)
    with pytest.raises((AssertionError, KeyError)):
        exiger_buckets_proteges(blocage, versioning, {}, texte)
    with pytest.raises((AssertionError, KeyError)):
        exiger_buckets_proteges(
            blocage,
            versioning,
            chiffrement,
            texte.replace("aws:SecureTransport", "aws:autre"))


def exiger_aucun_secret_en_clair(nom: str, texte: str) -> None:
    motifs = [r"AKIA[0-9A-Z]{16}", r"(?i)aws_secret_access_key", r"(?i)BEGIN (RSA )?PRIVATE KEY"]
    for motif in motifs:
        assert not re.search(motif, texte), (nom, motif)
    hors_commentaires = _sans_commentaires(texte)
    # Un mot de passe littéral (password = "xxx") est interdit ; les références
    # (random_password.x.result, var.x) restent permises.
    assert not re.search(r'(?i)\b\w*password\w*\s*=\s*"[^"$]+"', hors_commentaires), nom


def test_aucun_secret_en_clair():
    for fichier in TERRAFORM.rglob("*"):
        if fichier.is_file() and fichier.suffix in {".tf", ".exemple"}:
            exiger_aucun_secret_en_clair(fichier.name, fichier.read_text(encoding="utf-8"))

    # Fausse clé et faux en-tête assemblés, pour que ce fichier ne ressemble pas lui-même à un
    # secret aux yeux d'un outil d'analyse.
    for faute in ("cle = " + "AKIA" + "ABCDEFGHIJKLMNOP", "aws_secret_access_key = x",
                  "-----BEGIN RSA " + "PRIVATE KEY-----", 'master_password = "azerty"',
                  'admin_password = "azerty"'):
        with pytest.raises((AssertionError, KeyError)):
            exiger_aucun_secret_en_clair("faute.tf", faute)
    exiger_aucun_secret_en_clair("ok.tf", "master_password = var.mot_de_passe\n# password = x\n")


def exiger_droits_iam_sans_joker(code_iam: str, texte_iam: str, kms: str) -> None:
    assert "*" not in code_iam
    for actions in re.findall(r"actions\s*=\s*\[([^\]]*)\]", texte_iam):
        assert not re.search(r'"[a-z0-9-]*:?\*"', actions), actions
    for ressources in re.findall(r"resources\s*=\s*\[([^\]]*)\]", texte_iam):
        assert not re.search(r'^\s*"\*"\s*$', ressources.strip()), ressources
    assert kms.count('"kms:*"') == 1


def test_droits_iam_sans_joker():
    """Aucune action « * » ni « service:* » dans le module iam ; l'unique exception du dépôt est
    la délégation à IAM de la politique d'une clé KMS (justifiée dans le module kms)."""
    code = _sans_chaines_ni_commentaires(_texte(TERRAFORM / "modules" / "iam"))
    texte = _sans_commentaires(_texte(TERRAFORM / "modules" / "iam"))
    kms = _sans_commentaires(_texte(TERRAFORM / "modules" / "kms"))
    exiger_droits_iam_sans_joker(code, texte, kms)

    with pytest.raises((AssertionError, KeyError)):
        exiger_droits_iam_sans_joker(code + "\nx = *\n", texte, kms)
    with pytest.raises((AssertionError, KeyError)):
        exiger_droits_iam_sans_joker(code, texte + '\nactions = ["s3:*"]\n', kms)
    with pytest.raises((AssertionError, KeyError)):
        exiger_droits_iam_sans_joker(code, texte + '\nresources = ["*"]\n', kms)
    with pytest.raises((AssertionError, KeyError)):
        exiger_droits_iam_sans_joker(code, texte, kms + '\n"kms:*"\n')


def exiger_cles_kms_qui_tournent(cle: dict, textes_environnements: dict[str, str]) -> None:
    assert _vrai(cle["enable_key_rotation"])
    for nom_env, texte in textes_environnements.items():
        for domaine in ("oltp", "documents", "analytique", "lac", "secrets", "journaux"):
            assert re.search(rf"^\s+{domaine}\s*=\s*\{{ description", texte, re.M), (
                nom_env, domaine)


def test_les_cles_kms_tournent_et_chaque_domaine_a_la_sienne():
    cle = _ressources(TERRAFORM / "modules" / "kms", "aws_kms_key")["cle"]
    textes = {env.name: (env / "main.tf").read_text(encoding="utf-8") for env in ENVIRONNEMENTS}
    exiger_cles_kms_qui_tournent(cle, textes)

    with pytest.raises((AssertionError, KeyError)):
        exiger_cles_kms_qui_tournent({**cle, "enable_key_rotation": ["false"]}, textes)
    sans_domaine = {nom: re.sub(r"^\s+lac\s*=\s*\{ description", "    lac_autre = { description",
                                texte, flags=re.M) for nom, texte in textes.items()}
    with pytest.raises((AssertionError, KeyError)):
        exiger_cles_kms_qui_tournent(cle, sans_domaine)


def exiger_journaux_conserves_douze_mois(variables_par_module: dict[str, dict]) -> None:
    for module, variables in variables_par_module.items():
        assert str(_un(variables["retention_journaux_jours"]["default"])) == "365", module


def test_journaux_conserves_au_moins_douze_mois():
    variables = {module: _blocs(TERRAFORM / "modules" / module, "variable")
                 for module in ("reseau", "aurora_postgresql", "documentdb")}
    exiger_journaux_conserves_douze_mois(variables)

    trop_court = copy.deepcopy(variables)
    trop_court["reseau"]["retention_journaux_jours"]["default"] = ["30"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_journaux_conserves_douze_mois(trop_court)
    sans_variable = copy.deepcopy(variables)
    del sans_variable["documentdb"]["retention_journaux_jours"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_journaux_conserves_douze_mois(sans_variable)


# ---------------------------------------------------------------- calcul et supervision
#
# Modules ajoutés le 19/10/2026 : `calcul` (ECS sur Fargate, autoscaling du score) et
# `supervision` (alarmes CloudWatch, rubrique SNS). Même méthode que ci-dessus : chaque règle
# `exiger_...` est appliquée au vrai code, puis à une copie fautive qu'elle doit refuser.

CALCUL = TERRAFORM / "modules" / "calcul"
SUPERVISION = TERRAFORM / "modules" / "supervision"


def _locaux(dossier: Path) -> dict:
    resultat: dict = {}
    for fichier in sorted(dossier.glob("*.tf")):
        for bloc in _charger(fichier).get("locals", []):
            resultat.update(bloc)
    return resultat


def exiger_service_fargate_prive(service: dict) -> None:
    assert _un(service["launch_type"]) == "FARGATE"
    assert not _vrai(_un(service["network_configuration"])["assign_public_ip"])
    disjoncteur = _un(service["deployment_circuit_breaker"])
    assert _vrai(disjoncteur["enable"]) and _vrai(disjoncteur["rollback"])
    assert _un(service["deployment_minimum_healthy_percent"]) == 100
    # Le nombre de tâches appartient à l'autoscaling : Terraform ne doit pas le réimposer.
    assert "${desired_count}" in str(service["lifecycle"])


def test_le_service_de_score_est_prive_et_se_deploie_sans_coupure():
    service = _ressources(CALCUL, "aws_ecs_service")["score"]
    exiger_service_fargate_prive(service)

    reseau_public = {**_un(service["network_configuration"]), "assign_public_ip": [True]}
    for cle, faute in (("launch_type", ["EC2"]),
                       ("network_configuration", [reseau_public]),
                       ("deployment_circuit_breaker", [{"enable": [True], "rollback": [False]}]),
                       ("deployment_minimum_healthy_percent", [50]),
                       ("lifecycle", [])):
        with pytest.raises((AssertionError, KeyError)):
            exiger_service_fargate_prive({**service, cle: faute})


def exiger_taches_fargate_durcies(definitions: dict, texte: str) -> None:
    assert set(definitions) == {"score", "etl"}
    for nom, definition in definitions.items():
        assert _un(definition["requires_compatibilities"]) == "FARGATE", nom
        assert _un(definition["network_mode"]) == "awsvpc", nom
        assert "local.conteneur_durci" in str(definition["container_definitions"]), nom
        assert _un(definition["execution_role_arn"]) != _un(definition["task_role_arn"]), nom
    code = _sans_commentaires(texte)
    assert re.search(r"readonlyRootFilesystem\s*=\s*true", code)
    assert re.search(r"privileged\s*=\s*false", code)
    assert re.search(r'drop\s*=\s*\["ALL"\]', code)
    assert re.search(r'user\s*=\s*"10001:10001"', code)  # l'utilisateur du Dockerfile


def test_les_taches_tournent_sans_privilege_en_lecture_seule():
    definitions = _ressources(CALCUL, "aws_ecs_task_definition")
    texte = _texte(CALCUL)
    exiger_taches_fargate_durcies(definitions, texte)

    for avant, apres in (("readonlyRootFilesystem = true", "readonlyRootFilesystem = false"),
                         ("privileged             = false", "privileged             = true"),
                         ('drop = ["ALL"]', "drop = []"),
                         ('"10001:10001"', '"0:0"')):
        with pytest.raises((AssertionError, KeyError)):
            exiger_taches_fargate_durcies(definitions, texte.replace(avant, apres))
    meme_role = copy.deepcopy(definitions)
    meme_role["etl"]["execution_role_arn"] = meme_role["etl"]["task_role_arn"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_taches_fargate_durcies(meme_role, texte)
    sans_durcissement = copy.deepcopy(definitions)
    sans_durcissement["score"]["container_definitions"] = ['${jsonencode([{"name": "score"}])}']
    with pytest.raises((AssertionError, KeyError)):
        exiger_taches_fargate_durcies(sans_durcissement, texte)


MOTIF_EMPREINTE = "@sha256:[0-9a-f]{64}$"


def exiger_images_par_empreinte(variables: dict) -> None:
    for nom in ("image_score", "image_etl"):
        condition = _un(_un(variables[nom]["validation"])["condition"])
        assert MOTIF_EMPREINTE in condition, nom


def test_les_images_sont_designees_par_leur_empreinte():
    variables = _blocs(CALCUL, "variable")
    exiger_images_par_empreinte(variables)

    # Le motif lui-même : une empreinte complète passe, une étiquette modifiable est refusée.
    depot = "123456789012.dkr.ecr.eu-west-1.amazonaws.com/stripe"
    assert re.search(MOTIF_EMPREINTE, f"{depot}@sha256:{'ab' * 32}")
    assert not re.search(MOTIF_EMPREINTE, f"{depot}:latest")
    assert not re.search(MOTIF_EMPREINTE, f"{depot}@sha256:{'ab' * 31}")

    sans_controle = copy.deepcopy(variables)
    sans_controle["image_etl"]["validation"] = [{"condition": ["${true}"]}]
    with pytest.raises((AssertionError, KeyError)):
        exiger_images_par_empreinte(sans_controle)


def taille_fargate_admise(cpu: int, memoire: int) -> bool:
    """Même règle que la validation de `taches` dans modules/calcul/variables.tf."""
    plafond = 30720 if cpu == 4096 else 8 * cpu
    return cpu in (1024, 2048, 4096) and 2 * cpu <= memoire <= plafond


def exiger_tailles_et_bornes(locaux: dict) -> None:
    taches = _un(locaux["taches"])
    assert set(taches) == {"score", "etl"}
    for nom, taille in taches.items():
        assert taille_fargate_admise(taille["cpu"], taille["memoire"]), nom
    assert 1 <= _un(locaux["score_taches_min"]) <= _un(locaux["score_taches_max"])
    assert 0 < _un(locaux["score_cible_cpu_pourcent"]) < 100
    assert _un(locaux["delai_descente_s"]) >= _un(locaux["delai_montee_s"]) > 0


@pytest.mark.parametrize("environnement", ENVIRONNEMENTS, ids=lambda p: p.name)
def test_tailles_fargate_et_bornes_de_l_autoscaling_coherentes(environnement):
    locaux = _locaux(environnement)
    exiger_tailles_et_bornes(locaux)
    # La règle Python et la validation Terraform disent la même chose.
    assert "contains([1024, 2048, 4096], t.cpu)" in _texte(CALCUL)
    assert "30720" in _texte(CALCUL)

    for cle, faute in (("taches", [{"score": {"cpu": 1024, "memoire": 512},
                                    "etl": {"cpu": 1024, "memoire": 2048}}]),
                       ("taches", [{"score": {"cpu": 512, "memoire": 1024},
                                    "etl": {"cpu": 1024, "memoire": 2048}}]),
                       ("taches", [{"score": {"cpu": 4096, "memoire": 32768},
                                    "etl": {"cpu": 1024, "memoire": 2048}}]),
                       ("taches", [{"score": {"cpu": 1024, "memoire": 2048}}]),
                       ("score_taches_min", [_un(locaux["score_taches_max"]) + 1]),
                       ("score_taches_min", [0]),
                       ("score_cible_cpu_pourcent", [100]),
                       ("delai_descente_s", [_un(locaux["delai_montee_s"]) - 1])):
        with pytest.raises((AssertionError, KeyError)):
            exiger_tailles_et_bornes({**locaux, cle: faute})


def besoin_de_taches_a_la_pointe() -> dict[str, int]:
    """Tâches de notation nécessaires à la pointe, par scénario (document 09 § 4.1).

    Débit de pointe extrapolé × latence médiane mesurée d'une notation, arrondi au-dessus.
    """
    import math

    from stripe_demo import dimensionnement

    mesures = dimensionnement.charger_mesures()
    latence_s = mesures["demo"]["latence_mediane_ms"] / 1000
    return {s.nom: math.ceil(dimensionnement.extrapoler(mesures, s)["debit_pointe_paiements_s"]
                             * latence_s)
            for s in dimensionnement.SCENARIOS}


def exiger_bornes_qui_couvrent_la_pointe(locaux: dict, besoin: dict[str, int]) -> None:
    assert _un(locaux["score_taches_max"]) >= besoin["S3"]
    # Au moins deux tâches en production : une panne de zone n'arrête pas la notation.
    assert _un(locaux["score_taches_min"]) >= max(besoin["S2"], 2)


def test_les_bornes_de_prod_couvrent_la_pointe_extrapolee():
    besoin = besoin_de_taches_a_la_pointe()
    assert besoin["S1"] <= besoin["S2"] <= besoin["S3"]
    prod = _locaux(TERRAFORM / "environnements" / "prod")
    exiger_bornes_qui_couvrent_la_pointe(prod, besoin)

    with pytest.raises((AssertionError, KeyError)):
        exiger_bornes_qui_couvrent_la_pointe({**prod, "score_taches_max": [besoin["S3"] - 1]},
                                            besoin)
    with pytest.raises((AssertionError, KeyError)):
        exiger_bornes_qui_couvrent_la_pointe({**prod, "score_taches_min": [1]}, besoin)


def exiger_autoscaling_par_suivi_du_cpu(cible: dict, politique: dict) -> None:
    assert _un(cible["scalable_dimension"]) == "ecs:service:DesiredCount"
    assert _un(cible["min_capacity"]) == "${var.score_taches_min}"
    assert _un(cible["max_capacity"]) == "${var.score_taches_max}"
    assert _un(politique["policy_type"]) == "TargetTrackingScaling"
    configuration = _un(politique["target_tracking_scaling_policy_configuration"])
    metrique = _un(configuration["predefined_metric_specification"])
    assert _un(metrique["predefined_metric_type"]) == "ECSServiceAverageCPUUtilization"


def test_l_autoscaling_suit_le_cpu_entre_les_bornes():
    cible = _ressources(CALCUL, "aws_appautoscaling_target")["score"]
    politique = _ressources(CALCUL, "aws_appautoscaling_policy")["score_cpu"]
    exiger_autoscaling_par_suivi_du_cpu(cible, politique)

    with pytest.raises((AssertionError, KeyError)):
        exiger_autoscaling_par_suivi_du_cpu({**cible, "max_capacity": [20]}, politique)
    with pytest.raises((AssertionError, KeyError)):
        exiger_autoscaling_par_suivi_du_cpu(cible, {**politique, "policy_type": ["StepScaling"]})
    with pytest.raises((AssertionError, KeyError)):
        exiger_autoscaling_par_suivi_du_cpu(
            {**cible, "scalable_dimension": ["ecs:service:Autre"]}, politique)


def exiger_role_d_execution_au_plus_juste(texte: str) -> None:
    code = _sans_commentaires(texte)
    for actions in re.findall(r"actions\s*=\s*\[([^\]]*)\]", code):
        assert not re.search(r'"[a-z0-9-]*:?\*"', actions), actions
    # Une seule ressource « * » : ecr:GetAuthorizationToken, qui n'en admet pas d'autre.
    joker = re.findall(r'actions\s*=\s*\[([^\]]*)\]\s*\n\s*resources\s*=\s*\["\*"\]', code)
    assert joker == ['"ecr:GetAuthorizationToken"'], joker


def test_le_role_d_execution_n_a_aucun_joker_superflu():
    texte = _texte(CALCUL)
    exiger_role_d_execution_au_plus_juste(texte)

    with pytest.raises((AssertionError, KeyError)):
        exiger_role_d_execution_au_plus_juste(
            texte + '\nactions   = ["logs:PutLogEvents"]\n    resources = ["*"]\n')
    with pytest.raises((AssertionError, KeyError)):
        exiger_role_d_execution_au_plus_juste(texte + '\nactions = ["ecr:*"]\n')


def exiger_alarmes_reliees_a_la_rubrique(rubrique: dict, alarmes: dict,
                                         evenements: dict) -> None:
    assert _un(rubrique["kms_master_key_id"]) == "${var.cle_alertes_arn}"
    for nom, alarme in alarmes.items():
        for cle in ("alarm_actions", "ok_actions"):
            assert _un(alarme[cle]) == "${aws_sns_topic.alertes.arn}", (nom, cle)
    metriques = {_un(alarme["metric_name"]) for alarme in alarmes.values()}
    assert {"OldestReplicationSlotLag", "ComputeSeconds", "RunningTaskCount"} <= metriques
    # Sans aucune donnée, le service de score est arrêté : c'est une alarme, pas un silence.
    assert _un(alarmes["score_sans_tache"]["treat_missing_data"]) == "breaching"
    assert _un(evenements["sns_topic"]) == "${aws_sns_topic.alertes.arn}"
    assert {"failover", "failure"} <= set(_un(evenements["event_categories"]))


def test_chaque_alarme_previent_l_astreinte_par_une_rubrique_chiffree():
    rubrique = _ressources(SUPERVISION, "aws_sns_topic")["alertes"]
    alarmes = _ressources(SUPERVISION, "aws_cloudwatch_metric_alarm")
    evenements = _ressources(SUPERVISION, "aws_db_event_subscription")["aurora"]
    exiger_alarmes_reliees_a_la_rubrique(rubrique, alarmes, evenements)

    sans_cle = {cle: valeur for cle, valeur in rubrique.items() if cle != "kms_master_key_id"}
    with pytest.raises((AssertionError, KeyError)):
        exiger_alarmes_reliees_a_la_rubrique(sans_cle, alarmes, evenements)
    muette = copy.deepcopy(alarmes)
    del muette["consommation_entrepot"]["ok_actions"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_alarmes_reliees_a_la_rubrique(rubrique, muette, evenements)
    sans_retard = {nom: a for nom, a in alarmes.items() if nom != "retard_emplacement_replication"}
    with pytest.raises((AssertionError, KeyError)):
        exiger_alarmes_reliees_a_la_rubrique(rubrique, sans_retard, evenements)
    silencieuse = copy.deepcopy(alarmes)
    silencieuse["score_sans_tache"]["treat_missing_data"] = ["notBreaching"]
    with pytest.raises((AssertionError, KeyError)):
        exiger_alarmes_reliees_a_la_rubrique(rubrique, silencieuse, evenements)
    with pytest.raises((AssertionError, KeyError)):
        exiger_alarmes_reliees_a_la_rubrique(
            rubrique, alarmes, {**evenements, "event_categories": [["notification"]]})


def exiger_seuil_d_une_heure_de_wal(seuil: float, attendu: float) -> None:
    assert abs(seuil - attendu) <= 0.01 * attendu, (seuil, attendu)


def test_le_seuil_de_retard_vaut_une_heure_de_wal_a_la_pointe_en_s2():
    """H17 : le seuil d'alarme découle des mesures (WAL par paiement) et de H1, H2."""
    from stripe_demo import dimensionnement

    s2 = dimensionnement.SCENARIOS[1]
    extrapolation = dimensionnement.extrapoler(dimensionnement.charger_mesures(), s2)
    attendu = extrapolation["wal_octets_par_jour"] / 24 * s2.facteur_pointe
    for environnement in ENVIRONNEMENTS:
        seuil = _un(_locaux(environnement)["seuil_retard_emplacement_octets"])
        exiger_seuil_d_une_heure_de_wal(seuil, attendu)

    with pytest.raises(AssertionError):
        exiger_seuil_d_une_heure_de_wal(attendu * 2, attendu)
    with pytest.raises(AssertionError):  # une heure MOYENNE, sans le facteur de pointe
        exiger_seuil_d_une_heure_de_wal(attendu / s2.facteur_pointe, attendu)


def exiger_cle_et_plafond_partages(kms: str, modules_env: dict) -> None:
    code = _sans_commentaires(kms)
    assert re.search(r"for_each\s*=\s*each\.value\.alertes_sns", code)
    assert '"cloudwatch.amazonaws.com"' in code and '"events.rds.amazonaws.com"' in code
    cles = _un(modules_env["kms"]["cles"])
    assert _vrai(cles["alertes"]["alertes_sns"])
    supervision = modules_env["supervision"]
    assert _un(supervision["cle_alertes_arn"]) == '${module.kms.arns["alertes"]}'
    # L'alarme de consommation et le plafond de l'entrepôt lisent la même valeur.
    assert (_un(supervision["plafond_rpu_heures_mensuel"])
            == _un(modules_env["redshift"]["plafond_rpu_heures_mensuel"]))


@pytest.mark.parametrize("environnement", ENVIRONNEMENTS, ids=lambda p: p.name)
def test_la_rubrique_a_sa_cle_et_l_alarme_le_plafond_de_l_entrepot(environnement):
    kms = _texte(TERRAFORM / "modules" / "kms")
    modules_env = _blocs(environnement, "module")
    exiger_cle_et_plafond_partages(kms, modules_env)

    with pytest.raises((AssertionError, KeyError)):
        exiger_cle_et_plafond_partages(kms.replace('"cloudwatch.amazonaws.com", ', ""),
                                       modules_env)
    cle_fermee = copy.deepcopy(modules_env)
    _un(cle_fermee["kms"]["cles"])["alertes"]["alertes_sns"] = False
    with pytest.raises((AssertionError, KeyError)):
        exiger_cle_et_plafond_partages(kms, cle_fermee)
    autre_cle = copy.deepcopy(modules_env)
    autre_cle["supervision"]["cle_alertes_arn"] = ['${module.kms.arns["journaux"]}']
    with pytest.raises((AssertionError, KeyError)):
        exiger_cle_et_plafond_partages(kms, autre_cle)
    autre_plafond = copy.deepcopy(modules_env)
    autre_plafond["supervision"]["plafond_rpu_heures_mensuel"] = [1]
    with pytest.raises((AssertionError, KeyError)):
        exiger_cle_et_plafond_partages(kms, autre_plafond)


def exiger_images_tirees_sans_internet(services: list[str], texte_reseau: str) -> None:
    assert {"ecr.api", "ecr.dkr", "logs"} <= set(services)
    code = _sans_commentaires(texte_reseau)
    assert re.search(r"prefix_list_id\s*=\s*aws_vpc_endpoint\.s3\.prefix_list_id", code)


def test_les_taches_tirent_leurs_images_par_le_reseau_prive():
    """Pas de NAT : le registre et S3 (couches d'image, lac) se joignent par points privés."""
    services = _un(_blocs(TERRAFORM / "modules" / "reseau", "variable")
                   ["services_points_terminaison"]["default"])
    texte = _texte(TERRAFORM / "modules" / "reseau")
    exiger_images_tirees_sans_internet(services, texte)

    with pytest.raises((AssertionError, KeyError)):
        exiger_images_tirees_sans_internet([s for s in services if s != "ecr.dkr"], texte)
    with pytest.raises((AssertionError, KeyError)):
        exiger_images_tirees_sans_internet(
            services, texte.replace("aws_vpc_endpoint.s3.prefix_list_id", '"pl-0"'))


#: Nom affiché dans le schéma physique (document 01 § 3) de chaque point de terminaison.
NOMS_DU_SCHEMA = {"secretsmanager": "Secrets Manager", "kms": "KMS", "logs": "Logs", "sts": "STS",
                  "ecr.api": "ECR (API)", "ecr.dkr": "ECR (registre Docker)"}


def exiger_points_de_terminaison_dessines(services: list[str], document_01: str) -> None:
    noeud = re.search(r'EPI\["([^"]*)"\]', document_01)
    assert noeud, "nœud EPI absent du schéma physique"
    absents = [s for s in services if NOMS_DU_SCHEMA.get(s, s) not in noeud.group(1)]
    assert absents == [], f"points de terminaison absents du schéma : {absents}"


def test_le_schema_physique_dessine_chaque_point_de_terminaison_du_module_reseau():
    """Le document 01 § 3 et le module `reseau` montrent les mêmes points de terminaison."""
    services = _un(_blocs(TERRAFORM / "modules" / "reseau", "variable")
                   ["services_points_terminaison"]["default"])
    document_01 = (RACINE.parent / "livrables" / "aia2"
                   / "01_architecture_globale.md").read_text(encoding="utf-8")
    exiger_points_de_terminaison_dessines(services, document_01)

    # Un service ajouté au Terraform sans être dessiné est signalé, par son nom.
    with pytest.raises(AssertionError, match=r"\['ssm'\]"):
        exiger_points_de_terminaison_dessines([*services, "ssm"], document_01)
