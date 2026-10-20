# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Types de la partie d'Apache Airflow 2.10 utilisée par les DAG (voir ``dags/``)."""

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Any

from airflow.timetables.interval import CronDataIntervalTimetable

class DAG:
    dag_id: str
    def __init__(
        self,
        dag_id: str,
        description: str | None = None,
        schedule: CronDataIntervalTimetable | str | None = None,
        start_date: datetime | None = None,
        catchup: bool = ...,
        max_active_runs: int = ...,
        default_args: dict[str, Any] | None = None,
        sla_miss_callback: Callable[..., Any] | None = None,
        tags: list[str] | None = None,
        doc_md: str | None = None,
        **kwargs: Any,
    ) -> None: ...
    def __enter__(self) -> DAG: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
