# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests de `att_spam.donnees` : chargement, normalisation, quasi-doublons, découpage, encodage."""

import inspect

import numpy as np
import pandas as pd
import pytest
import torch

from att_spam import donnees
from att_spam.donnees import (
    Augmenteur,
    Decoupage,
    Encodeur,
    charger_sms,
    decouper_mots,
    decouper_par_groupes,
    grouper_quasi_doublons,
    matrice_presence,
    normaliser,
    similarites,
)

ENTETE = "v1,v2,Unnamed: 2,Unnamed: 3,Unnamed: 4\n"


def ecrire_csv(chemin, lignes):
    """Écrit un petit `spam.csv` en latin-1, comme le fichier d'origine."""
    chemin.write_bytes((ENTETE + "\n".join(lignes) + "\n").encode("latin-1"))
    return chemin


# --------------------------------------------------------------------------- chargement


def test_charger_sms_recolle_decode_et_compte(tmp_path):
    """Recolle les morceaux, lit le latin-1, accepte un chemin texte, compte lus, coupés et
    doublons."""
    csv = ecrire_csv(
        tmp_path / "spam.csv",
        [
            "ham,Bonjour,,,",
            "spam,Win now,call 1,again,",
            "ham,Bonjour,,,",  # doublon exact de la première ligne
            "spam,x,y,,z",
            "ham,a,b,,",  # coupé seulement dans la première colonne « Unnamed »
            "spam,Bonjour,,,",  # même texte, étiquette contraire : doublon de texte aussi
        ],
    )
    chargement = charger_sms(csv)
    textes = ["Bonjour", "Win now,call 1,again", "x,y,z", "a,b"]
    assert chargement.donnees["texte"].tolist() == textes
    assert (chargement.nb_lus, chargement.nb_coupes, chargement.nb_doublons) == (6, 3, 2)
    assert chargement.donnees["etiquette"].tolist() == ["ham", "spam", "spam", "ham"]
    assert chargement.donnees["cible"].tolist() == [0, 1, 1, 0]
    assert pd.api.types.is_integer_dtype(chargement.donnees["cible"])  # 0/1 entiers, pas 0.0/1.0
    assert chargement.donnees.columns.tolist() == ["etiquette", "texte", "cible"]
    assert chargement.donnees.index.tolist() == [0, 1, 2, 3]

    csv = ecrire_csv(tmp_path / "spam.csv", ["ham,caf\xe9 \xa3 5,,,"])
    assert charger_sms(csv).donnees["texte"].tolist() == ["café £ 5"]

    csv = ecrire_csv(tmp_path / "spam.csv", ["ham,Bonjour,,,"])
    assert charger_sms(str(csv)).nb_lus == 1


def test_charger_sms_refuse_un_fichier_sans_la_colonne_attendue(tmp_path):
    csv = tmp_path / "spam.csv"
    csv.write_bytes(b"v1,v2,Unnamed: 2,Unnamed: 3\nham,Bonjour,,\n")
    with pytest.raises(ValueError) as erreur:
        charger_sms(csv)
    assert str(erreur.value) == f"colonne(s) absente(s) de {csv} : Unnamed: 4"


def test_charger_sms_nomme_toutes_les_colonnes_absentes(tmp_path):
    csv = tmp_path / "spam.csv"
    csv.write_bytes(b"label,texte\nham,Bonjour\n")
    with pytest.raises(ValueError) as erreur:
        charger_sms(csv)
    assert str(erreur.value) == (
        f"colonne(s) absente(s) de {csv} : v1, v2, Unnamed: 2, Unnamed: 3, Unnamed: 4"
    )


