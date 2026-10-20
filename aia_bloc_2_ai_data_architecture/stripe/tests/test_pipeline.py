# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Démonstration de bout en bout : chaque étape, chaque nombre affiché, chaque fichier produit.

La démonstration complète traite 48 000 paiements (une minute). Ces tests la rejouent en petit
(3 jours de 100 paiements, puis les 5 micro-lots de 200 paiements du flux, qui sont fixes) avec
deux simplifications qui la rendent exactement reproductible : l'horloge est remplacée par un
compteur (les durées affichées deviennent des constantes) et le modèle de fraude par un faux dont
les scores sont connus (`test_fraude.py` prouve le vrai modèle). Le texte affiché est comparé à
une transcription de référence, ligne à ligne ; un test sans simplification, avec le vrai modèle,
prouve que l'ensemble fonctionne réellement.

L'exécution de référence (la plus longue : 5 000 événements sont insérés dans la base documents)
n'est faite qu'une fois pour tout le fichier ; les variantes (seuil d'alerte, seuil du PSI,
graines) suppriment la partie documents, qui ne les concerne pas.
"""

from __future__ import annotations

import inspect
import io
import json
import runpy
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
from aide_demo import INSTANT_FIGE, executer_en_petit

from stripe_demo import console, nosql, oltp, pipeline

RACINE = Path(__file__).resolve().parent.parent
TRANSCRIPTION = Path(__file__).resolve().parent / "donnees" / "executer_transcription.txt"
LOTS_DU_FLUX, PAIEMENTS_PAR_LOT = 5, 200  # le flux est fixe : 5 micro-lots de 200 paiements
PAIEMENTS_DU_FLUX = LOTS_DU_FLUX * PAIEMENTS_PAR_LOT


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    """L'exécution de référence : une seule fois pour tout le fichier.

    Les remplacements (horloge, modèle factice, espion d'ouverture de fichiers) sont défaits dès
    la fin de l'exécution : les autres tests du fichier, dont celui du vrai modèle, retrouvent
    le code réel.
    """
    dossier = tmp_path_factory.mktemp("demonstration") / "sorties"
    with pytest.MonkeyPatch.context() as mp:
        execution = executer_en_petit(mp, dossier)
    return execution


@pytest.fixture
def variante(monkeypatch, tmp_path):
    """Exécution en petit, sans la partie documents, pour les tests qui font varier un réglage."""

    def lancer(**options):
        return executer_en_petit(monkeypatch, tmp_path / "sorties", avec_documents=False,
                                 **options)

    return lancer


# ---------------------------------------------------------------- fichier de requêtes et titres


def test_le_fichier_de_requetes_est_decoupe_en_questions_et_un_fichier_absent_est_refuse(
        tmp_path, lecture_ecriture_en_utf8, capsys):
    fichier = tmp_path / "requetes.sql"

    # Question et requête, sans point-virgule final ; l'en-tête avant le premier marqueur est
    # ignoré.
    fichier.write_text(
        "-- en-tête ignoré, jusqu'au premier marqueur\n\n"
        "-- @ Première question ?\nSELECT 1;\n\n"
        "-- @   Deuxième question   \nSELECT a,\n       b\nFROM t;;\n", encoding="utf-8")
    assert pipeline.requetes_sql(fichier) == [
        ("Première question ?", "SELECT 1"),
        ("Deuxième question", "SELECT a,\n       b\nFROM t")]
    # Un bloc sans requête est ignoré.
    fichier.write_text("-- @ Question sans requête\n   \n-- @ Vraie question\nSELECT 2;\n",
                       encoding="utf-8")
    assert pipeline.requetes_sql(fichier) == [("Vraie question", "SELECT 2")]
    # Le marqueur exige l'espace après l'arobase.
    fichier.write_text("-- @Collé\nSELECT 1;\n-- @ Bon\nSELECT 2;\n", encoding="utf-8")
    assert pipeline.requetes_sql(fichier) == [("Bon", "SELECT 2")]

    # Un fichier sans marqueur ne donne aucune requête ; un fichier accentué est lu en UTF-8.
    fichier.write_text("SELECT 1;\n-- un commentaire\n", encoding="utf-8")
    assert pipeline.requetes_sql(fichier) == []
    fichier.write_text("-- @ Épreuve accentuée\nSELECT 'é';\n", encoding="utf-8")
    assert pipeline.requetes_sql(fichier) == [("Épreuve accentuée", "SELECT 'é'")]
    # Seuls les points-virgules de fin sont retirés : une requête qui finit par une lettre (ici
    # « X », que l'on pourrait confondre avec un autre caractère de fin) la garde.
    fichier.write_text("-- @ Finit par X\nSELECT 1 AS X;\n", encoding="utf-8")
    assert pipeline.requetes_sql(fichier) == [("Finit par X", "SELECT 1 AS X")]
    # Les sept requêtes livrées.
    livrees = pipeline.requetes_sql(RACINE / "sql" / "requetes_olap.sql")
    assert [question.split(" ")[0] for question, _ in livrees] == [
        "Chiffre", "Les", "Taux", "Fraude", "Fraude", "Segmentation", "Série"]
    assert all(sql.startswith("SELECT") and not sql.endswith(";") for _, sql in livrees)

    with pytest.raises(FileNotFoundError):
        pipeline.requetes_sql(tmp_path / "absent.sql")

    # Un titre est encadré et précédé d'une ligne vide.
    pipeline.titre("Étape")
    assert capsys.readouterr().out == "\n=== Étape ===\n"


# ---------------------------------------------------------------- réglages de la démonstration


def test_les_reglages_sont_ceux_de_la_note_et_sans_paiement_la_demonstration_s_arrete(
        tmp_path):
    parametres = inspect.signature(pipeline.executer).parameters
    assert parametres["dossier"].default == pipeline.SORTIES == RACINE / "sorties"
    assert pipeline.SEUIL_ALERTE == 0.8
    assert pipeline.RACINE == RACINE
    assert (pipeline.RACINE / "stripe_demo" / "pipeline.py").is_file()

    # Sans aucun paiement, il n'y a rien à apprendre : l'entraînement refuse, la démonstration
    # s'arrête avec son message au lieu de publier un modèle vide.
    with redirect_stdout(io.StringIO()), pytest.raises(ValueError):
        pipeline.executer(tmp_path / "sorties", jours=0, par_jour=10)


def test_lancee_comme_programme_la_demonstration_demarre_et_importee_elle_ne_fait_rien(
        monkeypatch):
    """Le point d'entrée `python -m stripe_demo.pipeline` appelle `executer`, et lui seul.

    La démonstration complète dure une minute : au lieu de la lancer, on remplace la première
    chose qu'elle fait (régler la sortie en UTF-8) par une exception, qui prouve que `executer`
    a été appelé. Chargé sous un autre nom (comme le fait un `import`), le fichier ne lance rien.
    """

    class Demarree(Exception):
        pass

    def arreter():
        raise Demarree

    monkeypatch.setattr(console, "forcer_utf8", arreter)
    fichier = str(Path(pipeline.__file__))

    with pytest.raises(Demarree):
        runpy.run_path(fichier, run_name="__main__")
    resultat = runpy.run_path(fichier, run_name="stripe_demo.pipeline_importe")
    assert "executer" in resultat


class DemonstrationArretee(Exception):
    """Levée par un faux entraînement pour arrêter la démonstration après l'étape du batch."""


def test_sans_reglage_la_demonstration_charge_soixante_jours_de_huit_cents_paiements(
        tmp_path, monkeypatch):
    """Les réglages par défaut sont observés EN APPELANT sans les donner, pas en lisant la
    signature : c'est ce que fait `python -m stripe_demo.pipeline`."""
    jours_demandes, lots = [], []
    monkeypatch.setattr(pipeline.oltp, "referentiels",
                        lambda moteur, jours: jours_demandes.append(jours))

    def lot(moteur, debut, nombre, graine, premier_id):
        lots.append((debut, nombre, graine, premier_id))
        return premier_id + nombre - 1

    def arreter(_entrepot):
        raise DemonstrationArretee

    monkeypatch.setattr(pipeline.oltp, "paiements_synthetiques", lot)
    monkeypatch.setattr(pipeline.olap, "charger_batch", lambda entrepot, chemin: {"faits": 0})
    monkeypatch.setattr(pipeline.fraude, "entrainer", arreter)

    with redirect_stdout(io.StringIO()), pytest.raises(DemonstrationArretee):
        pipeline.executer(tmp_path / "sorties")

    assert jours_demandes == [62]  # soixante jours de paiements, deux de plus pour les taux
    debut = datetime(2026, 8, 1, tzinfo=UTC)
    assert lots == [(debut + timedelta(days=j), 800, 1000 + j, 1 + 800 * j) for j in range(60)]


# ---------------------------------------------------------------- exécution de référence


def _verifier_le_texte_le_rapport_et_le_json(reference):
    attendu = TRANSCRIPTION.read_text(encoding="utf-8").split("\n", 1)[1]
    rapport = reference.rapport

    assert reference.texte == attendu
    assert rapport["oltp"] == {"marchands": 40, "clients": 2000, "paiements": 300,
                               "litiges": 8, "remboursements": 12, "journal_changements": 300}
    assert rapport["batch"] == {"faits": 300}
    assert rapport["modele"] == {
        "version": "fraude-hgb-1.0", "taux_fraude": 0.025, "pr_auc_test": 0.5,
        "roc_auc_test": 0.9, "lignes_apprentissage": 300, "lignes_test": 0}
    assert rapport["flux"]["paiements_notes"] == PAIEMENTS_DU_FLUX
    assert rapport["flux"]["latence_mediane_ms"] == 123.5  # 123,456 ms arrondi à 1 décimale
    assert list(rapport) == ["oltp", "batch", "modele", "flux", "surveillance", "requetes_sql",
                             "requetes_nosql"]
    assert set(rapport["requetes_nosql"]) == {
        "erreurs de paiement les plus fréquentes (30 jours)",
        "sessions mobiles abandonnées les plus longues", "thèmes des avis négatifs"}
    # Le rapport contient les documents rendus par chaque requête, pas seulement leurs intitulés.
    for question, etapes in nosql.REQUETES.items():
        assert rapport["requetes_nosql"][question] == list(
            reference.base.evenements.aggregate(etapes)) != []
    for question, etapes in nosql.REQUETES_RETOURS.items():
        assert rapport["requetes_nosql"][question] == list(
            reference.base.retours_clients.aggregate(etapes)) != []
    lignes = rapport["requetes_sql"]["Taux de remboursement et de litige par catégorie "
                                     "d'activité (MCC)"]
    assert set(lignes[0]) == {"code_mcc", "nb_paiements", "taux_remboursement_pct",
                              "taux_litige_pct"}
    assert all(isinstance(valeur, str) for ligne in lignes for valeur in ligne.values())

    # Le rapport complet est écrit en JSON UTF-8 (CRLF sous Windows : on compare à fins égales).
    ecrit = (reference.dossier / "rapport_demo.json").read_bytes().decode("utf-8")
    assert ecrit.replace("\r\n", "\n") == json.dumps(
        rapport, ensure_ascii=False, indent=2, default=str)
    assert "réentraînement" in ecrit  # accents conservés, pas échappés
    assert list(json.loads(ecrit)["requetes_sql"]) == [
        question for question, _ in pipeline.requetes_sql(RACINE / "sql" / "requetes_olap.sql")]


def _verifier_les_fichiers_les_documents_et_les_signaux(reference):
    assert sorted(p.name for p in reference.dossier.iterdir()) == [
        "olap.duckdb", "oltp.sqlite", "rapport_demo.json"]
    entrepot = duckdb.connect(str(reference.dossier / "olap.duckdb"), read_only=True)
    assert entrepot.execute("SELECT count(*) FROM faits_paiements").fetchall()[0][0] == 1300
    assert entrepot.execute("SELECT count(*) FROM dim_client").fetchall()[0][0] == 2000

    assert reference.base.evenements.count_documents({}) == 5000
    assert reference.base.retours_clients.count_documents({}) == 400

    # Un signal de fraude pour chaque paiement du flux : score arrondi à 4 décimales.
    signaux = list(reference.base.signaux_fraude.find({}, {"_id": 0}))
    assert len(signaux) == PAIEMENTS_DU_FLUX
    assert sorted(s["id_paiement"] for s in signaux) == list(range(301, 1301))
    assert {s["score"] for s in signaux} == {0.9, 0.1235}  # 0,123456 arrondi à 4 décimales
    assert {s["version_modele"] for s in signaux} == {"fraude-hgb-1.0"}
    # Horodatés à « maintenant » (fixé par l'aide de test), en UTC : MongoDB range l'UTC sans
    # fuseau.
    assert {s["horodatage"] for s in signaux} == {INSTANT_FIGE.replace(tzinfo=None)}


def _verifier_le_service_de_score_et_les_lots_du_flux(reference):
    paiement, carac, anciennete, pays = reference.appels_noter[0]
    entrepot = duckdb.connect(str(reference.dossier / "olap.duckdb"), read_only=True)
    attendu = entrepot.execute(
        "SELECT code_pays, anciennete_jours FROM dim_client WHERE cle_client = ?",
        [paiement["id_client"]]).fetchall()[0]

    assert set(paiement) == {"id_client", "montant_usd", "heure", "pays_ip", "appareil"}
    assert carac["id_client"] == paiement["id_client"]
    assert carac["nb_paiements"] >= 1
    assert (pays, anciennete) == attendu
    assert 0 <= paiement["heure"] <= 23
    assert len(reference.appels_noter) == PAIEMENTS_DU_FLUX

    debut = datetime(2026, 8, 1, tzinfo=UTC)
    jour_flux = debut + timedelta(days=3)
    assert reference.appels_paiements[:3] == [
        (debut, 100, 1000, 1), (debut + timedelta(days=1), 100, 1001, 101),
        (debut + timedelta(days=2), 100, 1002, 201)]
    assert reference.appels_paiements[3:] == [
        (jour_flux + timedelta(hours=4 * lot), 200, 9000 + lot, 301 + 200 * lot)
        for lot in range(5)]


def _verifier_les_requetes_telles_qu_elles_sont_ecrites(reference):
    """DuckDB et SQLite ignorent la casse du SQL : seul le texte envoyé prouve ce qui est écrit."""
    vers_oltp = reference.requetes_oltp
    assert "SELECT max(position) FROM journal_changements" in vers_oltp
    for table in ("marchands", "clients", "paiements", "litiges", "remboursements",
                  "journal_changements"):
        assert f"SELECT count(*) FROM {table}" in vers_oltp

    vers_entrepot = reference.entrepot.textes()
    assert "SELECT cle_client, code_pays, anciennete_jours FROM dim_client" in vers_entrepot
    flux = ("SELECT id_paiement, cle_client, montant_usd, heure, pays_ip, appareil "
            "FROM faits_paiements WHERE id_paiement > ? ORDER BY id_paiement")
    # Une lecture par micro-lot, à partir du dernier paiement déjà chargé (300, puis +200).
    assert [parametres for sql, parametres in reference.entrepot.instructions if sql == flux] == [
        (300 + 200 * lot,) for lot in range(LOTS_DU_FLUX)]
    for _question, sql in pipeline.requetes_sql(RACINE / "sql" / "requetes_olap.sql"):
        assert " ".join(sql.split()) in vers_entrepot
    assert vers_entrepot[-1] == "close()"  # la connexion DuckDB est refermée à la fin

    # Arguments des appels dont la sortie ne dit pas ce qui est écrit (None vaut False ici).
    nombre_de_questions = len(pipeline.requetes_sql(RACINE / "sql" / "requetes_olap.sql"))
    assert [o for o in reference.appels_to_string if "index" in o] == nombre_de_questions * [
        {"index": False}]
    assert {"ensure_ascii": False, "indent": 2, "default": str} in reference.appels_json
    assert reference.appels_to_dict == nombre_de_questions * [(("records",), {})]


def test_la_demonstration_en_petit_est_exacte_de_bout_en_bout(reference):
    """Texte, rapport, fichiers, documents, score et requêtes de l'exécution de référence.

    Pourquoi UN seul test pour tout ce qui relit `reference` : l'exécution de référence est faite
    une fois (fixture de module), au moment où le premier test la demande. Les outils de mutation
    (`mutmut`) rattachent l'appel de `pipeline.executer` à CE test-là seulement : les tests qui ne
    font que relire le résultat ne seraient jamais lancés contre une version altérée de
    `executer`, et une altération qu'eux seuls verraient passerait inaperçue. Les quatre
    vérifications ci-dessous (écrites comme des fonctions à part pour rester lisibles) sont donc
    rassemblées ici.
    """
    _verifier_le_texte_le_rapport_et_le_json(reference)
    _verifier_les_fichiers_les_documents_et_les_signaux(reference)
    _verifier_le_service_de_score_et_les_lots_du_flux(reference)
    _verifier_les_requetes_telles_qu_elles_sont_ecrites(reference)


# ---------------------------------------------------------------- variantes


def test_les_seuils_d_alerte_et_de_reentrainement_sont_stricts_ou_inclus_comme_documente(
        variante):
    # Alerte : le score ATTEINT le seuil (0,8 inclus), 0,7999999 n'alerte pas.
    scores = iter([0.8, 0.7999999, 0.8000001, 0.0, 1.0])

    def noter_aux_limites(modele, paiement, caracteristiques, anciennete_jours, pays_client):
        return next(scores, 0.0)

    assert variante(noter=noter_aux_limites).rapport["flux"]["alertes"] == 3

    # Réentraînement : PSI STRICTEMENT supérieur à 0,2 ; arrondi à trois décimales.
    for psi, decision in ((0.2, "stable"), (0.2000001, "réentraînement"),
                          (0.1999999, "stable"), (0.0, "stable"), (5.0, "réentraînement")):
        execution = variante(psi=psi)
        assert execution.rapport["surveillance"] == {"psi": round(psi, 3), "decision": decision}
        assert f"'decision': '{decision}'" in execution.texte
    assert variante(psi=0.123456).rapport["surveillance"]["psi"] == 0.123

    # Les taux de change couvrent les jours demandés plus deux.
    appels = []
    variante(jours=2, par_jour=30, appels_referentiels=appels)
    assert appels == [4]


def test_une_execution_precedente_est_effacee_et_un_dossier_sans_parent_est_refuse(
        variante, tmp_path, monkeypatch):
    # La base OLTP ne reste pas ouverte : sous Windows, une connexion gardée en réserve par
    # SQLAlchemy verrouillerait `oltp.sqlite` et empêcherait de relancer la démonstration.
    moteurs = []
    vrai_moteur = oltp.moteur

    def moteur_note(chemin):
        engine = vrai_moteur(chemin)
        moteurs.append((engine, engine.pool))
        return engine

    monkeypatch.setattr(pipeline.oltp, "moteur", moteur_note)
    premier = variante()
    assert moteurs
    assert all(engine.pool is not pool_initial for engine, pool_initial in moteurs)
    (tmp_path / "sorties" / "oltp.sqlite").write_bytes(b"pas une base")

    second = variante()  # le dossier existe déjà : pas d'erreur

    assert second.rapport["oltp"] == premier.rapport["oltp"]
    assert second.rapport["batch"] == {"faits": 300}
    assert (tmp_path / "sorties" / "oltp.sqlite").read_bytes()[:15] == b"SQLite format 3"

    with pytest.raises(FileNotFoundError):
        executer_en_petit(monkeypatch, tmp_path / "absent" / "sorties", avec_documents=False)
    assert not (tmp_path / "absent").exists()


# ---------------------------------------------------------------- avec le vrai modèle


def test_la_demonstration_en_petit_fonctionne_avec_le_vrai_modele(tmp_path):
    """Sans aucune simplification : 8 jours de 250 paiements, vrai entraînement, vrai score."""
    sortie = io.StringIO()
    with redirect_stdout(sortie):
        rapport = pipeline.executer(tmp_path / "sorties", jours=8, par_jour=250)

    assert rapport["oltp"]["paiements"] == 2000
    assert rapport["batch"] == {"faits": 2000}
    assert rapport["modele"]["version"] == "fraude-hgb-1.0"
    assert rapport["modele"]["lignes_apprentissage"] + rapport["modele"]["lignes_test"] == 2000
    assert rapport["modele"]["lignes_test"] > 0  # le modèle factice n'évalue rien : ici, si
    assert 0.5 < rapport["modele"]["roc_auc_test"] <= 1.0
    assert rapport["flux"]["paiements_notes"] == PAIEMENTS_DU_FLUX
    assert 0 <= rapport["flux"]["alertes"] <= PAIEMENTS_DU_FLUX
    assert rapport["surveillance"]["decision"] in ("stable", "réentraînement")
    assert rapport["surveillance"]["psi"] >= 0.0
    assert sortie.getvalue().count("=== ") >= 6  # un titre encadré pour chacune des six étapes
