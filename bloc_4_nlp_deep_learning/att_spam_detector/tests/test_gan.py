# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests de `att_spam.gan` : vocabulaire, générateur, discriminateur, masquage, qualité."""

import collections
import re

import numpy as np
import pytest
import torch
from torch import nn

from att_spam import gan
from att_spam.gan import (
    Discriminateur,
    Generateur,
    VocabulaireGAN,
    compter_mots,
    distinct,
    entrainer_adverse,
    forme_phrases,
    generer,
    masquer_apres_fin,
    melanger,
    mesurer,
    pre_entrainer,
    quadrigrammes,
    suites,
    tirer_independamment,
)

V, L = 8, 6  # mini-vocabulaire et longueur de phrase des modèles de test


def petit_generateur(graine=0):
    torch.manual_seed(graine)
    return Generateur(V, L, dim_bruit=4, dim=5, cache=7)


def petit_discriminateur(graine=0):
    torch.manual_seed(graine)
    return Discriminateur(V, dim=5, filtres=4)


def reels_jouets(n=40, graine=0):
    """Spams jouets : mots 3 à 7, un <fin> (2) en position 4, puis du remplissage."""
    hasard = torch.Generator().manual_seed(graine)
    reels = torch.randint(3, V, (n, L), generator=hasard)
    reels[:, 4] = 2
    reels[:, 5] = 0
    return reels


def en_one_hot(indices):
    return nn.functional.one_hot(torch.tensor(indices), V).float()


# --------------------------------------------------------------------------- vocabulaire

SPAMS = [["a", "b", "a"], ["a", "c"], ["b", "d"]]


def test_vocabulaire_gan_frequences_et_indices():
    """Fréquences, seuil réglable, indice inverse et constantes réservées du vocabulaire du GAN."""
    compteur = compter_mots(SPAMS)
    assert list(compteur.items()) == [("a", 3), ("b", 2), ("c", 1), ("d", 1)]

    vocabulaire = VocabulaireGAN.apprendre(SPAMS)
    assert vocabulaire.mot_vers_indice == {"<pad>": 0, "<debut>": 1, "<fin>": 2, "a": 3, "b": 4}
    assert vocabulaire.longueur == 25
    assert vocabulaire.taille == len(vocabulaire.mot_vers_indice)

    tout = VocabulaireGAN.apprendre(SPAMS, frequence_min=1)
    assert tout.mot_vers_indice == {
        "<pad>": 0, "<debut>": 1, "<fin>": 2, "a": 3, "b": 4, "c": 5, "d": 6,
    }  # fmt: skip
    assert list(VocabulaireGAN.apprendre(SPAMS, frequence_min=3).mot_vers_indice) == [
        "<pad>", "<debut>", "<fin>", "a",
    ]  # fmt: skip

    inverse = VocabulaireGAN.apprendre(SPAMS, longueur=4).inverse
    assert inverse == {0: "<pad>", 1: "<debut>", 2: "<fin>", 3: "a", 4: "b"}

    assert (gan.PAD, gan.DEBUT, gan.FIN) == (0, 1, 2)


def test_vocabulaire_gan_encode_les_spams():
    """Termine par <fin>, ignore les mots rares, complète, tronque en gardant la place du <fin>."""
    vocabulaire = VocabulaireGAN.apprendre(SPAMS, longueur=6)
    assert vocabulaire.encoder(["a", "zzz", "b"]) == [3, 4, 2, 0, 0, 0]
    assert vocabulaire.encoder([]) == [2, 0, 0, 0, 0, 0]

    vocabulaire = VocabulaireGAN.apprendre(SPAMS, longueur=4)
    assert vocabulaire.encoder(["a", "b", "a"]) == [3, 4, 3, 2]  # trois mots + <fin> : juste
    assert vocabulaire.encoder(["a", "b", "a", "b", "a"]) == [3, 4, 3, 2]

    vocabulaire = VocabulaireGAN.apprendre(SPAMS, longueur=5)
    tenseur = vocabulaire.tenseur([["a", "b"], ["b"]])
    assert tenseur.dtype == torch.long
    assert tenseur.tolist() == [[3, 4, 2, 0, 0], [4, 2, 0, 0, 0]]


def test_vocabulaire_laisse_les_spams_d_entree_inchanges():
    spams = [["a", "b", "a"], ["a", "c"]]
    avant = [list(m) for m in spams]
    vocabulaire = VocabulaireGAN.apprendre(spams, longueur=3)
    vocabulaire.tenseur(spams)
    assert spams == avant


# --------------------------------------------------------------------------- générateur


def test_generateur_structure_et_etat_initial():
    """Structure par défaut ; l'état initial vient du bruit, la mémoire est nulle."""
    modele = Generateur(50, 25)
    assert modele.dim_bruit == 32
    assert modele.depuis_bruit.in_features == 32
    assert modele.depuis_bruit.out_features == 128
    assert modele.plongement.num_embeddings == 50
    assert modele.plongement.embedding_dim == 64
    assert modele.cellule.input_size == 64
    assert modele.cellule.hidden_size == 128
    assert modele.sortie.in_features == 128
    assert modele.sortie.out_features == 50
    assert (modele.taille_vocab, modele.longueur) == (50, 25)

    modele = petit_generateur()
    torch.manual_seed(7)
    h, c = modele.etat_initial(3)
    torch.manual_seed(7)
    attendu = torch.tanh(modele.depuis_bruit(torch.randn(3, 4)))
    assert torch.equal(h, attendu)
    assert c.tolist() == [[0.0] * 7] * 3
    assert h.abs().max().item() < 1.0  # tanh