# --------------------------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        ("WIN a Prize", "win a prize"),
        ("see www.example.com now", "see  <url>  now"),
        ("go to http://a.io/x?y=1 ok", "go to  <url>  ok"),
        # Seul cas de la branche co.uk du motif ; domaine sous .example, réservé (RFC 2606).
        ("mail me@site.co.uk.example please", "mail  <url>  please"),
        ("visit news.net today", "visit  <url>  today"),
        ("visit ong.org today", "visit  <url>  today"),
        ("win £100 now", "win  <argent>  now"),
        ("only $5 today", "only  <argent>  today"),
        ("only € 3.50 today", "only  <argent>  today"),
        ("only £2,5 today", "only  <argent>  today"),
        ("cost 10p each", "cost  <argent>  each"),
        ("cost 10 pounds each", "cost  <argent>  each"),
        ("cost 2.50 pence each", "cost  <argent>  each"),
        ("cost 5€ each", "cost  <argent>  each"),
        ("call 09061701461 now", "call  <tel>  now"),
        ("code 12345 ok", "code  <tel>  ok"),
        ("code 1234 ok", "code  <nombre>  ok"),
        ("on 21 may 2005", "on  <nombre>  may  <nombre> "),
        ("a 2nd, 4u and txt2win", "a 2nd, 4u and txt2win"),
        ("08452810075over18's", "08452810075over18's"),
    ],
)
def test_normaliser_remplace_les_signaux_par_des_jetons(brut, attendu):
    assert normaliser(brut) == attendu


def test_normaliser_ordre_des_motifs_et_casse():
    """Le montant passe avant le nombre ; la mise en minuscules précède les motifs, qui ignorent la
    casse."""
    # Si le nombre passait d'abord, « £100 » deviendrait « £ <nombre> » et non « <argent> ».
    assert normaliser("£100") == " <argent> "

    assert normaliser("WWW.EXAMPLE.COM") == " <url> "
    assert normaliser("100 POUNDS") == " <argent> "

    assert donnees.MOTIF_URL.search("HTTP://A.IO") is not None
    assert donnees.MOTIF_URL.search("WWW.A.IO") is not None
    assert donnees.MOTIF_URL.search("A.CO.UK") is not None
    assert donnees.MOTIF_ARGENT.search("5 POUNDS") is not None
    assert donnees.MOTIF_ARGENT.search("5 PENCE") is not None


def test_decouper_mots_garde_jetons_mots_et_apostrophes():
    """Garde les jetons, les mots et les apostrophes ; écarte la ponctuation."""
    texte = "free <tel> 2nd don't a_b"
    assert decouper_mots(texte) == ["free", "<tel>", "2nd", "don't", "a", "b"]

    assert decouper_mots("ok, fine! (yes)") == ["ok", "fine", "yes"]
    assert decouper_mots("") == []


# --------------------------------------------------------------------------- quasi-doublons

#: Six SMS : 0-1-2 forment une chaîne (0~1 vaut exactement 0,8 ; 1~2 vaut 5/6 ; 0~2 vaut seulement
#: 2/3), 3 et 4 se ressemblent à 0,5 seulement, 5 est un SMS long qui ne ressemble à personne.
SMS_CHAINE = [
    list("abcd"),
    list("abcde"),
    list("abcdef"),
    list("xy"),
    list("xyzw"),
    list("abcdefghi"),
]


def test_matrice_presence_compte_chaque_mot_une_fois_par_sms():
    """Un mot compte une fois par SMS ; une liste vide donne une matrice vide."""
    presence = matrice_presence([["a", "b", "a"], ["b", "c", "c"], []])
    assert presence.tailles.tolist() == [2.0, 2.0, 0.0]
    assert presence.tailles.dtype == np.float64
    # Le produit M·Mᵀ ne dépend pas de l'ordre des colonnes : mots communs de chaque paire.
    communs = (presence.matrice @ presence.matrice.T).toarray()
    assert communs.tolist() == [[2.0, 1.0, 0.0], [1.0, 2.0, 0.0], [0.0, 0.0, 0.0]]
    assert presence.matrice.shape == (3, 3)

    presence = matrice_presence([])
    assert presence.matrice.shape == (0, 0)
    assert presence.tailles.tolist() == []


