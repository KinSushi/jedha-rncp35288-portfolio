# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Surveillance du retard de réplication : ce qui déclenche une alerte, et ce qui est refusé.

Pourquoi : un connecteur Debezium arrêté fait grossir le WAL retenu par PostgreSQL jusqu'à
remplir le disque. La commande doit alerter dans deux cas (emplacement sans lecteur, retard
au-delà du seuil), se taire sinon, et refuser un seuil absurde plutôt que de rassurer à tort.
Ces tests n'ont besoin d'aucune base : la lecture est remplacée par une liste écrite à la main ;
le test Docker optionnel (`test_postgres_docker.py`) exécute la vraie requête sur PostgreSQL 16.
"""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from stripe_demo import supervision
from stripe_demo.supervision import Emplacement

SEUIL = 1000
RACINE = Path(__file__).resolve().parent.parent
DOCUMENT_10 = (RACINE.parent / "livrables" / "aia2"
               / "10_continuite_supervision_maintenance.md")


def test_rien_a_signaler_sous_le_seuil_et_a_l_egalite():
    emplacements = [Emplacement("debezium", True, 999), Emplacement("autre", True, SEUIL)]
    assert supervision.evaluer(emplacements, SEUIL) == []


def test_alerte_au_dela_du_seuil_avec_le_retard_et_le_seuil():
    alertes = supervision.evaluer([Emplacement("debezium", True, SEUIL + 1)], SEUIL)
    assert alertes == ["debezium : retard de 1001 octets, au-delà du seuil de 1000"]


def test_alerte_pour_un_emplacement_sans_lecteur_meme_avec_un_retard_faible():
    alertes = supervision.evaluer([Emplacement("debezium", False, 10)], SEUIL)
    assert alertes == ["debezium : aucun lecteur (connecteur arrêté), WAL retenu : 10 octets"]


def test_un_retard_inconnu_n_alerte_que_si_l_emplacement_est_inactif():
    assert supervision.evaluer([Emplacement("physique", True, None)], SEUIL) == []
    assert supervision.evaluer([Emplacement("physique", False, None)], SEUIL) == [
        "physique : aucun lecteur (connecteur arrêté), WAL retenu : None octets"]


@pytest.mark.parametrize("seuil", [0, -1])
def test_un_seuil_nul_ou_negatif_est_refuse(seuil):
    with pytest.raises(ValueError) as erreur:
        supervision.evaluer([Emplacement("debezium", True, 5)], seuil)
    assert str(erreur.value) == "le seuil doit être un nombre d'octets strictement positif"


class _Resultat:
    def __init__(self, lignes):
        self._lignes = lignes

    def all(self):
        return self._lignes


class _Connexion:
    def __init__(self, journal, lignes):
        self.journal, self.lignes = journal, lignes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.journal.append("fermée")
        return False

    def execute(self, requete):
        self.journal.append(str(requete))
        return _Resultat(self.lignes)


class _Moteur:
    def __init__(self, lignes):
        self.journal: list[str] = []
        self.lignes = lignes
        self.libere = False

    def connect(self):
        return _Connexion(self.journal, self.lignes)

    def dispose(self):
        self.libere = True


def test_la_requete_du_document_est_executee_telle_quelle_et_les_lignes_converties():
    moteur = _Moteur([("debezium", 1, 2048.0), ("physique", 0, None)])
    emplacements = supervision.lire_emplacements(moteur)
    assert emplacements == [Emplacement("debezium", True, 2048),
                            Emplacement("physique", False, None)]
    assert isinstance(emplacements[0].retard_octets, int)
    assert moteur.journal == [supervision.REQUETE_RETARD, "fermée"]
    assert "pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)" in moteur.journal[0]
    assert "FROM pg_replication_slots" in moteur.journal[0]


def test_la_lecture_postgres_utilise_le_compte_demande_et_libere_le_moteur(monkeypatch):
    moteur = _Moteur([("debezium", True, 1)])
    demandes = []

    def faux_moteur(utilisateur, secret):
        demandes.append((utilisateur, secret))
        return moteur

    monkeypatch.setattr(supervision.vers_postgres, "moteur", faux_moteur)
    assert supervision.lire_depuis_postgres("lecteur", "SECRET_LECTEUR") == [
        Emplacement("debezium", True, 1)]
    assert demandes == [("lecteur", "SECRET_LECTEUR")]
    assert moteur.libere


def test_le_moteur_est_libere_meme_si_la_requete_echoue(monkeypatch):
    moteur = _Moteur([])

    def connexion_qui_echoue():
        raise RuntimeError("base injoignable")

    moteur.connect = connexion_qui_echoue
    monkeypatch.setattr(supervision.vers_postgres, "moteur", lambda utilisateur, secret: moteur)
    with pytest.raises(RuntimeError, match="base injoignable"):
        supervision.lire_depuis_postgres("lecteur", "SECRET")
    assert moteur.libere


def _lecteur(emplacements, appels):
    def lire(utilisateur, secret):
        appels.append((utilisateur, secret))
        return emplacements
    return lire


def test_commande_sans_alerte_rend_zero_et_affiche_chaque_emplacement(capsys, monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "proprietaire")
    appels: list[tuple[str, str]] = []
    code = supervision.main(["--seuil-octets", "1000"],
                            lecteur=_lecteur([Emplacement("debezium", True, 12)], appels))
    assert code == 0
    assert capsys.readouterr().out == "debezium : actif=True, retard_octets=12\n"
    assert appels == [("proprietaire", "POSTGRES_PASSWORD")]


def test_commande_avec_alerte_rend_un_et_affiche_l_alerte(capsys):
    appels: list[tuple[str, str]] = []
    code = supervision.main(
        ["--seuil-octets", "1000", "--utilisateur", "moniteur", "--secret", "MONITEUR"],
        lecteur=_lecteur([Emplacement("debezium", False, 5000)], appels))
    assert code == 1
    assert capsys.readouterr().out == (
        "debezium : actif=False, retard_octets=5000\n"
        "ALERTE debezium : aucun lecteur (connecteur arrêté), WAL retenu : 5000 octets\n")
    assert appels == [("moniteur", "MONITEUR")]


def test_commande_sans_emplacement_le_dit_et_rend_zero(capsys, monkeypatch):
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    appels: list[tuple[str, str]] = []
    assert supervision.main(["--seuil-octets", "1"], lecteur=_lecteur([], appels)) == 0
    assert capsys.readouterr().out == "aucun emplacement de réplication : rien à surveiller\n"
    assert appels == [("postgres", "POSTGRES_PASSWORD")]


@pytest.mark.parametrize("arguments, motif", [
    ([], "the following arguments are required: --seuil-octets"),
    (["--seuil-octets", "0"], "error: --seuil-octets doit être strictement positif"),
    (["--seuil-octets", "-5"], "error: --seuil-octets doit être strictement positif"),
    (["--seuil-octets", "beaucoup"], "invalid int value: 'beaucoup'"),
])
def test_commande_sans_seuil_valide_est_refusee_sans_lire_la_base(arguments, motif, capsys):
    appels: list[tuple[str, str]] = []
    with pytest.raises(SystemExit) as sortie:
        supervision.main(arguments, lecteur=_lecteur([], appels))
    assert sortie.value.code == 2
    assert appels == []  # la base n'est jamais interrogée avec un seuil absurde
    assert motif in capsys.readouterr().err


def test_l_aide_reprend_la_premiere_ligne_du_module(capsys):
    with pytest.raises(SystemExit) as sortie:
        supervision.main(["--help"])
    assert sortie.value.code == 0
    aide = " ".join(capsys.readouterr().out.split())  # sans les retours à la ligne d'argparse
    assert supervision.__doc__.splitlines()[0] in aide
    for texte in ("WAL retenu au-delà duquel alerter (hypothèse H17)",
                  "compte de lecture (en production : un compte membre de pg_monitor)",
                  "nom du secret du compte (<NOM>_FILE ou <NOM>)"):
        assert f" {texte} " in f" {aide} ", texte  # le texte entier, ni plus ni moins


def test_la_requete_est_celle_du_bloc_sql_du_document_10_au_caractere_pres():
    """Le document 10 dit que sa requête est « exécutable telle quelle » : c'est vérifié ici.

    Sans ce test, la constante pourrait diverger du document (une ligne retirée, un mot changé)
    sans qu'aucun test sans base ne le voie : les autres tests comparent la requête à elle-même.
    """
    texte = DOCUMENT_10.read_text(encoding="utf-8")
    blocs = re.findall(r"```sql\n(.*?)```", texte, flags=re.DOTALL)
    retard = [bloc for bloc in blocs if "pg_replication_slots" in bloc]
    # Un seul bloc décrit la requête, et il est identique à la constante (plus le « ; » final).
    assert retard == [supervision.REQUETE_RETARD + ";\n"]


def test_un_emplacement_lu_ne_peut_pas_etre_modifie_apres_coup():
    """Un `Emplacement` est une mesure : la modifier après lecture fausserait l'alerte."""
    emplacement = Emplacement("debezium", False, 5000)
    with pytest.raises(dataclasses.FrozenInstanceError):
        emplacement.actif = True
    assert emplacement == Emplacement("debezium", False, 5000)


def test_la_commande_python_m_est_un_vrai_point_d_entree_qui_refuse_l_absence_de_seuil():
    """Lancée comme programme, la commande exécute `main` : sans seuil, code 2 et aucune lecture.

    Sans point d'entrée, le processus se terminerait en silence avec le code 0, et un
    ordonnanceur le prendrait pour « rien à signaler ». Aucune base n'est nécessaire : argparse
    refuse la ligne de commande avant toute connexion.
    """
    resultat = subprocess.run(
        [sys.executable, "-m", "stripe_demo.supervision"], cwd=RACINE, capture_output=True,
        check=False, timeout=120, env={**os.environ, "PYTHONPATH": str(RACINE)})
    assert resultat.returncode == 2
    assert b"the following arguments are required: --seuil-octets" in resultat.stderr
    assert resultat.stdout == b""
