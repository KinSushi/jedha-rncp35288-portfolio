# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Téléchargement vérifié du jeu : chaque refus, chaque contrôle, rien d'abîmé (critères 3.3, 4.1).

Le client HTTP est remplacé par une doublure qui enregistre ses arguments exacts (flux, délais,
taille de bloc) : aucun test n'ouvre de connexion. Les tests inverses prouvent qu'un jeu altéré,
tronqué ou refusé par le serveur n'est jamais conservé et que le jeu déjà en place n'est ni
modifié ni exposé à moitié écrit.
"""

from __future__ import annotations

import hashlib
import os
import sys
import types
from dataclasses import replace
from typing import Any

import pytest
import requests

import detection_fraude.telechargement as telechargement_module
from detection_fraude.modele import empreinte_fichier
from detection_fraude.telechargement import main, telecharger_jeu

URL_JEU = "https://exemple.test/jeu.csv"
CONTENU = b"a;b\n1;2\n" * 50
SHA = hashlib.sha256(CONTENU).hexdigest()
ANCIEN = b"ANCIEN JEU DE DONNEES"
REFUS = "Téléchargement refusé : "
MESSAGE_EMPREINTE = "empreinte SHA-256 différente de celle attendue"
MESSAGE_RELECTURE = "le fichier relu depuis le disque diffère du flux reçu"


# --------------------------------------------------------------------------------------------
# Doublures
# --------------------------------------------------------------------------------------------
class FauxFlux:
    """Réponse HTTP en flux : garde l'ordre des appels et la taille de bloc demandée."""

    def __init__(
        self,
        blocs: list[bytes],
        erreur_statut: Exception | None = None,
        coupure: Exception | None = None,
        sonde: Any = None,
    ) -> None:
        self.blocs = blocs
        self.erreur_statut = erreur_statut
        self.coupure = coupure
        self.sonde = sonde
        self.journal: list[str] = []
        self.tailles: list[Any] = []

    def __enter__(self) -> FauxFlux:
        self.journal.append("entree")
        return self

    def __exit__(self, *details: Any) -> bool:
        self.journal.append("sortie")
        return False

    def raise_for_status(self) -> None:
        self.journal.append("statut")
        if self.erreur_statut is not None:
            raise self.erreur_statut

    def iter_content(self, chunk_size: Any):
        self.tailles.append(chunk_size)
        for bloc in self.blocs:
            if self.sonde is not None:
                self.sonde()
            yield bloc
        if self.coupure is not None:
            raise self.coupure


class FauxGet:
    def __init__(self, reponse: FauxFlux | None = None) -> None:
        self.reponse = reponse
        self.appels: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **options: Any) -> FauxFlux:
        self.appels.append((args, options))
        if self.reponse is None:
            raise AssertionError("aucun téléchargement n'aurait dû être tenté")
        return self.reponse


def configurer(config, tmp_path, *, taille=0, sha="", chemin=None):
    chemin = chemin or tmp_path / "donnees" / "jeu.csv"
    source = replace(
        config.source, jeu_chemin=chemin, jeu_taille_octets=taille, jeu_sha256=sha, jeu_url=URL_JEU
    )
    return replace(config, source=source)


def installer(monkeypatch, flux: FauxFlux | None) -> FauxGet:
    faux_get = FauxGet(flux)
    monkeypatch.setattr(requests, "get", faux_get)
    return faux_get


def espionner_relecture(monkeypatch) -> list[Any]:
    """Enregistre les chemins relus par ``empreinte_fichier`` tout en gardant le vrai calcul."""
    chemins: list[Any] = []

    def espion(chemin):
        chemins.append(chemin)
        return empreinte_fichier(chemin)

    monkeypatch.setattr(telechargement_module, "empreinte_fichier", espion)
    return chemins


def poser_ancien(chemin) -> bytes:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(ANCIEN)
    return chemin.read_bytes()


