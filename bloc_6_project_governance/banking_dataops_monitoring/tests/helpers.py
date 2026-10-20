"""Small helpers shared by the tests (not a test module: nothing here is collected)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest


def bounded(function: Callable[..., Any], *args: Any, seconds: float = 5.0) -> Any:
    """Run ``function`` in a thread and fail the test if it has not returned within ``seconds``.

    Works on every platform. A function that walks a text or a file with an index would not fail an assertion if
    its index stopped advancing: it would loop for ever, and the mutation tool would record a time-out, not a
    detection. Under the watchdog the loop becomes a failed test.
    """
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = function(*args)
        except BaseException as error:  # handed back to the test, which may expect it
            outcome["error"] = error

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(f"{function.__name__} did not terminate within {seconds} s: its index no longer advances")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]
