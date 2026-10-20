# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Garanties du modèle exporté (critères 2.3 et 5.2) : un refus, un message exact, un état intact.

Ce que chaque test prouve, au-delà du « ça marche » :

* une entrée interdite est refusée avec le **message exact** attendu par l'exploitation ;
* après le refus, rien n'est abîmé : le dossier des modèles, le pointeur ``ACTUEL`` et le modèle
  en service sont comparés octet par octet à l'instantané pris ``avant`` ;
* un artefact ``pickle`` n'est jamais exécuté avant la vérification de son empreinte SHA-256
  (un faux pipeline « piégé » laisse une trace s'il est déserialisé) ;
* les valeurs transmises aux dépendances (``gzip``, ``pickle``, ``os.replace``) sont enregistrées
  puis comparées exactement.

Les modèles sont minuscules (``DummyClassifier``) : aucun entraînement, aucun réseau.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import pickle
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier

from detection_fraude import modele
from detection_fraude.fonctionnalites import COLONNES_MODELE
from detection_fraude.modele import (
    ChargeurModele,
    ErreurModele,
    ModeleFraude,
    _ecrire_atomiquement,
    charger_modele,
    ecrire_artefact,
    empreinte_fichier,
    exporter_modele,
    lire_artefact,
    lire_metadonnees,
    nouvelle_version,
    promouvoir,
    version_en_service,
)

COLONNES_ATTENDUES = (
    "montant, age, distance_km, population_ville, heure, jour_semaine, "
    "nb_tx_24h, montant_relatif, categorie, etat"
)
NOM_MODELE = "modele.pkl.gz"
NOM_METADONNEES = "metadonnees.json"


# --------------------------------------------------------------------------------------------
# Outils des tests
# --------------------------------------------------------------------------------------------
CHARGEMENTS: list[str] = []


def signaler_chargement() -> str:
    """Appelée par ``pickle`` quand un ``PipelinePiege`` est déserialisé : laisse une trace."""
    CHARGEMENTS.append("charge")
    return "piege"


class PipelinePiege:
    """Pipeline dont la désérialisation s'exécute : prouve si un artefact a été chargé."""

    def __reduce__(self) -> tuple[Any, tuple[()]]:
        return (signaler_chargement, ())


class NonSerialisable:
    """Objet que ``pickle`` ne peut pas écrire (l'écriture lève toujours la même erreur)."""

    def __reduce__(self) -> tuple[Any, ...]:
        raise RuntimeError("non sérialisable (test)")


class PipelineEspion:
    """Pipeline factice : renvoie des probabilités fixes et garde les tableaux reçus."""

    def __init__(self, probabilites: list[list[float]]) -> None:
        self.probabilites = np.asarray(probabilites, dtype=float)
        self.recus: list[pd.DataFrame] = []

    def predict_proba(self, tableau: pd.DataFrame) -> np.ndarray:
        self.recus.append(tableau.copy())
        return self.probabilites


@pytest.fixture
def chargements() -> list[str]:
    CHARGEMENTS.clear()
    return CHARGEMENTS


def tableau_complet(nb_lignes: int = 2) -> pd.DataFrame:
    """Un tableau de caractéristiques valide, dans l'ordre du modèle."""
    valeurs: dict[str, list[Any]] = {
        c: [float(i + 1) for i in range(nb_lignes)]
        for c in COLONNES_MODELE
        if c not in ("categorie", "etat")
    }
    valeurs["categorie"] = ["grocery_pos"] * nb_lignes
    valeurs["etat"] = ["NY"] * nb_lignes
    return pd.DataFrame(valeurs)[list(COLONNES_MODELE)]


def pipeline_a_taux(positifs: int, total: int = 4) -> DummyClassifier:
    """Modèle minuscule dont la probabilité de fraude est toujours ``positifs / total``."""
    etiquettes = [1] * positifs + [0] * (total - positifs)
    return DummyClassifier(strategy="prior").fit(np.zeros((total, 1)), etiquettes)


def publier(
    dossier: Path, version: str, positifs: int = 1, seuil: float = 0.5, promu: bool = False
) -> Path:
    """Exporte (et au besoin promeut) un modèle minuscule ; renvoie le dossier de la version."""
    chemin = exporter_modele(dossier, pipeline_a_taux(positifs), seuil, {"version": version})
    if promu:
        promouvoir(dossier, version)
    return chemin


def instantane(racine: Path) -> dict[str, bytes | None]:
    """Chemin relatif -> octets (``None`` pour un dossier) de tout ce que contient ``racine``."""
    return {
        chemin.relative_to(racine).as_posix(): chemin.read_bytes() if chemin.is_file() else None
        for chemin in sorted(racine.rglob("*"))
    }


class FluxPlafonne:
    """Flux de lecture qui note la taille de chaque lecture et s'arrête au bout de 50 lectures.

    Chien de garde : une boucle de lecture qui ne se terminerait jamais échoue au lieu de figer.
    """

    def __init__(self, flux: Any, tailles: list[int]) -> None:
        self.flux = flux
        self.tailles = tailles

    def __enter__(self) -> FluxPlafonne:
        return self

    def __exit__(self, *details: object) -> None:
        self.flux.close()

    def read(self, taille: int) -> bytes:
        self.tailles.append(taille)
        if len(self.tailles) > 50:
            raise AssertionError("lecture sans fin : la boucle de hachage ne s'arrête pas")
        return self.flux.read(taille)


class CheminPlafonne:
    """Chemin factice : ``empreinte_fichier`` n'a besoin que de ``open(mode)``."""

    def __init__(self, ouvrir: Any) -> None:
        self.ouvrir = ouvrir
        self.modes: list[str] = []
        self.tailles: list[int] = []

    def open(self, mode: str) -> FluxPlafonne:
        self.modes.append(mode)
        return FluxPlafonne(self.ouvrir(mode), self.tailles)


def espionner_chargements(monkeypatch: pytest.MonkeyPatch, pannes: dict[str, BaseException]):
    """Remplace ``charger_modele`` par une doublure qui note ses arguments et peut échouer."""
    appels: list[tuple[Path, str | None]] = []
    original = modele.charger_modele

    def espion(dossier: Path, version: str | None = None):
        appels.append((dossier, version))
        if version in pannes:
            raise pannes[version]
        return original(dossier, version)

    monkeypatch.setattr(modele, "charger_modele", espion)
    return appels


# --------------------------------------------------------------------------------------------
# ModeleFraude.noter et decider
# --------------------------------------------------------------------------------------------
def test_noter_refuse_une_caracteristique_manquante_avec_le_message_exact():
    """Une colonne absente stoppe le scoring : le pipeline ne devine jamais une valeur."""
    espion = PipelineEspion([[0.9, 0.1], [0.2, 0.8]])
    entree = tableau_complet().drop(columns=["age"])
    entree_avant = entree.copy()
    with pytest.raises(ErreurModele) as exc:
        ModeleFraude(espion, 0.5, "v-1").noter(entree)
    assert str(exc.value) == "Caractéristiques manquantes : age"
    pd.testing.assert_frame_equal(entree, entree_avant)


def test_noter_liste_les_manquantes_dans_l_ordre_du_modele_et_non_celui_du_tableau():
    """Le message est stable : il suit l'ordre de ``COLONNES_MODELE``, séparé par « , »."""
    entree = tableau_complet()[list(reversed(COLONNES_MODELE))]
    entree = entree.drop(columns=["etat", "montant", "heure"])
    with pytest.raises(ErreurModele) as exc:
        ModeleFraude(PipelineEspion([[0.5, 0.5]] * 2), 0.5, "v-1").noter(entree)
    assert str(exc.value) == "Caractéristiques manquantes : montant, heure, etat"


def test_noter_refuse_un_tableau_sans_aucune_colonne_attendue():
    """Un tableau étranger au modèle liste les dix caractéristiques manquantes, sans exception."""
    with pytest.raises(ErreurModele) as exc:
        ModeleFraude(PipelineEspion([[0.5, 0.5]]), 0.5, "v-1").noter(pd.DataFrame({"autre": [1]}))
    assert str(exc.value) == f"Caractéristiques manquantes : {COLONNES_ATTENDUES}"


def test_noter_refuse_un_nom_de_colonne_qui_differe_par_la_casse():
    """Les noms de caractéristiques sont exacts : « Montant » n'est pas « montant »."""
    entree = tableau_complet().rename(columns={"montant": "Montant"})
    with pytest.raises(ErreurModele) as exc:
        ModeleFraude(PipelineEspion([[0.5, 0.5]] * 2), 0.5, "v-1").noter(entree)
    assert str(exc.value) == "Caractéristiques manquantes : montant"


def test_noter_n_appelle_jamais_le_pipeline_quand_une_colonne_manque():
    """Aucun score ne doit sortir d'un tableau incomplet : le pipeline n'est même pas appelé."""
    espion = PipelineEspion([[0.1, 0.9]])
    with pytest.raises(ErreurModele):
        ModeleFraude(espion, 0.5, "v-1").noter(tableau_complet(1).drop(columns=["etat"]))
    assert espion.recus == []


def test_noter_reordonne_les_colonnes_ecarte_les_intruses_et_laisse_le_tableau_intact():
    """Le pipeline reçoit exactement les dix colonnes dans l'ordre d'entraînement ; l'appelant
    récupère son tableau intact (colonnes en trop et ordre d'origine compris)."""
    espion = PipelineEspion([[0.9, 0.1], [0.2, 0.8]])
    entree = tableau_complet()[list(reversed(COLONNES_MODELE))]
    entree["intrus"] = ["a", "b"]
    entree_avant = entree.copy()
    scores = ModeleFraude(espion, 0.5, "v-1").noter(entree)
    assert isinstance(scores, np.ndarray)
    assert scores.tolist() == [0.1, 0.8]  # colonne 1 : probabilité de la classe « fraude »
    recu = espion.recus[0]
    assert recu.columns.tolist() == list(COLONNES_MODELE)
    pd.testing.assert_frame_equal(recu, entree[list(COLONNES_MODELE)])
    pd.testing.assert_frame_equal(entree, entree_avant)


def test_les_metadonnees_par_defaut_ne_sont_pas_partagees_et_l_autre_modele_reste_intact():
    """Deux modèles sans métadonnées ont chacun leur dictionnaire : écrire dans l'un laisse
    l'autre intact."""
    premier = ModeleFraude(None, 0.5, "v-1")
    second = ModeleFraude(None, 0.5, "v-2")
    assert premier.metadonnees == {}
    premier.metadonnees["cle"] = "valeur"
    assert second.metadonnees == {}


@pytest.mark.parametrize(
    ("seuil", "scores", "attendu"),
    [
        (0.5, [0.4999, 0.5, 0.5001], [0, 1, 1]),
        (0.25, [0.0, 0.2499, 0.25, 1.0], [0, 0, 1, 1]),
        (0.0, [0.0, 0.1], [1, 1]),
        (1.0, [0.9999, 1.0], [0, 1]),
    ],
)
def test_decider_ne_modifie_pas_les_scores_et_applique_un_seuil_inclusif(seuil, scores, attendu):
    """Un score égal au seuil déclenche l'alerte ; les scores consignés restent inchangés."""
    tableau = np.array(scores)
    scores_avant = tableau.copy()
    decisions = ModeleFraude(None, seuil, "v-1").decider(tableau)
    assert decisions.tolist() == attendu
    assert decisions.dtype.kind == "i"
    assert np.array_equal(tableau, scores_avant)
    assert ModeleFraude(None, seuil, "v-1").decider(scores).tolist() == attendu  # liste acceptée


# --------------------------------------------------------------------------------------------
# empreinte_fichier et nouvelle_version
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("contenu", "attendu", "lectures"),
    [
        pytest.param(
            b"",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            1,
            id="fichier_vide",
        ),
        pytest.param(
            b"abc",
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            2,
            id="abc",
        ),
        pytest.param(
            b"a" * 1_000_000,
            "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0",
            2,
            id="un_million_de_a",
        ),
    ],
)
def test_empreinte_fichier_donne_le_sha256_connu_et_laisse_le_fichier_intact(
    tmp_path, contenu, attendu, lectures
):
    """Le SHA-256 est celui de la référence publique ; le fichier mesuré reste intact."""
    fichier = tmp_path / "artefact.bin"
    fichier.write_bytes(contenu)
    avant = instantane(tmp_path)
    surveille = CheminPlafonne(fichier.open)
    assert empreinte_fichier(surveille) == attendu
    assert empreinte_fichier(fichier) == attendu  # même résultat sur un vrai chemin
    assert surveille.modes == ["rb"]
    assert surveille.tailles == [1_048_576] * lectures
    assert instantane(tmp_path) == avant


