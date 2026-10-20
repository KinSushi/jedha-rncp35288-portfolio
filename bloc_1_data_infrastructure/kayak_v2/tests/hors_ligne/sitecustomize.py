# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Garde « hors ligne » : interdit toute connexion qui sort de la machine.

Python importe ``sitecustomize`` au démarrage de CHAQUE interpréteur dont ``PYTHONPATH`` contient ce
dossier. Les tests du notebook (tests/test_notebook.py) l'imposent au noyau du notebook et, par
héritage de l'environnement, aux processus Python de Spark. Une connexion vers une adresse autre
que la boucle locale, et toute résolution de nom autre que « localhost », lève une erreur : le
notebook ne peut donc interroger ni Booking, ni Nominatim, ni Overpass, ni Open-Meteo. Les
connexions locales restent permises, car le noyau Jupyter et la passerelle Java de Spark en ont
besoin.
"""

import socket
from typing import Any

HOTES_LOCAUX = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::", ""}

FAMILLE_UNIX = getattr(socket, "AF_UNIX", None)  # absente de Python sous Windows
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def _hote_local(hote: Any) -> bool:
    """Vrai pour la boucle locale (``127.x.x.x``, ``::1``, ``localhost``) ou une absence d'hôte."""
    if hote is None:
        return True
    if isinstance(hote, bytes):
        hote = hote.decode("ascii", "replace")
    return hote in HOTES_LOCAUX or str(hote).startswith("127.")


def _refuser(hote: Any) -> None:
    raise OSError(f"réseau interdit (mode hors ligne) : connexion vers {hote!r} refusée")


def _connect_local(self: socket.socket, adresse: Any) -> None:
    if self.family != FAMILLE_UNIX:
        hote = adresse[0] if isinstance(adresse, tuple) and adresse else adresse
        if not _hote_local(hote):
            _refuser(hote)
    return _connect(self, adresse)


def _connect_ex_local(self: socket.socket, adresse: Any) -> int:
    if self.family != FAMILLE_UNIX:
        hote = adresse[0] if isinstance(adresse, tuple) and adresse else adresse
        if not _hote_local(hote):
            _refuser(hote)
    return _connect_ex(self, adresse)


def _getaddrinfo_local(hote: Any, *arguments: Any, **options: Any) -> Any:
    if not _hote_local(hote):
        _refuser(hote)
    return _getaddrinfo(hote, *arguments, **options)


socket.socket.connect = _connect_local  # type: ignore[method-assign,assignment]
socket.socket.connect_ex = _connect_ex_local  # type: ignore[method-assign,assignment]
socket.getaddrinfo = _getaddrinfo_local  # type: ignore[assignment]
