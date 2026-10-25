# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests de `att_spam.bert` avec un mini-DistilBERT local : aucun réseau, aucun téléchargement."""

import copy
import inspect
import logging
import math
import warnings

import numpy as np
import pytest
import torch
from huggingface_hub.utils import logging as journal_hub
from torch.utils.data import DataLoader, TensorDataset
from transformers import DistilBertConfig, DistilBertModel, DistilBertTokenizer
from transformers.utils import logging as journal_transformers

from att_spam import bert
from att_spam.bert import (
    ajuster_distilbert,
    charger_distilbert,
    encoder_bert,
    probabilites_bert,
)
from att_spam.reseaux import creer_perte, ligne_epoque

MOTS = [
    "[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]",
    "free", "call", "now", "win", "prize", "hello", "ok", "tel",
]  # fmt: skip


@pytest.fixture(scope="session")
def dossier_mini(tmp_path_factory):
    """Mini-DistilBERT (sans tête de classification) et son tokeniseur, écrits sur le disque."""
    dossier = tmp_path_factory.mktemp("mini_distilbert")
    tokeniseur = DistilBertTokenizer(vocab={m: i for i, m in enumerate(MOTS)}, do_lower_case=True)
    tokeniseur.save_pretrained(dossier)
    torch.manual_seed(0)
    config = DistilBertConfig(
        vocab_size=len(MOTS), dim=8, hidden_dim=16, n_layers=1, n_heads=2,
        max_position_embeddings=70,
    )  # fmt: skip
    DistilBertModel(config).save_pretrained(dossier)
    return str(dossier)


@pytest.fixture
def mini(dossier_mini):
    return charger_distilbert(dossier_mini, graine=0)


def jeu_jouet(tokeniseur, n, graine=0):
    """n textes de trois mots du mini-vocabulaire ; la cible vaut 1 si « free » y figure."""
    hasard = np.random.default_rng(graine)
    mots = ["free", "call", "now", "win", "prize", "hello", "ok"]
    textes = [" ".join(hasard.choice(mots, 3)) for _ in range(n)]
    cibles = np.array([int("free" in t) for t in textes])
    return encoder_bert(tokeniseur, textes, cibles), cibles


# --------------------------------------------------------------------------- chargement


def test_charger_distilbert_modele_a_une_sortie_et_graine(mini, dossier_mini):
    """Modèle à une sortie, tokeniseur associé, tête neuve dépendant de la graine."""
    tokeniseur, modele = mini
    assert modele.config.num_labels == 1
    assert modele.classifier.out_features == 1
    assert tokeniseur(["free call"])["input_ids"][0] == [2, 5, 6, 3]

    _, a = charger_distilbert(dossier_mini, graine=3)
    _, b = charger_distilbert(dossier_mini, graine=3)
    _, c = charger_distilbert(dossier_mini, graine=4)
    assert torch.equal(a.classifier.weight, b.classifier.weight)
    assert not torch.equal(a.classifier.weight, c.classifier.weight)


def test_charger_distilbert_masque_journaux_et_avertissement(dossier_mini):
    """Seules les erreurs des bibliothèques restent visibles ; l'avertissement d'authentification
    est masqué."""
    journal_transformers.set_verbosity_warning()
    journal_hub.set_verbosity_warning()
    journal_transformers.enable_progress_bar()
    charger_distilbert(dossier_mini, graine=0)
    assert journal_transformers.get_verbosity() == logging.ERROR
    assert journal_hub.get_verbosity() == logging.ERROR
    assert journal_transformers.is_progress_bar_enabled() is False

    with warnings.catch_warnings():
        warnings.resetwarnings()
        charger_distilbert(dossier_mini, graine=0)
        with warnings.catch_warnings(record=True) as enregistres:
            warnings.warn("vous faites des unauthenticated requests au Hub", stacklevel=1)
            warnings.warn("un tout autre avertissement", stacklevel=1)
    assert [str(w.message) for w in enregistres] == ["un tout autre avertissement"]


