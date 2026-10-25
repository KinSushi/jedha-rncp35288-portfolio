// Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
//
// Requêtes MongoDB en syntaxe mongosh : mêmes pipelines que stripe_demo/nosql.py (le test
// tests/test_requetes_mongo_js.py compare les deux, au caractère près pour les pipelines).
//
// Usage :  mongosh "$URI_MONGO" nosql/requetes_mongo.js
// Prérequis : nosql/creer_collections.js exécuté, collections alimentées.
// En démonstration locale, les résultats équivalents sont produits par mongomock
// (python -m stripe_demo.pipeline) ; ce fichier n'a pas été exécuté sur un serveur MongoDB.

const base = db.getSiblingDB("stripe");

// ---------------------------------------------------------------------------------------------
// 1. Questions métier : pipelines d'agrégation (identiques à nosql.REQUETES / REQUETES_RETOURS)
// ---------------------------------------------------------------------------------------------
const requetes = [
  {
    question: "erreurs de paiement les plus fréquentes (30 jours)",
    collection: "evenements",
    pipeline: [
      { $match: { type: "erreur_paiement" } },
      { $group: { _id: "$erreur.code", nombre: { $sum: 1 } } },
      { $sort: { nombre: -1 } },
    ],
  },
  {
    question: "sessions mobiles abandonnées les plus longues",
    collection: "evenements",
    pipeline: [
      { $match: { type: "abandon", "session.appareil": "mobile" } },
      { $sort: { "session.duree_s": -1 } },
      { $limit: 3 },
      { $project: { _id: 0, id_client: 1, duree_s: "$session.duree_s" } },
    ],
  },
  {
    question: "thèmes des avis négatifs",
    collection: "retours_clients",
    pipeline: [
      { $match: { "analyse.sentiment": "negatif" } },
      { $unwind: "$analyse.themes" },
      { $group: { _id: "$analyse.themes", nombre: { $sum: 1 } } },
      { $sort: { nombre: -1 } },
    ],
  },
];

for (const requete of requetes) {
  print("\n-- " + requete.question);
  printjson(base[requete.collection].aggregate(requete.pipeline).toArray());
}

// ---------------------------------------------------------------------------------------------
// 2. Mise à jour atomique des caractéristiques en ligne (équivalent de
//    nosql.mettre_a_jour_caracteristiques) : $inc, $addToSet et $max sont atomiques, deux
//    paiements simultanés du même client ne perdent aucune mise à jour.
// ---------------------------------------------------------------------------------------------
const paiement = { id_client: 42, montant_usd: 25.5, pays_ip: "FR" };  // valeurs d'exemple
printjson(base.caracteristiques_clients.findOneAndUpdate(
  { id_client: paiement.id_client },
  {
    $inc: { nb_paiements: 1, montant_total_usd: paiement.montant_usd },
    $addToSet: { pays_ip_vus: paiement.pays_ip },
    $max: { montant_max_usd: paiement.montant_usd },
    $set: { mis_a_jour_le: new Date() },
  },
  { upsert: true, returnDocument: "after" },
));

// ---------------------------------------------------------------------------------------------
// 3. Requêtes complémentaires (hors démonstration Python) : chacune s'appuie sur un index.
// ---------------------------------------------------------------------------------------------
// Historique récent d'un client : index composé (id_client, horodatage décroissant).
printjson(base.evenements.find({ id_client: 42 }).sort({ horodatage: -1 }).limit(5).toArray());
// Preuve d'utilisation de l'index : le plan doit montrer IXSCAN et non COLLSCAN.
printjson(base.evenements.find({ id_client: 42 }).sort({ horodatage: -1 })
  .explain("queryPlanner").queryPlanner.winningPlan);
// Paiements les plus suspects : index décroissant sur le score.
printjson(base.signaux_fraude.find({ score: { $gte: 0.8 } }).sort({ score: -1 }).limit(10)
  .toArray());
// Score d'un paiement donné, avec la version du modèle qui l'a calculé.
printjson(base.signaux_fraude.find({ id_paiement: 12345 }).toArray());