# --------------------------------------------------------------------------------------------
# Jeu déjà présent
# --------------------------------------------------------------------------------------------
def test_un_jeu_deja_present_n_est_pas_retelecharge_et_reste_inchange(
    config, tmp_path, monkeypatch, evenements
):
    """Sans ``--forcer``, aucun appel réseau : le fichier en place est renvoyé tel quel."""
    cfg = configurer(config, tmp_path, taille=len(CONTENU), sha=SHA)
    octets_avant = poser_ancien(cfg.source.jeu_chemin)
    faux_get = installer(monkeypatch, None)
    assert telecharger_jeu(cfg) == cfg.source.jeu_chemin
    assert faux_get.appels == []
    assert cfg.source.jeu_chemin.read_bytes() == octets_avant
    assert evenements.tous() == [
        (
            "INFO",
            "jeu_deja_present",
            {"chemin": str(cfg.source.jeu_chemin), "octets": len(ANCIEN)},
        )
    ]


# --------------------------------------------------------------------------------------------
# Téléchargement réussi
# --------------------------------------------------------------------------------------------
def test_le_jeu_en_place_reste_intact_pendant_le_flux_puis_est_remplace_d_un_bloc(
    config, tmp_path, monkeypatch, evenements
):
    """Écriture dans un fichier partiel : l'ancien jeu n'est jamais vu à moitié remplacé."""
    chemin = tmp_path / "donnees" / "profond" / "jeu.csv"
    cfg = configurer(config, tmp_path, taille=len(CONTENU), sha=SHA, chemin=chemin)
    octets_avant = poser_ancien(chemin)
    partiel = chemin.with_suffix(".partiel")
    partiel.write_bytes(b"DECHET" * 200)
    vus: list[tuple[bytes, int]] = []

    def sonde():
        vus.append((chemin.read_bytes(), partiel.stat().st_size))

    blocs = [CONTENU[:100], b"", CONTENU[100:250], CONTENU[250:]]
    flux = FauxFlux(blocs, sonde=sonde)
    faux_get = installer(monkeypatch, flux)
    assert telecharger_jeu(cfg, forcer=True) == chemin
    assert vus == [(octets_avant, 0)] * 4
    assert faux_get.appels == [((URL_JEU,), {"stream": True, "timeout": (10, 120)})]
    assert flux.journal == ["entree", "statut", "sortie"]
    assert flux.tailles == [1 << 20]
    assert chemin.read_bytes() == CONTENU
    assert not partiel.exists()
    assert evenements.tous() == [
        (
            "INFO",
            "jeu_telecharge",
            {"chemin": str(chemin), "octets": len(CONTENU), "sha256": SHA},
        )
    ]


def test_les_octets_sont_forces_sur_le_disque_avant_la_verification_et_le_renommage(
    config, tmp_path, monkeypatch
):
    """``fsync`` est appelé une fois, après le vidage du tampon et avant tout renommage."""
    chemin = tmp_path / "donnees" / "jeu.csv"
    cfg = configurer(config, tmp_path, taille=len(CONTENU), sha=SHA, chemin=chemin)
    octets_avant = poser_ancien(chemin)
    partiel = chemin.with_suffix(".partiel")
    appels: list[tuple[bool, bytes, bytes]] = []
    fsync = os.fsync

    def espion(descripteur):
        appels.append((isinstance(descripteur, int), partiel.read_bytes(), chemin.read_bytes()))
        return fsync(descripteur)

    monkeypatch.setattr(os, "fsync", espion)
    installer(monkeypatch, FauxFlux([CONTENU[:7], CONTENU[7:]]))
    telecharger_jeu(cfg, forcer=True)
    assert appels == [(True, CONTENU, octets_avant)]
    assert chemin.read_bytes() == CONTENU

    appels.clear()
    cfg_refus = configurer(config, tmp_path, taille=len(CONTENU) + 1, chemin=chemin)
    installer(monkeypatch, FauxFlux([CONTENU]))
    with pytest.raises(RuntimeError):
        telecharger_jeu(cfg_refus, forcer=True)
    assert appels == [(True, CONTENU, CONTENU)]