def test_grouper_regroupe_de_proche_en_proche_selon_le_seuil():
    """Groupes de proche en proche, à la limite du seuil 0,8, plus haut, plus bas, et sans mot
    commun."""
    nb_groupes, etiquettes = grouper_quasi_doublons(matrice_presence(SMS_CHAINE))
    assert nb_groupes == 4
    assert etiquettes.tolist() == [0, 0, 0, 1, 2, 3]

    nb_groupes, etiquettes = grouper_quasi_doublons(matrice_presence(SMS_CHAINE), seuil=0.81)
    assert nb_groupes == 5
    assert etiquettes.tolist() == [0, 1, 1, 2, 3, 4]

    nb_groupes, etiquettes = grouper_quasi_doublons(matrice_presence(SMS_CHAINE), seuil=0.5)
    assert nb_groupes == 2  # la paire 3~4 vaut exactement 0,5 : elle est regroupée
    assert etiquettes.tolist() == [0, 0, 0, 1, 1, 0]

    nb_groupes, etiquettes = grouper_quasi_doublons(matrice_presence([["a"], ["b"], ["c"]]))
    assert nb_groupes == 3
    assert etiquettes.tolist() == [0, 1, 2]

    assert donnees.SEUIL_QUASI_DOUBLON == 0.8
    presence = matrice_presence(SMS_CHAINE)
    assert grouper_quasi_doublons(presence)[1].tolist() == grouper_quasi_doublons(presence, 0.8)[
        1
    ].tolist()


def test_similarites_donne_le_jaccard_de_chaque_paire():
    """Jaccard de chaque paire ; un SMS avec lui-même vaut un."""
    presence = matrice_presence(SMS_CHAINE)
    attendu = [[0.8, 4 / 6, 0.0], [0.0, 0.0, 0.5]]
    resultat = similarites(presence, np.array([0, 3]), np.array([1, 2, 4]))
    assert resultat.shape == (2, 3)
    assert resultat == pytest.approx(np.array(attendu))

    presence = matrice_presence(SMS_CHAINE)
    assert similarites(presence, np.array([2]), np.array([2])).tolist() == [[1.0]]


# --------------------------------------------------------------------------- découpage


def jeu_groupe(nb_groupes=40, taille=3):
    """Jeu jouet : `nb_groupes` groupes de `taille` SMS, un groupe sur quatre est du spam."""
    lignes = []
    for groupe in range(nb_groupes):
        for _ in range(taille):
            lignes.append({"cible": int(groupe % 4 == 0), "groupe": groupe})
    return pd.DataFrame(lignes)


def test_decouper_separe_les_groupes_et_stratifie():
    """Aucun groupe partagé, pli 0 pour le test, pli 1 pour la validation, nombre de plis, même part
    de spams."""
    jeu = jeu_groupe()
    decoupage = decouper_par_groupes(jeu, graine=7)
    assert isinstance(decoupage, Decoupage)
    assert decoupage.groupes_partages() == set()
    indices = [decoupage.apprentissage.index, decoupage.validation.index, decoupage.test.index]
    assert sorted(i for ensemble in indices for i in ensemble) == list(jeu.index)

    decoupage = decouper_par_groupes(jeu_groupe(), graine=7)
    assert decoupage.test.index.tolist() == decoupage.plis[0].tolist()
    assert decoupage.validation.index.tolist() == decoupage.plis[1].tolist()
    reste = set(range(len(jeu_groupe()))) - set(decoupage.plis[0]) - set(decoupage.plis[1])
    assert set(decoupage.apprentissage.index) == reste

    jeu = jeu_groupe()
    par_defaut = inspect.signature(decouper_par_groupes).parameters["nb_plis"].default
    assert par_defaut == 7
    assert len(decouper_par_groupes(jeu, graine=7).plis) == par_defaut
    nb_plis = 4
    decoupage = decouper_par_groupes(jeu, graine=7, nb_plis=nb_plis)
    assert len(decoupage.plis) == nb_plis

    decoupage = decouper_par_groupes(jeu_groupe(), graine=7, nb_plis=3)
    assert decoupage.groupes_partages() == set()
    assert set(decoupage.test["groupe"]).isdisjoint(set(decoupage.validation["groupe"]))

    decoupage = decouper_par_groupes(jeu_groupe(nb_groupes=200), graine=7)
    for jeu in (decoupage.apprentissage, decoupage.validation, decoupage.test):
        assert jeu["cible"].mean() == pytest.approx(0.25, abs=0.04)


