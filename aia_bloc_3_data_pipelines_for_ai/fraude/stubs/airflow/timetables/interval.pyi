# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Types de ``airflow.timetables.interval`` (Airflow 2.10) : ``CronDataIntervalTimetable``."""

from pendulum.tz.timezone import FixedTimezone, Timezone

class CronDataIntervalTimetable:
    def __init__(self, cron: str, timezone: str | Timezone | FixedTimezone) -> None: ...