def test_charger_distilbert_refuse_un_modele_introuvable(tmp_path):
    with pytest.raises((OSError, ValueError)):
        charger_distilbert(str(tmp_path / "absent"), graine=0)


# --------------------------------------------------------------------------- encodage


def test_encoder_bert_identifiants_masque_cibles_et_troncature(mini):
    """Identifiants WordPiece, masque d'attention, cibles réelles, troncature à 64 jetons."""
    tokeniseur, _ = mini
    jeu = encoder_bert(tokeniseur, ["free call now", "hello"], np.array([1, 0]))
    ids, masque, cibles = jeu.tensors
    assert ids.shape == (2, 64)
    assert masque.shape == (2, 64)
    assert ids[0, :6].tolist() == [2, 5, 6, 7, 3, 0]  # [CLS] free call now [SEP] puis [PAD]
    assert ids[1, :4].tolist() == [2, 10, 3, 0]
    assert masque[0].sum().item() == 5
    assert masque[1].sum().item() == 3
    assert masque[0, :5].tolist() == [1, 1, 1, 1, 1]
    assert cibles.dtype == torch.float32
    assert cibles.tolist() == [1.0, 0.0]

    tokeniseur, _ = mini
    ids, masque, _ = encoder_bert(tokeniseur, ["free " * 100], np.array([1])).tensors
    assert ids.shape == (1, 64)
    assert masque.sum().item() == 64
    assert ids[0, 0].item() == 2 and ids[0, -1].item() == 3  # [CLS] au début, [SEP] à la fin


# --------------------------------------------------------------------------- probabilités


def test_probabilites_bert_sigmoide_par_lots_de_64_en_mode_evaluation(mini, monkeypatch):
    """Sigmoïde du logit pour chaque message, lots de 64 sans mélange, mode évaluation."""
    tokeniseur, modele = mini
    jeu, _ = jeu_jouet(tokeniseur, 70)  # 70 messages : plus d'un lot de 64
    probas = probabilites_bert(modele, jeu)
    ids, masque, _ = jeu.tensors
    modele.eval()
    with torch.no_grad():
        attendu = torch.sigmoid(modele(input_ids=ids, attention_mask=masque).logits.squeeze(1))
    assert probas.dtype == np.float32
    assert probas.shape == (70,)
    assert probas.tolist() == pytest.approx(attendu.tolist(), abs=1e-5)
    assert ((probas > 0) & (probas < 1)).all()

    tokeniseur, modele = mini
    jeu, _ = jeu_jouet(tokeniseur, 10)
    modele.train()
    a = probabilites_bert(modele, jeu)
    b = probabilites_bert(modele, jeu)
    assert modele.training is False
    assert a.tolist() == b.tolist()

    tokeniseur, modele = mini
    jeu, _ = jeu_jouet(tokeniseur, 10)
    appels = []

    def espion(*args, **options):
        appels.append(options)
        return DataLoader(*args, **options)

    monkeypatch.setattr(bert, "DataLoader", espion)
    probabilites_bert(modele, jeu)
    assert appels == [{"batch_size": 64}]


# --------------------------------------------------------------------------- ajustement


def lancer_ajustement(dossier_mini, graine=0, n_app=70, n_val=12, **options):
    tokeniseur, modele = charger_distilbert(dossier_mini, graine=0)
    jeu_app, y_app = jeu_jouet(tokeniseur, n_app, graine=1)
    jeu_val, _ = jeu_jouet(tokeniseur, n_val, graine=2)
    perte = creer_perte(torch.tensor(y_app, dtype=torch.float32))
    historique = ajuster_distilbert(
        modele, jeu_app, jeu_val, perte, graine=graine, **options
    )
    return modele, historique, (jeu_app, jeu_val, perte)


