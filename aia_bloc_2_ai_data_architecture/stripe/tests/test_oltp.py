# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Base transactionnelle de démonstration : moteur, déclencheurs, référentiels, paiements.

Les contraintes d'intégrité de la base sont déjà testées dans `test_oltp_contraintes.py`. Ici, on
vérifie le CODE qui construit la base et fabrique les données : le moteur active les clés
étrangères, les déclencheurs ont la forme attendue, les référentiels sont ceux de la note 02, et
la simulation des paiements respecte ses probabilités (une fraude de base de 0,2 %, plus
probable pour un pays d'IP différent, la nuit, ou pour un gros montant). Comme pour les
événements, deux preuves : l'empreinte d'un lot (graine fixe) et les bornes (faux générateur).
Chaque groupe est suivi de ses refus : identifiant déjà pris, client absent, base déjà remplie.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, date, datetime

import pytest
from aide_aleatoire import AleaEspion
from aide_demo import noter_les_requetes_sqlalchemy
from sqlalchemy import event, insert, text
from sqlalchemy.exc import IntegrityError

from stripe_demo import modele_oltp as m
from stripe_demo import oltp

DEBUT = datetime(2026, 8, 1, tzinfo=UTC)
TABLES_REFERENTIELS = ("pays", "devises", "taux_change", "marchands", "clients",
                       "moyens_paiement", "produits")
TABLES_PAIEMENTS = ("paiements", "litiges", "remboursements")
EMPREINTE_REFERENTIELS = "89378d9a66910bba1456a2a8021260b2ce40d116c2d2cf2c1c80382a2120683a"
EMPREINTE_PAIEMENTS = "f5480d007bd20f991b37da73a8d71429e97357adcf8be22da05f66d49059faac"


@pytest.fixture
def moteur(tmp_path):
    engine = oltp.moteur(str(tmp_path / "oltp.sqlite"))
    oltp.creer(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def base_avec_referentiels(moteur):
    oltp.referentiels(moteur, jours=3)
    return moteur


@pytest.fixture
def base_minimale(moteur):
    """Un seul marchand, un seul client (aux États-Unis) et son moyen de paiement.

    Avec un seul client, « tirer un client au hasard » n'a qu'une réponse : les tests peuvent
    alors contrôler le reste (heure, montant, pays de l'IP, tirage de la fraude) sans hasard.
    """
    with moteur.begin() as cx:
        cx.execute(insert(m.pays), [{"code_pays": c, "nom": n, "region": r}
                                    for c, n, r in oltp.PAYS])
        cx.execute(insert(m.devises), [{"code_devise": c, "nom": n, "decimales": d}
                                       for c, n, d in oltp.DEVISES])
        cx.execute(insert(m.marchands), [{
            "id_marchand": 1, "raison_sociale": "M", "code_pays": "FR", "code_mcc": "5411",
            "statut": "actif", "cree_le": DEBUT}])
        cx.execute(insert(m.clients), [{
            "id_client": 1, "id_marchand": 1, "email_hache": "h", "code_pays": "US",
            "cree_le": DEBUT}])
        cx.execute(insert(m.moyens_paiement), [{
            "id_moyen": 1, "id_client": 1, "type": "carte", "jeton_psp": "pm_1"}])
    return moteur


def _compter(engine, table: str) -> int:
    with engine.connect() as cx:
        return cx.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def _lignes(engine, requete: str) -> list[tuple]:
    with engine.connect() as cx:
        return [tuple(ligne) for ligne in cx.execute(text(requete)).fetchall()]


def _empreinte(engine, tables) -> str:
    """Empreinte SHA-256 du contenu de plusieurs tables (lignes triées par leurs deux clés)."""
    resume = hashlib.sha256()
    for table in tables:
        lignes = _lignes(engine, f"SELECT * FROM {table} ORDER BY 1, 2")
        resume.update(repr((table, lignes)).encode("utf-8"))
    return resume.hexdigest()


# ---------------------------------------------------------------- moteur et déclencheurs


def test_le_moteur_active_les_cles_etrangeres_et_les_declencheurs_ont_la_forme_attendue(
        tmp_path, moteur):
    chemin = tmp_path / "ma_base.sqlite"
    autre = oltp.moteur(str(chemin))
    assert autre.url.drivername == "sqlite"
    assert autre.url.database == str(chemin)
    autre.dispose()

    # SQLite ignore les clés étrangères par défaut ; le moteur les active à chaque connexion.
    for _ in range(2):
        with moteur.connect() as cx:
            assert cx.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    with pytest.raises(IntegrityError), moteur.begin() as cx:
        cx.execute(insert(m.clients), [{
            "id_client": 1, "id_marchand": 99, "email_hache": "h", "code_pays": "FR",
            "cree_le": DEBUT}])

    # L'instruction envoyée à chaque nouvelle connexion est exactement celle-ci.
    ecouteur = next(f for f in moteur.pool.dispatch.connect
                    if f.__name__ == "_activer_cles_etrangeres")
    instructions = []

    class FausseConnexion:
        @staticmethod
        def execute(sql):
            instructions.append(sql)

    ecouteur(FausseConnexion(), None)
    assert instructions == ["PRAGMA foreign_keys = ON"]

    noms = {ligne[0] for ligne in _lignes(
        moteur, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(m.schema.tables) <= noms
    declencheurs = dict(_lignes(
        moteur, "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"))
    assert set(declencheurs) == {"cdc_paiements_insert", "cdc_paiements_update"}
    assert "AFTER INSERT ON paiements" in declencheurs["cdc_paiements_insert"]
    assert "VALUES ('paiements', 'INSERT', NEW.id_paiement, CURRENT_TIMESTAMP)" in (
        declencheurs["cdc_paiements_insert"])
    assert "AFTER UPDATE OF statut ON paiements" in declencheurs["cdc_paiements_update"]
    assert "VALUES ('paiements', 'UPDATE', NEW.id_paiement, CURRENT_TIMESTAMP)" in (
        declencheurs["cdc_paiements_update"])


def test_creer_efface_les_donnees_et_ne_double_pas_les_declencheurs(base_avec_referentiels):
    assert _compter(base_avec_referentiels, "clients") == 2000

    oltp.creer(base_avec_referentiels)
    oltp.creer(base_avec_referentiels)  # rejouable

    for table in TABLES_REFERENTIELS:
        assert _compter(base_avec_referentiels, table) == 0, table
    assert [ligne[0] for ligne in _lignes(
        base_avec_referentiels,
        "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name")] == [
        "cdc_paiements_insert", "cdc_paiements_update"]


def test_la_generation_lit_les_clients_par_une_requete_ecrite_exactement(base_minimale):
    requetes = noter_les_requetes_sqlalchemy(base_minimale)

    oltp.paiements_synthetiques(base_minimale, DEBUT, 2, graine=1, premier_id=1)

    assert "SELECT id_client, id_marchand, code_pays, cree_le FROM clients" in requetes


def test_une_mise_a_jour_d_un_autre_champ_que_le_statut_n_est_pas_journalisee(base_minimale):
    """Le déclencheur d'UPDATE ne regarde que la colonne `statut` : le reste est sans effet."""
    oltp.paiements_synthetiques(base_minimale, DEBUT, 3, graine=1, premier_id=1)
    avant = _compter(base_minimale, "journal_changements")

    with base_minimale.begin() as cx:
        cx.execute(text("UPDATE paiements SET score_risque = 0.5 WHERE id_paiement = 1"))

    assert _compter(base_minimale, "journal_changements") == avant


# ---------------------------------------------------------------- référentiels


def test_les_referentiels_ont_les_volumes_l_empreinte_et_les_hachages_de_la_note_02(
        moteur, base_avec_referentiels):
    assert {t: _compter(base_avec_referentiels, t) for t in TABLES_REFERENTIELS} == {
        "pays": 8, "devises": 5, "taux_change": 15, "marchands": 40, "clients": 2000,
        "moyens_paiement": 2000, "produits": 80}
    assert _empreinte(base_avec_referentiels, TABLES_REFERENTIELS) == EMPREINTE_REFERENTIELS
    # Courriel haché avec le sel de démonstration ; jeton du prestataire issu du hachage de l'id.
    attendu = hashlib.sha256(b"sel-demo:client1@exemple.org").hexdigest()
    assert _lignes(base_avec_referentiels,
                   "SELECT email_hache FROM clients WHERE id_client = 1") == [(attendu,)]
    jeton = "pm_" + hashlib.sha256(b"sel-demo:7").hexdigest()[:24]
    assert _lignes(base_avec_referentiels,
                   "SELECT jeton_psp FROM moyens_paiement WHERE id_moyen = 7") == [(jeton,)]


def test_les_taux_couvrent_soixante_jours_par_defaut_et_partent_de_la_date_demandee(
        tmp_path, moteur):
    oltp.referentiels(moteur)

    assert _compter(moteur, "taux_change") == 5 * 60
    assert _lignes(moteur, "SELECT min(date_taux), max(date_taux) FROM taux_change") == [
        ("2026-08-01", "2026-09-29")]
    autre = oltp.moteur(str(tmp_path / "autre.sqlite"))
    oltp.creer(autre)
    oltp.referentiels(autre, jours=2, debut=date(2027, 1, 30))
    assert _lignes(autre, "SELECT DISTINCT date_taux FROM taux_change ORDER BY 1") == [
        ("2027-01-30",), ("2027-01-31",)]
    autre.dispose()


def test_les_referentiels_aux_bornes_basses_et_hautes_et_leurs_tirages_exacts(
        tmp_path, moteur, monkeypatch):
    espion = AleaEspion(randint="min", choix=0)
    monkeypatch.setattr(oltp.random, "Random", espion)
    oltp.referentiels(moteur, jours=3)

    assert espion.graines == [42]
    assert _lignes(moteur, "SELECT code_devise, date_taux, taux_vers_usd FROM taux_change "
                           "WHERE code_devise = 'EUR' ORDER BY date_taux") == [
        ("EUR", "2026-08-01", 1.0692), ("EUR", "2026-08-02", 1.0692),
        ("EUR", "2026-08-03", 1.0692)]  # 1,08 x 0,99
    assert _lignes(moteur, "SELECT * FROM marchands WHERE id_marchand = 1") == [
        (1, "Marchand 001", "FR", "5411", "actif", "2025-01-01 00:00:00.000000")]
    assert _lignes(moteur, "SELECT id_client, id_marchand, code_pays, cree_le "
                           "FROM clients WHERE id_client = 1") == [
        (1, 1, "FR", "2026-01-01 00:00:00.000000")]
    assert _lignes(moteur, "SELECT type, marque, quatre_derniers, pays_emission "
                           "FROM moyens_paiement WHERE id_moyen = 1") == [
        ("carte", "visa", "0000", "FR")]
    assert _lignes(moteur, "SELECT prix_mineur, code_devise, periodicite FROM produits "
                           "WHERE id_produit = 1") == [(499, "EUR", None)]
    pays = tuple(oltp.PAYS)
    codes = tuple(p[0] for p in oltp.PAYS)
    assert Counter(espion.appels) == Counter({
        ("uniform", (-0.01, 0.01), ()): 5 * 3,
        ("choice", (pays[:6],), ()): 40,
        ("choice", (tuple(oltp.MCC),), ()): 40,
        ("choices", (codes, (20, 15, 5, 30, 10, 8, 7, 5)), (("k", 1),)): 2000,
        ("randint", (1, 40), ()): 2000,
        ("randint", (0, 240), ()): 2000,
        ("choices", (("carte", "sepa", "portefeuille"), (80, 10, 10)), (("k", 1),)): 2000,
        ("choice", (("visa", "mastercard", "amex"),), ()): 2000,
        ("randint", (0, 9999), ()): 2000,
        ("choice", ((499, 999, 1999, 4999),), ()): 80,
        ("choice", ((None, "mois", "an"),), ()): 80,
    })

    haute = oltp.moteur(str(tmp_path / "haute.sqlite"))
    oltp.creer(haute)
    monkeypatch.setattr(oltp.random, "Random", AleaEspion(randint="max", choix=-1))
    oltp.referentiels(haute, jours=2)
    assert _lignes(haute, "SELECT taux_vers_usd FROM taux_change WHERE code_devise = 'BRL' "
                          "ORDER BY date_taux") == [(0.1818,), (0.1818,)]  # 0,18 x 1,01
    assert _lignes(haute, "SELECT code_pays, code_mcc FROM marchands "
                          "WHERE id_marchand = 40") == [("SG", "4121")]
    assert _lignes(haute, "SELECT id_marchand, code_pays, cree_le FROM clients "
                          "WHERE id_client = 2000") == [(40, "NG", "2026-08-29 00:00:00.000000")]
    assert _lignes(haute, "SELECT type, marque, quatre_derniers, pays_emission "
                          "FROM moyens_paiement WHERE id_moyen = 2000") == [
        ("portefeuille", "amex", "9999", "NG")]
    assert _lignes(haute, "SELECT prix_mineur, periodicite FROM produits "
                          "WHERE id_produit = 80") == [(4999, "an")]
    haute.dispose()


def test_relancer_les_referentiels_sur_une_base_remplie_est_refuse_et_laisse_la_base_intacte(
        base_avec_referentiels):
    avant = _empreinte(base_avec_referentiels, TABLES_REFERENTIELS)

    with pytest.raises(IntegrityError):
        oltp.referentiels(base_avec_referentiels, jours=3)

    assert _empreinte(base_avec_referentiels, TABLES_REFERENTIELS) == avant


def test_les_dates_de_creation_des_referentiels_sont_envoyees_en_utc(moteur):
    """SQLite range les dates sans fuseau : seul ce qui est envoyé à la base montre l'UTC."""
    envoyees = []

    def noter(_connexion, _instruction, multiparametres, _parametres, _options):
        for lot in multiparametres:
            envoyees.extend(lot if isinstance(lot, list) else [lot])

    event.listen(moteur, "before_execute", noter)

    oltp.referentiels(moteur, jours=2)

    dates = [valeur for ligne in envoyees for valeur in ligne.values()
             if isinstance(valeur, datetime)]
    assert {d.year for d in dates} == {2025, 2026}  # marchands (2025) puis clients (2026)
    assert all(d.tzinfo is UTC for d in dates)


def test_une_ligne_de_pays_ou_de_devise_incomplete_est_refusee_et_rien_n_est_ecrit(
        moteur, monkeypatch):
    """Un pays sans région ou une devise sans décimales : refus net, pas de ligne tronquée."""
    monkeypatch.setattr(oltp, "PAYS", [*oltp.PAYS, ("XX", "Xland")])
    with pytest.raises(ValueError, match="shorter"):
        oltp.referentiels(moteur, jours=2)
    assert _compter(moteur, "pays") == 0
    monkeypatch.undo()

    monkeypatch.setattr(oltp, "DEVISES", [*oltp.DEVISES, ("XXX", "monnaie")])
    with pytest.raises(ValueError, match="shorter"):
        oltp.referentiels(moteur, jours=2)
    assert _compter(moteur, "devises") == 0


# ---------------------------------------------------------------- paiements synthétiques


def _paiement(engine, id_paiement: int = 1) -> tuple:
    return _lignes(engine, "SELECT id_paiement, id_marchand, id_client, id_moyen, "
                           "montant_mineur, code_devise, statut, cree_le, pays_ip, appareil, "
                           f"cle_idempotence FROM paiements WHERE id_paiement = {id_paiement}")[0]


def _simuler(engine, monkeypatch, *, randint="max", choix=0, montant=100.0, secondes=43_200,
             tirages=(0.5, 0.5), graine=5, premier_id=1) -> AleaEspion:
    """Fabrique UN paiement avec un faux générateur.

    `tirages` : valeurs successives de `random()` (la 1re décide du pays de l'IP, la 2e de la
    fraude) ; `secondes` : seconde du jour tirée ; `montant` : tirage log-normal.
    """
    espion = AleaEspion(randint=randint, choix=choix, reponses={(0, 86_399): [secondes]})
    file_de_tirages = list(tirages)

    def tirer_random():
        espion.noter("random")
        return file_de_tirages.pop(0)

    def tirer_lognormvariate(mu, sigma):
        espion.noter("lognormvariate", mu, sigma)
        return montant

    espion.random = tirer_random
    espion.lognormvariate = tirer_lognormvariate
    monkeypatch.setattr(oltp.random, "Random", espion)
    oltp.paiements_synthetiques(engine, DEBUT, 1, graine=graine, premier_id=premier_id)
    return espion


def test_les_paiements_sont_reproductibles_numerotes_a_la_suite_et_un_lot_rejoue_est_refuse(
        base_avec_referentiels):
    dernier = oltp.paiements_synthetiques(base_avec_referentiels, DEBUT, 25, graine=1000,
                                          premier_id=100)
    assert dernier == 124
    assert _lignes(base_avec_referentiels, "SELECT min(id_paiement), max(id_paiement), "
                                           "count(*) FROM paiements") == [(100, 124, 25)]
    assert _lignes(base_avec_referentiels, "SELECT cle_idempotence FROM paiements "
                                           "ORDER BY id_paiement LIMIT 3") == [
        ("idem-1000-0",), ("idem-1000-1",), ("idem-1000-2",)]  # graine et rang

    # Un identifiant déjà pris, ou la même graine rejouée : refusé, base intacte.
    avant = _empreinte(base_avec_referentiels, TABLES_PAIEMENTS)
    with pytest.raises(IntegrityError):
        oltp.paiements_synthetiques(base_avec_referentiels, DEBUT, 5, graine=2, premier_id=110)
    with pytest.raises(IntegrityError):
        oltp.paiements_synthetiques(base_avec_referentiels, DEBUT, 5, graine=1000,
                                    premier_id=500)
    assert _empreinte(base_avec_referentiels, TABLES_PAIEMENTS) == avant

    base_avec_referentiels.dispose()
    oltp.creer(base_avec_referentiels)
    oltp.referentiels(base_avec_referentiels, jours=3)
    oltp.paiements_synthetiques(base_avec_referentiels, DEBUT, 300, graine=1000, premier_id=1)
    assert _empreinte(base_avec_referentiels, TABLES_PAIEMENTS) == EMPREINTE_PAIEMENTS
    instants = _lignes(base_avec_referentiels, "SELECT min(cree_le), max(cree_le) FROM paiements")
    assert "2026-08-01 00:00:00" <= instants[0][0]
    assert instants[0][1] <= "2026-08-01 23:59:59.000000"


def test_sans_client_ou_sans_devise_aucun_paiement_n_est_fabrique(moteur, base_minimale):
    with base_minimale.begin() as cx:
        cx.execute(text("INSERT INTO pays VALUES ('JP', 'Japon', 'Asie-Pacifique')"))
        cx.execute(text("UPDATE clients SET code_pays = 'JP' WHERE id_client = 1"))
    with pytest.raises(KeyError):  # le pays du client n'a pas de devise dans DEVISE_PAR_PAYS
        oltp.paiements_synthetiques(base_minimale, DEBUT, 1, graine=1, premier_id=1)
    assert _compter(base_minimale, "paiements") == 0

    with base_minimale.begin() as cx:
        cx.execute(text("DELETE FROM moyens_paiement"))
        cx.execute(text("DELETE FROM clients"))
    with pytest.raises(IndexError):  # aucun client à qui attribuer un paiement
        oltp.paiements_synthetiques(base_minimale, DEBUT, 3, graine=1, premier_id=1)
    assert _compter(base_minimale, "paiements") == 0


def test_un_paiement_aux_bornes_basses_et_hautes_et_selon_chaque_statut_et_appareil(
        base_minimale, monkeypatch):
    # Minuit pile, montant minuscule : le plancher de 50 unités mineures s'applique.
    espion = _simuler(base_minimale, monkeypatch, randint="min", choix=0, montant=1.0,
                      secondes=0, tirages=(0.5, 0.5), graine=5, premier_id=1)
    assert _paiement(base_minimale, 1) == (
        1, 1, 1, 1, 50, "USD", "reussi", "2026-08-01 00:00:00.000000", "US", "mobile",
        "idem-5-0")
    assert espion.graines == [5]
    assert [nom for nom, _, _ in espion.appels] == [
        "choice", "randint", "lognormvariate", "random", "random", "choices", "choices"]
    assert espion.appels_de("randint") == [((0, 86_399), ())]
    assert espion.appels_de("lognormvariate") == [((7.8, 1.0), ())]
    assert _compter(base_minimale, "litiges") == _compter(base_minimale, "remboursements") == 0

    # Dernière seconde du jour, statut et appareil derniers de leurs listes : un remboursement.
    _simuler(base_minimale, monkeypatch, randint="max", choix=-1, montant=2440.6,
             secondes=86_399, tirages=(0.5, 0.5), graine=6, premier_id=2)
    assert _paiement(base_minimale, 2) == (
        2, 1, 1, 1, 2440, "USD", "rembourse", "2026-08-01 23:59:59.000000", "US", "api",
        "idem-6-0")
    assert _lignes(base_minimale, "SELECT * FROM remboursements") == [
        (2, 2, 2440, "demande_client", "2026-08-03 23:59:59.000000")]

    # Un tirage de fraude de 0 est inférieur à toute probabilité : contesté, avec son litige.
    espion = _simuler(base_minimale, monkeypatch, montant=100.0, secondes=43_200,
                      tirages=(0.5, 0.0), graine=7, premier_id=3)
    assert _paiement(base_minimale, 3)[6] == "conteste"
    assert _lignes(base_minimale, "SELECT * FROM litiges") == [
        (3, 3, "fraude", "ouvert", "2026-08-01 12:00:00.000000")]
    assert [nom for nom, _, _ in espion.appels].count("choices") == 1  # seul l'appareil est tiré

    # Montant : partie entière du tirage ; plancher à 50 ; 50,9 donne 50 et 51,9 donne 51.
    for numero, (tirage, attendu) in enumerate(((1234.9, 1234), (49.9, 50), (50.9, 50),
                                                (51.9, 51)), start=4):
        _simuler(base_minimale, monkeypatch, montant=tirage, graine=100 + numero,
                  premier_id=numero)
        assert _paiement(base_minimale, numero)[4] == attendu

    # Chaque statut sans fraude, chaque appareil, la devise du pays du client.
    suivant = 10
    for position, statut in enumerate(("reussi", "echoue", "rembourse")):
        espion = _simuler(base_minimale, monkeypatch, choix=position, graine=100 + suivant,
                          premier_id=suivant)
        assert _paiement(base_minimale, suivant)[6] == statut
        assert espion.appels_de("choices")[0] == (
            (("reussi", "echoue", "rembourse"), (92, 5, 3)), (("k", 1),))
        suivant += 1
    for position, appareil in enumerate(("mobile", "ordinateur", "tablette", "api")):
        espion = _simuler(base_minimale, monkeypatch, choix=position, graine=100 + suivant,
                          premier_id=suivant)
        assert _paiement(base_minimale, suivant)[9] == appareil
        assert espion.appels_de("choices")[-1] == (
            (("mobile", "ordinateur", "tablette", "api"), (55, 35, 5, 5)), (("k", 1),))
        assert _paiement(base_minimale, suivant)[5] == oltp.DEVISE_PAR_PAYS["US"] == "USD"
        suivant += 1


def test_le_pays_de_l_ip_est_celui_du_client_au_dessus_de_six_pour_cent_sinon_il_est_tire(
        base_minimale, monkeypatch):
    """Tirage ≤ 0,06 : l'IP vient d'un pays pris au hasard dans `PAYS` (ici le premier, FR)."""
    espion = _simuler(base_minimale, monkeypatch, tirages=(0.06, 0.99), graine=1, premier_id=1)
    assert _paiement(base_minimale, 1)[8] == "FR"
    assert ("choice", (tuple(oltp.PAYS),), ()) in espion.appels

    espion = _simuler(base_minimale, monkeypatch, tirages=(0.0600001, 0.99), graine=2,
                      premier_id=2)
    assert _paiement(base_minimale, 2)[8] == "US"
    assert ("choice", (tuple(oltp.PAYS),), ()) not in espion.appels


# Probabilité de fraude : 0,2 % de base ; +25 % si le pays de l'IP diffère de celui du client ;
# +4 % la nuit (avant 6 h) ; +8 % au-dessus de 30 000 unités mineures. Un paiement est contesté
# quand le tirage uniforme est STRICTEMENT inférieur à cette probabilité.
SIGNAUX = [
    # (IP d'un autre pays, nuit, gros montant, probabilité attendue)
    (False, False, False, 0.002),
    (True, False, False, 0.252),
    (False, True, False, 0.042),
    (False, False, True, 0.082),
    (True, True, True, 0.372),
]


def test_la_probabilite_de_fraude_additionne_les_signaux_et_ses_seuils_sont_stricts(
        base_minimale, monkeypatch):
    numero = 0
    for etranger, nuit, gros, risque in SIGNAUX:
        parametres = {"secondes": 3 * 3600 if nuit else 12 * 3600,
                      "montant": 30_001.0 if gros else 30_000.0}
        pays_ip = 0.0 if etranger else 0.5  # <= 0,06 : IP d'un pays tiré (FR) ; sinon, le client
        for tirage, attendu in ((risque - 1e-6, "conteste"), (risque + 1e-6, "reussi")):
            numero += 1
            _simuler(base_minimale, monkeypatch, tirages=(pays_ip, tirage), graine=numero,
                     premier_id=numero, **parametres)
            assert _paiement(base_minimale, numero)[6] == attendu, (etranger, nuit, gros, tirage)

    # Un tirage EXACTEMENT égal à la probabilité de base (0,002) ne conteste pas le paiement : la
    # comparaison est stricte, la probabilité se lit « tirage < probabilité ».
    numero += 1
    _simuler(base_minimale, monkeypatch, tirages=(0.5, 0.002), graine=numero, premier_id=numero)
    assert _paiement(base_minimale, numero)[6] == "reussi"

    # La nuit s'arrête à six heures exactement ; 30 000 unités ne font pas un gros montant.
    risque_nuit, risque_gros = 0.042, 0.082
    for secondes, attendu in ((6 * 3600 - 1, "conteste"), (6 * 3600, "reussi")):
        numero += 1
        _simuler(base_minimale, monkeypatch, secondes=secondes,
                 tirages=(0.5, risque_nuit - 1e-6), graine=numero, premier_id=numero)
        assert _paiement(base_minimale, numero)[6] == attendu
    for montant, attendu in ((30_001.0, "conteste"), (30_000.0, "reussi")):
        numero += 1
        _simuler(base_minimale, monkeypatch, montant=montant,
                 tirages=(0.5, risque_gros - 1e-6), graine=numero, premier_id=numero)
        assert _paiement(base_minimale, numero)[6] == attendu
