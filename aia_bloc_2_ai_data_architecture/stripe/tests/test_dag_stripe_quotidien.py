# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Le DAG quotidien, testé SANS Airflow : analyse du fichier, tâches exécutées, graphe simulé.

Pourquoi : Airflow n'est pas installé dans l'environnement du projet (c'est un service à part,
lourd). Trois preuves le remplacent :
1. l'analyse du fichier (AST) : Airflow n'est importé qu'à l'intérieur de `construire_dag` ou
   sous garde, et les huit tâches sont celles du document 05 ;
2. chaque tâche est une fonction ordinaire, exécutée ici sur une mini-démonstration (OLTP SQLite,
   entrepôt DuckDB, lac dans un dossier temporaire), rejouée sans doublon, et refusée quand son
   entrée manque ou que l'entrepôt est incohérent ; le texte exact des requêtes envoyées à
   DuckDB est noté par une connexion espion (DuckDB ignore la casse : le résultat seul ne le
   montrerait pas) ;
3. le graphe : un faux Airflow minimal (trois classes) enregistre ce que `construire_dag`
   déclare (tâches, paramètres, dépendances, tentatives, rappel d'échec).
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import sqlite3
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
from aide_demo import ConnexionEspion, faux_entrainer

from dags import stripe_quotidien as dag_module
from stripe_demo import pipeline

FICHIER = Path(dag_module.__file__)
DOCUMENT_05 = (Path(__file__).resolve().parent.parent.parent / "livrables" / "aia2"
               / "05_pipeline.md")
JOUR = "2026-08-02"  # un dimanche, deuxième jour de la mini-démonstration (120 paiements)
LUNDI = "2026-08-03"


# ---------------------------------------------------------------- analyse statique (AST)


def _imports_airflow(noeud: ast.AST) -> list[ast.AST]:
    return [n for n in ast.walk(noeud)
            if (isinstance(n, ast.Import) and any(a.name.split(".")[0] == "airflow"
                                                   for a in n.names))
            or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "airflow")]


def exiger_airflow_importe_sous_garde(arbre: ast.Module) -> None:
    """Au premier niveau du module, Airflow n'est importé que dans un `try` qui gère son absence.

    Le corps d'une fonction n'est pas visité : un import y est paresseux (exécuté à l'appel
    seulement), le module s'importe donc sans Airflow. Quelles fonctions importent Airflow : voir
    `test_le_fichier_s_analyse_et_n_importe_airflow_que_sous_garde`.
    """
    for noeud in arbre.body:
        if isinstance(noeud, ast.FunctionDef):
            continue
        if isinstance(noeud, ast.Try):
            gardes = [h for h in noeud.handlers
                      if isinstance(h.type, ast.Name) and h.type.id == "ModuleNotFoundError"]
            assert gardes, "import d'Airflow sans garde ModuleNotFoundError"
            assert not any(_imports_airflow(h) for h in noeud.handlers)
            continue
        assert not _imports_airflow(noeud), ast.dump(noeud)[:80]


def test_le_fichier_s_analyse_et_n_importe_airflow_que_sous_garde():
    arbre = ast.parse(FICHIER.read_text(encoding="utf-8"))
    exiger_airflow_importe_sous_garde(arbre)
    importeuses = [n for n in arbre.body
                   if isinstance(n, ast.FunctionDef) and _imports_airflow(n)]
    # Seule `construire_dag` importe Airflow. L'outil de mutation (mutmut) réécrit le fichier :
    # le corps de la fonction y devient `x_construire_dag__mutmut_orig` et ses copies mutées,
    # d'où la comparaison sur le nom contenu plutôt que sur le nom exact.
    assert importeuses, "aucune fonction n'importe Airflow"
    assert all("construire_dag" in f.name for f in importeuses), [f.name for f in importeuses]
    assert {"airflow", "airflow.operators.python", "airflow.sensors.python"} <= {
        n.module for n in _imports_airflow(importeuses[0]) if isinstance(n, ast.ImportFrom)}

    for faute in ("from airflow import DAG\n", "import airflow.models\n",
                  "try:\n    import airflow\nexcept ImportError:\n    pass\n"):
        with pytest.raises(AssertionError):
            exiger_airflow_importe_sous_garde(ast.parse(faute))