def test_vraisemblance_est_l_entropie_croisee_du_mot_suivant_sans_compter_le_remplissage():
    modele = petit_generateur()
    cibles = torch.tensor([[3, 4, 2, 0, 0, 0], [5, 6, 7, 3, 4, 2]])
    torch.manual_seed(11)
    perte = modele.vraisemblance(cibles)
    # même calcul à la main : entrée = <debut> puis les cibles décalées, bruit identique
    torch.manual_seed(11)
    h, c = modele.etat_initial(2)
    entrees = torch.cat([torch.ones(2, 1, dtype=torch.long), cibles[:, :-1]], dim=1)
    log_probas = []
    for t in range(L):
        h, c = modele.cellule(modele.plongement(entrees[:, t]), (h, c))
        log_probas.append(torch.log_softmax(modele.sortie(h), dim=-1))
    log_probas = torch.stack(log_probas, dim=1)
    pris = log_probas.gather(2, cibles.unsqueeze(-1)).squeeze(-1)
    reel = cibles != 0
    attendu = -(pris * reel).sum() / reel.sum()
    assert perte.item() == pytest.approx(attendu.item(), rel=1e-5)


def test_echantillonner_phrases_one_hot_reproductibles():
    """Phrases one-hot, reproductibles sous la même graine, recalculées pas à pas."""
    modele = petit_generateur()
    torch.manual_seed(1)
    phrases = modele.echantillonner(5, 1.0)
    assert phrases.shape == (5, L, V)
    assert set(phrases.unique().tolist()) == {0.0, 1.0}
    assert phrases.sum(dim=-1).unique().tolist() == [1.0]

    modele = petit_generateur()
    torch.manual_seed(1)
    a = modele.echantillonner(5, 0.5)
    torch.manual_seed(1)
    b = modele.echantillonner(5, 0.5)
    torch.manual_seed(2)
    c = modele.echantillonner(5, 0.5)
    assert torch.equal(a, b)
    assert not torch.equal(a, c)

    modele = petit_generateur()
    torch.manual_seed(5)
    obtenu = modele.echantillonner(3, 0.7)
    torch.manual_seed(5)
    h, c = modele.etat_initial(3)
    entree = modele.plongement(torch.ones(3, dtype=torch.long))
    mots = []
    for _ in range(L):
        h, c = modele.cellule(entree, (h, c))
        mot = nn.functional.gumbel_softmax(modele.sortie(h), tau=0.7, hard=True, dim=-1)
        mots.append(mot)
        entree = mot @ modele.plongement.weight
    assert torch.equal(obtenu, torch.stack(mots, dim=1))


def test_echantillonner_gradient_straight_through_et_temperature():
    """Le gradient passe malgré les mots durs ; la température le modifie."""
    modele = petit_generateur()
    torch.manual_seed(1)
    phrases = modele.echantillonner(6, 1.0)
    poids = torch.randn_like(phrases)
    (phrases * poids).sum().backward()
    gradient = modele.sortie.weight.grad
    assert gradient is not None
    assert gradient.abs().sum().item() > 0.0

    def gradient(temperature):
        modele = petit_generateur()
        torch.manual_seed(1)
        phrases = modele.echantillonner(6, temperature)
        torch.manual_seed(2)
        (phrases * torch.randn_like(phrases)).sum().backward()
        return modele.sortie.weight.grad

    assert not torch.allclose(gradient(1.0), gradient(0.3))


# --------------------------------------------------------------------------- discriminateur


def test_discriminateur_structure_score_et_dropout():
    """Structure par défaut, un score par phrase one-hot, dropout en entraînement seulement."""
    modele = Discriminateur(50)
    assert modele.projection.in_features == 50
    assert modele.projection.out_features == 64
    assert modele.projection.bias is None
    assert [c.kernel_size for c in modele.convolutions] == [(3,), (4,), (5,)]
    assert [c.out_channels for c in modele.convolutions] == [64, 64, 64]
    assert modele.abandon.p == 0.3
    assert modele.sortie.in_features == 3 * 64
    assert modele.sortie.out_features == 1

    modele = petit_discriminateur().eval()
    phrases = en_one_hot([[3, 4, 5, 6, 7, 3], [7, 7, 6, 6, 5, 5]]).reshape(2, L, V)
    assert modele(phrases).shape == (2,)
    assert modele(phrases[:1]).shape == (1,)  # une seule phrase : un vecteur, pas un scalaire
    h = modele.projection(phrases).transpose(1, 2)
    attendu = modele.sortie(
        torch.cat([torch.relu(c(h)).amax(dim=2) for c in modele.convolutions], dim=1)
    ).squeeze(1)
    assert modele(phrases).tolist() == pytest.approx(attendu.tolist(), abs=1e-6)
    colonne = modele.projection.weight[:, 3]  # le mot 3 en one-hot sélectionne la colonne 3
    assert modele.projection(en_one_hot([3])[0]).tolist() == pytest.approx(colonne.tolist())

    modele = petit_discriminateur()
    phrases = en_one_hot([[3, 4, 5, 6, 7, 3]] * 4).reshape(4, L, V)
    modele.eval()
    assert torch.equal(modele(phrases), modele(phrases))
    modele.train()
    assert not torch.equal(modele(phrases), modele(phrases))


# --------------------------------------------------------------------------- masquage


