# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Types de ``airflow.timetables.interval`` (Airflow 2.10) : ``CronDataIntervalTimetable``."""

from pendulum.tz.timezone import FixedTimezone, Timezone

class CronDataIntervalTimetable:
    def __init__(self, cron: str, timezone: str | Timezone | FixedTimezone) -> None: ...