def test_le_module_s_importe_sans_airflow_et_ne_publie_alors_aucun_dag():
    if importlib.util.find_spec("airflow") is not None:
        pytest.skip("Airflow est installé ici : le DAG est construit à l'import")
    assert dag_module.dag is None


def test_les_huit_taches_sont_celles_du_document_05_dans_le_meme_ordre():
    texte = DOCUMENT_05.read_text(encoding="utf-8")
    positions = [texte.find(f"`{tache}`") for tache in dag_module.TACHES]
    assert -1 not in positions, dict(zip(dag_module.TACHES, positions, strict=True))
    assert positions == sorted(positions)
    assert dag_module.TACHES[0] == "attendre_sources"
    assert dag_module.TACHES[-1] == "reentrainer_si_planifie"
    assert len(set(dag_module.TACHES)) == len(dag_module.TACHES)


# ---------------------------------------------------------------- fonctions des tâches


def test_la_date_logique_est_lue_au_format_iso():
    assert dag_module.date_logique("2026-08-02").isoformat() == "2026-08-02"
    assert dag_module.partition("lac", "2026-08-02") == Path("lac/paiements/ds=2026-08-02")
    assert dag_module.chemins(Path("d")) == {"chemin_oltp": str(Path("d/oltp.sqlite")),
                                             "chemin_olap": str(Path("d/olap.duckdb")),
                                             "dossier_lac": str(Path("d/lac"))}


@pytest.mark.parametrize("ds, message", [
    ("20260802", "date logique attendue au format AAAA-MM-JJ : '20260802'"),
    ("2026-8-2", "date logique attendue au format AAAA-MM-JJ : '2026-8-2'"),
    ("02/08/2026", "date logique attendue au format AAAA-MM-JJ : '02/08/2026'"),
    ("2026-08-02T00:00", "date logique attendue au format AAAA-MM-JJ : '2026-08-02T00:00'"),
    ("", "date logique attendue au format AAAA-MM-JJ : ''"),
    ("2026-02-30", "day is out of range for month"),  # bon format, jour impossible
])
def test_une_date_logique_mal_formee_est_refusee(ds, message):
    with pytest.raises(ValueError) as erreur:
        dag_module.date_logique(ds)
    assert str(erreur.value) == message  # le message dit à l'exploitant ce qui est attendu
    with pytest.raises(ValueError):
        dag_module.partition("lac", ds)


def _derouler(demo, ds: str) -> dict:
    """Les sept tâches après le capteur, dans l'ordre du DAG, sur la mini-démonstration."""
    c = dag_module.chemins(demo.chemin_oltp.parent)
    return {
        "extraits": dag_module.extraire_oltp_vers_lac(ds, c["chemin_oltp"], c["dossier_lac"]),
        "etape": dag_module.copier_vers_staging(ds, c["dossier_lac"], c["chemin_olap"]),
        "faits": dag_module.fusionner_faits(c["chemin_oltp"], c["chemin_olap"]),
        "agregats": dag_module.rafraichir_agregats(c["chemin_olap"]),
        "qualite": dag_module.controler_qualite(ds, c["chemin_olap"]),
        "marqueur": dag_module.publier_partition(ds, c["dossier_lac"]),
        "modele": dag_module.reentrainer_si_planifie(ds, c["chemin_olap"]),
    }


def test_la_chaine_complete_charge_le_jour_et_se_rejoue_sans_doublon(mini_demo):
    mini_demo.entrepot.close()  # les tâches ouvrent leur propre connexion
    c = dag_module.chemins(mini_demo.chemin_oltp.parent)
    assert dag_module.sources_pretes(JOUR, c["chemin_oltp"]) is True

    premier = _derouler(mini_demo, JOUR)
    attendu = {"extraits": 120, "etape": 120, "faits": 480, "agregats": None,
               "qualite": {"paiements_du_jour": 120, "faits": 480},
               "marqueur": str(Path(c["dossier_lac"]) / "paiements" / f"ds={JOUR}" / "_PRET"),
               "modele": None}  # dimanche : pas de réentraînement
    assert premier == attendu
    assert Path(premier["marqueur"]).read_bytes() == f"{JOUR}\n".encode()

    # Rejouer le même jour réécrit les mêmes données au même endroit : rien ne double.
    assert _derouler(mini_demo, JOUR) == attendu
    fichiers = sorted(p.name for p in (Path(c["dossier_lac"]) / "paiements").rglob("*"))
    assert fichiers == ["_PRET", "ds=2026-08-02", "paiements.parquet"]