# --------------------------------------------------------------------------------------------
# Contrôles de taille et d'empreinte
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("taille", "sha", "servi", "accepte"),
    [
        (len(CONTENU), "", CONTENU, True),
        (0, SHA, CONTENU, True),
        (len(CONTENU), SHA, CONTENU, True),
        (0, "", CONTENU, True),
        (0, "", b"", True),
        (len(CONTENU) + 1, "", CONTENU, False),
        (len(CONTENU) - 1, "", CONTENU, False),
        (5, SHA, CONTENU, False),
        (0, "0" * 64, CONTENU, False),
        (len(CONTENU), "0" * 64, CONTENU, False),
        (0, SHA.upper(), CONTENU, False),
        (len(CONTENU), SHA, CONTENU + b"x", False),
    ],
)
def test_les_controles_sont_optionnels_a_zero_ou_vide_et_sinon_toute_difference_est_refusee(
    config, tmp_path, monkeypatch, taille, sha, servi, accepte
):
    """Taille 0 et empreinte vide = pas de contrôle ; une valeur attendue doit être exacte."""
    chemin = tmp_path / "donnees" / "jeu.csv"
    cfg = configurer(config, tmp_path, taille=taille, sha=sha, chemin=chemin)
    installer(monkeypatch, FauxFlux([servi]))
    if accepte:
        assert telecharger_jeu(cfg) == chemin
        assert chemin.read_bytes() == servi
    else:
        with pytest.raises(RuntimeError):
            telecharger_jeu(cfg)
        assert not chemin.exists()
    assert not chemin.with_suffix(".partiel").exists()


@pytest.mark.parametrize(("ecart", "servi"), [(-1, CONTENU), (1, CONTENU), (5, b""), (2, b"abc")])
def test_une_taille_differente_est_refusee_avec_les_deux_valeurs_et_le_jeu_reste_inchange(
    config, tmp_path, monkeypatch, evenements, ecart, servi
):
    """Message exact « taille N au lieu de M » ; fichier partiel supprimé ; jeu en place intact."""
    chemin = tmp_path / "donnees" / "jeu.csv"
    attendue = len(servi) + ecart
    cfg = configurer(config, tmp_path, taille=attendue, chemin=chemin)
    octets_avant = poser_ancien(chemin)
    relectures = espionner_relecture(monkeypatch)
    installer(monkeypatch, FauxFlux([servi]))
    with pytest.raises(RuntimeError) as exc:
        telecharger_jeu(cfg, forcer=True)
    assert str(exc.value) == f"{REFUS}taille {len(servi)} au lieu de {attendue}"
    assert chemin.read_bytes() == octets_avant
    assert not chemin.with_suffix(".partiel").exists()
    assert relectures == []
    assert evenements.tous() == []


def test_une_empreinte_differente_est_refusee_sans_relire_le_disque_et_le_jeu_reste_inchange(
    config, tmp_path, monkeypatch, evenements
):
    """Empreinte du flux différente : refus immédiat, sans relecture, jeu en place intact."""
    chemin = tmp_path / "donnees" / "jeu.csv"
    cfg = configurer(config, tmp_path, sha="0" * 64, chemin=chemin)
    octets_avant = poser_ancien(chemin)
    relectures = espionner_relecture(monkeypatch)
    installer(monkeypatch, FauxFlux([CONTENU]))
    with pytest.raises(RuntimeError) as exc:
        telecharger_jeu(cfg, forcer=True)
    assert str(exc.value) == f"{REFUS}{MESSAGE_EMPREINTE}"
    assert chemin.read_bytes() == octets_avant
    assert not chemin.with_suffix(".partiel").exists()
    assert relectures == []
    assert evenements.tous() == []


