# 03 — Sécurité et conformité (RGPD, PCI DSS, chiffrement, secrets)

*Bloc 3 AIA — Détection automatique de fraude. Ce document répond aux indicateurs 5.1, 5.2 et 5.3
du référentiel. Les données du projet sont **synthétiques** (jeu public de l'énoncé) ; le RGPD et
PCI DSS sont appliqués **par transposition**, comme si elles étaient réelles. Les références de
textes sont citées sans leur prêter plus qu'elles ne disent ; les numéros d'exigence PCI DSS
(version 4.0.1) sont à faire confirmer par le responsable de la sécurité des systèmes
d'information (RSSI).*

## 1. Ce qui entre dans le pipeline

Une transaction de l'API contient 21 champs. Classement :

| Catégorie | Champs | Traitement |
|---|---|---|
| Identifiant direct de la personne | `first`, `last`, `street` | **supprimés** à l'ingestion |
| Donnée de paiement | `cc_num` (numéro de carte) | **haché à clé** + 4 derniers chiffres |
| Quasi-identifiants | `dob`, `lat`, `long`, `zip`, `city`, `job`, `gender` | supprimés, ou remplacés par une valeur généralisée (`age`, `distance_km`) |
| Données de la transaction | `merchant`, `category`, `amt`, `state`, `city_pop`, `merch_lat`, `merch_long`, `trans_num`, `current_time`, `is_fraud` | conservées (non identifiantes seules) |

## 2. Privacy by Design : le masquage dès l'ingestion — indicateur 5.1

> *« Le code implémente-t-il techniquement l'anonymisation ou le masquage des champs sensibles
> identifiés ? »*

Le masquage est dans `fraude/detection_fraude/masquage.py`, appelé par le **producteur** avant
toute publication dans la file et toute écriture sur disque. Il est construit en **liste
blanche** : le message masqué est un nouveau dictionnaire, champ par champ ; un champ ajouté demain
par la source ne passe pas.

| Champ source | Traitement | Justification |
|---|---|---|
| `cc_num` | `carte_hash` : HMAC-SHA256 à clé secrète, 32 premiers caractères hexadécimaux ; `carte_fin` : 4 derniers chiffres | garder le lien « même carte » (utile à la détection) sans conserver le numéro. Un hachage **sans clé** serait réversible par énumération (le numéro a peu d'entropie : préfixe d'émetteur, clé de Luhn) ; d'où le HMAC |
| `first`, `last`, `street` | supprimés | inutiles à la finalité, directement identifiants |
| `dob` | `age` en années révolues à la date de la transaction | généralisation : l'âge sert au modèle, la date de naissance identifie |
| `lat`, `long` (domicile) | remplacés par `distance_km` (domicile → commerçant), puis supprimés | l'information utile est l'éloignement, pas la position du domicile |
| `zip`, `city` | supprimés (on garde `state`) | quasi-identifiants |
| `job`, `gender` | supprimés | inutiles ; `gender` ferait courir un risque de biais discriminatoire |
| `is_fraud` | conservé sous le nom `etiquette_source`, **jamais utilisé pour décider** | sert uniquement à mesurer la qualité du modèle |

Le contrat de la file (`schema.valider_masque`) **refuse tout champ inattendu** : si un champ
sensible échappait un jour au masquage, il serait arrêté avant d'atteindre l'entrepôt (test
`test_message_masque_refuse_un_champ_sensible_qui_aurait_survecu`). Les lignes rejetées vont en DLQ
sous une forme encore plus stricte (`masquer_pour_rejet`) : valeurs de la liste blanche
seulement, champs sensibles réduits à leur nom, tout nombre ressemblant à un numéro de carte
effacé, `detail` des erreurs jamais rempli pour un champ sensible.

### Preuves exécutables

* `tests/test_masquage.py` : aucune valeur sensible dans le message ; empreinte déterministe,
  dépendante de la clé, différente d'un SHA-256 simple ; âge exact au jour près ; distance
  de haversine vérifiée (Paris-Londres, environ 343 km) ; DLQ sans donnée identifiante.
* `tests/test_bout_en_bout.py::test_aucune_donnee_personnelle_dans_aucun_fichier_produit` :
  après un parcours complet (collecte, DLQ, file, bronze, argent, or, alertes, rapports,
  journaux, état, fichiers du modèle), **tous les fichiers produits** sont parcourus à la recherche
  des prénoms, noms, rues, villes, métiers, dates de naissance et numéros de carte du jeu d'entrée :
  aucune occurrence. C'est la vérification la plus forte, car elle porte sur le résultat et non sur
  l'intention du code.
* `journal.py` : défense en profondeur. Même une erreur de programmation qui passerait un champ
  sensible à un journal serait neutralisée : champs `cc_num`, `first`, `last`… remplacés par
  `[MASQUE]`, toute suite de 13 à 19 chiffres isolée effacée (`test_journal.py`).

### Limites à connaître

* **Pseudonymisation n'est pas anonymisation.** Au sens du RGPD (article 4, point 5, et
  considérant 26), des données pseudonymisées restent des données à caractère personnel : la
  combinaison `carte_hash` stable + `carte_fin` + âge + État + horodatage + commerçant pourrait,
  recoupée avec d'autres sources, permettre une réidentification. Le jeu masqué reste donc
  **soumis au RGPD** ; le masquage réduit le risque, il ne le supprime pas.