def test_ajuster_distilbert_courbes_poids_et_graine(dossier_mini):
    """Quatre courbes par époque, deux époques par défaut, poids modifiés, reproductible pour une
    graine."""
    epoques = 3
    _, historique, _ = lancer_ajustement(dossier_mini, epoques=epoques)
    assert set(historique) == {"perte_app", "perte_val", "f1_app", "f1_val"}
    assert all(len(courbe) == epoques for courbe in historique.values())
    assert all(math.isfinite(v) for courbe in historique.values() for v in courbe)
    assert all(0.0 <= f1 <= 1.0 for f1 in historique["f1_app"] + historique["f1_val"])

    par_defaut = inspect.signature(ajuster_distilbert).parameters["epoques"].default
    assert par_defaut == 2
    _, historique, _ = lancer_ajustement(dossier_mini)
    assert all(len(courbe) == par_defaut for courbe in historique.values())

    tokeniseur, modele = charger_distilbert(dossier_mini, graine=0)
    avant = copy.deepcopy(modele.state_dict())
    jeu_app, y_app = jeu_jouet(tokeniseur, 40, graine=1)
    jeu_val, _ = jeu_jouet(tokeniseur, 12, graine=2)
    perte = creer_perte(torch.tensor(y_app, dtype=torch.float32))
    ajuster_distilbert(modele, jeu_app, jeu_val, perte, graine=0, epoques=2)
    apres = modele.state_dict()
    assert any(not torch.equal(avant[nom], apres[nom]) for nom in avant)

    modele_a, historique_a, _ = lancer_ajustement(dossier_mini, graine=5, epoques=2)
    modele_b, historique_b, _ = lancer_ajustement(dossier_mini, graine=5, epoques=2)
    _, historique_c, _ = lancer_ajustement(dossier_mini, graine=6, epoques=2)
    assert historique_a == historique_b
    assert all(
        torch.equal(p, q) for p, q in zip(modele_a.parameters(), modele_b.parameters(), strict=True)
    )
    assert historique_a["perte_app"] != historique_c["perte_app"]


def test_ajuster_distilbert_reglages_et_perte_moyenne(dossier_mini, monkeypatch):
    """AdamW à 5e-5, lots de 32, perte d'apprentissage = moyenne des pertes des lots."""
    options_adamw, options_lots = [], []
    adamw_reel = torch.optim.AdamW

    def espion_adamw(parametres, **options):
        options_adamw.append(options)
        return adamw_reel(parametres, **options)

    def espion_lots(*args, **options):
        options_lots.append(options)
        return DataLoader(*args, **options)

    monkeypatch.setattr(torch.optim, "AdamW", espion_adamw)
    monkeypatch.setattr(bert, "DataLoader", espion_lots)
    lancer_ajustement(dossier_mini, graine=7, epoques=1)
    assert options_adamw == [{"lr": 5e-5}]
    entrainement = options_lots[0]
    assert (entrainement["batch_size"], entrainement["shuffle"]) == (32, True)
    assert entrainement["generator"].initial_seed() == 7
    assert {"batch_size": 64} in options_lots[1:]  # la mesure des probabilités, par lots de 64

    tokeniseur, modele = charger_distilbert(dossier_mini, graine=0)
    jeu_app, y_app = jeu_jouet(tokeniseur, 70, graine=1)  # lots de 32, 32 et 6
    jeu_val, _ = jeu_jouet(tokeniseur, 12, graine=2)
    reelle = creer_perte(torch.tensor(y_app, dtype=torch.float32))
    vues = []

    def perte(scores, cibles):
        valeur = reelle(scores, cibles)
        vues.append((len(cibles), valeur.item()))
        return valeur

    historique = ajuster_distilbert(modele, jeu_app, jeu_val, perte, graine=0, epoques=1)
    lots = [valeur for taille, valeur in vues if taille in (32, 6)]
    assert [taille for taille, _ in vues] == [32, 32, 6, 12]
    assert historique["perte_app"] == pytest.approx([sum(lots) / len(lots)])


class SondeProbabilites:
    """Remplace `probabilites_bert` : probabilités imposées sur la validation, 0,5 ailleurs."""

    def __init__(self, jeu_val, probas_val):
        self.jeu_val = jeu_val
        self.probas_val = probas_val
        self.etats_val = []

    def __call__(self, modele, jeu):
        if jeu is self.jeu_val:
            self.etats_val.append(copy.deepcopy(modele.state_dict()))
            return self.probas_val[len(self.etats_val) - 1]
        return np.full(len(jeu), 0.5, dtype=np.float32)