def test_decouper_depend_de_la_graine_seule():
    """Même graine, même découpage ; autre graine, autre découpage."""
    a = decouper_par_groupes(jeu_groupe(), graine=7)
    b = decouper_par_groupes(jeu_groupe(), graine=7)
    assert [p.tolist() for p in a.plis] == [p.tolist() for p in b.plis]

    a = decouper_par_groupes(jeu_groupe(), graine=7)
    b = decouper_par_groupes(jeu_groupe(), graine=8)
    assert a.test.index.tolist() != b.test.index.tolist()


def test_decouper_laisse_le_jeu_d_entree_inchange():
    jeu = jeu_groupe()
    avant = jeu.copy()
    decoupage = decouper_par_groupes(jeu, graine=7)
    decoupage.test.loc[:, "cible"] = 9  # modifier un jeu rendu ne doit pas toucher l'entrée
    decoupage.apprentissage.loc[:, "cible"] = 9
    pd.testing.assert_frame_equal(jeu, avant)


@pytest.mark.parametrize("nb_plis", [2, 1, 0])
def test_decouper_refuse_moins_de_trois_plis(nb_plis):
    with pytest.raises(ValueError) as erreur:
        decouper_par_groupes(jeu_groupe(), graine=7, nb_plis=nb_plis)
    assert str(erreur.value) == "il faut au moins 3 plis : test, validation et apprentissage"


def test_groupes_partages_detecte_un_groupe_commun():
    app = pd.DataFrame({"groupe": [1, 2, 3]})
    val = pd.DataFrame({"groupe": [3, 4]})
    test = pd.DataFrame({"groupe": [5, 2]})
    plis = [np.array([0])]
    assert Decoupage(app, val, test, plis).groupes_partages() == {2, 3}
    assert Decoupage(app, val.iloc[1:], test.iloc[:1], plis).groupes_partages() == set()


# --------------------------------------------------------------------------- encodeur

CORPUS = [["a", "b", "a"], ["a", "c", "c"], ["b", "a"]]


def test_encodeur_apprend_le_vocabulaire_de_l_apprentissage_seul():
    """Vocabulaire par fréquence, seuil de fréquence, indice inverse, mots du test ignorés."""
    encodeur = Encodeur.apprendre(CORPUS)
    assert encodeur.vocabulaire == {"<pad>": 0, "<unk>": 1, "a": 2, "b": 3, "c": 4}

    corpus = [["a", "b", "a"], ["a", "c", "c"], ["b", "a", "d"]]
    assert Encodeur.apprendre(corpus).vocabulaire == {
        "<pad>": 0,
        "<unk>": 1,
        "a": 2,
        "b": 3,
        "c": 4,
    }
    assert Encodeur.apprendre(corpus, frequence_min=3).vocabulaire == {
        "<pad>": 0,
        "<unk>": 1,
        "a": 2,
    }
    assert Encodeur.apprendre(corpus, frequence_min=1).vocabulaire["d"] == 5

    assert Encodeur.apprendre(CORPUS).inverse == {0: "<pad>", 1: "<unk>", 2: "a", 3: "b", 4: "c"}

    apprentissage = [["a", "a", "b", "b"]]
    encodeur = Encodeur.apprendre(apprentissage)
    assert "z" not in encodeur.vocabulaire
    assert encodeur.encoder(["z"])[0] == 1


def test_encodeur_la_longueur_est_le_95e_centile_tronque():
    corpus = [["a"] * n for n in range(1, 101)]
    assert Encodeur.apprendre(corpus).longueur == 95
    assert Encodeur.apprendre(corpus, centile=50).longueur == 50
    assert Encodeur.apprendre(corpus, centile=100).longueur == 100
    petit = [["a"] * n for n in range(1, 11)]
    assert Encodeur.apprendre(petit).longueur == 9  # 9,55 tronqué, non arrondi


