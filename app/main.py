from __future__ import annotations
import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.aggregator import aggregate_report
from app.analyzer import analyze_reviews
from app.job_store import job_store
from app.models import (
    FullReport,
    JobStatus,
    JobStatusResponse,
    StartJobRequest,
    StartJobResponse,
)
from app.scraper import ScraperError, _extract_company_slug, scrape_trustpilot

logging.basicConfig(level=logging.INFO)
logging.getLogger("app.scraper").setLevel(logging.DEBUG)  # show per-page HTTP status
logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
REVIEWS_DIR = DATA_DIR / "reviews"
REPORTS_DIR = DATA_DIR / "reports"


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file."
        )
    for d in [REVIEWS_DIR, REPORTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Trustpilot CRO Analyzer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_index():
    return FileResponse("app/static/index.html")


@app.post("/api/jobs", response_model=StartJobResponse, status_code=202)
async def start_job(payload: StartJobRequest, background_tasks: BackgroundTasks):
    url = payload.trustpilot_url.strip()
    if not url.startswith("https://www.trustpilot.com/review/"):
        raise HTTPException(
            status_code=422,
            detail="URL must start with https://www.trustpilot.com/review/",
        )
    try:
        company_slug = _extract_company_slug(url)
    except ScraperError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    job = job_store.create_job(url, company_slug)
    background_tasks.add_task(_run_analysis_pipeline, job.job_id)
    return StartJobResponse(job_id=job.job_id, status=job.status)


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    job = job_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        progress_message=job.progress_message,
        pages_scraped=job.pages_scraped,
        total_reviews=job.total_reviews,
        reviews_analyzed=job.reviews_analyzed,
        error_message=job.error_message,
        report_id=job.report_id,
    )


@app.get("/api/jobs/{job_id}/stream")
async def stream_job_progress(job_id: str):
    async def event_generator():
        tick = 0
        while True:
            job = job_store.get_job(job_id)
            if not job:
                payload = {"status": "error", "error_message": "Job not found", "progress_message": "", "pages_scraped": 0, "total_reviews": 0, "reviews_analyzed": 0, "report_id": None}
                yield f"data: {json.dumps(payload)}\n\n"
                break

            payload = {
                "status": job.status,
                "progress_message": job.progress_message,
                "pages_scraped": job.pages_scraped,
                "total_reviews": job.total_reviews,
                "reviews_analyzed": job.reviews_analyzed,
                "report_id": job.report_id,
                "error_message": job.error_message,
            }
            yield f"data: {json.dumps(payload)}\n\n"

            if job.status in (JobStatus.DONE, JobStatus.ERROR):
                break

            tick += 1
            if tick % 15 == 0:
                yield ": keep-alive\n\n"

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs")
async def list_jobs():
    jobs = job_store.list_jobs()
    return {"jobs": [j.model_dump(mode="json") for j in jobs]}


@app.get("/api/reports/{report_id}")
async def get_report(report_id: str):
    report_path = REPORTS_DIR / f"{report_id}.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return JSONResponse(content=json.loads(report_path.read_text()))


@app.get("/api/reports/{report_id}/export")
async def export_report(report_id: str):
    report_path = REPORTS_DIR / f"{report_id}.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(
        report_path,
        media_type="application/json",
        filename=f"trustpilot_report_{report_id}.json",
    )


# ---------------------------------------------------------------------------
# Pipeline background task
# ---------------------------------------------------------------------------

async def _run_analysis_pipeline(job_id: str) -> None:
    try:
        job = job_store.get_job(job_id)

        # ── Phase 1: Scraping ────────────────────────────────────────────
        await job_store.update_job(
            job_id,
            status=JobStatus.SCRAPING,
            progress_message="Starting browser…",
        )

        async def scrape_progress(msg: str, pages: int, total: int) -> None:
            await job_store.update_job(
                job_id,
                progress_message=msg,
                pages_scraped=pages,
                total_reviews=total,
            )

        reviews, trustpilot_total_reviews = await scrape_trustpilot(job.trustpilot_url, scrape_progress)
        scraped_at = datetime.now(timezone.utc)

        # Cache raw reviews
        reviews_path = REVIEWS_DIR / f"{job.company_slug}.json"
        reviews_path.write_text(
            json.dumps([r.model_dump(mode="json") for r in reviews], indent=2)
        )

        # ── Phase 2: Analyzing ───────────────────────────────────────────
        await job_store.update_job(
            job_id,
            status=JobStatus.ANALYZING,
            progress_message=f"Analyzing {len(reviews)} reviews with Claude…",
            total_reviews=len(reviews),
        )

        async def analysis_progress(done: int, total: int) -> None:
            await job_store.update_job(
                job_id,
                progress_message=f"Analyzed {done}/{total} reviews…",
                reviews_analyzed=done,
            )

        analyses = await analyze_reviews(reviews, analysis_progress)
        analyzed_at = datetime.now(timezone.utc)

        # ── Phase 3: Aggregating ─────────────────────────────────────────
        await job_store.update_job(
            job_id,
            status=JobStatus.AGGREGATING,
            progress_message="Building report…",
            reviews_analyzed=len(analyses),
        )

        full_report = aggregate_report(
            company_slug=job.company_slug,
            trustpilot_url=job.trustpilot_url,
            reviews=reviews,
            analyses=analyses,
            scraped_at=scraped_at,
            analyzed_at=analyzed_at,
            trustpilot_total_reviews=trustpilot_total_reviews,
        )

        # ── Phase 4: Persist ─────────────────────────────────────────────
        report_id = str(uuid.uuid4())
        report_path = REPORTS_DIR / f"{report_id}.json"
        report_path.write_text(
            json.dumps(full_report.model_dump(mode="json"), indent=2)
        )

        await job_store.update_job(
            job_id,
            status=JobStatus.DONE,
            progress_message="Analysis complete!",
            report_id=report_id,
        )

    except Exception as exc:
        logger.exception("Pipeline failed for job %s", job_id)
        await job_store.update_job(
            job_id,
            status=JobStatus.ERROR,
            progress_message="An error occurred.",
            error_message=str(exc),
        )