def test_taille_et_empreinte_fausses_sont_toutes_deux_citees_dans_cet_ordre(
    config, tmp_path, monkeypatch
):
    """Les écarts sont réunis par « ; » : la taille d'abord, puis l'empreinte."""
    chemin = tmp_path / "donnees" / "jeu.csv"
    cfg = configurer(config, tmp_path, taille=len(CONTENU) - 1, sha="f" * 64, chemin=chemin)
    relectures = espionner_relecture(monkeypatch)
    installer(monkeypatch, FauxFlux([CONTENU]))
    with pytest.raises(RuntimeError) as exc:
        telecharger_jeu(cfg)
    assert str(exc.value) == (
        f"{REFUS}taille {len(CONTENU)} au lieu de {len(CONTENU) - 1} ; {MESSAGE_EMPREINTE}"
    )
    assert not chemin.exists()
    assert not chemin.with_suffix(".partiel").exists()
    assert relectures == []


def test_une_taille_juste_avec_une_empreinte_fausse_ne_cite_que_l_empreinte(
    config, tmp_path, monkeypatch
):
    """Seul l'écart réel est cité : une taille correcte n'apparaît pas dans le message."""
    cfg = configurer(config, tmp_path, taille=len(CONTENU), sha="1" * 64)
    installer(monkeypatch, FauxFlux([CONTENU]))
    with pytest.raises(RuntimeError) as exc:
        telecharger_jeu(cfg)
    assert str(exc.value) == f"{REFUS}{MESSAGE_EMPREINTE}"


def test_la_relecture_du_disque_porte_sur_le_partiel_et_seulement_si_une_empreinte_est_attendue(
    config, tmp_path, monkeypatch
):
    """Seconde vérification : une fois, sur le fichier partiel, jamais sans empreinte attendue."""
    chemin = tmp_path / "donnees" / "jeu.csv"
    partiel = chemin.with_suffix(".partiel")
    relectures = espionner_relecture(monkeypatch)
    installer(monkeypatch, FauxFlux([CONTENU]))
    telecharger_jeu(configurer(config, tmp_path, taille=len(CONTENU), sha=SHA, chemin=chemin))
    assert relectures == [partiel]
    relectures.clear()
    installer(monkeypatch, FauxFlux([CONTENU]))
    telecharger_jeu(configurer(config, tmp_path, taille=len(CONTENU), chemin=chemin), forcer=True)
    assert relectures == []
    installer(monkeypatch, FauxFlux([CONTENU]))
    with pytest.raises(RuntimeError):
        telecharger_jeu(configurer(config, tmp_path, sha="0" * 64, chemin=chemin), forcer=True)
    assert relectures == []


def test_un_fichier_relu_different_du_flux_est_refuse_et_le_jeu_reste_inchange(
    config, tmp_path, monkeypatch, evenements
):
    """Le disque a pu corrompre l'écriture : la relecture divergente fait refuser le jeu."""
    chemin = tmp_path / "donnees" / "jeu.csv"
    cfg = configurer(config, tmp_path, taille=len(CONTENU), sha=SHA, chemin=chemin)
    octets_avant = poser_ancien(chemin)
    monkeypatch.setattr(telechargement_module, "empreinte_fichier", lambda chemin_relu: "0" * 64)
    installer(monkeypatch, FauxFlux([CONTENU]))
    with pytest.raises(RuntimeError) as exc:
        telecharger_jeu(cfg, forcer=True)
    assert str(exc.value) == f"{REFUS}{MESSAGE_RELECTURE}"
    assert chemin.read_bytes() == octets_avant
    assert not chemin.with_suffix(".partiel").exists()
    assert evenements.tous() == []