def test_encodeur_refuse_un_corpus_vide():
    with pytest.raises(ValueError) as erreur:
        Encodeur.apprendre([])
    assert str(erreur.value) == "impossible d'apprendre un vocabulaire sans aucun SMS"


def test_encoder_complete_remplace_et_tronque():
    """Complète par du remplissage, remplace un mot inconnu par <unk>, tronque."""
    encodeur = Encodeur({"<pad>": 0, "<unk>": 1, "a": 2, "b": 3}, longueur=4)
    assert encodeur.encoder(["a", "b"]) == [2, 3, 0, 0]
    assert encodeur.encoder([]) == [0, 0, 0, 0]

    encodeur = Encodeur({"<pad>": 0, "<unk>": 1, "a": 2}, longueur=3)
    assert encodeur.encoder(["a", "zzz", "a"]) == [2, 1, 2]

    encodeur = Encodeur({"<pad>": 0, "<unk>": 1, "a": 2, "b": 3}, longueur=2)
    assert encodeur.encoder(["b", "a", "b", "a"]) == [3, 2]
    assert encodeur.encoder(["b", "a"]) == [3, 2]


def test_encoder_laisse_la_liste_d_entree_inchangee():
    encodeur = Encodeur({"<pad>": 0, "<unk>": 1, "a": 2}, longueur=3)
    mots = ["a", "zzz"]
    avant = list(mots)
    encodeur.encoder(mots)
    assert mots == avant


def test_tenseurs_rend_entiers_et_cibles_reelles():
    encodeur = Encodeur({"<pad>": 0, "<unk>": 1, "a": 2, "b": 3}, longueur=3)
    jeu = pd.DataFrame({"mots": [["a", "b"], ["b", "zzz", "a", "a"]], "cible": [0, 1]})
    x, y = encodeur.tenseurs(jeu)
    assert x.dtype == torch.long
    assert x.tolist() == [[2, 3, 0], [3, 1, 2]]
    assert y.dtype == torch.float32
    assert y.tolist() == [0.0, 1.0]


def test_tenseurs_refuse_un_jeu_vide():
    encodeur = Encodeur({"<pad>": 0, "<unk>": 1}, longueur=3)
    jeu = pd.DataFrame({"mots": [], "cible": []})
    with pytest.raises(ValueError):
        encodeur.tenseurs(jeu)


def test_lire_saute_le_remplissage_mais_garde_le_mot_inconnu():
    encodeur = Encodeur({"<pad>": 0, "<unk>": 1, "a": 2, "b": 3}, longueur=4)
    assert encodeur.lire([2, 1, 0, 3]) == "a <unk> b"
    assert encodeur.lire(torch.tensor([3, 0, 0, 2])) == "b a"


# --------------------------------------------------------------------------- augmentation


class HasardScripte:
    """Faux générateur : rend des valeurs imposées et note ses appels."""

    def __init__(self, valeurs, indice=0):
        self.valeurs = list(valeurs)
        self.indice = indice
        self.appels_random = 0
        self.appels_randrange = []
        self.graines = []

    def random(self):
        self.appels_random += 1
        return self.valeurs.pop(0)

    def randrange(self, borne):
        self.appels_randrange.append(borne)
        return self.indice

    def seed(self, graine):
        self.graines.append(graine)


def test_augmenter_selon_les_tirages_imposes():
    """Avec des tirages imposés : mots supprimés, échange de deux voisins, remplissage ignoré."""
    augmenteur = Augmenteur(longueur=6, graine=0)
    augmenteur.hasard = HasardScripte([0.1, 0.11, 0.05, 0.99], indice=0)
    resultat = augmenteur.augmenter([5, 6, 7, 8, 0, 0])
    # 5 supprimé (0,1 n'est pas > 0,1), 6 gardé, 7 supprimé, 8 gardé ; puis échange des deux
    assert resultat == [8, 6, 0, 0, 0, 0]
    assert augmenteur.hasard.appels_randrange == [1]

    augmenteur = Augmenteur(longueur=5, graine=0)
    augmenteur.hasard = HasardScripte([0.9, 0.9], indice=0)
    assert augmenteur.augmenter([3, 4, 0, 0, 0]) == [4, 3, 0, 0, 0]
    assert augmenteur.hasard.appels_random == 2

    augmenteur = Augmenteur(longueur=4, graine=0)
    augmenteur.hasard = HasardScripte([0.9, 0.0])
    assert augmenteur.augmenter([7, 8, 0, 0]) == [7, 0, 0, 0]
    assert augmenteur.hasard.appels_randrange == []

    augmenteur = Augmenteur(longueur=3, graine=0)
    augmenteur.hasard = HasardScripte([0.9, 0.9], indice=0)
    assert augmenteur.augmenter([7, 8, 0]) == [8, 7, 0]
    assert augmenteur.hasard.appels_randrange == [1]


