# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Chiffres de la préparation du notebook, rejoués avec le paquet sur le vrai `spam.csv`.

Ces tests durent quelques secondes (pas d'entraînement). Les effectifs du jeu de données sont des
faits publiés dans le notebook (cellules 3, 8, 9, 10, 12 et 25) : ils portent l'annotation
`cardinal-ok`.
"""

import hashlib
import re
from pathlib import Path

import pytest

from att_spam.donnees import (
    Encodeur,
    charger_sms,
    decouper_mots,
    decouper_par_groupes,
    grouper_quasi_doublons,
    matrice_presence,
    normaliser,
    similarites,
)
from att_spam.gan import VocabulaireGAN

SPAM_CSV = Path(__file__).resolve().parents[1] / "spam.csv"
GRAINE = 42


@pytest.fixture(scope="module")
def chaine():
    """Préparation complète du notebook : chargement, normalisation, groupes, découpage."""
    chargement = charger_sms(SPAM_CSV)
    donnees = chargement.donnees
    donnees["mots"] = donnees["texte"].map(normaliser).map(decouper_mots)
    presence = matrice_presence(donnees["mots"].tolist())
    nb_groupes, donnees["groupe"] = grouper_quasi_doublons(presence)
    decoupage = decouper_par_groupes(donnees, GRAINE)
    return chargement, presence, nb_groupes, decoupage


def test_chargement_et_quasi_doublons_reproduisent_les_cellules_3_et_8(chaine):
    """Comptes du chargement (cellule 3) et des groupes de quasi-doublons (cellule 8)."""
    chargement, *_ = chaine
    assert (chargement.nb_lus, chargement.nb_coupes, chargement.nb_doublons) == (5572, 50, 403)
    donnees = chargement.donnees
    assert donnees.shape[0] == 5169
    assert donnees["etiquette"].value_counts().to_dict() == {"ham": 4516, "spam": 653}
    assert donnees["cible"].mean() == pytest.approx(0.126, abs=5e-4)

    chargement, _, nb_groupes, _ = chaine
    donnees = chargement.donnees
    taille_groupe = donnees["groupe"].map(donnees["groupe"].value_counts())
    assert nb_groupes == 4969
    assert taille_groupe.max() == 12
    assert (taille_groupe >= 2).sum() == 330
    assert ((taille_groupe >= 2) & (donnees["cible"] == 1)).sum() == 252
    assert (donnees.groupby("groupe")["cible"].nunique() > 1).sum() == 0


def test_decoupage_et_similarites_reproduisent_la_cellule_9(chaine):
    """Effectifs des trois jeux, similarité maximale sous le seuil, ressemblances restantes."""
    _, _, _, decoupage = chaine
    effectifs = {
        "apprentissage": (decoupage.apprentissage, 3691, 466),
        "validation": (decoupage.validation, 739, 94),
        "test": (decoupage.test, 739, 93),
    }
    for nom, (jeu, taille, spams) in effectifs.items():
        assert jeu.shape[0] == taille, nom
        assert jeu["cible"].sum() == spams, nom
    assert decoupage.groupes_partages() == set()

    _, presence, _, decoupage = chaine
    jaccard = similarites(
        presence, decoupage.test.index.to_numpy(), decoupage.apprentissage.index.to_numpy()
    )
    assert round(float(jaccard.max()), 3) == 0.786
    assert jaccard.max() < 0.8

    # Le titre « sans quasi-doublon » ne dit pas « sans ressemblance » : 34 sur 93 à 0,5.
    _, presence, _, decoupage = chaine
    test, apprentissage = decoupage.test, decoupage.apprentissage
    jaccard = similarites(presence, test.index.to_numpy(), apprentissage.index.to_numpy())
    plus_proche = jaccard[test["cible"].to_numpy() == 1].max(axis=1)
    assert plus_proche.shape[0] == 93
    assert [(plus_proche >= seuil).sum() for seuil in (0.5, 0.6, 0.7)] == [34, 17, 10]


def test_rgpd_numeros_colles_et_vocabulaire_du_gan(chaine):
    """Vocabulaire du GAN (cellule 25) et numéros surtaxés collés à un mot (cellules 5 et 40)."""
    *_, decoupage = chaine
    app = decoupage.apprentissage
    spams = app.loc[app["cible"] == 1, "mots"].tolist()
    vocabulaire = VocabulaireGAN.apprendre(spams)
    assert vocabulaire.taille == 916
    assert vocabulaire.tenseur(spams).shape == (466, 25)

    # Phrase de la cellule 40 (RGPD) : à revérifier si le découpage ou le vocabulaire change.
    # Un numéro surtaxé britannique compte 11 chiffres : deux mots du vocabulaire en portent au
    # moins dix. Un troisième mot à chiffres, `acl03530150pm` (8 chiffres), est un code de
    # référence collé à la fin d'un message de gain, pas un numéro : il n'entre pas dans ce compte.
    *_, decoupage = chaine
    app = decoupage.apprentissage
    vocabulaire = VocabulaireGAN.apprendre(app.loc[app["cible"] == 1, "mots"].tolist())
    numeros = {mot for mot in vocabulaire.mot_vers_indice if re.search(r"\d{10,}", mot)}
    assert numeros == {"call09050000327", "tscs087147403231winawk"}
    assert "acl03530150pm" in vocabulaire.mot_vers_indice

    # Limite RGPD de la cellule 5 : 22 SMS sur 5 169 gardent un jeton de sept chiffres ou plus.
    chargement, *_ = chaine
    donnees = chargement.donnees
    garde = donnees["mots"].map(lambda mots: any(re.search(r"\d{7,}", m) for m in mots))
    assert garde.sum() == 22
    assert donnees.loc[garde, "cible"].eq(1).all()  # tous des spams


def test_normalisation_jetons_vocabulaire_et_tenseurs_reproduisent_les_cellules_6_10_12(chaine):
    """Exemple normalisé, part des jetons-signaux, vocabulaire et tenseurs."""
    chargement, *_ = chaine
    donnees = chargement.donnees
    exemple = donnees.loc[donnees["cible"] == 1].iloc[0]
    # Le SMS n'est pas recopié ici (corpus non redistribué) : son découpage est vérifié par son
    # empreinte SHA-256 (mots joints par le séparateur U+001F), sa longueur et ses marqueurs.
    assert len(exemple["mots"]) == 31
    assert exemple["mots"].count("<nombre>") == 2 and exemple["mots"].count("<tel>") == 1
    empreinte = hashlib.sha256("\x1f".join(exemple["mots"]).encode("utf-8")).hexdigest()
    assert empreinte == "0a33c8bf9e1a652fc8e547a770fbffc4c8b1d0ddfa63cad0eee1bb9b546a0e2a"

    *_, decoupage = chaine
    app = decoupage.apprentissage
    attendu = {  # jeton : (part des spams, part des messages légitimes), en %
        "<url>": (19.1, 0.3),
        "<argent>": (50.2, 0.2),
        "<tel>": (71.9, 0.0),
        "<nombre>": (63.9, 12.2),
    }
    for jeton, (part_spams, part_legitimes) in attendu.items():
        presence = app["mots"].map(lambda mots, j=jeton: j in mots)
        assert 100 * presence[app["cible"] == 1].mean() == pytest.approx(part_spams, abs=0.05)
        assert 100 * presence[app["cible"] == 0].mean() == pytest.approx(part_legitimes, abs=0.05)

    *_, decoupage = chaine
    codeur = Encodeur.apprendre(decoupage.apprentissage["mots"].tolist())
    assert (len(codeur.vocabulaire), codeur.longueur) == (2996, 33)  # cardinal-ok: cellule 12
    x, y = codeur.tenseurs(decoupage.apprentissage)
    assert tuple(x.shape) == (3691, 33)
    assert y.shape[0] == x.shape[0]


def test_contre_epreuve_un_decoupage_au_hasard_partage_des_groupes(chaine):
    """Contre-épreuve : au hasard, 62 groupes partagés, copies parfaites.

    Par groupes (cellule 9 du notebook) : aucun groupe partagé et similarité maximale de 0,786.
    """
    from sklearn.model_selection import StratifiedKFold

    chargement, presence, _, decoupage = chaine
    donnees = chargement.donnees
    decoupeur = StratifiedKFold(n_splits=7, shuffle=True, random_state=GRAINE)
    plis = [indices for _, indices in decoupeur.split(donnees, donnees["cible"])]
    test = donnees.iloc[plis[0]]
    validation = donnees.iloc[plis[1]]
    apprentissage = donnees.drop(index=test.index.union(validation.index))
    partages = set(apprentissage["groupe"]) & (set(test["groupe"]) | set(validation["groupe"]))
    assert len(partages) == 62  # cardinal-ok: valeur mesurée (contre-épreuve)
    jaccard = similarites(presence, test.index.to_numpy(), apprentissage.index.to_numpy())
    assert round(float(jaccard.max()), 3) == 1.0  # des copies parfaites de part et d'autre
    assert decoupage.groupes_partages() == set()  # le découpage par groupes n'en laisse aucun