def test_empreinte_fichier_lit_par_blocs_de_1_mio_et_n_ouvre_jamais_en_ecriture():
    """Le fichier n'est ouvert qu'en lecture binaire (donc intact), bloc par bloc de 1 Mio."""
    octets = bytes(range(256)) * 12_000  # 3 072 000 octets : trois blocs de 1 Mio moins un reste
    surveille = CheminPlafonne(lambda mode: io.BytesIO(octets))
    assert empreinte_fichier(surveille) == hashlib.sha256(octets).hexdigest()
    assert surveille.modes == ["rb"]
    assert surveille.tailles == [1_048_576] * 4  # trois lectures de données + la lecture vide


def test_empreinte_d_un_fichier_absent_leve_file_not_found_et_ne_cree_rien(tmp_path):
    """Un artefact disparu est une erreur franche, jamais l'empreinte du vide."""
    avant = instantane(tmp_path)
    with pytest.raises(FileNotFoundError):
        empreinte_fichier(tmp_path / "absent.pkl.gz")
    assert instantane(tmp_path) == avant


def test_empreinte_d_un_dossier_est_refusee(tmp_path):
    """Un dossier ne se hache pas : l'appelant doit recevoir une erreur du système de fichiers."""
    avant = instantane(tmp_path)
    with pytest.raises(OSError):
        empreinte_fichier(tmp_path)
    assert instantane(tmp_path) == avant