def test_un_partiel_deja_supprime_n_empeche_pas_le_refus_d_etre_signale(
    config, tmp_path, monkeypatch
):
    """Le nettoyage tolère l'absence du fichier partiel : l'erreur de refus reste la bonne."""
    chemin = tmp_path / "donnees" / "jeu.csv"
    cfg = configurer(config, tmp_path, sha=SHA, chemin=chemin)

    def supprimer_puis_diverger(chemin_relu):
        chemin_relu.unlink()
        return "0" * 64

    monkeypatch.setattr(telechargement_module, "empreinte_fichier", supprimer_puis_diverger)
    installer(monkeypatch, FauxFlux([CONTENU]))
    with pytest.raises(RuntimeError) as exc:
        telecharger_jeu(cfg)
    assert str(exc.value) == f"{REFUS}{MESSAGE_RELECTURE}"
    assert not chemin.exists()


# --------------------------------------------------------------------------------------------
# Pannes du serveur ou du réseau
# --------------------------------------------------------------------------------------------
def test_un_refus_du_serveur_est_propage_flux_ferme_et_le_jeu_en_place_reste_inchange(
    config, tmp_path, monkeypatch, evenements
):
    """Un 404 lève l'erreur d'origine avant toute écriture ; le dossier du jeu est prêt."""
    chemin = tmp_path / "donnees" / "profond" / "jeu.csv"
    cfg = configurer(config, tmp_path, taille=len(CONTENU), sha=SHA, chemin=chemin)
    octets_avant = poser_ancien(chemin)
    erreur = requests.HTTPError("404 Client Error: Not Found for url: " + URL_JEU)
    flux = FauxFlux([CONTENU], erreur_statut=erreur)
    installer(monkeypatch, flux)
    with pytest.raises(requests.HTTPError) as exc:
        telecharger_jeu(cfg, forcer=True)
    assert exc.value is erreur
    assert flux.journal == ["entree", "statut", "sortie"]
    assert flux.tailles == []
    assert chemin.read_bytes() == octets_avant
    assert not chemin.with_suffix(".partiel").exists()
    assert evenements.tous() == []


def test_un_refus_du_serveur_sans_jeu_en_place_cree_le_dossier_mais_aucun_fichier(
    config, tmp_path, monkeypatch
):
    """Le dossier de destination est créé d'abord ; sans réponse valide, aucun fichier."""
    chemin = tmp_path / "donnees" / "profond" / "jeu.csv"
    cfg = configurer(config, tmp_path, chemin=chemin)
    assert not chemin.parent.exists()
    installer(monkeypatch, FauxFlux([CONTENU], erreur_statut=requests.HTTPError("503")))
    with pytest.raises(requests.HTTPError):
        telecharger_jeu(cfg)
    assert chemin.parent.is_dir()
    assert list(chemin.parent.iterdir()) == []


def test_une_coupure_en_cours_de_flux_est_propagee_et_le_jeu_en_place_reste_inchange(
    config, tmp_path, monkeypatch, evenements
):
    """Connexion coupée à mi-chemin : l'erreur remonte, flux fermé, ancien jeu intact."""
    chemin = tmp_path / "donnees" / "jeu.csv"
    cfg = configurer(config, tmp_path, taille=len(CONTENU), sha=SHA, chemin=chemin)
    octets_avant = poser_ancien(chemin)
    coupure = requests.ConnectionError("connexion coupée")
    flux = FauxFlux([CONTENU[:50]], coupure=coupure)
    installer(monkeypatch, flux)
    with pytest.raises(requests.ConnectionError) as exc:
        telecharger_jeu(cfg, forcer=True)
    assert exc.value is coupure
    assert flux.journal == ["entree", "statut", "sortie"]
    assert chemin.read_bytes() == octets_avant
    assert evenements.tous() == []


