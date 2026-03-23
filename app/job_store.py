from __future__ import annotations
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.models import JobState, JobStatus

DATA_DIR = Path("data")
JOBS_FILE = DATA_DIR / "jobs.json"


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock = asyncio.Lock()
        self._load_from_disk()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_job(self, trustpilot_url: str, company_slug: str) -> JobState:
        now = datetime.now(timezone.utc)
        job = JobState(
            job_id=str(uuid.uuid4()),
            status=JobStatus.PENDING,
            trustpilot_url=trustpilot_url,
            company_slug=company_slug,
            progress_message="Queued…",
            created_at=now,
            updated_at=now,
        )
        self._jobs[job.job_id] = job
        self._persist()
        return job

    async def update_job(self, job_id: str, **kwargs) -> JobState:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job {job_id} not found")
            updated = job.model_copy(
                update={**kwargs, "updated_at": datetime.now(timezone.utc)}
            )
            self._jobs[job_id] = updated
            self._persist()
            return updated

    def get_job(self, job_id: str) -> Optional[JobState]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[JobState]:
        return sorted(
            self._jobs.values(),
            key=lambda j: j.created_at,
            reverse=True,
        )

    # ------------------------------------------------------------------
    # Disk persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            jid: job.model_dump(mode="json")
            for jid, job in self._jobs.items()
        }
        JOBS_FILE.write_text(json.dumps(payload, indent=2, default=str))

    def _load_from_disk(self) -> None:
        if not JOBS_FILE.exists():
            return
        try:
            raw = json.loads(JOBS_FILE.read_text())
            for jid, data in raw.items():
                self._jobs[jid] = JobState(**data)
        except Exception:
            # Corrupt file — start fresh
            self._jobs = {}


# Singleton used across the app
job_store = JobStore()