def test_nouvelle_version_est_la_date_utc_au_format_triable(monkeypatch):
    """Les versions se trient dans l'ordre chronologique ; l'heure est lue en UTC."""
    assert re.fullmatch(r"\d{8}T\d{6}Z", nouvelle_version())  # AAAAMMJJTHHMMSSZ
    fuseaux: list[Any] = []
    instants = [
        datetime(2026, 1, 9, 23, 59, 59, 999999),
        datetime(2026, 10, 1, 0, 0, 0, 1),
    ]

    class HorlogeFixe(datetime):
        @classmethod
        def now(cls, tz=None):
            fuseaux.append(tz)
            return instants[len(fuseaux) - 1].replace(tzinfo=tz)

    monkeypatch.setattr(modele, "datetime", HorlogeFixe)
    premiere, seconde = nouvelle_version(), nouvelle_version()
    assert premiere == "20260109T235959Z"
    assert seconde == "20261001T000000Z"
    assert premiere < seconde
    assert fuseaux == [UTC, UTC]


# --------------------------------------------------------------------------------------------
# _ecrire_atomiquement
# --------------------------------------------------------------------------------------------
def test_ecriture_atomique_garde_l_ancien_contenu_intact_si_le_remplacement_echoue(
    tmp_path, monkeypatch
):
    """Un disque plein au moment du renommage ne doit jamais abîmer le pointeur existant."""
    cible = tmp_path / "ACTUEL"
    cible.write_text("v-ancienne\n", encoding="utf-8")
    contenu_avant = cible.read_bytes()

    def echouer(source, destination):
        raise OSError("disque plein (test)")

    monkeypatch.setattr(modele.os, "replace", echouer)
    with pytest.raises(OSError) as exc:
        _ecrire_atomiquement(cible, "v-nouvelle\n")
    assert str(exc.value) == "disque plein (test)"
    assert cible.read_bytes() == contenu_avant
    assert [p.name for p in tmp_path.iterdir()] == ["ACTUEL"]  # le temporaire a disparu


def test_ecriture_atomique_nettoie_aussi_apres_une_interruption_clavier(tmp_path, monkeypatch):
    """Ctrl-C pendant l'écriture (BaseException) ne laisse ni temporaire ni demi-fichier."""
    cible = tmp_path / "ACTUEL"
    cible.write_text("v-ancienne\n", encoding="utf-8")
    contenu_avant = cible.read_bytes()

    def interrompre(source, destination):
        raise KeyboardInterrupt

    monkeypatch.setattr(modele.os, "replace", interrompre)
    with pytest.raises(KeyboardInterrupt):
        _ecrire_atomiquement(cible, "v-nouvelle\n")
    assert cible.read_bytes() == contenu_avant
    assert [p.name for p in tmp_path.iterdir()] == ["ACTUEL"]


def test_ecriture_atomique_ne_masque_pas_l_erreur_d_origine_si_le_temporaire_a_disparu(
    tmp_path, monkeypatch
):
    """Le nettoyage tolère un temporaire déjà supprimé : l'appelant voit l'erreur d'origine."""
    cible = tmp_path / "ACTUEL"

    def supprimer_puis_echouer(source, destination):
        Path(source).unlink()
        raise OSError("verrou (test)")

    monkeypatch.setattr(modele.os, "replace", supprimer_puis_echouer)
    with pytest.raises(OSError) as exc:
        _ecrire_atomiquement(cible, "v-1\n")
    assert str(exc.value) == "verrou (test)"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("existait", [False, True])
def test_ecriture_atomique_d_un_contenu_non_textuel_laisse_le_dossier_intact(tmp_path, existait):
    """Un contenu invalide échoue pendant l'écriture : ni cible créée, ni cible abîmée."""
    if existait:
        (tmp_path / "ACTUEL").write_text("v-ancienne\n", encoding="utf-8")
    avant = instantane(tmp_path)
    with pytest.raises(TypeError):
        _ecrire_atomiquement(tmp_path / "ACTUEL", 12345)  # type: ignore[arg-type]
    assert instantane(tmp_path) == avant


def test_ecriture_atomique_cree_les_dossiers_ecrit_en_utf8_et_laisse_les_voisins_intacts(
    tmp_path, monkeypatch
):
    """Écrit à côté de la cible puis renommé (UTF-8) ; les fichiers voisins restent intacts."""
    voisin = tmp_path / "a" / "voisin.txt"
    voisin.parent.mkdir()
    voisin.write_bytes(b"ne pas toucher")
    voisin_avant = voisin.read_bytes()
    cible = tmp_path / "a" / "b" / "c" / "ACTUEL"
    renommages: list[tuple[Path, Path]] = []
    vrai_remplacer = modele.os.replace

    def espion(source, destination):
        renommages.append((Path(source), Path(destination)))
        vrai_remplacer(source, destination)

    monkeypatch.setattr(modele.os, "replace", espion)
    _ecrire_atomiquement(cible, "café — été")  # dossiers b/ et c/ créés
    assert cible.read_bytes() == "café — été".encode()
    _ecrire_atomiquement(cible, "second")  # le dossier existe déjà : aucune erreur
    assert cible.read_bytes() == b"second"
    assert [(s.parent, s.name.startswith(".tmp_"), d) for s, d in renommages] == [
        (cible.parent, True, cible),
        (cible.parent, True, cible),
    ]
    assert sorted(p.name for p in cible.parent.iterdir()) == ["ACTUEL"]
    assert voisin.read_bytes() == voisin_avant


