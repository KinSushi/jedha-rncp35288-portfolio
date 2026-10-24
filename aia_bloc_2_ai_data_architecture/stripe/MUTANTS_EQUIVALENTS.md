# Mutants équivalents : Stripe (bloc 2)

Un mutant est **équivalent** quand aucun test ne peut le distinguer du code d'origine, parce que les
deux programmes se comportent de la même façon pour toute entrée. Cette note dit ce que `mutmut` a
mesuré sur `stripe_demo/`, `deploiement/` et `dags/`, et nomme chaque mutant équivalent avec sa preuve.

**Résultat : aucun mutant équivalent n'est retenu.** Mesure du 19/10/2026 (après l'ajout de
`stripe_demo/supervision.py` et `dags/stripe_quotidien.py`) : 3 186 mutants, 3 186 tués, 0 survivant,
0 délai dépassé, 0 suspect, 0 sans test. Il n'y a donc rien à nommer ici, et aucun fait de bibliothèque à contrôler à part (le dépôt
n'a pas de `test_equivalent_mutants_premises.py`). Mesure du 12/10/2026, avant ces deux modules : 2 721
mutants, 2 721 tués.

## Mesure

`mutmut` 3.8.0 sous Linux (Ubuntu, Python 3.12.15), 4 processus enfants au plus, un seul fil par bibliothèque de calcul (`OMP_NUM_THREADS`,
`OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `LOKY_MAX_CPU_COUNT` à 1, `JOBLIB_MULTIPROCESSING=0`). Tous
les fichiers de test sont fournis, sauf deux : `test_conformite_depot.py` (il lit l'arborescence entière
du dépôt, absente de la copie de mutation) et `test_postgres_docker.py` (les 6 tests Docker restent
ignorés : aucun Docker dans cette mesure).

Déroulé du 19/10/2026, dans l'ordre :

1. Première mesure complète : 3 198 mutants, 3 164 tués, **34 survivants, tous dans les deux modules
   ajoutés** (les 2 721 mutants des autres modules, inchangés, tous tués). Aucun n'était équivalent :
   - mots-clés et noms SQL en minuscules ou en majuscules (DuckDB ignore la casse) : le test
     `test_les_requetes_des_taches_partent_telles_qu_ecrites` note le texte exact envoyé à DuckDB
     (connexion espion de `tests/aide_demo.py`) ;
   - `if ligne else 0` inatteignable (un `count(*)` rend toujours une ligne) : **redondance retirée**,
     l'extraction et la table d'étape comptent par la fonction commune `compter` ;
   - `compter` sur une somme vide (NULL) : `test_une_somme_sur_une_table_vide_compte_zero` ;
   - capteur `> 0` changé en `> 1` : `test_le_capteur_se_declenche_des_le_premier_paiement_du_jour` ;
   - encodage et fin de ligne du marqueur `_PRET` (identiques sous Linux) : le marqueur est **écrit en
     octets**, ce qui retire ces arguments ;
   - messages d'erreur et textes d'aide altérés (`XX…XX`, `None`) : les tests comparent le message
     entier.
2. Contrôle ciblé des deux modules, avec leurs deux fichiers de test : 465 mutants, 465 tués.
3. Mesure finale complète : 3 186 mutants, 3 165 tués, 0 survivant, **21 délais dépassés**, tous
   dans `mesures.mesures_complementaires` (code inchangé depuis le 12/10, mutants tués le 12/10 et à la
   première mesure du 19/10). La machine exécutait alors plusieurs mesures en parallèle. Ces 21
   mutants, rejoués seuls dans la même copie avec 2 processus, sont **tous tués**. Un délai dépassé
   compte comme un mutant non tué : cette mesure n'était donc pas concluante.
4. Mesure finale refaite le 19/10/2026 au soir, après les tests ajoutés pour les littéraux hors
   fonction (voir plus bas) : 3 186 mutants, **3 186 tués**, aucun délai dépassé.

| Module | Mutants | Tués (mesure 4) | Délais dépassés (mesure 3, tués au rejeu) | Survivants |
|---|---:|---:|---:|---:|
| `stripe_demo/console.py` | 4 | 4 | 0 | 0 |
| `stripe_demo/dimensionnement.py` | 159 | 159 | 0 | 0 |
| `stripe_demo/fraude.py` | 193 | 193 | 0 | 0 |
| `stripe_demo/generer_ddl.py` | 360 | 360 | 0 | 0 |
| `stripe_demo/mesures.py` | 502 | 502 | 21 | 0 |
| `stripe_demo/nosql.py` | 239 | 239 | 0 | 0 |
| `stripe_demo/olap.py` | 83 | 83 | 0 | 0 |
| `stripe_demo/oltp.py` | 481 | 481 | 0 | 0 |
| `stripe_demo/pipeline.py` | 380 | 380 | 0 | 0 |
| `stripe_demo/supervision.py` | 108 | 108 | 0 | 0 |
| `stripe_demo/validation_mongo.py` | 40 | 40 | 0 | 0 |
| `stripe_demo/vers_postgres.py` | 256 | 256 | 0 | 0 |
| `deploiement/generer_secrets.py` | 24 | 24 | 0 | 0 |
| `dags/stripe_quotidien.py` | 357 | 357 | 0 | 0 |
| **Total** | **3 186** | **3 186** | **21** | **0** |

`stripe_demo/modele_oltp.py` et `stripe_demo/__init__.py` n'ont aucun mutant : `mutmut` ne mute que le
corps des fonctions, et ces deux fichiers n'en contiennent pas (le premier ne fait que déclarer les
treize tables). Leur couverture est décrite plus bas.

## Les mutants qui avaient l'air équivalents, et ce qui en a été fait

Une première passe complète avait laissé 51 survivants sur 2 722 mutants. Aucun n'était équivalent ; ceux
qui en avaient l'air ont été éliminés de l'une de ces trois façons, sans rien nommer ici.

1. **Le code contenait une redondance : elle a été retirée.**
   - `vers_postgres.lire_secret` : `os.environ.get(nom, "")` devenait `get(nom, None)`, indiscernable
     puisque la ligne suivante refuse tout secret vide ou absent (`if not valeur`). Le défaut `""` était
     inutile : le code lit maintenant `os.environ.get(nom)`, et le mutant `get(None)` est tué.
   - `pipeline.executer` : `alertes.append(id_p)` devenait `append(None)`, indiscernable puisque la liste
     n'était lue que par `len(...)`. Un compteur (`alertes += 1`) remplace la liste.
2. **Le mutant ne diffère que par un argument que la sortie ne montre pas : le test regarde l'argument.**
   - Mots-clés SQL en minuscules ou en majuscules (DuckDB, SQLite et SQLAlchemy ignorent la casse) :
     les tests comparent le texte exact envoyé à la base (`ConnexionEspion`,
     `noter_les_requetes_sqlalchemy` dans `tests/aide_demo.py`).
   - `ensure_ascii=None` à la place de `False`, `to_dict("RECORDS")` à la place de `"records"`,
     `to_string(index=None)` : `noter_les_appels_json`, `noter_les_appels_to_string` et
     `noter_les_appels_to_dict` notent les arguments reçus.
   - Encodage `"UTF-8"` à la place de `"utf-8"` : la fixture `lecture_ecriture_en_utf8`
     (`tests/conftest.py`) refuse toute ouverture de fichier texte dont l'encodage n'est pas exactement
     `"utf-8"`.
3. **Le test existait, mais `mutmut` ne le lançait pas contre le mutant : le test a été réorganisé.**
   - `mutmut` rattache l'appel d'une fonction au seul test pendant lequel elle s'exécute. Une fixture de
     module (l'exécution de référence de la démonstration) s'exécute pendant le premier test qui la
     demande ; les autres tests ne font que relire son résultat et n'étaient jamais lancés contre une
     version altérée de `pipeline.executer` : 33 survivants. Les vérifications sont maintenant
     rassemblées dans un seul test (`test_la_demonstration_en_petit_est_exacte_de_bout_en_bout`).
   - `validation_mongo.schema` garde son résultat (`functools.cache`) : la traduction des schémas
     n'était exécutée que par le premier appel du processus, jamais dans le processus de test d'un
     mutant : 8 survivants. La fixture `schemas_mongo_sans_memoire` vide ce cache avant chaque test.

## Ce que `mutmut` ne mute pas, et comment c'est couvert

`mutmut` ne mute pas le code situé hors des fonctions : constantes de module (`PAYS`, `TAUX`,
`DDL_ETOILE`, `REQUETE_ENTRAINEMENT`, `TABLES_CDC`, `INDEX_MONGO`, `REQUETES`…), valeurs par défaut des
paramètres, décorateurs, déclarations de `modele_oltp.py`, gardes `if __name__ == "__main__"`.

Un contrôle d'appoint, fait hors du dépôt, a muté chacun de ces littéraux (texte, nombre, booléen), un à
la fois, plus, pour chaque long texte SQL, le retrait de chaque ligne, puis a relancé les tests du module.
Il a trouvé 20 mutants que les tests laissaient passer, tous corrigés depuis par des tests (points
d'entrée jamais lancés comme programme, dossiers `secrets/` et `sorties/`, immuabilité de `TableOlap`,
types BSON `number` et `bool`, six lignes du DDL de l'entrepôt qu'une fixture transformait en tests
ignorés). État final au 12/10/2026 : 760 mutants de ce type, 760 tués.

| Module | Littéraux hors fonction | Tués |
|---|---:|---:|
| `stripe_demo/modele_oltp.py` | 238 | 238 |
| `stripe_demo/generer_ddl.py` | 316 | 316 |
| `stripe_demo/oltp.py` | 75 | 75 |
| `stripe_demo/nosql.py` | 48 | 48 |
| `stripe_demo/dimensionnement.py` | 20 | 20 |
| `stripe_demo/olap.py` | 19 | 19 |
| `stripe_demo/fraude.py` | 15 | 15 |
| `deploiement/generer_secrets.py` | 10 | 10 |
| `stripe_demo/validation_mongo.py` | 8 | 8 |
| `stripe_demo/pipeline.py` | 5 | 5 |
| `stripe_demo/mesures.py` | 4 | 4 |
| `stripe_demo/vers_postgres.py` | 2 | 2 |
| `dags/stripe_quotidien.py` (19/10/2026) | 16 | 16 |
| `stripe_demo/supervision.py` (19/10/2026) | 6 | 6 |
| **Total** | **782** | **782** |

Ce contrôle d'appoint n'est pas livré dans ce dépôt. Il n'a pas non plus
muté les opérateurs ni les noms hors fonction, seulement les littéraux. Les douze premiers modules datent
du 12/10/2026 (leur code n'a pas changé depuis). Les deux modules ajoutés le 19/10 (`supervision.py`,
`dags/stripe_quotidien.py`) ont été contrôlés le 19/10, avec leur seul fichier de test : une première
passe a laissé 5 survivants, aucun équivalent, tous tués depuis par un test :
- `REQUETE_RETARD` altérée (texte ajouté, première ligne retirée) : le test sans base comparait la
  requête à elle-même ; elle est maintenant comparée au bloc SQL du document 10, au caractère près ;
- `@dataclass(frozen=True)` passé à `False` : un test vérifie qu'un `Emplacement` lu ne se modifie pas
  (`FrozenInstanceError`) ;
- garde `if __name__ == "__main__"` altérée : la commande `python -m stripe_demo.supervision` est
  lancée par un sous-processus (code 2 sans seuil, aucune lecture de la base) ;
- dossier par défaut du DAG (`sorties`) altéré : un test vérifie que, sans `STRIPE_DOSSIER_DONNEES`,
  le DAG lit `stripe/sorties`, le dossier où la démonstration écrit.
Seconde passe : 22 mutants, 22 tués.

## Limites de la mesure

- Les 6 tests Docker (`STRIPE_TESTS_DOCKER=1`) n'ont pas été exécutés : ils prouvent le comportement du
  DDL sur un vrai PostgreSQL 16, que les mutants n'atteignent pas autrement que par le texte généré.
- Node.js est absent de l'environnement Linux de mesure : les 4 tests de `test_requetes_mongo_js.py` y sont
  ignorés pendant la mutation (ils comparent des constantes de `nosql.py`, pas des corps de fonctions) ;
  ils passent dans la suite complète lancée sous Windows (924 réussis, 6 ignorés, le 19/10/2026).
- Le code Terraform n'est pas du Python ; il n'est pas muté. Ses garanties sont celles de
  `tests/test_terraform.py` et de `checkov.txt`.
- Un mutant « tué » l'est par un test qui échoue. La valeur d'un test se juge aussi à sa lecture : les
  tests de ce dépôt sont écrits pour être expliqués (une règle `exiger_...`, puis la copie fautive qu'elle
  doit refuser).

---

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