def test_masquer_apres_fin_remplace_la_queue_et_garde_le_gradient():
    """La queue après le premier <fin> devient du remplissage ; valeurs souples et gradient
    conservés avant."""
    phrases = en_one_hot(
        [[5, 2, 7, 2, 3, 6], [4, 5, 6, 7, 3, 4], [2, 4, 4, 3, 3, 3], [3, 4, 5, 6, 7, 2]]
    )
    masque = masquer_apres_fin(phrases.reshape(4, L, V))
    attendu = en_one_hot(
        [[5, 2, 0, 0, 0, 0], [4, 5, 6, 7, 3, 4], [2, 0, 0, 0, 0, 0], [3, 4, 5, 6, 7, 2]]
    )
    assert torch.equal(masque, attendu.reshape(4, L, V))

    phrases = torch.tensor(
        [[[0.2, 0.1, 0.1, 0.6], [0.0, 0.1, 0.8, 0.1], [0.1, 0.2, 0.3, 0.4], [0.4, 0.3, 0.2, 0.1]]]
    )  # le deuxième mot est <fin> (indice 2 : le plus probable)
    masque = masquer_apres_fin(phrases)
    attendu = torch.tensor(
        [[[0.2, 0.1, 0.1, 0.6], [0.0, 0.1, 0.8, 0.1], [1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]]]
    )
    assert torch.equal(masque, attendu)

    phrases = en_one_hot([[5, 2, 7, 3, 3, 3]]).reshape(1, L, V).requires_grad_(True)
    poids = torch.arange(1.0, L * V + 1).reshape(1, L, V)
    (masquer_apres_fin(phrases) * poids).sum().backward()
    garde = torch.tensor([1.0, 1.0, 0.0, 0.0, 0.0, 0.0]).reshape(1, L, 1)
    assert torch.equal(phrases.grad, poids * garde)


def test_masquer_apres_fin_laisse_la_phrase_d_entree_inchangee():
    phrases = en_one_hot([[5, 2, 7, 3, 3, 3]]).reshape(1, L, V)
    avant = phrases.clone()
    masquer_apres_fin(phrases)
    assert torch.equal(phrases, avant)


# --------------------------------------------------------------------------- pré-entraînement


class Espion:
    """Remplace une fonction en gardant la trace de ses appels, puis l'appelle."""

    def __init__(self, reelle):
        self.reelle = reelle
        self.appels = []

    def __call__(self, *args, **options):
        self.appels.append((args, options))
        return self.reelle(*args, **options)


def test_pre_entrainer_baisse_la_perte_et_reste_reproductible():
    """La perte baisse, zéro époque ne fait rien, une époque donne une perte, même graine même
    résultat."""
    modele = petit_generateur()
    epoques = 6
    pertes = pre_entrainer(modele, reels_jouets(), epoques=epoques, bavard=False)
    assert len(pertes) == epoques
    assert all(isinstance(p, float) for p in pertes)
    assert pertes[-1] < pertes[0]

    modele = petit_generateur()
    avant = [p.clone() for p in modele.parameters()]
    assert pre_entrainer(modele, reels_jouets(), epoques=0) == []
    assert all(torch.equal(p, q) for p, q in zip(modele.parameters(), avant, strict=True))

    assert len(pre_entrainer(petit_generateur(), reels_jouets(), epoques=1, bavard=False)) == 1

    def pertes():
        torch.manual_seed(3)
        return pre_entrainer(petit_generateur(), reels_jouets(), epoques=3, bavard=False)

    assert pertes() == pertes()


def test_pre_entrainer_lots_de_32_moyenne_ponderee_et_reglages(monkeypatch):
    """Lots de 32 sans répétition, moyenne pondérée par la taille des lots, norme 5 et Adam à un
    millième."""
    modele = petit_generateur()
    reels = torch.arange(70).reshape(70, 1).repeat(1, L)
    lots = []

    def vraisemblance(lot):
        lots.append(lot.clone())
        return modele.depuis_bruit.weight.sum() * 0.0 + 1.0

    modele.vraisemblance = vraisemblance
    pre_entrainer(modele, reels, epoques=1, bavard=False)
    assert [lot.shape[0] for lot in lots] == [32, 32, 6]
    vus = torch.cat([lot[:, 0] for lot in lots]).tolist()
    assert sorted(vus) == list(range(70))
    assert vus != list(range(70))  # ordre mélangé à chaque époque

    modele = petit_generateur()
    reels = torch.arange(70).reshape(70, 1).repeat(1, L)
    valeurs = iter([1.0, 2.0, 4.0])

    def vraisemblance(lot):
        return modele.depuis_bruit.weight.sum() * 0.0 + next(valeurs)

    modele.vraisemblance = vraisemblance
    pertes = pre_entrainer(modele, reels, epoques=1, bavard=False)
    assert pertes == pytest.approx([(32 * 1.0 + 32 * 2.0 + 6 * 4.0) / 70])

    espion_norme = Espion(nn.utils.clip_grad_norm_)
    espion_adam = Espion(torch.optim.Adam)
    monkeypatch.setattr(nn.utils, "clip_grad_norm_", espion_norme)
    monkeypatch.setattr(torch.optim, "Adam", espion_adam)
    modele = petit_generateur()
    pre_entrainer(modele, reels_jouets(40), epoques=1, bavard=False)
    assert [options for _, options in espion_adam.appels] == [{"lr": 1e-3}]
    assert [args[1] for args, _ in espion_norme.appels] == [5.0, 5.0]  # 40 spams : lots de 32 et 8