def test_augmenter_bornes_de_la_probabilite_et_longueur_fixe():
    """Probabilité nulle, probabilité un, longueur toujours fixe."""
    augmenteur = Augmenteur(longueur=5, graine=3)
    resultat = augmenteur.augmenter([2, 3, 4, 5, 6], p_suppression=0.0)
    assert sorted(resultat) == [2, 3, 4, 5, 6]

    augmenteur = Augmenteur(longueur=4, graine=3)
    assert augmenteur.augmenter([2, 3, 4, 5], p_suppression=1.0) == [0, 0, 0, 0]

    augmenteur = Augmenteur(longueur=5, graine=3)
    for _ in range(20):
        sortie = augmenteur.augmenter([2, 3, 4, 5, 6])
        assert len(sortie) == augmenteur.longueur
        assert sortie == sorted(sortie, key=lambda mot: mot == 0)  # mots d'abord, remplissage après


def test_augmenter_est_reproductible_par_graine():
    """Même graine, mêmes augmentations ; réinitialiser remet le hasard à l'état de la graine."""
    a, b = Augmenteur(5, graine=11), Augmenteur(5, graine=11)
    sequence = [2, 3, 4, 5, 6]
    assert [a.augmenter(sequence) for _ in range(5)] == [b.augmenter(sequence) for _ in range(5)]

    a = Augmenteur(5, graine=11)
    premiers = [a.augmenter([2, 3, 4, 5, 6]) for _ in range(4)]
    a.reinitialiser(11)
    assert [a.augmenter([2, 3, 4, 5, 6]) for _ in range(4)] == premiers
    a.reinitialiser(12)
    assert [a.augmenter([2, 3, 4, 5, 6]) for _ in range(4)] != premiers


