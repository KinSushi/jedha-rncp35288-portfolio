# ADR-0002 : Règles déterministes plutôt qu'apprentissage automatique

**Date** : 9 juin 2026 · **Statut** : acceptée, précisée le 21 juillet 2026 (voir ADR-0008)

## Contexte

La détection d'anomalies dans des données transactionnelles se prête à
l'apprentissage non supervisé : isolation forest, autoencodeurs, détection de
nouveauté. La question s'est posée d'en faire le cœur du dispositif.

## Options

Modèle d'anomalie. Capte des motifs non anticipés, s'adapte à la dérive,
détecte des combinaisons de variables qu'aucune règle n'exprime.

Règles déterministes. Chaque contrôle est une requête SQL nommée, dont le
résultat est explicable en une phrase et reproductible à l'identique.

## Décision

Règles déterministes exclusivement. Aucun modèle dans le périmètre.

## Justification

Le critère décisif est l'opposabilité. Quand un lot est bloqué, la personne
qui prend la décision doit pouvoir dire quelle règle a été violée et sur quelles
lignes. Devant un auditeur ou un comité de conformité, « le modèle a attribué un
score de 0,87 » n'est pas une justification recevable.

S'ajoute une contrainte de reproductibilité : un contrôle doit rendre le même
verdict sur les mêmes données, indéfiniment. Un modèle réentraîné ne l'assure pas
sans un dispositif de versionnement et de gel qui dépasse largement le périmètre.

Enfin, un modèle d'anomalie appliqué à des décisions financières relèverait
vraisemblablement des obligations du règlement européen sur l'intelligence
artificielle. L'éviter n'est pas de la prudence excessive, c'est une réduction
de charge de conformité.

## Conséquences

Positives : chaque échec est explicable, reproductible et défendable. Aucune
dette de gouvernance de modèle.

Négatives : le dispositif ne détecte que ce qui a été anticipé. Les anomalies
de forme inédite passent.

Atténuation : la matrice de contrôles est revue à chaque incident. Un modèle
d'anomalie reste envisageable en complément, comme signal d'alerte pour la
revue humaine, jamais comme critère de blocage.

---

## Suite donnée (21 juillet 2026)

Cette atténuation a été mise en œuvre : le projet embarque désormais un
classifieur supervisé, décrit dans l'ADR-0008.

La décision initiale n'est ni annulée ni contournée. Elle est appliquée à la
lettre :

| | Décide | Ne décide pas |
|---|---|---|
| Contrôles déterministes | Le blocage de la publication | Rien d'autre |
| Modèle supervisé | L'ordre de la file d'investigation | Jamais le blocage |

Les deux vivent dans des tables distinctes (`quality_check_results` et
`gold.predictions`) et la seconde porte cette phrase en commentaire de table.
Un verdict de publication ne consulte jamais un score de modèle.

---

## Précision (11 octobre 2026)

La section « Justification » affirme qu'un modèle d'anomalie appliqué à des décisions financières relèverait
« vraisemblablement » du règlement européen sur l'intelligence artificielle. Cette affirmation n'était pas sourcée ;
elle est précisée à la lecture du texte (EUR-Lex, règlement (UE) 2024/1689, texte français publié au Journal officiel
de l'Union européenne du 12 juillet 2024, consulté le 11 octobre 2026 ; ce n'est pas une version consolidée).

L'annexe III, point 5, b), vise les « systèmes d'IA destinés à être utilisés pour évaluer la solvabilité des personnes
physiques ou pour établir leur note de crédit, à l'exception des systèmes d'IA utilisés à des fins de détection de
fraudes financières ». Le classifieur de l'ADR-0008 priorise des transactions suspectes pour la revue humaine : il relève
de l'exception de détection de fraudes et n'est pas un système « à haut risque » au titre de ce point. Les obligations
de transparence du règlement et le RGPD (article 22) restent à respecter. La décision de l'ADR-0002 ne change pas : elle
repose sur l'opposabilité et la reproductibilité, pas sur ce règlement.

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
