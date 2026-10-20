# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests des trois défauts relevés par les tests de la mise en conformité, puis corrigés.

Chaque test échouait avant la correction :
- le journal HTTP gardait les identifiants d'une URL (``utilisateur:secret@hôte``) ;
- la taille du corps d'une requête texte comptait des caractères, pas des octets ;
- un robots.txt qui commence par une marque d'ordre des octets (BOM) n'était pas lu, et le verdict
  devenait « tout est autorisé ».
"""

import io
import json

import requests

from kayak.collecte.journal import journaliser_requetes
from kayak.collecte.robots import lire_groupes, verdict
from kayak.stockage.data_lake import DataLake


class _Transport(requests.adapters.BaseAdapter):
    """Adaptateur qui répond « 200 » sans réseau."""

    def send(self, requete, **options):
        reponse = requests.Response()
        reponse.status_code = 200
        reponse.raw = io.BytesIO(b"ok")
        reponse.url = requete.url
        reponse.request = requete
        return reponse

    def close(self):
        return None


def _journal(tmp_path, monkeypatch, envoi) -> list[dict]:
    """Journalise une requête faite par ``envoi(session)`` et rend les lignes archivées."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-11")
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    session = requests.Session()
    session.mount("https://", _Transport())
    with journaliser_requetes(lake):
        envoi(session)
    brut = lake.lire("raw/journal_http/date=2026-10-11/journal_http.jsonl").decode("utf-8")
    return [json.loads(ligne) for ligne in brut.splitlines()]


def test_les_identifiants_d_une_url_n_entrent_pas_dans_le_journal(tmp_path, monkeypatch):
    """Seul l'hôte est consigné : ni l'utilisateur ni le mot de passe de l'URL n'y figurent."""
    lignes = _journal(
        tmp_path, monkeypatch, lambda s: s.get("https://utilisateur:secret@h.test:8443/p?q=1")
    )
    assert lignes[0]["hote"] == "h.test:8443"
    assert "secret" not in json.dumps(lignes) and "utilisateur" not in json.dumps(lignes)


def test_la_taille_du_corps_texte_est_comptee_en_octets(tmp_path, monkeypatch):
    """Un corps texte avec des accents pèse plus d'octets que de caractères (« é » = 2 octets)."""
    corps = "données=été"
    lignes = _journal(tmp_path, monkeypatch, lambda s: s.post("https://h.test/p", data=corps))
    assert len(corps) < len(corps.encode("utf-8"))
    assert lignes[0]["octets_envoyes"] == len(corps.encode("utf-8"))


def test_robots_txt_avec_marque_d_ordre_des_octets_est_lu_comme_un_autre():
    """La marque U+FEFF en tête n'empêche pas de reconnaître le premier « User-agent »."""
    texte = "User-agent: *\nDisallow: /prive/\n"
    assert lire_groupes("﻿" + texte) == lire_groupes(texte)
    assert not verdict("﻿" + texte, "Bot/1.0", "/prive/page")["autorise"]


def test_numero_de_ville_a_trois_chiffres_n_est_pas_tronque(tmp_path):
    """« ville_123.json » est la ville 123, pas la 23 : le nom est lu en entier."""
    import pandas as pd

    from kayak.outils_notebook import bilan_overpass

    lake = DataLake(bucket=None, racine_locale=tmp_path)
    lake.ecrire("raw/osm/date=2026-10-11/ville_123.json", json.dumps({"elements": []}))
    villes = {"id_ville": [23, 123], "ville": ["Vingt-trois", "Cent-vingt-trois"]}
    coordonnees = pd.DataFrame(villes)
    bilan = bilan_overpass(lake, "2026-10-11", coordonnees)
    assert bilan["id_ville"].tolist() == [123]
    assert bilan["ville"].tolist() == ["Cent-vingt-trois"]


def test_archive_overpass_au_nom_inattendu_est_refusee(tmp_path):
    """Un fichier .json qui n'est pas « ville_N.json » est refusé avec son nom, pas deviné."""
    import pandas as pd
    import pytest

    from kayak.outils_notebook import bilan_overpass

    lake = DataLake(bucket=None, racine_locale=tmp_path)
    lake.ecrire("raw/osm/date=2026-10-11/autre.json", json.dumps({"elements": []}))
    coordonnees = pd.DataFrame({"id_ville": [1], "ville": ["A"]})
    with pytest.raises(ValueError, match="nom d'archive Overpass inattendu : .*autre.json"):
        bilan_overpass(lake, "2026-10-11", coordonnees)


def test_dossier_de_travail_vide_est_retire_meme_quand_le_bloc_leve(tmp_path):
    """Une exception dans le bloc ne laisse ni dossier temporaire ni dossier de travail vide."""
    import pytest

    from kayak.etl.spark_etl import _dossier_de_travail

    lake = DataLake(bucket=None, racine_locale=tmp_path)
    with pytest.raises(RuntimeError, match="panne"), _dossier_de_travail(lake) as travail:
        assert travail.is_dir()
        raise RuntimeError("panne")
    assert not travail.exists()
    assert not (tmp_path / ".tmp").exists()