def sonde_et_ajustement(dossier_mini, monkeypatch, probas_val):
    tokeniseur, modele = charger_distilbert(dossier_mini, graine=0)
    jeu_app, y_app = jeu_jouet(tokeniseur, 24, graine=1)
    jeu_val = encoder_bert(tokeniseur, ["free"] * 4, np.array([1, 1, 0, 0]))
    sonde = SondeProbabilites(jeu_val, probas_val)
    monkeypatch.setattr(bert, "probabilites_bert", sonde)
    perte = creer_perte(torch.tensor(y_app, dtype=torch.float32))
    historique = ajuster_distilbert(
        modele, jeu_app, jeu_val, perte, graine=0, epoques=len(probas_val)
    )
    return modele, historique, sonde, jeu_app, perte


def egaux(etat_a, etat_b):
    return all(torch.equal(etat_a[nom], etat_b[nom]) for nom in etat_a)


MAUVAIS = np.array([0.1, 0.1, 0.9, 0.9], dtype=np.float32)  # tout à l'envers : F1 de 0
BON = np.array([0.9, 0.9, 0.1, 0.1], dtype=np.float32)  # parfait : F1 de 1


def test_ajuster_distilbert_garde_la_meilleure_epoque(dossier_mini, monkeypatch):
    """Garde l'état de la meilleure époque ; en cas d'égalité, la première."""
    modele, historique, sonde, *_ = sonde_et_ajustement(dossier_mini, monkeypatch, [BON, MAUVAIS])
    assert historique["f1_val"] == pytest.approx([1.0, 0.0])
    assert egaux(modele.state_dict(), sonde.etats_val[0])
    assert not egaux(modele.state_dict(), sonde.etats_val[1])

    modele, historique, sonde, *_ = sonde_et_ajustement(dossier_mini, monkeypatch, [MAUVAIS, BON])
    assert historique["f1_val"] == pytest.approx([0.0, 1.0])
    assert egaux(modele.state_dict(), sonde.etats_val[1])

    modele, historique, sonde, *_ = sonde_et_ajustement(dossier_mini, monkeypatch, [BON, BON])
    assert historique["f1_val"] == pytest.approx([1.0, 1.0])
    assert egaux(modele.state_dict(), sonde.etats_val[0])
    assert not egaux(sonde.etats_val[0], sonde.etats_val[1])


def test_ajuster_distilbert_f1_au_seuil_un_demi_et_perte_de_validation_bornee(
    dossier_mini, monkeypatch
):
    extremes = np.array([1.0, 0.5, 1.0, 0.0], dtype=np.float32)
    _, historique, _, jeu_app, perte = sonde_et_ajustement(dossier_mini, monkeypatch, [extremes])
    # validation : [1, 1, 0, 0] prédit [1, 1, 1, 0] (0,5 compte comme spam) : 2 vrais positifs,
    # 1 faux positif, aucun manqué
    assert historique["f1_val"] == pytest.approx([0.8])
    # les probabilités extrêmes sont ramenées à [1e-6 ; 1 - 1e-6] avant le logit
    logit = math.log((1 - 1e-6) / 1e-6)
    scores = torch.tensor([logit, 0.0, logit, -logit])
    attendu = perte(scores, torch.tensor([1.0, 1.0, 0.0, 0.0])).item()
    assert historique["perte_val"] == pytest.approx([attendu], rel=1e-3)
    # apprentissage : probabilité 0,5 pour tous, un seuil « >= 0,5 » déclare donc tout spam
    y_app = jeu_app.tensors[2]
    vrais = y_app.sum().item()
    attendu_f1_app = 2 * vrais / (2 * vrais + (len(y_app) - vrais))
    assert historique["f1_app"] == pytest.approx([attendu_f1_app])