def test_le_capteur_attend_tant_que_le_jour_n_a_aucun_paiement(mini_demo, tmp_path):
    chemin = str(mini_demo.chemin_oltp)
    assert dag_module.sources_pretes("2026-08-04", chemin) is True  # dernier jour chargé
    assert dag_module.sources_pretes("2026-08-05", chemin) is False
    assert dag_module.sources_pretes("2026-07-31", chemin) is False
    assert dag_module.sources_pretes(JOUR, str(tmp_path / "absente.sqlite")) is False
    with pytest.raises(ValueError):
        dag_module.sources_pretes("demain", chemin)


def test_le_capteur_se_declenche_des_le_premier_paiement_du_jour(tmp_path):
    # Une base OLTP réduite à la seule colonne lue par le capteur : un seul paiement le 2 août.
    chemin = tmp_path / "oltp.sqlite"
    with sqlite3.connect(chemin) as cx:
        cx.execute("CREATE TABLE paiements (id_paiement INTEGER PRIMARY KEY, cree_le DATETIME)")
        cx.execute("INSERT INTO paiements VALUES (1, '2026-08-02 23:59:59')")
    cx.close()
    assert dag_module.sources_pretes(JOUR, str(chemin)) is True
    assert dag_module.sources_pretes("2026-08-03", str(chemin)) is False  # état inchangé : rien


def test_une_somme_sur_une_table_vide_compte_zero():
    # En SQL, sum() sur aucune ligne vaut NULL : le contrôle de qualité doit lire 0, pas planter.
    with duckdb.connect() as cx:
        cx.execute("CREATE TABLE t (n INTEGER)")
        assert dag_module.compter(cx, "SELECT sum(n) FROM t") == 0
        cx.execute("INSERT INTO t VALUES (2), (3)")
        assert dag_module.compter(cx, "SELECT sum(n) FROM t") == 5
        with pytest.raises(duckdb.CatalogException):  # table inconnue : l'erreur remonte
            dag_module.compter(cx, "SELECT count(*) FROM absente")


class _EspionDuckdb(ConnexionEspion):
    """La connexion espion de `aide_demo`, utilisable dans un `with` comme une connexion DuckDB."""

    def __enter__(self):
        return self

    def __exit__(self, *erreur):
        self.connexion.close()


def test_les_requetes_des_taches_partent_telles_qu_ecrites(mini_demo, monkeypatch):
    # DuckDB ignore la casse des mots-clés et des noms : seul le texte envoyé distingue un
    # `select` d'un `SELECT`. On note, tâche par tâche, les instructions reçues par DuckDB.
    mini_demo.entrepot.close()
    c = dag_module.chemins(mini_demo.chemin_oltp.parent)
    _derouler(mini_demo, JOUR)
    espions: list[_EspionDuckdb] = []
    vrai_connect = duckdb.connect

    def connect_espionne(*arguments, **options):
        espions.append(_EspionDuckdb(vrai_connect(*arguments, **options)))
        return espions[-1]

    monkeypatch.setattr(dag_module.duckdb, "connect", connect_espionne)

    def requetes(tache, *arguments):
        espions.clear()
        resultat = tache(*arguments)
        return resultat, [texte for espion in espions for texte in espion.textes()]

    pret, textes = requetes(dag_module.sources_pretes, JOUR, c["chemin_oltp"])
    assert pret is True
    assert "INSTALL sqlite; LOAD sqlite;" in textes
    assert "SELECT count(*) FROM oltp.paiements WHERE CAST(cree_le AS DATE) = ?" in textes

    lignes, textes = requetes(dag_module.copier_vers_staging, JOUR, c["dossier_lac"],
                              c["chemin_olap"])
    fichier = (dag_module.partition(c["dossier_lac"], JOUR) / "paiements.parquet").as_posix()
    assert lignes == 120
    assert textes[-2:] == [
        f"CREATE OR REPLACE TABLE stg_paiements AS SELECT * FROM read_parquet('{fichier}')",
        "SELECT count(*) FROM stg_paiements"]

    bilan, textes = requetes(dag_module.controler_qualite, JOUR, c["chemin_olap"])
    assert bilan == {"paiements_du_jour": 120, "faits": 480}
    assert textes[1:5] == [
        "SELECT count(*) FROM stg_paiements",
        "SELECT count(*) FROM faits_paiements WHERE cle_date = 20260802",
        "SELECT count(*) FROM faits_paiements",
        "SELECT sum(nb_paiements) FROM agr_chiffre_quotidien"]


