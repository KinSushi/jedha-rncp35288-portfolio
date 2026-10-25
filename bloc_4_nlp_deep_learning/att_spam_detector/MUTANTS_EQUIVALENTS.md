# Mutants équivalents : paquet `att_spam`

Mesure : `mutmut` 3, sous Linux, sur les cinq modules du paquet
(`donnees.py`, `reseaux.py`, `bert.py`, `gan.py`, `evaluation.py`), avec leurs tests
(`tests/test_donnees.py`, `test_reseaux.py`, `test_bert.py`, `test_gan.py`,
`test_evaluation.py`). Un mutant n'est nommé ici que si **aucun test ne peut le distinguer** du
code d'origine : même comportement, mêmes valeurs rendues.

| Mutant | Changement | Pourquoi le comportement est identique |
|---|---|---|
| `att_spam.gan.xǁDiscriminateurǁ__init____mutmut_6` | `nn.Linear(taille_vocab, dim, bias=False)` devient `bias=None` | PyTorch ne crée le biais d'une couche linéaire que si l'argument `bias` est vrai (`if bias:` dans `torch/nn/modules/linear.py`). `False` et `None` sont tous deux faux : la couche n'a pas de biais dans les deux cas, mêmes paramètres, mêmes sorties. |

Preuve exécutée : `tests/test_gan.py`,
`test_premisse_une_projection_sans_biais_s_ecrit_bias_false_ou_bias_none` (pas de biais dans les
deux cas, un seul paramètre `weight`, mêmes poids pour la même graine, mêmes sorties), et
`test_discriminateur_structure_score_et_dropout` (`projection.bias is None`).

Les dix autres survivants de la première mesure du 19/10/2026 (1 646 tués sur 1 657) sont tués
par des tests ajoutés ou complétés le même jour :
- `torch.squeeze(…, dim=1)` devenu `torch.squeeze(…)` dans `TextCNN`, `BiGRU` et
  `Discriminateur` : un lot d'un seul message doit rester un vecteur d'un score
  (`test_textcnn_structure`, `test_bigru_structure`, `test_discriminateur_structure_score_et_dropout`) ;
- `perte_adverse + perte_mv` devenu `perte_adverse - perte_mv` :
  `test_entrainer_adverse_le_pas_du_generateur_fait_baisser_sa_perte_de_vraisemblance` ;
- part des quadrigrammes déjà vus comptée à l'envers : `test_mesurer_indicateurs_de_qualite`
  (corpus où la part vaut 2/3) ;
- percentile 97,5 devenu 98,5 : `test_intervalle_bootstrap_percentiles` (40 messages, 400 tirages) ;
- masque d'attention retiré ou remplacé par `None` (quatre mutants) :
  `test_probabilites_bert_le_remplissage_ne_change_pas_la_probabilite` et
  `test_ajuster_distilbert_transmet_le_masque_d_attention_a_chaque_pas`.

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
