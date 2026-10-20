# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Alimentation d'un PostgreSQL « de production locale » : secrets, moteur, amorçage, flux.

Aucun PostgreSQL n'est disponible dans ces tests (les tests Docker optionnels le prouvent sur un
vrai serveur). On vérifie donc tout ce qui n'en dépend pas : la lecture des secrets (jamais de
mot de passe par défaut), l'URL de connexion construite, et la logique d'amorçage et de flux, que
les requêtes simples utilisées rendent exécutable sur SQLite. Les appels réseau (`create_engine`,
`time.sleep`) sont remplacés par des enregistreurs. Chaque fonction est testée avec ses refus.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aide_aleatoire import AleaEspion
from sqlalchemy import event, insert, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError

from stripe_demo import modele_oltp as m
from stripe_demo import oltp
from stripe_demo import vers_postgres as vp

MOMENT = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
MESSAGE_SECRET = "secret manquant : définir SECRET_X_FILE ou SECRET_X"


@pytest.fixture
def environnement_propre(monkeypatch):
    """Retire de l'environnement toutes les variables que le module lit."""
    for nom in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER",
                "POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE", "POSTGRES_APP_PASSWORD",
                "POSTGRES_APP_PASSWORD_FILE", "SECRET_X", "SECRET_X_FILE"):
        monkeypatch.delenv(nom, raising=False)


# ---------------------------------------------------------------- secrets


def test_un_secret_vient_du_fichier_ou_de_la_variable_et_son_absence_est_refusee(
        environnement_propre, monkeypatch, tmp_path, lecture_ecriture_en_utf8):
    # La variable d'environnement.
    monkeypatch.setenv("SECRET_X", "mot-de-passe-1")
    assert vp.lire_secret("SECRET_X") == "mot-de-passe-1"

    # Le fichier désigné par <NOM>_FILE, sans sa fin de ligne, lu en UTF-8 explicite, l'emporte.
    fichier = tmp_path / "secret.txt"
    fichier.write_text("depuis-le-fichier-é\n", encoding="utf-8")
    monkeypatch.setenv("SECRET_X_FILE", str(fichier))
    assert vp.lire_secret("SECRET_X") == "depuis-le-fichier-é"

    # Absent, vide, fichier vide ou fait d'espaces, fichier introuvable : refusé.
    monkeypatch.delenv("SECRET_X_FILE")
    monkeypatch.delenv("SECRET_X")
    with pytest.raises(SystemExit) as erreur:
        vp.lire_secret("SECRET_X")
    assert str(erreur.value) == MESSAGE_SECRET
    monkeypatch.setenv("SECRET_X", "")
    with pytest.raises(SystemExit) as erreur:
        vp.lire_secret("SECRET_X")
    assert str(erreur.value) == MESSAGE_SECRET
    for contenu in ("", "\n", "   \n\t"):
        fichier.write_text(contenu, encoding="utf-8")
        monkeypatch.setenv("SECRET_X_FILE", str(fichier))
        with pytest.raises(SystemExit) as erreur:
            vp.lire_secret("SECRET_X")
        assert str(erreur.value) == MESSAGE_SECRET
    monkeypatch.setenv("SECRET_X_FILE", str(tmp_path / "absent.txt"))
    monkeypatch.setenv("SECRET_X", "ignoree")
    with pytest.raises(FileNotFoundError):
        vp.lire_secret("SECRET_X")


# ---------------------------------------------------------------- moteur