def test_pre_entrainer_affiche_toutes_les_dix_epoques_si_bavard(capsys):
    """Une ligne toutes les dix époques ; rien si `bavard` est faux."""
    pertes = pre_entrainer(petit_generateur(), reels_jouets(8), epoques=20)
    attendu = [
        f"Pré-entraînement, époque {e} : perte de vraisemblance {pertes[e - 1]:.3f}"
        for e in (10, 20)
    ]
    assert capsys.readouterr().out.splitlines() == attendu

    pre_entrainer(petit_generateur(), reels_jouets(8), epoques=10, bavard=False)
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- entraînement adverse


class DiscriminateurScripte:
    """Remplace `forward` du discriminateur : scores imposés, entrées conservées."""

    def __init__(self, discriminateur, scores):
        self.parametre = next(discriminateur.parameters())
        self.scores = scores  # liste de tenseurs, un par appel, répétée à chaque itération
        self.entrees = []

    def __call__(self, phrases):
        rang = len(self.entrees) % len(self.scores)
        self.entrees.append(phrases)
        return self.parametre.sum() * 0.0 + self.scores[rang]


def scores_de_l_adversaire():
    """Scores imposés au discriminateur : vrais (mi-positifs), faux détachés, faux du pas de G.

    Les valeurs 0,5, -0,5 et 0 séparent « > 0 » de « >= 0 » et de « > 1 », et « < 0 » de « <= 0 »
    et de « < 1 » : exactitude de D = (16/32 + 16/32) / 2 = 0,5.
    """
    return [
        torch.cat([torch.full((16,), 0.5), torch.full((8,), -0.5), torch.zeros(8)]),
        torch.cat([torch.full((16,), -0.5), torch.full((8,), 0.5), torch.zeros(8)]),
        torch.full((32,), 0.5),
    ]


def adversaire_scripte(**options):
    generateur, discriminateur = petit_generateur(), petit_discriminateur()
    scores = scores_de_l_adversaire()
    discriminateur.forward = DiscriminateurScripte(discriminateur, scores)
    journal = entrainer_adverse(generateur, discriminateur, reels_jouets(), **options)
    return generateur, discriminateur, journal, scores


def test_entrainer_adverse_journal_mises_a_jour_et_graine():
    """Une valeur par itération, deux itérations suffisent, les deux réseaux changent, même graine
    même journal."""
    iterations = 5
    journal = entrainer_adverse(
        petit_generateur(), petit_discriminateur(), reels_jouets(), iterations=iterations,
        bavard=False,
    )
    assert set(journal) == {"perte_d", "perte_g_adverse", "perte_g_mv", "precision_d"}
    assert all(len(serie) == iterations for serie in journal.values())
    assert all(np.isfinite(v) for serie in journal.values() for v in serie)

    iterations = 2
    journal = entrainer_adverse(
        petit_generateur(), petit_discriminateur(), reels_jouets(), iterations=iterations,
        bavard=False,
    )
    assert len(journal["perte_d"]) == iterations

    generateur, discriminateur = petit_generateur(), petit_discriminateur()
    g_avant = [p.clone() for p in generateur.parameters()]
    d_avant = [p.clone() for p in discriminateur.parameters()]
    entrainer_adverse(generateur, discriminateur, reels_jouets(), iterations=3, bavard=False)
    assert any(not torch.equal(p, q) for p, q in zip(generateur.parameters(), g_avant, strict=True))
    assert any(
        not torch.equal(p, q) for p, q in zip(discriminateur.parameters(), d_avant, strict=True)
    )

    def journal():
        torch.manual_seed(4)
        return entrainer_adverse(
            petit_generateur(), petit_discriminateur(), reels_jouets(), iterations=3, bavard=False
        )

    assert journal() == journal()


def test_entrainer_adverse_pertes_cibles_et_entrees_du_discriminateur():
    """Pertes avec les bonnes cibles ; le discriminateur voit des vrais puis des faux masqués."""
    _, _, journal, scores = adversaire_scripte(iterations=3, bavard=False)
    bce = nn.BCEWithLogitsLoss()
    perte_d = bce(scores[0], torch.ones(32)) + bce(scores[1], torch.zeros(32))
    assert journal["perte_d"] == pytest.approx([perte_d.item()] * 3)
    assert journal["perte_g_adverse"] == pytest.approx([bce(scores[2], torch.ones(32)).item()] * 3)
    # exactitude de D : la moitié des vrais reconnus (0,5) et la moitié des faux reconnus (0,5)
    assert journal["precision_d"] == pytest.approx([0.5] * 3)

    _, discriminateur, _, _ = adversaire_scripte(iterations=2, bavard=False)
    vrais, faux_detaches, faux = discriminateur.forward.entrees[:3]  # premier tour de boucle
    assert vrais.shape == (32, L, V)
    assert vrais.sum(dim=-1).unique().tolist() == [1.0]
    assert set(vrais.argmax(dim=-1)[:, 4].tolist()) == {2}  # <fin> en position 4 dans les vrais
    assert faux_detaches.requires_grad is False  # D seul est mis à jour avec les faux détachés
    assert faux.requires_grad is True  # le pas de G dépend des faux
    for phrases in (faux_detaches, faux):
        indices = phrases.argmax(dim=-1)
        apres_fin = (indices == 2).long().cumsum(dim=1) - (indices == 2).long() > 0
        assert (indices[apres_fin] == 0).all()  # rien après le premier <fin>
    assert torch.equal(faux_detaches, faux.detach())