* **Le producteur voit la donnée brute**, en mémoire, le temps d'une ligne. Il appartient à la zone
  la plus sensible (voir PCI DSS ci-dessous) et doit être durci en conséquence.
* **Changer la clé du HMAC change toutes les empreintes** : la rotation de la clé coupe la
  continuité des historiques de carte (voir l'exploitation des secrets, section 5).

## 3. RGPD : mise en conformité

| Exigence | Réponse du projet | Texte |
|---|---|---|
| **Base légale** | intérêt légitime du responsable (prévention de la fraude) ; le considérant 47 indique que le traitement strictement nécessaire à la prévention de la fraude constitue un intérêt légitime. Une mise en balance documentée reste à écrire | art. 6, par. 1, point f ; considérant 47 |
| **Finalité** | unique : détecter la fraude à la carte et en rendre compte | art. 5, par. 1, point b |
| **Minimisation** | 11 champs sur 21 sont supprimés, hachés ou généralisés ; seuls les 10 champs utiles et non identifiants sont conservés | art. 5, par. 1, point c |
| **Exactitude** | validation de schéma à chaque étape ; rejet des lignes incohérentes | art. 5, par. 1, point d |
| **Limitation de conservation** | bronze : 90 jours à compter de la **réception** (`purger_bronze`) ; durée des couches argent et or à fixer avec le métier (hypothèse : 13 mois, pour comparer d'une année sur l'autre) | art. 5, par. 1, point e |
| **Intégrité et confidentialité** | masquage, secrets, TLS, droits minimaux, journaux d'audit | art. 5, par. 1, point f ; art. 32 |
| **Protection dès la conception et par défaut** | le masquage est dans le code d'ingestion, pas dans une procédure | art. 25 |
| **Registre des traitements** | à tenir par le responsable ; les tableaux de ce dossier fournissent la matière (finalités, catégories de données, durées, mesures) | art. 30 |
| **Analyse d'impact (AIPD)** | **probablement requise** (évaluation systématique par traitement automatisé à grande échelle) ; à faire avec le DPO avant production réelle | art. 35 |
| **Décision automatisée** | le système **n'alerte qu'un humain** : « l'alerte est un signal, pas une décision de blocage » (texte de chaque notification). Aucune décision produisant des effets juridiques n'est prise sans intervention humaine. Si un blocage automatique était ajouté, l'article 22 s'appliquerait pleinement (information, droit à l'intervention humaine, contestation) | art. 22 |
| **Droit d'accès, d'effacement, d'opposition** | une personne qui fournit son numéro de carte permet à l'exploitant (seul détenteur de la clé) de recalculer `carte_hash` puis de retrouver et supprimer ses lignes dans le bronze, l'argent et les alertes ; la DLQ ne contient aucun numéro. Procédure à outiller | art. 15, 17, 21 |
| **Sous-traitance et transferts** | l'API est hébergée par un tiers hors Union européenne (Space Hugging Face, région « us ») ; elle est ici une **source**, pas un sous-traitant. En production réelle, le flux viendrait du prestataire de paiement, sous contrat | art. 28, chapitre V |

## 4. PCI DSS (version 4.0.1, par transposition)

Hypothèse : la donnée de paiement est un numéro de carte (PAN). Périmètre :

| Zone | Contient un PAN ? | Conséquence |
|---|---|---|
| Producteur (mémoire uniquement) | **oui, le temps d'une ligne** | appartient à l'environnement de données de titulaires de cartes (CDE) ; à durcir et à auditer |
| File, bronze, argent, or, DLQ, rapports, journaux, MLflow, Airflow | **non** (empreinte à clé + 4 chiffres) | hors périmètre de stockage du PAN |

| Exigence (v4.0.1) | Réponse |
|---|---|
| 3.4.1 — PAN masqué à l'affichage (au plus les 4 derniers chiffres affichables) | rapports, alertes, journaux : `****1234` seulement |
| 3.5.1 — PAN rendu illisible partout où il est stocké | il n'est stocké nulle part |
| 3.5.1.1 — hachage par fonction à clé | HMAC-SHA256 à clé secrète, non un hachage simple |
| 3.6 — clés protégées | clé dans un secret monté en lecture seule, hors dépôt et hors image |
| 4.2.1 — cryptographie forte pour les transmissions sur réseau ouvert | API en HTTPS exigé ; SMTP en TLS exigé ; webhook en HTTPS exigé (voir section 5) |
| 10.2 — journaux d'audit | journaux JSON structurés, sans PAN (`06_*` et `04_exploitation.md`) |

Limite : le périmètre exact, la rotation de clé, l'accès à la clé et la segmentation réseau du
producteur relèvent d'une évaluation par un évaluateur qualifié (QSA).

## 5. Chiffrement des transferts entre zones — indicateur 5.3

> *« Le candidat justifie-t-il l'utilisation de protocoles sécurisés pour le transfert des données
> entre les zones ? »*

| Flux | Protocole | État dans le projet | Justification |
|---|---|---|---|
| API → producteur | **HTTPS** (TLS, certificat vérifié par `requests`) | **imposé** : la configuration de production refuse une URL non HTTPS (test) | la ligne brute contient des données personnelles et un numéro de carte : c'est le flux le plus sensible, sur Internet |
| Producteur / consommateur ↔ Kafka | TLS (+ SASL) en production réelle | **pris en charge** (variables `FRAUDE_KAFKA_PROTOCOLE_SECURITE`, `_SASL_*`, `_CA`), **non activé** dans la production locale : trafic en clair sur un réseau Docker interne, sans port exposé hors `127.0.0.1` | le message est masqué ; sur un réseau interne isolé le risque est faible. En production réelle : `SASL_SSL` |
| Consommateur / DAG ↔ PostgreSQL | TLS (`sslmode=require`) en production réelle | **non activé** localement (aucun certificat) ; se règle dans le DSN secret | idem ; l'entrepôt ne contient aucune donnée identifiante directe |
| Applications ↔ MLflow | HTTPS derrière un mandataire inverse en production réelle | HTTP interne en production locale ; intégrité des modèles garantie indépendamment (SHA-256 vérifié avant le chargement du pickle) | l'artefact est un fichier exécutable au chargement : on contrôle son empreinte, pas seulement le canal |
| Notification par e-mail | **STARTTLS** (587) ou TLS implicite (465), certificat et nom d'hôte vérifiés | **imposé** en production ; **les identifiants SMTP sont refusés sans TLS** (test) | l'alerte porte un montant, un commerçant, 4 chiffres de carte |
| Notification par webhook | **HTTPS** | **imposé** en production (test) ; l'URL (qui contient souvent un jeton) est un secret | idem |
| Interfaces Airflow, MLflow | HTTP sur `127.0.0.1` seulement | ports publiés uniquement sur la boucle locale ; en production réelle : mandataire inverse TLS + authentification unique | pas d'accès réseau aux consoles d'administration |
| Données au repos | volumes Docker | non chiffrés dans la production locale ; en production réelle : chiffrement du disque ou du volume géré par le fournisseur | les couches ne contiennent pas d'identifiant direct |

## 6. Gestion des secrets — indicateur 5.2

> *« Les identifiants de connexion aux bases/API sont-ils externalisés […] et non écrits en clair
> dans le code ? »*

| Secret | Utilisé par | En développement | Production locale (Docker) |
|---|---|---|---|
| Clé de hachage des cartes (`SEL_HACHAGE`) | producteur, entraînement, DAG | variable d'environnement, sinon fichier généré au premier lancement dans `donnees/` (jamais versionné) | secret Docker `sel_hachage` |
| Chaîne de connexion de l'entrepôt (`DSN_ENTREPOT`) | consommateur, DAG | SQLite, aucun secret | secret Docker `dsn_entrepot` |
| Mots de passe PostgreSQL (un rôle par composant) | PostgreSQL, MLflow, Airflow, application | — | secrets `pg_*_motdepasse`, lus par `POSTGRES_PASSWORD_FILE` et par le script d'initialisation |
| Fernet, clé secrète et compte administrateur d'Airflow | Airflow | — | secrets montés, lus par les variables `..._CMD` d'Airflow |
| Identifiants SMTP, URL du webhook | notification | variables d'environnement | secrets `smtp_utilisateur`, `smtp_mot_de_passe`, `webhook_url` |
| Identifiants Kafka (SASL) | file | variables d'environnement | à fournir pour une production réelle |

Mécanisme unique, `configuration.lire_secret(nom)` : lit `FRAUDE_<NOM>_FICHIER` (fichier monté,
prioritaire), sinon `FRAUDE_<NOM>`. Les secrets **ne sont jamais conservés** dans l'objet de
configuration (pas de fuite par `repr`, test dédié) ; les fichiers `config/*.yaml` n'en contiennent
aucun (test qui échoue si un mot comme « password » ou « token » y apparaît) ; la configuration
de production **refuse de démarrer** sans clé de hachage ni DSN, et refuse un DSN à identifiants
écrit dans un YAML. `scripts/initialiser_secrets.py` génère les secrets (valeurs aléatoires fortes,
non affichées, jamais écrasées) dans `secrets/`, dossier exclu du dépôt par le `.gitignore` ; un
test vérifie que ce `.gitignore` protège `secrets/`, `.env`, le jeu de données et les bases. Les
images Docker n'embarquent aucun secret (`.dockerignore`).

**Rotation** : mot de passe d'un service → régénérer le secret et recréer le conteneur concerné ;
clé de hachage → **change toutes les empreintes** (coupure de l'historique de carte) : à
planifier avec un retraitement et une période de double lecture, hors périmètre de ce projet ;
jeton de webhook → remplacer le fichier et redémarrer le consommateur.

## 7. Autres mesures de sécurité

| Sujet | Mesure |
|---|---|
| Injection SQL | requêtes paramétrées (`:nom`) partout ; seuls des noms de tables constants sont assemblés dans le texte SQL |
| Page HTML du rapport | toute valeur est échappée (`html.escape`), test avec une balise `<script>` ; aucune ressource externe |
| Journaux | JSON, une ligne par événement : pas d'injection de lignes ; nettoyage des champs sensibles |
| Modèle exporté (format pickle) | empreinte SHA-256 vérifiée **avant** chargement ; dossier des modèles en écriture réservée au job d'entraînement |
| Source non fiable | l'API est traitée comme hostile : bornes, expressions régulières, longueurs, types stricts (un booléen n'est pas un nombre) |
| Conteneurs | utilisateur non privilégié (uid 50000), ports sur `127.0.0.1`, mémoire et CPU bornés, contrôles de santé, journaux avec rotation, un rôle et une base PostgreSQL distincts pour Airflow, MLflow et l'entrepôt |
| Canal d'alerte | un canal en panne ne bloque pas les autres ; l'échec est visible (statut `echec`, constat de supervision) |

## 8. Risques résiduels et suites

| Risque | Niveau | Traitement proposé (voir `05_backlog_sprints.md`) |
|---|---|---|
| Réidentification par recoupement des quasi-identifiants masqués | moyen | AIPD ; contrôle d'accès à l'entrepôt ; réduction des durées ; k-anonymat sur les exports |
| Compromission de la clé de hachage | élevé | coffre de secrets avec audit d'accès, séparation des rôles, rotation planifiée |
| TLS absent entre services en production locale | faible en local, **inacceptable en production réelle** | TLS Kafka et PostgreSQL, mTLS entre services |
| Producteur dans le périmètre PCI | à évaluer | segmentation réseau, QSA |
| Alertes en clair par e-mail | moyen | webhook vers un outil d'astreinte authentifié ; limiter les champs |

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
