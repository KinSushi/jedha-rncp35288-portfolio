# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Scripts mongosh : ils s'exécutent (contre un simulateur) et reprennent les requêtes de nosql.py.

Pourquoi un simulateur : aucun serveur MongoDB n'est disponible dans les tests. On exécute donc
les fichiers `.js` avec Node.js, en remplaçant l'objet `db` par un enregistreur d'appels : on
vérifie ainsi que le JavaScript est syntaxiquement correct, que les pipelines envoyés au serveur
sont EXACTEMENT ceux de `nosql.py`, et que chaque collection reçoit son validateur et ses index.
Les tests sont ignorés si Node.js n'est pas installé.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from stripe_demo import generer_ddl as g
from stripe_demo import nosql

RACINE = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js absent : simulation mongosh ignorée")

# Simulateur de mongosh : `db.getSiblingDB(...)` renvoie un objet qui consigne chaque appel de
# méthode (collection, méthode, arguments) et se laisse chaîner (`find().sort().limit()`).
ENREGISTREUR = r"""
const __appels = [];
const __sorties = [];
const __copie = (x) => JSON.parse(JSON.stringify(x));
globalThis.print = (texte) => __sorties.push(String(texte));
globalThis.printjson = () => {};
const __chaine = () => new Proxy(function () {}, {
  get: (_c, propriete) => (propriete === "toArray" ? () => [] : __chaine()),
  apply: () => __chaine(),
});
const __collection = (nom) => new Proxy({}, {
  get: (_c, methode) => (...args) => {
    __appels.push({ collection: nom, methode, args: __copie(args) });
    return methode === "aggregate" ? { toArray: () => [] } : __chaine();
  },
});
const __base = new Proxy({}, {
  get: (_c, nom) => {
    if (nom === "getCollectionNames") return () => [];
    if (nom === "runCommand" || nom === "createCollection") {
      return (...args) => __appels.push({
        collection: nom === "runCommand" ? null : args[0], methode: nom, args: __copie(args) });
    }
    return __collection(nom);
  },
});
globalThis.db = { getSiblingDB: () => __base };
const __fichier = process.argv[1];
require("vm").runInThisContext(require("fs").readFileSync(__fichier, "utf8"),
                               { filename: __fichier });
process.stdout.write(JSON.stringify({ appels: __appels, sorties: __sorties }));
"""


def _executer(fichier: Path) -> dict:
    resultat = subprocess.run([NODE, "-e", ENREGISTREUR, str(fichier)], capture_output=True,
                              text=True, encoding="utf-8", timeout=60, check=False)
    assert resultat.returncode == 0, resultat.stderr
    return json.loads(resultat.stdout)


# ---------------------------------------------------------------- règles


def exiger_pipelines_de_nosql_py(sortie: dict) -> None:
    agregations = [(a["collection"], a["args"][0]) for a in sortie["appels"]
                   if a["methode"] == "aggregate"]
    attendu = [("evenements", p) for p in nosql.REQUETES.values()]
    attendu += [("retours_clients", p) for p in nosql.REQUETES_RETOURS.values()]
    assert agregations == attendu


def exiger_intitules_de_nosql_py(sortie: dict) -> None:
    intitules = [s.removeprefix("\n-- ") for s in sortie["sorties"]]
    assert intitules == list(nosql.REQUETES) + list(nosql.REQUETES_RETOURS)


def exiger_mise_a_jour_atomique_en_upsert(sortie: dict) -> None:
    mises_a_jour = [a for a in sortie["appels"] if a["methode"] == "findOneAndUpdate"]
    assert len(mises_a_jour) == 1
    filtre, modification, options = mises_a_jour[0]["args"]
    assert set(modification) == {"$inc", "$addToSet", "$max", "$set"}
    assert options == {"upsert": True, "returnDocument": "after"}
    assert filtre == {"id_client": 42}


def exiger_validateurs_et_index(sortie: dict) -> None:
    creations = {a["args"][0]: a["args"][1] for a in sortie["appels"]
                 if a["methode"] == "createCollection"}
    assert set(creations) == set(g.schemas_mongo())
    for nom, options in creations.items():
        assert options["validator"] == {"$jsonSchema": g.schemas_mongo()[nom]}
        assert options["validationLevel"] == "strict"
        assert options["validationAction"] == "error"
    index = [(a["collection"], a["args"][0], a["args"][1]) for a in sortie["appels"]
             if a["methode"] == "createIndex"]
    assert index == [(c, cles, options) for c, cles, options in g.INDEX_MONGO]
    ttl = [o for _, cles, o in index if "expireAfterSeconds" in o]
    assert ttl == [{"expireAfterSeconds": 90 * 24 * 3600}]