def test_entrainer_adverse_le_pas_du_generateur_fait_baisser_sa_perte_de_vraisemblance():
    """Le générateur descend la somme « perte adverse + vraisemblance ». Avec un discriminateur
    scripté (scores imposés, donc aucun gradient vers le générateur), seul le terme de
    vraisemblance le guide : sa perte sur les vrais spams doit baisser, pas monter."""
    reels = reels_jouets()
    generateur, discriminateur = petit_generateur(), petit_discriminateur()
    discriminateur.forward = DiscriminateurScripte(discriminateur, scores_de_l_adversaire())
    torch.manual_seed(1)  # même bruit initial pour les deux mesures
    avant = generateur.vraisemblance(reels).item()
    entrainer_adverse(generateur, discriminateur, reels, iterations=30, bavard=False)
    torch.manual_seed(1)
    apres = generateur.vraisemblance(reels).item()
    assert apres < avant


def test_entrainer_adverse_les_vrais_ont_la_taille_du_vocabulaire_meme_sans_le_dernier_mot():
    """Le one-hot des vrais compte V colonnes, même si l'indice V - 1 ne figure dans aucun lot."""
    reels = reels_jouets()
    reels[reels == V - 1] = 3  # le dernier indice du vocabulaire n'apparaît plus nulle part
    generateur, discriminateur = petit_generateur(), petit_discriminateur()
    discriminateur.forward = DiscriminateurScripte(discriminateur, scores_de_l_adversaire())
    entrainer_adverse(generateur, discriminateur, reels, iterations=2, bavard=False)
    assert discriminateur.forward.entrees[0].shape == (32, L, V)


def test_entrainer_adverse_temperature_lots_et_reglages(monkeypatch):
    """Température de 1 à 0,3 par lots de 32, vraisemblance sur 32 vrais, Adam à 1e-4, norme 5."""
    generateur = petit_generateur()
    espion = Espion(generateur.echantillonner)
    monkeypatch.setattr(generateur, "echantillonner", espion)
    entrainer_adverse(
        generateur, petit_discriminateur(), reels_jouets(), iterations=4, bavard=False
    )
    tailles = [args[0] for args, _ in espion.appels]
    temperatures = [args[1] for args, _ in espion.appels]
    assert tailles == [32, 32, 32, 32]
    assert temperatures == pytest.approx([1.0, 1 - 0.7 / 3, 1 - 1.4 / 3, 0.3])

    generateur = petit_generateur()
    espion = Espion(generateur.vraisemblance)
    monkeypatch.setattr(generateur, "vraisemblance", espion)
    entrainer_adverse(
        generateur, petit_discriminateur(), reels_jouets(), iterations=2, bavard=False
    )
    assert [args[0].shape for args, _ in espion.appels] == [(32, L), (32, L)]

    espion_adam = Espion(torch.optim.Adam)
    espion_norme = Espion(nn.utils.clip_grad_norm_)
    monkeypatch.setattr(torch.optim, "Adam", espion_adam)
    monkeypatch.setattr(nn.utils, "clip_grad_norm_", espion_norme)
    entrainer_adverse(
        petit_generateur(), petit_discriminateur(), reels_jouets(), iterations=3, bavard=False
    )
    assert [options for _, options in espion_adam.appels] == [{"lr": 1e-4}, {"lr": 1e-4}]
    assert [args[1] for args, _ in espion_norme.appels] == [5.0, 5.0, 5.0]


def test_entrainer_adverse_affiche_toutes_les_cinquante_iterations_si_bavard(capsys):
    """Une ligne toutes les 50 itérations ; rien si `bavard` est faux."""
    _, _, journal, _ = adversaire_scripte(iterations=100)
    lignes = capsys.readouterr().out.splitlines()
    assert len(lignes) == len(range(50, 101, 50))
    bce = nn.BCEWithLogitsLoss()
    vrais, faux_detaches, faux = scores_de_l_adversaire()
    perte_d = (bce(vrais, torch.ones(32)) + bce(faux_detaches, torch.zeros(32))).item()
    adverse = bce(faux, torch.ones(32)).item()
    for ligne, iteration, temperature in zip(lignes, (50, 100), (0.65, 0.30), strict=True):
        motif = (
            rf"itération {iteration} \| température {temperature:.2f} \| perte D {perte_d:.3f} "
            rf"\| perte G adverse {adverse:.3f} \| vraisemblance \d+\.\d{{3}} "
            r"\| exactitude de D 0\.50"
        )
        assert re.fullmatch(motif, ligne), ligne
    assert journal["precision_d"][0] == pytest.approx(0.5)

    adversaire_scripte(iterations=100, bavard=False)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("iterations", [1, 0, -3])
def test_entrainer_adverse_refuse_moins_de_deux_iterations(iterations):
    generateur = petit_generateur()
    avant = [p.clone() for p in generateur.parameters()]
    with pytest.raises(ValueError) as erreur:
        entrainer_adverse(
            generateur, petit_discriminateur(), reels_jouets(), iterations=iterations
        )
    assert str(erreur.value) == "le recuit de la température demande au moins 2 itérations"
    assert all(torch.equal(p, q) for p, q in zip(generateur.parameters(), avant, strict=True))


# --------------------------------------------------------------------------- formes et génération


def generateur_scripte(monkeypatch, phrases_indices):
    """Générateur dont `echantillonner` rend des phrases one-hot imposées, avec trace des appels."""
    generateur = petit_generateur()
    appels = []

    def echantillonner(n, temperature):
        appels.append((n, temperature))
        return en_one_hot(phrases_indices).reshape(len(phrases_indices), L, V)

    monkeypatch.setattr(generateur, "echantillonner", echantillonner)
    return generateur, appels