def test_ajuster_distilbert_affiche_une_ligne_par_epoque(dossier_mini, capsys):
    _, historique, _ = lancer_ajustement(dossier_mini, epoques=2)
    attendu = [
        ligne_epoque("DistilBERT", e, {cle: v[:e] for cle, v in historique.items()}, largeur=1)
        for e in (1, 2)
    ]
    assert capsys.readouterr().out.splitlines() == attendu


@pytest.mark.parametrize("epoques", [0, -2])
def test_ajuster_distilbert_refuse_moins_d_une_epoque(dossier_mini, epoques):
    tokeniseur, modele = charger_distilbert(dossier_mini, graine=0)
    jeu, y = jeu_jouet(tokeniseur, 10)
    avant = copy.deepcopy(modele.state_dict())
    with pytest.raises(ValueError) as erreur:
        ajuster_distilbert(
            modele, jeu, jeu, creer_perte(torch.tensor(y, dtype=torch.float32)), graine=0,
            epoques=epoques,
        )
    assert str(erreur.value) == "il faut au moins une époque pour retenir un meilleur état"
    assert egaux(modele.state_dict(), avant)  # le modèle n'a pas bougé


def test_constantes_du_module():
    assert bert.NOM_MODELE == "distilbert/distilbert-base-uncased"
    assert bert.LONGUEUR_MAX == 64


# --------------------------------------------------------------------------- entrées refusées
# Chaque test ci-dessous porte sur une entrée interdite ou absurde : elle doit être refusée par
# une exception, ou laisser les données de l'appelant exactement comme elles étaient.


def test_charger_distilbert_refuse_une_graine_qui_n_est_pas_un_entier(dossier_mini):
    with pytest.raises(ValueError):
        charger_distilbert(dossier_mini, graine="graine")


def test_encoder_bert_refuse_des_textes_absents(mini):
    tokeniseur, _ = mini
    with pytest.raises((TypeError, ValueError)):
        encoder_bert(tokeniseur, [None], np.array([1]))


def test_encoder_bert_refuse_des_cibles_de_longueur_differente_des_textes(mini):
    tokeniseur, _ = mini
    with pytest.raises(AssertionError):
        encoder_bert(tokeniseur, ["free call", "hello"], np.array([1]))


def test_encoder_bert_laisse_les_textes_inchanges(mini):
    tokeniseur, _ = mini
    textes = ["free call now", "hello"]
    cibles = np.array([1, 0])
    textes_avant, cibles_avant = list(textes), cibles.copy()
    encoder_bert(tokeniseur, textes, cibles)
    assert textes == textes_avant
    assert np.array_equal(cibles, cibles_avant)


def test_probabilites_bert_refuse_un_jeu_sans_masque_d_attention(mini):
    _, modele = mini
    sans_masque = jeu_sans_masque()
    with pytest.raises(ValueError):
        probabilites_bert(modele, sans_masque)


def test_probabilites_bert_laisse_les_poids_inchanges(mini):
    tokeniseur, modele = mini
    jeu, _ = jeu_jouet(tokeniseur, 10)
    avant = copy.deepcopy(modele.state_dict())
    probabilites_bert(modele, jeu)
    assert egaux(modele.state_dict(), avant)


def test_ajuster_distilbert_refuse_une_validation_vide(dossier_mini):
    tokeniseur, modele = charger_distilbert(dossier_mini, graine=0)
    jeu_app, y_app = jeu_jouet(tokeniseur, 10)
    vide = jeu_vide()
    with pytest.raises(ValueError, match="non-empty"):
        ajuster_distilbert(
            modele, jeu_app, vide, creer_perte(torch.tensor(y_app, dtype=torch.float32)),
            graine=0, epoques=1,
        )  # fmt: skip


def test_ajuster_distilbert_refuse_un_apprentissage_vide(dossier_mini):
    tokeniseur, modele = charger_distilbert(dossier_mini, graine=0)
    jeu_val, y_val = jeu_jouet(tokeniseur, 10)
    vide = jeu_vide()
    with pytest.raises(ValueError):
        ajuster_distilbert(
            modele, vide, jeu_val, creer_perte(torch.tensor(y_val, dtype=torch.float32)),
            graine=0, epoques=1,
        )  # fmt: skip