def test_une_source_absente_est_refusee_sans_rien_creer(tmp_path):
    lac = tmp_path / "lac"
    with pytest.raises(FileNotFoundError, match="base OLTP introuvable"):
        dag_module.extraire_oltp_vers_lac(JOUR, str(tmp_path / "absente.sqlite"), str(lac))
    with pytest.raises(FileNotFoundError, match="partition du jour absente du lac"):
        dag_module.copier_vers_staging(JOUR, str(lac), str(tmp_path / "olap.duckdb"))
    with pytest.raises(FileNotFoundError, match="rien à publier"):
        dag_module.publier_partition(JOUR, str(lac))
    assert not lac.exists()  # aucune partition vide ni marqueur posé


def test_un_jour_sans_paiement_donne_une_partition_vide_que_la_qualite_accepte(mini_demo):
    mini_demo.entrepot.close()
    c = dag_module.chemins(mini_demo.chemin_oltp.parent)
    assert dag_module.extraire_oltp_vers_lac("2026-08-09", c["chemin_oltp"],
                                             c["dossier_lac"]) == 0
    assert dag_module.copier_vers_staging("2026-08-09", c["dossier_lac"], c["chemin_olap"]) == 0
    assert dag_module.controler_qualite("2026-08-09", c["chemin_olap"]) == {
        "paiements_du_jour": 0, "faits": 480}


def _entrepot_charge(mini_demo):
    mini_demo.entrepot.close()
    c = dag_module.chemins(mini_demo.chemin_oltp.parent)
    _derouler(mini_demo, JOUR)
    return c


def _ecrire(chemin_olap: str, sql: str) -> None:
    import duckdb

    with duckdb.connect(chemin_olap) as cx:
        cx.execute(sql)


def test_un_fait_du_jour_perdu_arrete_le_dag(mini_demo):
    c = _entrepot_charge(mini_demo)
    _ecrire(c["chemin_olap"], """
        DELETE FROM faits_paiements WHERE id_paiement =
            (SELECT min(id_paiement) FROM faits_paiements WHERE cle_date = 20260802)""")
    with pytest.raises(dag_module.ControleQualiteEchoue) as erreur:
        dag_module.controler_qualite(JOUR, c["chemin_olap"])
    assert str(erreur.value) == ("faits du jour 119 au lieu de 120 paiements extraits ; "
                                 "agrégats 480 au lieu de 479 faits")


def test_des_agregats_perimes_arretent_le_dag(mini_demo):
    c = _entrepot_charge(mini_demo)
    _ecrire(c["chemin_olap"], "UPDATE agr_chiffre_quotidien SET nb_paiements = nb_paiements + 1")
    with pytest.raises(dag_module.ControleQualiteEchoue, match=r"^agrégats \d+ au lieu de 480"):
        dag_module.controler_qualite(JOUR, c["chemin_olap"])