PHRASES = [
    [3, 2, 4, 4, 0, 0],  # <fin> en 2e position ; 4 positions après, 2 occupées
    [3, 4, 5, 6, 7, 3],  # aucun <fin> : exclue des mesures « après <fin> »
    [2, 0, 0, 0, 0, 0],  # <fin> d'emblée ; 5 positions après, aucune occupée
    [4, 2, 2, 5, 0, 0],  # deux <fin> ; 4 positions strictement après le premier, 2 occupées
]


def test_forme_phrases_un_seul_mot_apres_fin_ne_se_divise_pas_par_deux(monkeypatch):
    generateur, _ = generateur_scripte(monkeypatch, [[3, 4, 5, 6, 2, 7]])
    part_avec_fin, part_occupee = forme_phrases(generateur)
    assert part_avec_fin == 1.0
    assert part_occupee == 1.0  # 1 position après <fin>, occupée


def test_pre_entrainer_fait_par_defaut_quarante_epoques():
    pertes = pre_entrainer(petit_generateur(), reels_jouets(8), bavard=False)
    assert pertes[39:] != []  # une quarantième époque a eu lieu
    assert pertes[40:] == []  # et pas de quarante et unième


def test_entrainer_adverse_fait_par_defaut_trois_cents_iterations():
    _, _, journal, _ = adversaire_scripte(bavard=False)
    assert journal["perte_d"][299:] != []
    assert journal["perte_d"][300:] == []


def test_forme_phrases_indices_effectif_et_mode(monkeypatch):
    """Indices de forme avant masquage, effectif et température transmis, mode rétabli."""
    generateur, _ = generateur_scripte(monkeypatch, PHRASES)
    part_avec_fin, part_occupee = forme_phrases(generateur)
    assert part_avec_fin == pytest.approx(3 / 4)
    assert part_occupee == pytest.approx(4 / 13)

    generateur, _ = generateur_scripte(monkeypatch, [[3, 4, 5, 6, 7, 3], [4, 4, 4, 4, 4, 4]])
    assert forme_phrases(generateur) == (0.0, 0.0)

    generateur, appels = generateur_scripte(monkeypatch, PHRASES)
    generateur.eval()
    forme_phrases(generateur)
    forme_phrases(generateur, n=7, temperature=0.9)
    assert appels == [(500, 0.3), (7, 0.9)]
    assert generateur.training is True


def test_generer_decode_les_phrases_jusqu_au_premier_fin(monkeypatch):
    """S'arrête au premier <fin>, ignore remplissage et <debut> ; reproductible par graine."""
    generateur, appels = generateur_scripte(
        monkeypatch, [[3, 1, 4, 0, 2, 5], [2, 3, 3, 3, 3, 3], [6, 7, 0, 0, 0, 0]]
    )
    vocabulaire = VocabulaireGAN(
        {"<pad>": 0, "<debut>": 1, "<fin>": 2, "a": 3, "b": 4, "c": 5, "d": 6, "e": 7}, longueur=L
    )
    assert generer(generateur, vocabulaire, 3) == [["a", "b"], [], ["d", "e"]]
    assert appels == [(3, 0.3)]
    assert generateur.training is False  # laissé en mode évaluation, comme dans le notebook
    generer(generateur, vocabulaire, 4, temperature=0.5)
    assert appels[-1] == (4, 0.5)

    vocabulaire = VocabulaireGAN.apprendre(
        [["a", "b", "c", "d", "e", "f"]] * 3, longueur=L
    )  # 6 mots + 3 réservés = 9 entrées
    modele = Generateur(vocabulaire.taille, L, dim_bruit=4, dim=5, cache=7)
    torch.manual_seed(1)
    premiere = generer(modele, vocabulaire, 6)
    torch.manual_seed(1)
    assert generer(modele, vocabulaire, 6) == premiere
    mots_permis = set("abcdef")
    assert all(mot in mots_permis for phrase in premiere for mot in phrase)
    assert all(len(phrase) <= L for phrase in premiere)


# --------------------------------------------------------------------------- qualité


def test_suites_distinct_et_quadrigrammes():
    """n-grammes avec doublons, part de n-grammes distincts, quadrigrammes sans doublon."""
    phrases = [["a", "b", "c"], ["a", "b"]]
    assert suites(phrases, 2) == [("a", "b"), ("b", "c"), ("a", "b")]
    assert suites(phrases, 3) == [("a", "b", "c")]
    assert suites(phrases, 1) == [("a",), ("b",), ("c",), ("a",), ("b",)]
    assert suites(phrases, 4) == []

    phrases = [["a", "b", "c"], ["a", "b"]]
    assert distinct(phrases, 2) == pytest.approx(2 / 3)
    assert distinct(phrases, 1) == pytest.approx(3 / 5)
    assert distinct([], 1) == 0.0
    assert distinct([["a", "b"]], 2) == 1.0  # un seul bigramme : 1 sur 1, pas 1 sur 2
    assert distinct([["a"]], 2) == 0.0  # aucun bigramme : pas de division par zéro

    phrases = [list("abcde"), list("abcd"), list("abc")]
    assert quadrigrammes(phrases) == {("a", "b", "c", "d"), ("b", "c", "d", "e")}