def test_le_moteur_lit_son_url_dans_l_environnement_et_refuse_un_port_ou_un_secret_invalide(
        environnement_propre, monkeypatch):
    appels = []
    monkeypatch.setattr(vp, "create_engine", lambda url, **options: (
        appels.append((url, options)) or "MOTEUR"))

    # Valeurs par défaut.
    monkeypatch.setenv("POSTGRES_PASSWORD", "tres-secret")
    assert vp.moteur("alice", "POSTGRES_PASSWORD") == "MOTEUR"
    (url, options), = appels
    assert isinstance(url, URL)
    assert (url.drivername, url.username, url.password) == (
        "postgresql+psycopg", "alice", "tres-secret")
    assert (url.host, url.port, url.database) == ("localhost", 5432, "stripe")
    assert options == {"pool_pre_ping": True}
    assert "tres-secret" not in repr(url) and "tres-secret" not in str(url)  # jamais affiché

    # Hôte, port et base viennent de l'environnement.
    monkeypatch.setenv("POSTGRES_APP_PASSWORD", "secret")
    monkeypatch.setenv("POSTGRES_HOST", "base.interne")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "autre")
    vp.moteur("svc_application", "POSTGRES_APP_PASSWORD")
    url = appels[-1][0]
    assert (url.username, url.password) == ("svc_application", "secret")
    assert (url.host, url.port, url.database) == ("base.interne", 6543, "autre")

    # Un port qui n'est pas un nombre, ou un secret absent : refusé avant toute connexion.
    appels.clear()
    monkeypatch.setenv("POSTGRES_PORT", "cinq mille")
    with pytest.raises(ValueError):
        vp.moteur("alice", "POSTGRES_PASSWORD")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.delenv("POSTGRES_PASSWORD")
    with pytest.raises(SystemExit):
        vp.moteur("alice", "POSTGRES_PASSWORD")
    assert appels == []


# ---------------------------------------------------------------- amorçage


@pytest.fixture
def base_vide(tmp_path):
    engine = oltp.moteur(str(tmp_path / "oltp.sqlite"))
    oltp.creer(engine)
    yield engine
    engine.dispose()


def _instructions(engine) -> list[str]:
    """Liste (vivante) des instructions SQL envoyées au moteur, telles que la base les reçoit."""
    envoyees: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _noter(connexion, curseur, statement, parametres, contexte, plusieurs):
        envoyees.append(" ".join(statement.split()))

    return envoyees


def _compter(engine, table: str) -> int:
    with engine.connect() as cx:
        return cx.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_amorcer_charge_les_referentiels_une_fois_et_refuse_une_base_sans_schema(
        base_vide, capsys, tmp_path):
    instructions = _instructions(base_vide)

    vp.amorcer(base_vide)

    assert capsys.readouterr().out == "référentiels chargés (taux de change sur 14 jours)\n"
    assert instructions[0] == "SELECT count(*) FROM marchands"
    assert _compter(base_vide, "marchands") == 40
    assert _compter(base_vide, "taux_change") == 5 * 14

    # Déjà amorcée : rien n'est écrit ni changé, même avec un autre nombre de jours.
    avant = (_compter(base_vide, "marchands"), _compter(base_vide, "clients"),
             _compter(base_vide, "taux_change"))
    instructions.clear()
    vp.amorcer(base_vide, jours=5)
    assert capsys.readouterr().out == "référentiels déjà présents : rien à faire\n"
    assert (_compter(base_vide, "marchands"), _compter(base_vide, "clients"),
            _compter(base_vide, "taux_change")) == avant
    assert not any(i.startswith("INSERT") for i in instructions)

    # Le nombre de jours demandé est pris en compte dans une base vide.
    autre = oltp.moteur(str(tmp_path / "autre.sqlite"))
    oltp.creer(autre)
    vp.amorcer(autre, jours=3)
    assert capsys.readouterr().out == "référentiels chargés (taux de change sur 3 jours)\n"
    assert _compter(autre, "taux_change") == 5 * 3
    autre.dispose()

    sans_schema = oltp.moteur(str(tmp_path / "vide.sqlite"))  # aucune table
    with pytest.raises(OperationalError):
        vp.amorcer(sans_schema)
    sans_schema.dispose()