def test_ajuster_distilbert_laisse_les_jeux_de_donnees_inchanges(dossier_mini):
    tokeniseur, modele = charger_distilbert(dossier_mini, graine=0)
    jeu_app, y_app = jeu_jouet(tokeniseur, 20, graine=1)
    jeu_val, _ = jeu_jouet(tokeniseur, 8, graine=2)
    avant = [t.clone() for jeu in (jeu_app, jeu_val) for t in jeu.tensors]
    ajuster_distilbert(
        modele, jeu_app, jeu_val, creer_perte(torch.tensor(y_app, dtype=torch.float32)),
        graine=0, epoques=1,
    )  # fmt: skip
    apres = [t for jeu in (jeu_app, jeu_val) for t in jeu.tensors]
    assert all(torch.equal(a, b) for a, b in zip(apres, avant, strict=True))


def jeu_vide():
    """Jeu sans aucun message : identifiants, masque et cibles de longueur nulle."""
    return TensorDataset(
        torch.zeros(0, 64, dtype=torch.long), torch.zeros(0, 64, dtype=torch.long), torch.zeros(0)
    )


def jeu_sans_masque():
    """Jeu qui ne porte qu'un tenseur : ni masque d'attention, ni cibles."""
    return TensorDataset(torch.zeros(3, 4, dtype=torch.long))


def test_probabilites_bert_rend_toujours_du_float32_meme_pour_un_modele_en_double_precision(mini):
    tokeniseur, modele = mini
    jeu, _ = jeu_jouet(tokeniseur, 6)
    probas = probabilites_bert(modele.double(), jeu)
    assert probas.dtype == np.float32
    assert ((probas > 0) & (probas < 1)).all()


def test_probabilites_bert_le_remplissage_ne_change_pas_la_probabilite(mini):
    """Le masque d'attention écarte les [PAD] : un SMS rempli jusqu'à 64 jetons a la même
    probabilité que le même SMS seul, sans remplissage."""
    tokeniseur, modele = mini
    # La tête neuve du mini-modèle rend des logits minuscules (de l'ordre de 1e-4) : on les
    # agrandit, pour qu'un [PAD] lu par erreur change visiblement la probabilité.
    with torch.no_grad():
        modele.classifier.weight.mul_(1000.0)
    textes = ["hello", "free call now win"]
    probas = probabilites_bert(modele, encoder_bert(tokeniseur, textes, np.array([0, 1])))
    for texte, proba in zip(textes, probas, strict=True):
        seul = tokeniseur([texte], return_tensors="pt")  # aucun [PAD]
        with torch.no_grad():
            attendu = torch.sigmoid(modele(**seul).logits.squeeze(1)).item()
        assert proba == pytest.approx(attendu, abs=1e-6)


def test_ajuster_distilbert_transmet_le_masque_d_attention_a_chaque_pas(dossier_mini):
    """Chaque pas d'apprentissage donne au modèle le masque d'attention de son lot : sans lui,
    DistilBERT lirait aussi les [PAD] qui complètent les SMS courts."""
    tokeniseur, modele = charger_distilbert(dossier_mini, graine=0)
    jeu_app, y_app = jeu_jouet(tokeniseur, 40, graine=1)
    jeu_val, _ = jeu_jouet(tokeniseur, 12, graine=2)
    perte = creer_perte(torch.tensor(y_app, dtype=torch.float32))
    appels = []
    forward_reel = modele.forward

    def espion(*args, **options):
        appels.append((modele.training, options["input_ids"], options.get("attention_mask")))
        return forward_reel(*args, **options)

    modele.forward = espion
    ajuster_distilbert(modele, jeu_app, jeu_val, perte, graine=0, epoques=1)
    pas = [(ids, masque) for apprentissage, ids, masque in appels if apprentissage]
    assert [ids.shape[0] for ids, _ in pas] == [32, 8]  # 40 SMS en lots de 32 : deux pas
    for ids, masque in pas:
        assert masque is not None
        assert torch.equal(masque, (ids != 0).long())  # 1 sur les vrais jetons, 0 sur les [PAD]