def test_corpus_de_comparaison_tires_selon_les_frequences():
    """Mots tirés selon leurs fréquences et longueurs demandées, uniquement des mots du compteur."""
    compteur = collections.Counter({"a": 3, "b": 1})
    tirage = np.random.default_rng(1)
    phrases = tirer_independamment(tirage, compteur, [4, 0, 2])
    attendu_rng = np.random.default_rng(1)
    attendu = [list(attendu_rng.choice(["a", "b"], size=n, p=[0.75, 0.25])) for n in (4, 0, 2)]
    assert phrases == attendu
    assert [len(p) for p in phrases] == [4, 0, 2]

    compteur = collections.Counter({"a": 5, "b": 2, "c": 1})
    phrases = tirer_independamment(np.random.default_rng(0), compteur, [30])
    assert set(phrases[0]) <= {"a", "b", "c"}
    assert phrases[0].count("a") > phrases[0].count("c")


def test_melanger_garde_les_memes_mots_dans_un_autre_ordre():
    phrases = [list("abcdef"), list("uvwxyz")]
    resultat = melanger(np.random.default_rng(3), phrases)
    attendu_rng = np.random.default_rng(3)
    assert resultat == [list(attendu_rng.permutation(p)) for p in phrases]
    assert [sorted(p) for p in resultat] == [sorted(p) for p in phrases]
    assert resultat != phrases


def test_melanger_laisse_les_phrases_d_entree_inchangees():
    phrases = [list("abcdef")]
    avant = [list(p) for p in phrases]
    melanger(np.random.default_rng(3), phrases)
    assert phrases == avant


def test_mesurer_indicateurs_de_qualite():
    """Chaque indicateur est calculé sur les bons messages ; ni quadrigramme inconnu ni phrase
    nouvelle ne comptent."""
    # Deux messages comparables dont les distinct-1, -2 et -3 diffèrent (5/8, 4/6 et 3/4) : un
    # indicateur calculé avec le mauvais n ne passerait pas pour le bon.
    phrases = [list("abcd"), list("abce"), list("xy")]
    recus = []

    def part_spam(corpus):
        recus.append(corpus)
        return 0.75

    mesures = mesurer(
        phrases, nb_comparables=2, quadri_app={tuple("abcd")}, textes_app={"a b c d"},
        part_spam=part_spam,
    )
    assert list(mesures) == [
        "messages", "distinct-1", "distinct-2", "4-grammes déjà vus dans les spams d'app.",
        "copies exactes d'un spam d'app.", "classés spam par le TextCNN",
    ]  # fmt: skip
    assert mesures["messages"] == 3
    assert mesures["distinct-1"] == pytest.approx(5 / 8)  # 5 mots distincts sur 8 (2 messages)
    assert mesures["distinct-2"] == pytest.approx(4 / 6)  # 4 paires distinctes sur 6
    # « abcd » est vu dans l'apprentissage, « abce » non
    assert mesures["4-grammes déjà vus dans les spams d'app."] == pytest.approx(0.5)
    assert mesures["copies exactes d'un spam d'app."] == pytest.approx(1 / 3)  # « a b c d » seul
    assert mesures["classés spam par le TextCNN"] == 0.75
    assert recus == [phrases]  # le détecteur reçoit tout le corpus, pas seulement les comparables

    # Part asymétrique : 2 quadrigrammes vus sur 3 (« abcd » deux fois, « bcde » jamais) ; compter
    # les quadrigrammes jamais vus donnerait 1/3 (avec le corpus ci-dessus, 1/2 des deux côtés).
    mesures = mesurer(
        [list("abcd"), list("abcde")], nb_comparables=2, quadri_app={tuple("abcd")},
        textes_app=set(), part_spam=lambda corpus: 0.0,
    )  # fmt: skip
    assert mesures["4-grammes déjà vus dans les spams d'app."] == pytest.approx(2 / 3)

    phrases = [list("abcd"), list("wxyz")]
    mesures = mesurer(
        phrases, nb_comparables=2, quadri_app={tuple("abcd")}, textes_app={"a b c d"},
        part_spam=lambda corpus: 0.0,
    )
    assert mesures["4-grammes déjà vus dans les spams d'app."] == pytest.approx(0.5)
    assert mesures["copies exactes d'un spam d'app."] == pytest.approx(0.5)




# --------------------------------------------------------------------------- entrées refusées
# Chaque test ci-dessous porte sur une entrée interdite ou absurde : elle doit être refusée par
# une exception, ou laisser les données de l'appelant exactement comme elles étaient.


def test_vocabulaire_gan_refuse_des_spams_qui_ne_sont_pas_des_suites_de_mots():
    with pytest.raises(TypeError):
        VocabulaireGAN.apprendre([1, 2])
    with pytest.raises(TypeError):
        compter_mots([["a"], 5])


def test_vocabulaire_gan_refuse_un_spam_absent():
    vocabulaire = VocabulaireGAN.apprendre(SPAMS, longueur=5)
    with pytest.raises(TypeError):
        vocabulaire.encoder(None)


def test_compter_mots_laisse_les_spams_inchanges():
    spams = [["a", "b"], ["a"]]
    avant = [list(s) for s in spams]
    compter_mots(spams)
    assert spams == avant


def test_vraisemblance_refuse_des_phrases_plus_longues_que_le_generateur():
    modele = petit_generateur()
    with pytest.raises((ValueError, RuntimeError)):
        modele.vraisemblance(torch.full((2, L + 2), 3))


def test_vraisemblance_refuse_un_indice_hors_du_vocabulaire():
    modele = petit_generateur()
    with pytest.raises(IndexError):
        modele.vraisemblance(torch.full((2, L), V))


