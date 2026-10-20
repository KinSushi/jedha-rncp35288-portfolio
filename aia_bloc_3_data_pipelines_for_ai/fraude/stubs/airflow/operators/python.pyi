# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Types de ``airflow.operators.python`` (Airflow 2.10) : seul ``PythonOperator`` est utilisé."""

from collections.abc import Callable
from typing import Any, TypeVar

_T = TypeVar("_T")

class PythonOperator:
    task_id: str
    def __init__(
        self,
        *,
        task_id: str,
        python_callable: Callable[..., Any],
        op_args: list[Any] | None = None,
        op_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None: ...
    def __rshift__(self, other: _T) -> _T: ...