# --------------------------------------------------------------------------------------------
# ecrire_artefact et lire_artefact
# --------------------------------------------------------------------------------------------
def test_artefact_ecrit_en_gzip_niveau_3_pickle_protocole_maximal_sans_alterer_le_pipeline(
    tmp_path, monkeypatch
):
    """L'artefact relu donne les mêmes probabilités ; écrire ne modifie pas le pipeline source."""
    appels_gzip: list[tuple[str, str, dict[str, Any]]] = []
    appels_pickle: list[Any] = []
    vrai_gzip, vrai_dump = gzip.open, pickle.dump

    def espion_gzip(fichier, mode="rb", **options):
        appels_gzip.append((Path(fichier).name, mode, options))
        return vrai_gzip(fichier, mode, **options)

    def espion_dump(objet, flux, protocol=None, **options):
        appels_pickle.append(protocol)
        return vrai_dump(objet, flux, protocol=protocol, **options)

    monkeypatch.setattr(modele.gzip, "open", espion_gzip)
    monkeypatch.setattr(modele.pickle, "dump", espion_dump)
    pipeline = pipeline_a_taux(3)
    prediction_avant = pipeline.predict_proba(np.zeros((1, 1))).tolist()
    fichier = tmp_path / NOM_MODELE
    ecrire_artefact(pipeline, fichier)
    relu = lire_artefact(fichier)
    assert appels_gzip == [(NOM_MODELE, "wb", {"compresslevel": 3}), (NOM_MODELE, "rb", {})]
    assert appels_pickle == [pickle.HIGHEST_PROTOCOL]
    brut = gzip.decompress(fichier.read_bytes())
    assert brut[:2] == b"\x80" + bytes([pickle.HIGHEST_PROTOCOL])
    assert relu.predict_proba(np.zeros((1, 1))).tolist() == [[0.25, 0.75]]
    assert pipeline.predict_proba(np.zeros((1, 1))).tolist() == prediction_avant


def test_lire_artefact_refuse_un_fichier_qui_n_est_pas_du_gzip(tmp_path):
    """Un fichier arbitraire déposé à la place du modèle est refusé et reste tel quel."""
    fichier = tmp_path / NOM_MODELE
    fichier.write_bytes(b"pas du gzip")
    avant = instantane(tmp_path)
    with pytest.raises(gzip.BadGzipFile):
        lire_artefact(fichier)
    assert instantane(tmp_path) == avant


def test_lire_artefact_refuse_un_fichier_tronque(tmp_path):
    """Un transfert interrompu (fichier coupé) lève une erreur de fin de flux, pas un modèle."""
    fichier = tmp_path / NOM_MODELE
    ecrire_artefact(pipeline_a_taux(1), fichier)
    fichier.write_bytes(fichier.read_bytes()[:25])
    avant = instantane(tmp_path)
    with pytest.raises(EOFError):
        lire_artefact(fichier)
    assert instantane(tmp_path) == avant


def test_lire_artefact_refuse_un_contenu_gzip_qui_n_est_pas_un_pickle(tmp_path):
    """Un gzip valide mais sans pickle à l'intérieur est refusé par le désérialiseur."""
    fichier = tmp_path / NOM_MODELE
    fichier.write_bytes(gzip.compress(b"?ceci n'est pas un pickle"))
    avant = instantane(tmp_path)
    with pytest.raises(pickle.UnpicklingError):
        lire_artefact(fichier)
    assert instantane(tmp_path) == avant


def test_lire_artefact_d_un_fichier_absent_leve_file_not_found_sans_rien_creer(tmp_path):
    """Un artefact absent est une erreur franche ; la lecture ne crée pas de fichier vide."""
    avant = instantane(tmp_path)
    with pytest.raises(FileNotFoundError):
        lire_artefact(tmp_path / NOM_MODELE)
    assert instantane(tmp_path) == avant


def test_ecrire_artefact_non_serialisable_propage_l_erreur_et_laisse_les_voisins_intacts(tmp_path):
    """Un pipeline non sérialisable est signalé tel quel ; les autres fichiers restent intacts."""
    voisin = tmp_path / "autre_version.pkl.gz"
    ecrire_artefact(pipeline_a_taux(1), voisin)
    voisin_avant = voisin.read_bytes()
    with pytest.raises(RuntimeError) as exc:
        ecrire_artefact(NonSerialisable(), tmp_path / NOM_MODELE)
    assert str(exc.value) == "non sérialisable (test)"
    assert voisin.read_bytes() == voisin_avant


# --------------------------------------------------------------------------------------------
# exporter_modele
# --------------------------------------------------------------------------------------------
def test_exporter_modele_ecrit_l_arborescence_et_des_metadonnees_exactes_sans_toucher_aux_siennes(
    tmp_path,
):
    """Le contrat d'échange est exact au caractère près ; le dictionnaire de l'appelant reste
    intact (l'export travaille sur une copie)."""
    dossier = tmp_path / "modeles" / "profond"  # n'existe pas encore : créé avec ses parents
    metadonnees = {
        "version": "v-1",
        "modele": "régression",
        "cree_le": datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        "chemin": Path("donnees") / "jeu.csv",
    }
    metadonnees_avant = copy.deepcopy(metadonnees)
    cible = exporter_modele(dossier, pipeline_a_taux(1), 0.4, metadonnees)
    assert cible == dossier / "v-1"
    assert sorted(p.name for p in cible.iterdir()) == [NOM_METADONNEES, NOM_MODELE]
    sha = hashlib.sha256((cible / NOM_MODELE).read_bytes()).hexdigest()
    texte = (cible / NOM_METADONNEES).read_text(encoding="utf-8")
    chemin_json = json.dumps(str(Path("donnees") / "jeu.csv"))
    assert texte == (
        '{\n  "version": "v-1",\n  "modele": "régression",\n'
        '  "cree_le": "2026-10-11 12:00:00+00:00",\n'
        f'  "chemin": {chemin_json},\n  "seuil": 0.4,\n  "sha256_modele": "{sha}"\n}}'
    )
    octets = (cible / NOM_METADONNEES).read_bytes()
    assert "régression".encode() in octets  # ensure_ascii=False : accents écrits tels quels
    assert b"\\u00e9" not in octets
    relues = lire_metadonnees(dossier, "v-1")
    assert relues["sha256_modele"] == sha
    assert relues["modele"] == "régression"  # relu en UTF-8, sans mojibake
    assert metadonnees == metadonnees_avant
    assert version_en_service(dossier) is None  # l'export ne promeut jamais