def _appel(sortie: dict, methode: str) -> dict:
    return next(a for a in sortie["appels"] if a["methode"] == methode)


# ---------------------------------------------------------------- les tests


def test_les_pipelines_javascript_sont_ceux_de_nosql_py_et_un_script_faux_est_refuse(tmp_path):
    sortie = _executer(RACINE / "nosql" / "requetes_mongo.js")
    exiger_pipelines_de_nosql_py(sortie)

    # Un pipeline modifié, ou une requête retirée du script, est refusé.
    faux = copy.deepcopy(sortie)
    _appel(faux, "aggregate")["args"][0].append({"$limit": 1})
    with pytest.raises(AssertionError):
        exiger_pipelines_de_nosql_py(faux)
    manquante = copy.deepcopy(sortie)
    manquante["appels"].remove(_appel(manquante, "aggregate"))
    with pytest.raises(AssertionError):
        exiger_pipelines_de_nosql_py(manquante)
    # Un script JavaScript qui ne s'analyse pas fait échouer l'exécution, il n'est pas ignoré.
    casse = tmp_path / "casse.js"
    casse.write_text("db.getSiblingDB('x').evenements.aggregate([;\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="SyntaxError"):
        _executer(casse)


def test_les_intitules_des_questions_sont_ceux_de_nosql_py():
    sortie = _executer(RACINE / "nosql" / "requetes_mongo.js")
    exiger_intitules_de_nosql_py(sortie)

    inversee = copy.deepcopy(sortie)
    inversee["sorties"].reverse()
    with pytest.raises(AssertionError):  # les mêmes questions dans un autre ordre
        exiger_intitules_de_nosql_py(inversee)
    renommee = copy.deepcopy(sortie)
    renommee["sorties"][0] = "\n-- une autre question"
    with pytest.raises(AssertionError):
        exiger_intitules_de_nosql_py(renommee)


def test_la_mise_a_jour_des_caracteristiques_est_atomique_et_en_upsert():
    sortie = _executer(RACINE / "nosql" / "requetes_mongo.js")
    exiger_mise_a_jour_atomique_en_upsert(sortie)

    sans_increment = copy.deepcopy(sortie)
    del _appel(sans_increment, "findOneAndUpdate")["args"][1]["$inc"]
    with pytest.raises(AssertionError):  # sans `$inc`, deux paiements simultanés se perdraient
        exiger_mise_a_jour_atomique_en_upsert(sans_increment)
    sans_upsert = copy.deepcopy(sortie)
    _appel(sans_upsert, "findOneAndUpdate")["args"][2]["upsert"] = False
    with pytest.raises(AssertionError):  # sans upsert, un premier paiement ne créerait rien
        exiger_mise_a_jour_atomique_en_upsert(sans_upsert)
    doublee = copy.deepcopy(sortie)
    doublee["appels"].append(_appel(doublee, "findOneAndUpdate"))
    with pytest.raises(AssertionError):  # une seule mise à jour par exécution
        exiger_mise_a_jour_atomique_en_upsert(doublee)


def test_chaque_collection_recoit_son_validateur_et_ses_index():
    sortie = _executer(RACINE / "nosql" / "creer_collections.js")
    exiger_validateurs_et_index(sortie)

    permissif = copy.deepcopy(sortie)
    _appel(permissif, "createCollection")["args"][1]["validationAction"] = "warn"
    with pytest.raises(AssertionError):  # un validateur qui avertit au lieu de refuser
        exiger_validateurs_et_index(permissif)
    sans_index = copy.deepcopy(sortie)
    sans_index["appels"].remove(_appel(sans_index, "createIndex"))
    with pytest.raises(AssertionError):
        exiger_validateurs_et_index(sans_index)
    sans_collection = copy.deepcopy(sortie)
    sans_collection["appels"].remove(_appel(sans_collection, "createCollection"))
    with pytest.raises(AssertionError):
        exiger_validateurs_et_index(sans_collection)