def test_augmenter_lot_choisit_les_spams_a_modifier():
    """Seuls les spams sont touchés, selon la probabilité de choix (tous si elle vaut un)."""
    augmenteur = Augmenteur(longueur=3, graine=0)
    # pour chaque spam : un tirage de choix (0,0 < 0,5 : augmenter), trois tirages de mots (tous
    # gardés car 0,9 > 0,1), puis l'échange des deux premiers mots (indice 0)
    augmenteur.hasard = HasardScripte([0.0, 0.9, 0.9, 0.9, 0.0, 0.9, 0.9, 0.9], indice=0)
    x = torch.tensor([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
    y = torch.tensor([1.0, 0.0, 1.0])
    resultat = augmenteur.augmenter_lot(x, y)
    assert resultat.tolist() == [[2, 1, 3], [4, 5, 6], [8, 7, 9]]

    x = torch.tensor([[1, 2, 3], [4, 5, 6]])
    y = torch.tensor([1.0, 1.0])
    jamais = Augmenteur(longueur=3, graine=0)
    assert jamais.augmenter_lot(x, y, probabilite=0.0).tolist() == x.tolist()
    # un tirage égal à la probabilité ne déclenche pas l'augmentation (comparaison stricte)
    egal = Augmenteur(longueur=3, graine=0)
    egal.hasard = HasardScripte([0.5, 0.5])
    assert egal.augmenter_lot(x, y, probabilite=0.5).tolist() == x.tolist()
    assert egal.hasard.appels_random == 2

    augmenteur = Augmenteur(longueur=6, graine=5)
    x = torch.tensor([[1, 2, 3, 4, 5, 6]] * 4)
    y = torch.ones(4)
    sortie = augmenteur.augmenter_lot(x, y, probabilite=1.0)
    assert all(ligne.tolist() != [1, 2, 3, 4, 5, 6] for ligne in sortie)


def test_augmenter_lot_laisse_le_lot_d_entree_inchange():
    augmenteur = Augmenteur(longueur=4, graine=5)
    x = torch.tensor([[1, 2, 3, 4], [5, 6, 7, 8]])
    y = torch.tensor([1.0, 1.0])
    avant = x.clone()
    sortie = augmenteur.augmenter_lot(x, y, probabilite=1.0)
    assert torch.equal(x, avant)
    assert sortie is not x




# --------------------------------------------------------------------------- entrées refusées
# Chaque test ci-dessous porte sur une entrée interdite ou absurde : elle doit être refusée par
# une exception, ou laisser les données de l'appelant exactement comme elles étaient.


def test_charger_sms_refuse_un_fichier_absent(tmp_path):
    with pytest.raises(FileNotFoundError):
        charger_sms(tmp_path / "absent.csv")


def test_charger_sms_refuse_un_fichier_vide(tmp_path):
    vide = tmp_path / "vide.csv"
    vide.write_bytes(b"")
    with pytest.raises(pd.errors.EmptyDataError):
        charger_sms(vide)


@pytest.mark.parametrize("pas_du_texte", [None, 12, 3.5])
def test_normaliser_refuse_ce_qui_n_est_pas_du_texte(pas_du_texte):
    with pytest.raises(AttributeError):
        normaliser(pas_du_texte)


@pytest.mark.parametrize("pas_du_texte", [None, 12, ["free"]])
def test_decouper_mots_refuse_ce_qui_n_est_pas_du_texte(pas_du_texte):
    with pytest.raises(TypeError):
        decouper_mots(pas_du_texte)


def test_matrice_presence_refuse_un_sms_qui_n_est_pas_une_suite_de_mots():
    with pytest.raises(TypeError):
        matrice_presence([["a", "b"], 7])


def test_matrice_presence_laisse_les_mots_d_entree_inchanges():
    mots = [["a", "b", "a"], ["b", "c"]]
    avant = [list(sms) for sms in mots]
    matrice_presence(mots)
    assert mots == avant


def test_grouper_refuse_un_seuil_qui_n_est_pas_un_nombre():
    with pytest.raises(TypeError):
        grouper_quasi_doublons(matrice_presence(SMS_CHAINE), seuil="0.8")


def test_grouper_laisse_la_matrice_de_presence_inchangee():
    presence = matrice_presence(SMS_CHAINE)
    avant = presence.matrice.toarray().copy()
    tailles_avant = presence.tailles.copy()
    grouper_quasi_doublons(presence)
    assert np.array_equal(presence.matrice.toarray(), avant)
    assert np.array_equal(presence.tailles, tailles_avant)


def test_similarites_refuse_un_sms_hors_de_la_matrice():
    presence = matrice_presence(SMS_CHAINE)
    with pytest.raises(IndexError):
        similarites(presence, np.array([0]), np.array([len(SMS_CHAINE)]))


def test_decouper_refuse_un_jeu_sans_colonne_groupe():
    with pytest.raises(KeyError):
        decouper_par_groupes(jeu_groupe().drop(columns="groupe"), graine=7)


def test_decouper_refuse_plus_de_plis_que_de_groupes():
    with pytest.raises(ValueError):
        decouper_par_groupes(jeu_groupe(nb_groupes=3), graine=7, nb_plis=7)


def test_groupes_partages_laisse_les_jeux_inchanges():
    app = pd.DataFrame({"groupe": [1, 2, 3]})
    val = pd.DataFrame({"groupe": [3, 4]})
    test = pd.DataFrame({"groupe": [5, 2]})
    avant = (app.copy(), val.copy(), test.copy())
    Decoupage(app, val, test, [np.array([0])]).groupes_partages()
    for jeu, copie in zip((app, val, test), avant, strict=True):
        pd.testing.assert_frame_equal(jeu, copie)


def test_encodeur_apprendre_laisse_le_corpus_inchange():
    corpus = [["a", "b", "a"], ["a", "c", "c"], ["b", "a"]]
    avant = [list(sms) for sms in corpus]
    Encodeur.apprendre(corpus)
    assert corpus == avant


def test_lire_refuse_un_indice_inconnu_du_vocabulaire():
    encodeur = Encodeur({"<pad>": 0, "<unk>": 1, "a": 2}, longueur=3)
    with pytest.raises(KeyError):
        encodeur.lire([2, 99])


def test_tenseurs_refuse_un_jeu_sans_colonne_mots():
    encodeur = Encodeur({"<pad>": 0, "<unk>": 1, "a": 2}, longueur=3)
    with pytest.raises(KeyError):
        encodeur.tenseurs(pd.DataFrame({"cible": [0, 1]}))


def test_tenseurs_laisse_le_jeu_inchange():
    encodeur = Encodeur({"<pad>": 0, "<unk>": 1, "a": 2}, longueur=3)
    jeu = pd.DataFrame({"mots": [["a", "zzz"], ["a"]], "cible": [1, 0]})
    avant = jeu.copy(deep=True)
    encodeur.tenseurs(jeu)
    pd.testing.assert_frame_equal(jeu, avant)


def test_augmenter_refuse_une_sequence_absente():
    with pytest.raises(TypeError):
        Augmenteur(longueur=3, graine=0).augmenter(None)


def test_augmenter_laisse_la_sequence_d_entree_inchangee():
    sequence = [2, 3, 4, 0, 0]
    Augmenteur(longueur=5, graine=1).augmenter(sequence, p_suppression=1.0)
    assert sequence == [2, 3, 4, 0, 0]


def test_augmenter_lot_probabilite_de_choix_par_defaut_un_demi():
    augmenteur = Augmenteur(longueur=3, graine=0)
    augmenteur.hasard = HasardScripte([0.7])  # 0,7 > 0,5 : le spam n'est pas choisi
    x = torch.tensor([[1, 2, 3]])
    assert augmenteur.augmenter_lot(x, torch.ones(1)).tolist() == [[1, 2, 3]]
    assert inspect.signature(Augmenteur.augmenter).parameters["p_suppression"].default == 0.1


def test_augmenter_lot_refuse_plus_de_cibles_que_de_messages():
    augmenteur = Augmenteur(longueur=3, graine=0)
    x = torch.tensor([[1, 2, 3], [4, 5, 6]])
    with pytest.raises(IndexError):
        augmenteur.augmenter_lot(x, torch.ones(3), probabilite=1.0)


def test_augmenteur_refuse_une_graine_qui_n_est_pas_un_entier():
    with pytest.raises(TypeError):
        Augmenteur(longueur=3, graine=[1])
    augmenteur = Augmenteur(longueur=3, graine=0)
    with pytest.raises(TypeError):
        augmenteur.reinitialiser([1])


def test_charger_sms_laisse_le_fichier_d_origine_inchange(tmp_path):
    csv = ecrire_csv(tmp_path / "spam.csv", ["ham,Bonjour,,,", "spam,x,y,,z"])
    avant = csv.read_bytes()
    charger_sms(csv)
    assert csv.read_bytes() == avant


def test_normaliser_refuse_un_texte_en_octets():
    with pytest.raises(TypeError):
        normaliser(b"free entry")


def test_encoder_refuse_un_mot_qui_n_est_pas_un_texte_simple():
    encodeur = Encodeur({"<pad>": 0, "<unk>": 1, "a": 2}, longueur=3)
    with pytest.raises(TypeError):
        encodeur.encoder([["a"]])  # une liste n'est pas hachable : ce n'est pas un mot


def test_similarites_laisse_la_matrice_de_presence_inchangee():
    presence = matrice_presence(SMS_CHAINE)
    avant = presence.matrice.toarray().copy()
    similarites(presence, np.array([0, 3]), np.array([1, 2]))
    assert np.array_equal(presence.matrice.toarray(), avant)