def test_une_cle_orpheline_arrete_le_dag(mini_demo):
    c = _entrepot_charge(mini_demo)
    for colonne in ("cle_client", "cle_marchand", "cle_date"):
        _ecrire(c["chemin_olap"], f"""
            UPDATE faits_paiements SET {colonne} = -1
            WHERE id_paiement = (SELECT max(id_paiement) FROM faits_paiements)""")
        # Le fait modifié est du 4 août : le jour contrôlé (2 août) et les agrégats restent
        # exacts, seule la clé orpheline est signalée.
        with pytest.raises(dag_module.ControleQualiteEchoue) as erreur:
            dag_module.controler_qualite(JOUR, c["chemin_olap"])
        assert str(erreur.value) == "1 fait(s) avec une clé de dimension orpheline", colonne
        _derouler(mini_demo, JOUR)  # rechargement : l'entrepôt redevient cohérent
        assert dag_module.controler_qualite(JOUR, c["chemin_olap"])["faits"] == 480


def test_le_modele_n_est_reentraine_que_le_jour_prevu(mini_demo, monkeypatch):
    mini_demo.entrepot.close()
    monkeypatch.setattr(dag_module.fraude, "entrainer", faux_entrainer)
    chemin_olap = str(mini_demo.chemin_olap)
    assert dag_module.JOUR_REENTRAINEMENT == 1  # lundi, au sens d'isoweekday
    assert dag_module.reentrainer_si_planifie(JOUR, chemin_olap) is None
    assert dag_module.reentrainer_si_planifie(LUNDI, chemin_olap) == {
        "version": "fraude-hgb-1.0", "taux_fraude": 0.025, "pr_auc_test": 0.5,
        "roc_auc_test": 0.9, "lignes_apprentissage": 480, "lignes_test": 0}
    assert dag_module.reentrainer_si_planifie(JOUR, chemin_olap, jour_semaine=7) is not None
    with pytest.raises(ValueError):
        dag_module.reentrainer_si_planifie("lundi", chemin_olap)


def test_un_echec_de_tache_ecrit_une_alerte(caplog):
    instance = types.SimpleNamespace(task_id="controles_qualite")
    with caplog.at_level(logging.ERROR, logger="stripe_quotidien"):
        dag_module.signaler_echec({"task_instance": instance, "ds": JOUR})
        dag_module.signaler_echec({})
    assert [(r.levelno, r.name, r.getMessage()) for r in caplog.records] == [
        (logging.ERROR, "stripe_quotidien",
         "ALERTE stripe_quotidien : la tâche controles_qualite a échoué pour la date 2026-08-02"),
        (logging.ERROR, "stripe_quotidien",
         "ALERTE stripe_quotidien : la tâche inconnue a échoué pour la date inconnue"),
    ]


# ---------------------------------------------------------------- graphe, avec un faux Airflow


class _FauxDAG:
    courant: _FauxDAG | None = None

    def __init__(self, **parametres):
        self.parametres = parametres
        self.taches: dict[str, _FausseTache] = {}
        self.liens: list[tuple[str, str]] = []

    def __enter__(self):
        _FauxDAG.courant = self
        return self

    def __exit__(self, *exc):
        _FauxDAG.courant = None
        return False


class _FausseTache:
    def __init__(self, task_id, **parametres):
        self.task_id, self.parametres, self.genre = task_id, parametres, type(self).__name__
        self.dag = _FauxDAG.courant
        assert self.dag is not None, "tâche créée hors du bloc « with DAG »"
        self.dag.taches[task_id] = self

    def __rshift__(self, autre):
        self.dag.liens.append((self.task_id, autre.task_id))
        return autre


class PythonOperator(_FausseTache):
    pass


class PythonSensor(_FausseTache):
    pass


@pytest.fixture
def faux_airflow(monkeypatch):
    modules = {"airflow": types.ModuleType("airflow"),
               "airflow.operators": types.ModuleType("airflow.operators"),
               "airflow.operators.python": types.ModuleType("airflow.operators.python"),
               "airflow.sensors": types.ModuleType("airflow.sensors"),
               "airflow.sensors.python": types.ModuleType("airflow.sensors.python")}
    modules["airflow"].DAG = _FauxDAG
    modules["airflow.operators.python"].PythonOperator = PythonOperator
    modules["airflow.sensors.python"].PythonSensor = PythonSensor
    for nom, module in modules.items():
        monkeypatch.setitem(sys.modules, nom, module)
    return modules