def test_exporter_modele_ignore_une_empreinte_ou_un_seuil_fournis_dans_les_metadonnees(tmp_path):
    """Une empreinte forgée dans les métadonnées d'entrée ne passe pas : celle du fichier prime,
    et un artefact remplacé après l'export reste refusé au chargement."""
    forgees = {"version": "v-1", "seuil": 0.99, "sha256_modele": "0" * 64}
    exporter_modele(tmp_path, pipeline_a_taux(1), 0.4, forgees)
    meta = lire_metadonnees(tmp_path, "v-1")
    assert meta["seuil"] == 0.4
    assert meta["sha256_modele"] == empreinte_fichier(tmp_path / "v-1" / NOM_MODELE)
    assert charger_modele(tmp_path, "v-1").seuil == 0.4
    fichier = tmp_path / "v-1" / NOM_MODELE
    fichier.write_bytes(fichier.read_bytes() + b"x")
    with pytest.raises(ErreurModele):
        charger_modele(tmp_path, "v-1")


def test_exporter_modele_sans_version_est_refuse_sans_rien_creer(tmp_path):
    """Des métadonnées sans ``version`` n'ont pas de dossier cible : rien ne doit être écrit."""
    publier(tmp_path, "v-0", promu=True)
    avant = instantane(tmp_path)
    with pytest.raises(KeyError) as exc:
        exporter_modele(tmp_path, pipeline_a_taux(1), 0.5, {"modele": "sans version"})
    assert str(exc.value) == "'version'"
    assert instantane(tmp_path) == avant


def test_exporter_modele_laisse_le_pointeur_et_les_autres_versions_intacts(tmp_path):
    """Exporter (même deux fois la même version) ne change ni le modèle en service ni les
    versions voisines, et remplace bien le contenu de la version réexportée."""
    publier(tmp_path, "v-1", positifs=1, promu=True)
    pointeur_avant = (tmp_path / "ACTUEL").read_bytes()
    version_1_avant = instantane(tmp_path / "v-1")
    publier(tmp_path, "v-2", positifs=2, seuil=0.3)
    exporter_modele(tmp_path, pipeline_a_taux(3), 0.7, {"version": "v-2"})  # même version, rejouée
    assert (tmp_path / "ACTUEL").read_bytes() == pointeur_avant
    assert instantane(tmp_path / "v-1") == version_1_avant
    rejouee = charger_modele(tmp_path, "v-2")
    assert rejouee.seuil == 0.7
    assert rejouee.pipeline.predict_proba(np.zeros((1, 1))).tolist() == [[0.25, 0.75]]


def test_exporter_un_pipeline_non_serialisable_laisse_le_service_et_les_versions_intacts(tmp_path):
    """Un échec d'export ne publie aucune métadonnée (donc rien de chargeable) et ne touche pas
    au modèle en service."""
    publier(tmp_path, "v-1", promu=True)
    avant = instantane(tmp_path / "v-1")
    pointeur_avant = (tmp_path / "ACTUEL").read_bytes()
    with pytest.raises(RuntimeError) as exc:
        exporter_modele(tmp_path, NonSerialisable(), 0.5, {"version": "v-2"})
    assert str(exc.value) == "non sérialisable (test)"
    assert not (tmp_path / "v-2" / NOM_METADONNEES).exists()
    assert (tmp_path / "ACTUEL").read_bytes() == pointeur_avant
    assert instantane(tmp_path / "v-1") == avant
    with pytest.raises(ErreurModele) as refus:
        charger_modele(tmp_path, "v-2")
    assert str(refus.value) == "Métadonnées absentes pour la version v-2"


# --------------------------------------------------------------------------------------------
# promouvoir et version_en_service
# --------------------------------------------------------------------------------------------
def test_promouvoir_refuse_une_version_inconnue_et_laisse_le_pointeur_inchange(tmp_path):
    """Promouvoir une version qui n'existe pas ferait tomber le consommateur : refusé."""
    publier(tmp_path, "v-1", promu=True)
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele) as exc:
        promouvoir(tmp_path, "v-fantome")
    assert str(exc.value) == "Version inconnue : v-fantome"
    assert instantane(tmp_path) == avant


def test_promouvoir_une_version_inconnue_ne_cree_aucun_pointeur(tmp_path):
    """Sans modèle en service, un refus ne doit pas en inventer un : ``ACTUEL`` reste absent."""
    publier(tmp_path, "v-1")
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele) as exc:
        promouvoir(tmp_path, "v-2")
    assert str(exc.value) == "Version inconnue : v-2"
    assert not (tmp_path / "ACTUEL").exists()
    assert version_en_service(tmp_path) is None
    assert instantane(tmp_path) == avant


def test_promouvoir_refuse_un_dossier_de_version_sans_artefact(tmp_path):
    """Des métadonnées seules ne font pas une version : l'artefact du modèle doit exister."""
    (tmp_path / "v-vide").mkdir()
    (tmp_path / "v-vide" / NOM_METADONNEES).write_text("{}", encoding="utf-8")
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele) as exc:
        promouvoir(tmp_path, "v-vide")
    assert str(exc.value) == "Version inconnue : v-vide"
    assert instantane(tmp_path) == avant


def test_promouvoir_ecrit_le_pointeur_avec_fin_de_ligne_et_laisse_les_versions_intactes(tmp_path):
    """Le pointeur contient la version suivie d'une fin de ligne ; les dossiers de versions
    restent intacts et aucun fichier temporaire ne traîne."""
    publier(tmp_path, "v-1")
    publier(tmp_path, "v-2")
    versions_avant = {v: instantane(tmp_path / v) for v in ("v-1", "v-2")}
    promouvoir(tmp_path, "v-1")
    assert (tmp_path / "ACTUEL").read_text(encoding="utf-8") == "v-1\n"
    assert version_en_service(tmp_path) == "v-1"
    promouvoir(tmp_path, "v-2")  # le pointeur est remplacé, pas complété
    assert (tmp_path / "ACTUEL").read_text(encoding="utf-8") == "v-2\n"
    assert version_en_service(tmp_path) == "v-2"
    assert {v: instantane(tmp_path / v) for v in ("v-1", "v-2")} == versions_avant
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ACTUEL", "v-1", "v-2"]