def test_une_base_qui_n_a_qu_un_seul_marchand_est_deja_amorcee(base_vide, capsys):
    """La limite : un marchand suffit pour que la base ne soit plus « vide ».

    Les tests précédents ne voient que 0 marchand (base vide) ou 40 (amorcée) ; un seuil placé
    à « plus d'un » donnerait les mêmes résultats. Avec exactement un marchand, il faut refuser
    de recharger les référentiels (sinon les 40 marchands de la démonstration viendraient
    s'ajouter à celui-ci, ou échoueraient sur la clé déjà prise).
    """
    with base_vide.begin() as cx:
        cx.execute(insert(m.pays), [{"code_pays": "FR", "nom": "France", "region": "EEE"}])
        cx.execute(insert(m.marchands), [{
            "id_marchand": 1, "raison_sociale": "Seul marchand", "code_pays": "FR",
            "code_mcc": "5411", "statut": "actif", "cree_le": MOMENT}])
    instructions = _instructions(base_vide)

    vp.amorcer(base_vide)

    assert capsys.readouterr().out == "référentiels déjà présents : rien à faire\n"
    assert _compter(base_vide, "marchands") == 1
    assert _compter(base_vide, "clients") == 0
    assert not any(i.startswith("INSERT") for i in instructions)


# ---------------------------------------------------------------- flux


@pytest.fixture
def base_amorcee(base_vide):
    oltp.referentiels(base_vide, jours=3)
    return base_vide


@pytest.fixture
def horloge_figee(monkeypatch):
    class FauxDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC, "l'heure doit être demandée en UTC, pas dans le fuseau de la machine"
            return MOMENT

    monkeypatch.setattr(vp, "datetime", FauxDatetime)


@pytest.fixture
def pauses(monkeypatch):
    """Enregistre les pauses demandées sans attendre."""
    duree = []
    monkeypatch.setattr(vp.time, "sleep", duree.append)
    return duree


def test_le_flux_insere_les_lots_les_numerote_a_la_suite_et_dort_entre_deux_lots(
        base_amorcee, horloge_figee, pauses, monkeypatch, tmp_path):
    class Sortie(io.StringIO):
        vidages = 0

        def flush(self):
            Sortie.vidages += 1
            super().flush()

    oltp.paiements_synthetiques(base_amorcee, MOMENT, 3, graine=1, premier_id=1)
    sortie = Sortie()
    monkeypatch.setattr("sys.stdout", sortie)

    vp.flux(base_amorcee, lots=3, par_lot=10, pause_s=0.5, graine=7)

    # 3 paiements existants, puis 4..33 sans trou ni doublon ; une pause entre deux lots seulement.
    with base_amorcee.connect() as cx:
        ids = [ligne[0] for ligne in cx.execute(text(
            "SELECT id_paiement FROM paiements ORDER BY id_paiement"))]
    assert ids == list(range(1, 34))
    assert pauses == [0.5, 0.5]
    assert sortie.getvalue() == (
        "lot 1/3 : 10 paiements insérés (dernier id 13)\n"
        "lot 2/3 : 10 paiements insérés (dernier id 23)\n"
        "lot 3/3 : 10 paiements insérés (dernier id 33)\n")
    assert Sortie.vidages == 3  # `flush=True` à chaque compte rendu
    # Les paiements du flux datent de la veille du moment présent.
    with base_amorcee.connect() as cx:
        premier, dernier = cx.execute(text(
            "SELECT min(cree_le), max(cree_le) FROM paiements WHERE id_paiement > 3")).one()
    veille = MOMENT - timedelta(days=1)
    assert veille.strftime("%Y-%m-%d 12:00:00") <= premier
    assert dernier <= (veille + timedelta(seconds=86_399)).strftime("%Y-%m-%d %H:%M:%S.999999")

    # Un seul lot ne dort pas ; zéro lot ne fait rien du tout.
    pauses.clear()
    monkeypatch.undo()
    autre = oltp.moteur(str(tmp_path / "autre.sqlite"))
    oltp.creer(autre)
    oltp.referentiels(autre, jours=3)
    monkeypatch.setattr(vp.time, "sleep", pauses.append)
    vp.flux(autre, lots=1, par_lot=2, pause_s=9.0, graine=7)
    assert pauses == []
    avant = _compter(autre, "paiements")
    vp.flux(autre, lots=0, par_lot=5, pause_s=1.0, graine=7)
    assert pauses == [] and _compter(autre, "paiements") == avant
    autre.dispose()

    # Sur une base sans schéma, le flux est refusé.
    sans_schema = oltp.moteur(str(tmp_path / "vide.sqlite"))
    with pytest.raises(OperationalError):
        vp.flux(sans_schema, lots=1, par_lot=2, pause_s=0.0, graine=1)
    sans_schema.dispose()