def test_vraisemblance_laisse_les_cibles_inchangees():
    modele = petit_generateur()
    cibles = torch.tensor([[3, 4, 2, 0, 0, 0]])
    avant = cibles.clone()
    modele.vraisemblance(cibles)
    assert torch.equal(cibles, avant)


def test_echantillonner_refuse_un_effectif_negatif():
    with pytest.raises(RuntimeError):
        petit_generateur().echantillonner(-1, 1.0)


def test_premisse_une_projection_sans_biais_s_ecrit_bias_false_ou_bias_none():
    """Prémisse du mutant équivalent nommé dans MUTANTS_EQUIVALENTS.md : PyTorch traite
    `bias=None` comme `bias=False` (aucun biais créé), donc le discriminateur reste le même."""
    torch.manual_seed(0)
    sans_biais = nn.Linear(V, 5, bias=False)
    torch.manual_seed(0)
    biais_none = nn.Linear(V, 5, bias=None)
    assert sans_biais.bias is None
    assert biais_none.bias is None
    assert [nom for nom, _ in biais_none.named_parameters()] == ["weight"]
    assert torch.equal(sans_biais.weight, biais_none.weight)
    phrases = en_one_hot([[3, 4, 5, 6, 7, 3]])
    assert torch.equal(sans_biais(phrases), biais_none(phrases))


def test_discriminateur_refuse_une_phrase_plus_courte_que_le_plus_grand_filtre():
    modele = petit_discriminateur().eval()
    with pytest.raises(RuntimeError):
        modele(torch.zeros(2, 4, V))


def test_discriminateur_refuse_un_vocabulaire_de_taille_differente():
    modele = petit_discriminateur().eval()
    with pytest.raises(RuntimeError):
        modele(torch.zeros(2, L, V + 1))


def test_masquer_apres_fin_refuse_une_entree_sans_dimension_de_vocabulaire():
    with pytest.raises(IndexError):
        masquer_apres_fin(torch.zeros(2, L))


def test_pre_entrainer_refuse_un_indice_hors_du_vocabulaire():
    reels = reels_jouets(8)
    reels[0, 0] = V
    with pytest.raises(IndexError):
        pre_entrainer(petit_generateur(), reels, epoques=1, bavard=False)


def test_pre_entrainer_laisse_les_spams_d_entree_inchanges():
    reels = reels_jouets(40)
    avant = reels.clone()
    pre_entrainer(petit_generateur(), reels, epoques=1, bavard=False)
    assert torch.equal(reels, avant)


def test_entrainer_adverse_refuse_un_indice_hors_du_vocabulaire():
    reels = reels_jouets(40)
    reels[0, 0] = V
    reels = reels[:1].repeat(32, 1)
    with pytest.raises(RuntimeError):
        entrainer_adverse(
            petit_generateur(), petit_discriminateur(), reels, iterations=2, bavard=False
        )


def test_entrainer_adverse_laisse_les_spams_d_entree_inchanges():
    reels = reels_jouets(40)
    avant = reels.clone()
    entrainer_adverse(petit_generateur(), petit_discriminateur(), reels, iterations=2, bavard=False)
    assert torch.equal(reels, avant)


def test_forme_phrases_refuse_un_effectif_negatif():
    with pytest.raises(RuntimeError):
        forme_phrases(petit_generateur(), n=-1)


def test_generer_refuse_un_vocabulaire_plus_petit_que_celui_du_generateur(monkeypatch):
    petit_vocabulaire = VocabulaireGAN({"<pad>": 0, "<debut>": 1, "<fin>": 2, "a": 3}, longueur=L)
    generateur, _ = generateur_scripte(monkeypatch, [[3, 4, 5, 6, 7, 3]])
    with pytest.raises(KeyError):
        generer(generateur, petit_vocabulaire, 1)


def test_suites_refuse_une_phrase_absente():
    with pytest.raises(TypeError):
        suites([["a", "b"], None], 2)


def test_distinct_laisse_les_phrases_inchangees():
    phrases = [["a", "b", "c"], ["a", "b"]]
    avant = [list(p) for p in phrases]
    distinct(phrases, 2)
    quadrigrammes(phrases)
    assert phrases == avant


def test_tirer_independamment_refuse_un_compteur_vide():
    with pytest.raises(ValueError):
        tirer_independamment(np.random.default_rng(0), collections.Counter(), [3])


def test_melanger_refuse_une_phrase_absente():
    with pytest.raises((TypeError, ValueError, IndexError)):
        melanger(np.random.default_rng(0), [None])


def test_forme_phrases_et_generer_laissent_les_poids_du_generateur_inchanges():
    generateur = petit_generateur()
    mots = {"<pad>": 0, "<debut>": 1, "<fin>": 2, **{f"m{i}": i for i in range(3, V)}}
    vocabulaire = VocabulaireGAN(mots, L)
    avant = [p.clone() for p in generateur.parameters()]
    forme_phrases(generateur, n=5)
    generer(generateur, vocabulaire, 5)
    assert all(torch.equal(p, q) for p, q in zip(generateur.parameters(), avant, strict=True))


def test_mesurer_ne_cache_pas_l_echec_du_detecteur():
    def detecteur_en_panne(_phrases):
        raise RuntimeError("détecteur indisponible")

    with pytest.raises(RuntimeError, match="détecteur indisponible"):
        mesurer(
            [["a", "b", "c", "d"]], nb_comparables=1, quadri_app=set(), textes_app=set(),
            part_spam=detecteur_en_panne,
        )  # fmt: skip