@pytest.mark.parametrize(
    ("contenu", "attendu"),
    [
        (None, None),  # pas de fichier ACTUEL
        (b"", None),
        (b"   \n\t\n", None),
        (b"v-1\n", "v-1"),
        (b"  v-2  \r\n\n", "v-2"),
        (b"v-3", "v-3"),
    ],
)
def test_version_en_service_ne_modifie_pas_le_pointeur_et_traite_un_vide_comme_absent(
    tmp_path, contenu, attendu
):
    """Un pointeur vide ou blanc n'est pas une version ; lire le pointeur ne l'altère pas."""
    if contenu is not None:
        (tmp_path / "ACTUEL").write_bytes(contenu)
    avant = instantane(tmp_path)
    assert version_en_service(tmp_path) == attendu
    assert instantane(tmp_path) == avant


def test_version_en_service_d_un_dossier_inexistant_est_none_et_ne_cree_rien(tmp_path):
    """Avant tout entraînement le dossier est absent : ni erreur ni création (disque inchangé)."""
    absent = tmp_path / "pas_encore"
    assert version_en_service(absent) is None
    assert not absent.exists()


# --------------------------------------------------------------------------------------------
# lire_metadonnees
# --------------------------------------------------------------------------------------------
def test_lire_metadonnees_absentes_est_refuse_avec_le_message_exact(tmp_path):
    """Une version sans métadonnées n'est pas chargeable : l'erreur nomme la version."""
    (tmp_path / "v-1").mkdir()
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele) as exc:
        lire_metadonnees(tmp_path, "v-1")
    assert str(exc.value) == "Métadonnées absentes pour la version v-1"
    assert instantane(tmp_path) == avant


def test_lire_metadonnees_corrompues_est_refuse_et_le_fichier_reste_intact(tmp_path):
    """Un JSON tronqué n'est pas lu à moitié : l'erreur remonte, le fichier reste intact."""
    (tmp_path / "v-1").mkdir()
    (tmp_path / "v-1" / NOM_METADONNEES).write_text('{"version": "v-1", "seuil"', encoding="utf-8")
    avant = instantane(tmp_path)
    with pytest.raises(ValueError):  # json.JSONDecodeError en est une sous-classe
        lire_metadonnees(tmp_path, "v-1")
    assert instantane(tmp_path) == avant


# --------------------------------------------------------------------------------------------
# charger_modele
# --------------------------------------------------------------------------------------------
def test_charger_modele_sans_pointeur_est_refuse_avec_le_message_exact(tmp_path):
    """Sans modèle en service, le message indique le dossier et la commande d'entraînement."""
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele) as exc:
        charger_modele(tmp_path)
    assert str(exc.value) == (
        f"Aucun modèle en service dans {tmp_path}. "
        "Lancer l'entraînement : python -m detection_fraude.entrainement"
    )
    assert instantane(tmp_path) == avant


def test_charger_modele_avec_un_pointeur_vide_est_refuse_comme_sans_pointeur(tmp_path):
    """Un pointeur vide (écriture interrompue ailleurs) ne désigne aucun modèle."""
    (tmp_path / "ACTUEL").write_text("\n", encoding="utf-8")
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele) as exc:
        charger_modele(tmp_path)
    assert str(exc.value).startswith(f"Aucun modèle en service dans {tmp_path}. ")
    assert instantane(tmp_path) == avant


def test_charger_modele_d_une_version_inconnue_est_refuse_sans_toucher_au_service(tmp_path):
    """Charger explicitement une version absente ne modifie pas le modèle en service."""
    publier(tmp_path, "v-1", promu=True)
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele) as exc:
        charger_modele(tmp_path, "v-9")
    assert str(exc.value) == "Métadonnées absentes pour la version v-9"
    assert instantane(tmp_path) == avant


def test_charger_modele_n_execute_pas_un_artefact_altere(tmp_path, chargements):
    """Un pickle se lance à la lecture : avec une empreinte incorrecte il ne doit pas être lu."""
    exporter_modele(tmp_path, PipelinePiege(), 0.5, {"version": "v-1"})
    promouvoir(tmp_path, "v-1")
    fichier = tmp_path / "v-1" / NOM_MODELE
    fichier.write_bytes(fichier.read_bytes() + b"altere")
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele) as exc:
        charger_modele(tmp_path)
    assert str(exc.value) == f"Empreinte SHA-256 incorrecte pour {fichier} : chargement refusé."
    assert chargements == []  # le piège n'a pas été déclenché
    assert instantane(tmp_path) == avant
    exporter_modele(tmp_path, PipelinePiege(), 0.5, {"version": "v-2"})
    assert charger_modele(tmp_path, "v-2").pipeline == "piege"
    assert chargements == ["charge"]  # témoin : un artefact sain, lui, est bien désérialisé


def test_charger_modele_refuse_des_metadonnees_sans_empreinte(tmp_path, chargements):
    """Des métadonnées sans ``sha256_modele`` ne prouvent rien : le chargement est refusé."""
    exporter_modele(tmp_path, PipelinePiege(), 0.5, {"version": "v-1"})
    chemin = tmp_path / "v-1" / NOM_METADONNEES
    meta = json.loads(chemin.read_text(encoding="utf-8"))
    del meta["sha256_modele"]
    chemin.write_text(json.dumps(meta), encoding="utf-8")
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele) as exc:
        charger_modele(tmp_path, "v-1")
    assert str(exc.value) == (
        f"Empreinte SHA-256 incorrecte pour {tmp_path / 'v-1' / NOM_MODELE} : chargement refusé."
    )
    assert chargements == []
    assert instantane(tmp_path) == avant


def test_charger_modele_refuse_une_empreinte_falsifiee_dans_les_metadonnees(tmp_path, chargements):
    """Une empreinte qui ne correspond pas au fichier arrête le chargement avant le pickle."""
    exporter_modele(tmp_path, PipelinePiege(), 0.5, {"version": "v-1"})
    chemin = tmp_path / "v-1" / NOM_METADONNEES
    meta = json.loads(chemin.read_text(encoding="utf-8"))
    meta["sha256_modele"] = "0" * 64
    chemin.write_text(json.dumps(meta), encoding="utf-8")
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele):
        charger_modele(tmp_path, "v-1")
    assert chargements == []
    assert instantane(tmp_path) == avant


