# ADR-0007 : Traçabilité par journal d'audit et reverse tests

**Date** : 20 juillet 2026 · **Statut** : acceptée

## Contexte

Le projet produit une décision de publication. Trois questions doivent trouver
réponse des mois après, sans reconstitution : qu'est-ce qui a tourné, sur quelles
données, et pourquoi ce résultat.

Par ailleurs, la suite de tests ne couvrait que les chemins nominaux. Rien ne
garantissait qu'un chemin interdit échoue proprement.

## Options

Se contenter des logs applicatifs. Aucun coût. Mais un log est volatil,
non requêtable, et n'impose aucune structure : six mois plus tard, il n'est plus
exploitable.

Une table d'audit seule. Requêtable et durable, mais indisponible quand la
base l'est. Or les incidents les plus intéressants surviennent précisément
pendant une panne de base.

Double écriture, log structuré plus table. Le log est toujours écrit, la
table au mieux.

## Décision

Double écriture. Le log structuré est la source primaire, la table `audit_events`
la source durable. L'écriture en base est *best effort* : son échec est signalé
au niveau WARNING et n'interrompt jamais le traitement.

Le champ `reason` est obligatoire et non vide, refusé à la construction comme
par une contrainte `CHECK` en base.

Ajout d'une suite de reverse tests couvrant quatre modes de défaillance : fuite,
échec silencieux, corruption d'état, anticipation.

## Justification

Pourquoi `reason` est obligatoire. Un journal qui enregistre le quoi sans le
pourquoi est un log. La contrainte est déclarative des deux côtés, code et base
, parce qu'un contournement du code applicatif ne doit pas permettre d'insérer
un événement muet.

Pourquoi journaliser aussi les invariants satisfaits. Un invariant vérifié en
silence est indiscernable d'un invariant jamais exécuté. L'auditeur doit pouvoir
prouver que le contrôle a eu lieu.

Pourquoi des reverse tests. Un test avant prouve que le chemin nominal
fonctionne. Il ne dit rien de ce qui arrive quand on s'en écarte. Le premier
reverse test écrit a immédiatement révélé un défaut réel : le générateur écrivait
l'horloge murale dans les données, invalidant la reproductibilité affirmée par le
dossier.

## Conséquences

Positives : la chaîne complète est traçable par identifiant d'exécution. Un
défaut de reproductibilité a été trouvé et corrigé. Une faille d'interpolation
SQL a été fermée dans `fetch_rows`.

Négatives : une table de plus, sans politique de purge : définir une durée de
rétention est une décision de gouvernance, recensée parmi les points à trancher
avant toute mise en production. Chaque événement coûte une insertion, négligeable
au volume actuel, à réévaluer au-delà.

Réversibilité : élevée. La couche d'audit est additive, aucun module existant
n'en dépend pour fonctionner.

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