def test_le_graphe_enchaine_les_huit_taches_avec_leurs_parametres(faux_airflow, tmp_path):
    dag = dag_module.construire_dag(tmp_path)
    c = dag_module.chemins(tmp_path)

    assert list(dag.taches) == list(dag_module.TACHES)
    assert dag.liens == list(zip(dag_module.TACHES, dag_module.TACHES[1:], strict=False))
    assert dag.parametres == {
        "dag_id": "stripe_quotidien", "schedule": "@daily",
        "start_date": datetime(2026, 8, 1, tzinfo=UTC), "catchup": False, "max_active_runs": 1,
        "tags": ["stripe", "batch"],
        "default_args": {"owner": "donnees", "retries": 3, "retry_delay": timedelta(minutes=5),
                         "retry_exponential_backoff": True,
                         "on_failure_callback": dag_module.signaler_echec}}

    capteur = dag.taches["attendre_sources"]
    assert capteur.genre == "PythonSensor"
    assert capteur.parametres == {"python_callable": dag_module.sources_pretes,
                                  "op_kwargs": {"chemin_oltp": c["chemin_oltp"]},
                                  "mode": "reschedule", "poke_interval": timedelta(minutes=10),
                                  "timeout": timedelta(hours=6)}
    attendus = {
        "extraire_oltp_vers_lac": (dag_module.extraire_oltp_vers_lac,
                                   {"chemin_oltp", "dossier_lac"}),
        "copier_vers_staging": (dag_module.copier_vers_staging, {"dossier_lac", "chemin_olap"}),
        "fusionner_faits": (dag_module.fusionner_faits, {"chemin_oltp", "chemin_olap"}),
        "rafraichir_agregats": (dag_module.rafraichir_agregats, {"chemin_olap"}),
        "controles_qualite": (dag_module.controler_qualite, {"chemin_olap"}),
        "publier_partition": (dag_module.publier_partition, {"dossier_lac"}),
        "reentrainer_si_planifie": (dag_module.reentrainer_si_planifie, {"chemin_olap"}),
    }
    for identifiant, (fonction, parametres) in attendus.items():
        tache = dag.taches[identifiant]
        assert tache.genre == "PythonOperator", identifiant
        assert tache.parametres == {"python_callable": fonction,
                                    "op_kwargs": {nom: c[nom] for nom in parametres}}


def test_une_tache_hors_du_bloc_dag_est_refusee_par_le_faux_airflow(faux_airflow):
    """Contre-épreuve du faux Airflow lui-même : il ne doit pas tout accepter."""
    with pytest.raises(AssertionError, match="hors du bloc"):
        PythonOperator(task_id="orpheline", python_callable=print)


def test_avec_airflow_present_le_module_publie_son_dag(faux_airflow, tmp_path, monkeypatch):
    monkeypatch.setenv("STRIPE_DOSSIER_DONNEES", str(tmp_path))
    specification = importlib.util.spec_from_file_location("stripe_quotidien_copie", FICHIER)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    assert isinstance(module.dag, _FauxDAG)
    assert module.dag.taches["attendre_sources"].parametres["op_kwargs"] == {
        "chemin_oltp": str(tmp_path / "oltp.sqlite")}


def test_sans_variable_d_environnement_le_dag_lit_le_dossier_de_la_demonstration(
        faux_airflow, monkeypatch):
    """Par défaut, le DAG travaille sur `stripe/sorties`, le dossier où la démonstration écrit.

    Un autre dossier par défaut ferait attendre le capteur jusqu'à sa limite sur un OLTP absent.
    """
    monkeypatch.delenv("STRIPE_DOSSIER_DONNEES", raising=False)
    racine = Path(__file__).resolve().parent.parent
    assert dag_module.DOSSIER_PAR_DEFAUT == racine / "sorties" == pipeline.SORTIES
    specification = importlib.util.spec_from_file_location("stripe_quotidien_defaut", FICHIER)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    assert module.dag.taches["attendre_sources"].parametres["op_kwargs"] == {
        "chemin_oltp": str(racine / "sorties" / "oltp.sqlite")}