def test_charger_modele_refuse_un_artefact_remplace_par_celui_d_une_autre_version(
    tmp_path, chargements
):
    """Substituer le modèle d'une autre version (même valide) est détecté par l'empreinte."""
    publier(tmp_path, "v-1", positifs=1)
    exporter_modele(tmp_path, PipelinePiege(), 0.5, {"version": "v-2"})
    promouvoir(tmp_path, "v-1")
    (tmp_path / "v-1" / NOM_MODELE).write_bytes((tmp_path / "v-2" / NOM_MODELE).read_bytes())
    avant = instantane(tmp_path)
    with pytest.raises(ErreurModele) as exc:
        charger_modele(tmp_path)
    assert str(exc.value).startswith("Empreinte SHA-256 incorrecte pour ")
    assert chargements == []
    assert instantane(tmp_path) == avant


def test_charger_modele_dont_l_artefact_a_disparu_leve_file_not_found(tmp_path):
    """Des métadonnées sans artefact (volume mal copié) sont une erreur franche, pas un modèle."""
    publier(tmp_path, "v-1", promu=True)
    (tmp_path / "v-1" / NOM_MODELE).unlink()
    avant = instantane(tmp_path)
    with pytest.raises(FileNotFoundError):
        charger_modele(tmp_path)
    assert instantane(tmp_path) == avant


def test_charger_modele_prefere_la_version_explicite_au_pointeur_et_laisse_le_dossier_intact(
    tmp_path,
):
    """Charger une version demandée ne lit pas le pointeur et n'écrit rien dans le dossier."""
    publier(tmp_path, "v-1", positifs=1, seuil=0.3, promu=True)
    publier(tmp_path, "v-2", positifs=3, seuil=0.6)
    avant = instantane(tmp_path)
    charge = charger_modele(tmp_path, "v-2")
    assert charge.version == "v-2"
    assert charge.seuil == 0.6
    assert charge.metadonnees == lire_metadonnees(tmp_path, "v-2")
    assert charge.pipeline.predict_proba(np.zeros((1, 1))).tolist() == [[0.25, 0.75]]
    assert instantane(tmp_path) == avant


def test_charger_modele_sans_version_prend_celle_du_pointeur_et_laisse_le_dossier_intact(tmp_path):
    """Sans version demandée, c'est le modèle en service qui est chargé, en lecture seule."""
    publier(tmp_path, "v-1", positifs=1, seuil=1, promu=True)
    publier(tmp_path, "v-2", positifs=3)
    avant = instantane(tmp_path)
    charge = charger_modele(tmp_path)
    assert charge.version == "v-1"
    assert charge.seuil == 1.0
    assert type(charge.seuil) is float  # converti : le JSON stocke l'entier 1
    assert charge.metadonnees["version"] == "v-1"
    assert charge.pipeline.predict_proba(np.zeros((1, 1))).tolist() == [[0.75, 0.25]]
    assert instantane(tmp_path) == avant


# --------------------------------------------------------------------------------------------
# ChargeurModele.obtenir
# --------------------------------------------------------------------------------------------
def test_chargeur_charge_une_fois_et_ne_recharge_pas_tant_que_le_pointeur_est_le_meme(
    tmp_path, monkeypatch, evenements
):
    """Pas de rechargement inutile à chaque lot ; le dossier des modèles reste intact."""
    publier(tmp_path, "v-1", promu=True)
    appels = espionner_chargements(monkeypatch, {})
    avant = instantane(tmp_path)
    chargeur = ChargeurModele(tmp_path)
    premier = chargeur.obtenir()
    assert premier.version == "v-1"
    assert chargeur.obtenir() is premier
    assert chargeur.obtenir() is premier
    assert appels == [(tmp_path, "v-1")]
    assert evenements.tous() == []
    assert instantane(tmp_path) == avant


def test_chargeur_bascule_sur_la_version_promue_sans_redemarrage(
    tmp_path, monkeypatch, evenements
):
    """Une promotion est vue au lot suivant : modèle rechargé, aucune erreur journalisée."""
    publier(tmp_path, "v-1", promu=True)
    chargeur = ChargeurModele(tmp_path)
    assert chargeur.obtenir().version == "v-1"
    publier(tmp_path, "v-2", positifs=3, seuil=0.8, promu=True)
    avant = instantane(tmp_path)
    appels = espionner_chargements(monkeypatch, {})
    nouveau = chargeur.obtenir()
    assert (nouveau.version, nouveau.seuil) == ("v-2", 0.8)
    assert chargeur.obtenir() is nouveau
    assert appels == [(tmp_path, "v-2")]
    assert evenements.tous() == []
    assert instantane(tmp_path) == avant  # recharger ne fait que lire


def test_chargeur_premier_chargement_sans_modele_echoue_franchement_puis_reussit(
    tmp_path, evenements
):
    """Sans modèle rien ne peut être noté : erreur franche, rien de journalisé comme rechargement,
    et l'échec n'est pas mémorisé (le modèle promu ensuite est trouvé)."""
    chargeur = ChargeurModele(tmp_path)
    for _ in range(2):  # l'échec est rejoué à chaque appel tant qu'aucun modèle n'existe
        with pytest.raises(ErreurModele) as exc:
            chargeur.obtenir()
        assert str(exc.value).startswith(f"Aucun modèle en service dans {tmp_path}. ")
    assert evenements.tous() == []
    publier(tmp_path, "v-1", promu=True)
    assert chargeur.obtenir().version == "v-1"


def test_chargeur_premier_chargement_d_un_pointeur_vers_une_version_corrompue_echoue(
    tmp_path, evenements
):
    """Au premier chargement une version corrompue n'a pas d'ancien modèle à garder : erreur."""
    publier(tmp_path, "v-1", promu=True)
    fichier = tmp_path / "v-1" / NOM_MODELE
    fichier.write_bytes(fichier.read_bytes() + b"x")
    with pytest.raises(ErreurModele) as exc:
        ChargeurModele(tmp_path).obtenir()
    assert str(exc.value) == f"Empreinte SHA-256 incorrecte pour {fichier} : chargement refusé."
    assert evenements.tous() == []