# --------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------
@pytest.fixture
def espions(monkeypatch):
    """Remplace configuration, journal et téléchargement : on observe ce que main leur passe."""
    monkeypatch.setattr(sys, "argv", ["telechargement"])
    monkeypatch.setenv("COLUMNS", "200")
    configuration = types.SimpleNamespace(
        journal=types.SimpleNamespace(niveau="WARNING", console="json")
    )
    appels = types.SimpleNamespace(configuration=configuration, journal=[], telechargements=[])
    monkeypatch.setattr(telechargement_module, "charger_configuration", lambda: configuration)
    monkeypatch.setattr(
        telechargement_module,
        "configurer_journal",
        lambda *args, **options: appels.journal.append((args, options)),
    )
    monkeypatch.setattr(
        telechargement_module,
        "telecharger_jeu",
        lambda *args, **options: appels.telechargements.append((args, options)),
    )
    return appels


@pytest.mark.parametrize(
    ("argv", "forcer", "refuse"),
    [([], False, False), (["--forcer"], True, False), (["--forcer"], True, True)],
)
def test_main_telecharge_avec_ou_sans_forcer_et_laisse_remonter_un_refus_au_lanceur(
    espions, monkeypatch, argv, forcer, refuse
):
    """Journal (niveau, console) sans fichier, puis le téléchargement ; un refus n'est pas un 0."""
    refus = RuntimeError(REFUS + "taille 1 au lieu de 2")

    def telechargement_refuse(*args, **options):
        espions.telechargements.append((args, options))
        raise refus

    if refuse:
        monkeypatch.setattr(telechargement_module, "telecharger_jeu", telechargement_refuse)
        with pytest.raises(RuntimeError) as exc:
            main(argv)
        assert exc.value is refus
    else:
        code_sortie = main(argv)
        assert code_sortie == 0
    assert espions.journal == [(("WARNING", "json"), {})]
    assert espions.telechargements == [((espions.configuration,), {"forcer": forcer})]


def test_l_aide_de_main_decrit_forcer_et_sort_avec_le_code_0_sans_rien_telecharger(
    espions, capsys
):
    """``--help`` affiche la description et l'option, puis quitte sans toucher à rien."""
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    sortie = capsys.readouterr().out
    assert "Télécharge le jeu public fraudTest.csv." in sortie
    assert "[--forcer]" in sortie
    assert "retélécharge même si présent" in sortie
    assert espions.journal == []
    assert espions.telechargements == []


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--autre"], "unrecognized arguments: --autre"),
        (["forcer"], "unrecognized arguments: forcer"),
        (["--forcer=1"], "argument --forcer: ignored explicit argument '1'"),
    ],
)
def test_une_option_inconnue_est_refusee_avec_le_code_2_et_rien_n_est_telecharge(
    espions, capsys, argv, message
):
    """Une faute de frappe ne doit pas lancer un téléchargement de 150 Mo."""
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert capsys.readouterr().err.strip().splitlines()[-1] == f"telechargement: error: {message}"
    assert espions.journal == []
    assert espions.telechargements == []


def test_main_avec_la_vraie_configuration_refuse_un_faux_jeu_et_ne_garde_aucun_fichier(
    config, monkeypatch, evenements
):
    """Taille et empreinte attendues viennent de la configuration : un faux contenu est refusé."""
    journaux: list[Any] = []
    monkeypatch.setattr(
        telechargement_module, "configurer_journal", lambda *args: journaux.append(args)
    )
    flux = FauxFlux([b"faux contenu"])
    faux_get = installer(monkeypatch, flux)
    assert not config.source.jeu_chemin.exists()
    with pytest.raises(RuntimeError) as exc:
        main([])
    assert str(exc.value) == (
        f"{REFUS}taille {len(b'faux contenu')} au lieu de {config.source.jeu_taille_octets} ; "
        f"{MESSAGE_EMPREINTE}"
    )
    assert faux_get.appels == [((config.source.jeu_url,), {"stream": True, "timeout": (10, 120)})]
    assert journaux == [(config.journal.niveau, config.journal.console)]
    assert not config.source.jeu_chemin.exists()
    assert not config.source.jeu_chemin.with_suffix(".partiel").exists()
    assert evenements.tous() == []