def test_le_flux_conteste_un_paiement_reussi_du_dernier_lot_choisi_par_decalage(
        base_amorcee, horloge_figee, pauses, monkeypatch):
    """Décalage tiré entre 0 et par_lot // 2 : au maximum (5), le 6e paiement du lot ; au minimum,
    le premier du DERNIER lot (jamais un paiement plus ancien) ; sans paiement réussi, rien."""
    # 1. Décalage maximal : le 6e paiement d'un lot de 10.
    espion = AleaEspion(randint="max", choix=0)
    espion.random = lambda: 0.5  # ni fraude ni IP étrangère : tous les paiements sont « reussi »
    monkeypatch.setattr(vp.random, "Random", espion)
    vp.flux(base_amorcee, lots=1, par_lot=10, pause_s=0.0, graine=7)
    with base_amorcee.connect() as cx:
        statuts = dict(cx.execute(text("SELECT id_paiement, statut FROM paiements")).fetchall())
    assert statuts[6] == "conteste"
    assert [i for i, s in statuts.items() if s == "conteste"] == [6]
    assert ((0, 5), ()) in espion.appels_de("randint")

    # 2. Décalage minimal, avec des paiements plus anciens tous « reussi » : le premier du lot.
    with base_amorcee.begin() as cx:
        cx.execute(text("UPDATE paiements SET statut = 'reussi'"))
    espion = AleaEspion(randint="min", choix=0)
    espion.random = lambda: 0.5
    monkeypatch.setattr(vp.random, "Random", espion)
    vp.flux(base_amorcee, lots=1, par_lot=4, pause_s=0.0, graine=8)
    with base_amorcee.connect() as cx:
        contestes = [ligne[0] for ligne in cx.execute(text(
            "SELECT id_paiement FROM paiements WHERE statut = 'conteste'"))]
        operations = [ligne[0] for ligne in cx.execute(text(
            "SELECT operation FROM journal_changements WHERE id_ligne = 11 ORDER BY position"))]
    assert contestes == [11]  # lot 11 à 14 : décalage 0, le premier ; pas un paiement ancien
    assert operations == ["INSERT", "UPDATE"]  # la contestation est un événement de mise à jour

    # 3. Aucun paiement réussi (tous « echoue ») : aucune contestation, aucune mise à jour.
    espion = AleaEspion(randint="max", choix=1)
    espion.random = lambda: 0.5
    monkeypatch.setattr(vp.random, "Random", espion)
    vp.flux(base_amorcee, lots=2, par_lot=6, pause_s=0.0, graine=9)
    with base_amorcee.connect() as cx:
        nouveaux = cx.execute(text(
            "SELECT DISTINCT statut FROM paiements WHERE id_paiement > 14")).fetchall()
        mises_a_jour = cx.execute(text(
            "SELECT count(*) FROM journal_changements WHERE operation = 'UPDATE' "
            "AND id_ligne > 14")).scalar_one()
    assert nouveaux == [("echoue",)]
    assert mises_a_jour == 0


def test_le_flux_derive_une_graine_par_lot_et_envoie_les_requetes_attendues(
        base_amorcee, horloge_figee, pauses, monkeypatch):
    espion = AleaEspion(randint="min", choix=0)
    espion.random = lambda: 0.5
    monkeypatch.setattr(vp.random, "Random", espion)
    instructions = _instructions(base_amorcee)

    vp.flux(base_amorcee, lots=3, par_lot=4, pause_s=0.0, graine=7)

    # D'abord la graine du flux lui-même (7), puis une graine par lot : 7 * 1000 + numéro du lot.
    assert espion.graines == [7, 7001, 7002, 7003]
    assert instructions[0] == "SELECT coalesce(max(id_paiement), 0) FROM paiements"
    assert ("SELECT id_paiement FROM paiements WHERE statut = 'reussi' AND id_paiement > ? "
            "ORDER BY id_paiement LIMIT 1 OFFSET ?") in instructions
    assert "UPDATE paiements SET statut = 'conteste' WHERE id_paiement = ?" in instructions