def test_chargeur_garde_l_ancien_modele_intact_et_journalise_une_seule_fois_la_version_refusee(
    tmp_path, monkeypatch, evenements
):
    """Un rechargement raté ne coupe pas le flux : l'ancien modèle continue de noter, l'erreur est
    journalisée une fois (champs exacts) et pas rejouée à chaque lot."""
    publier(tmp_path, "v-1", promu=True)
    chargeur = ChargeurModele(tmp_path)
    modele_initial = chargeur.obtenir()
    publier(tmp_path, "v-2", positifs=3)
    fichier = tmp_path / "v-2" / NOM_MODELE
    fichier.write_bytes(fichier.read_bytes() + b"altere")
    promouvoir(tmp_path, "v-2")
    appels = espionner_chargements(monkeypatch, {})
    for _ in range(3):
        assert chargeur.obtenir() is modele_initial
    assert appels == [(tmp_path, "v-2")]  # une seule tentative, pas rejouée
    assert evenements.tous() == [
        (
            "ERROR",
            "rechargement_modele_impossible",
            {
                "version": "v-2",
                "gardee": "v-1",
                "cause": f"ErreurModele: Empreinte SHA-256 incorrecte pour {fichier}"
                " : chargement refusé.",
            },
        )
    ]


def test_chargeur_reprend_sur_une_version_meilleure_apres_une_version_refusee(
    tmp_path, evenements
):
    """Après une version refusée, la promotion d'une version saine est adoptée automatiquement."""
    publier(tmp_path, "v-1", promu=True)
    chargeur = ChargeurModele(tmp_path)
    chargeur.obtenir()
    publier(tmp_path, "v-2")
    (tmp_path / "v-2" / NOM_MODELE).write_bytes(b"corrompu")
    promouvoir(tmp_path, "v-2")
    assert chargeur.obtenir().version == "v-1"
    publier(tmp_path, "v-3", positifs=3, seuil=0.9, promu=True)
    avant = instantane(tmp_path)
    reprise = chargeur.obtenir()
    assert (reprise.version, reprise.seuil) == ("v-3", 0.9)
    assert instantane(tmp_path) == avant
    assert [e[1] for e in evenements.tous()] == [
        "rechargement_modele_impossible"
    ]  # une seule fois


def test_chargeur_journalise_chaque_version_refusee_une_fois_avec_la_version_gardee(
    tmp_path, monkeypatch, evenements
):
    """Deux versions refusées successivement donnent deux événements distincts (une par version),
    chacun nommant la version toujours en service."""
    publier(tmp_path, "v-1", promu=True)
    chargeur = ChargeurModele(tmp_path)
    chargeur.obtenir()
    publier(tmp_path, "v-2", promu=True)
    publier(tmp_path, "v-3")
    appels = espionner_chargements(
        monkeypatch, {"v-2": ValueError("deux"), "v-3": ValueError("trois")}
    )
    for _ in range(2):
        assert chargeur.obtenir().version == "v-1"
    promouvoir(tmp_path, "v-3")
    avant = instantane(tmp_path)
    for _ in range(2):
        assert chargeur.obtenir().version == "v-1"
    assert instantane(tmp_path) == avant
    assert appels == [(tmp_path, "v-2"), (tmp_path, "v-3")]
    assert evenements.tous() == [
        (
            "ERROR",
            "rechargement_modele_impossible",
            {"version": "v-2", "gardee": "v-1", "cause": "ValueError: deux"},
        ),
        (
            "ERROR",
            "rechargement_modele_impossible",
            {"version": "v-3", "gardee": "v-1", "cause": "ValueError: trois"},
        ),
    ]


@pytest.mark.parametrize(
    "longueur_message",
    [286, 287, 5000, 3],
)
def test_chargeur_tronque_la_cause_journalisee_a_300_caracteres(
    tmp_path, monkeypatch, evenements, longueur_message
):
    """La cause (type + message) est coupée à 300 caractères : un message géant ne gonfle pas
    l'audit, et une cause de 300 exactement reste entière."""
    publier(tmp_path, "v-1", promu=True)
    chargeur = ChargeurModele(tmp_path)
    chargeur.obtenir()
    publier(tmp_path, "v-2", promu=True)
    message = "y" * longueur_message
    espionner_chargements(monkeypatch, {"v-2": RuntimeError(message)})
    avant = instantane(tmp_path)
    chargeur.obtenir()
    assert instantane(tmp_path) == avant
    [(_, _, champs)] = evenements.tous()
    # « RuntimeError: » compte 14 caractères : 286 -> 300 pile (intacte), 287 -> 301 (coupée)
    assert champs["cause"] == ("RuntimeError: " + message)[:300]


def test_chargeur_ne_masque_pas_une_interruption_clavier_pendant_un_rechargement(
    tmp_path, monkeypatch, evenements
):
    """Seules les erreurs de chargement sont absorbées : Ctrl-C traverse, rien n'est journalisé."""
    publier(tmp_path, "v-1", promu=True)
    chargeur = ChargeurModele(tmp_path)
    modele_initial = chargeur.obtenir()
    publier(tmp_path, "v-2", promu=True)
    espionner_chargements(monkeypatch, {"v-2": KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        chargeur.obtenir()
    assert evenements.tous() == []
    monkeypatch.undo()
    assert chargeur.obtenir() is not modele_initial  # la version n'a pas été marquée « refusée »


def test_chargeur_conserve_son_modele_quand_le_pointeur_disparait(
    tmp_path, monkeypatch, evenements
):
    """Un pointeur effacé n'est pas une version à charger : on garde le modèle, sans erreur."""
    publier(tmp_path, "v-1", promu=True)
    chargeur = ChargeurModele(tmp_path)
    modele_initial = chargeur.obtenir()
    (tmp_path / "ACTUEL").unlink()
    appels = espionner_chargements(monkeypatch, {})
    sans_pointeur_avant = instantane(tmp_path)
    assert chargeur.obtenir() is modele_initial
    assert instantane(tmp_path) == sans_pointeur_avant
    (tmp_path / "ACTUEL").write_text("\n", encoding="utf-8")  # pointeur vide : même conclusion
    pointeur_vide_avant = instantane(tmp_path)
    assert chargeur.obtenir() is modele_initial
    assert instantane(tmp_path) == pointeur_vide_avant
    assert appels == []
    assert evenements.tous() == []


def test_chargeur_ne_recharge_pas_la_version_deja_en_memoire_meme_si_le_pointeur_est_reecrit(
    tmp_path, monkeypatch
):
    """Promouvoir la version déjà chargée est sans effet pour le chargeur (aucun rechargement)."""
    publier(tmp_path, "v-1", promu=True)
    chargeur = ChargeurModele(tmp_path)
    modele_initial = chargeur.obtenir()
    appels = espionner_chargements(monkeypatch, {})
    promouvoir(tmp_path, "v-1")
    avant = instantane(tmp_path)
    assert chargeur.obtenir() is modele_initial
    assert instantane(tmp_path) == avant
    assert appels == []