# ---------------------------------------------------------------- ligne de commande


@pytest.fixture
def commandes(monkeypatch, environnement_propre):
    """Remplace moteur, amorcer et flux : on observe seulement ce que `main` leur transmet."""
    appels = []
    monkeypatch.setattr(vp, "moteur", lambda utilisateur, secret: (
        appels.append(("moteur", utilisateur, secret)) or f"moteur:{utilisateur}"))
    monkeypatch.setattr(vp, "amorcer", lambda moteur: appels.append(("amorcer", moteur)))
    monkeypatch.setattr(vp, "flux", lambda *args: appels.append(("flux", *args)))
    return appels


def test_la_ligne_de_commande_choisit_le_compte_transmet_les_options_et_refuse_le_reste(
        commandes, monkeypatch, capsys):
    # amorcer : le compte propriétaire, « postgres » par défaut.
    vp.main(["amorcer"])
    assert commandes == [("moteur", "postgres", "POSTGRES_PASSWORD"),
                         ("amorcer", "moteur:postgres")]
    commandes.clear()
    monkeypatch.setenv("POSTGRES_USER", "proprietaire")
    vp.main(["amorcer"])
    assert commandes[0] == ("moteur", "proprietaire", "POSTGRES_PASSWORD")

    # flux : le compte applicatif, valeurs par défaut puis options.
    commandes.clear()
    vp.main(["flux"])
    assert commandes == [("moteur", "svc_application", "POSTGRES_APP_PASSWORD"),
                         ("flux", "moteur:svc_application", 20, 50, 3.0, 2026)]
    commandes.clear()
    vp.main(["flux", "--lots", "5", "--par-lot", "7", "--pause", "0.25", "--graine", "99"])
    assert commandes[1] == ("flux", "moteur:svc_application", 5, 7, 0.25, 99)

    # Commande inconnue, absente, option invalide : code 2 d'argparse, rien n'est exécuté.
    commandes.clear()
    for arguments in (["detruire"], [], ["flux", "--lots", "beaucoup"],
                      ["flux", "--par-lot", "1.5"], ["flux", "--pause", "longtemps"],
                      ["flux", "--graine", "x"]):
        with pytest.raises(SystemExit) as sortie:
            vp.main(arguments)
        assert sortie.value.code == 2, arguments
    assert commandes == []
    assert "invalid choice" in capsys.readouterr().err


def test_l_aide_decrit_la_commande_et_chaque_option(commandes, capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")

    with pytest.raises(SystemExit) as sortie:
        vp.main(["--help"])
    general = capsys.readouterr().out
    with pytest.raises(SystemExit):
        vp.main(["flux", "--help"])
    flux = capsys.readouterr().out

    assert sortie.value.code == 0
    assert vp.__doc__.splitlines()[0] in general
    assert re.search(r"amorcer\s+charger les référentiels \(compte propriétaire\)$", general,
                     re.M)
    assert re.search(r"flux\s+insérer des lots de paiements \(compte applicatif\)$", general,
                     re.M)
    assert re.search(r"--pause PAUSE\s+secondes entre deux lots$", flux, re.M)


def test_la_commande_se_lance_par_python_m_et_refuse_une_commande_absente():
    """Le point d'entrée existe : lancée en tant que programme, la commande affiche son aide."""
    racine = Path(__file__).resolve().parent.parent
    environnement = {**os.environ, "PYTHONPATH": str(racine), "PYTHONIOENCODING": "utf-8"}

    aide = subprocess.run([sys.executable, "-m", "stripe_demo.vers_postgres", "--help"],
                          cwd=racine, capture_output=True, check=False, timeout=120,
                          env=environnement)
    sans_commande = subprocess.run([sys.executable, "-m", "stripe_demo.vers_postgres"],
                                   cwd=racine, capture_output=True, check=False, timeout=120,
                                   env=environnement)

    assert aide.returncode == 0
    assert "amorcer" in aide.stdout.decode("utf-8") and "flux" in aide.stdout.decode("utf-8")
    assert sans_commande.returncode == 2  # une commande est obligatoire
