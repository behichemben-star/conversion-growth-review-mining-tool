from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Scraper output
# ---------------------------------------------------------------------------

class RawReview(BaseModel):
    id: str
    rating: int                     # 1-5
    title: Optional[str] = None
    text: str
    published_date: datetime
    consumer_name: str
    is_verified: bool = False
    company_reply: Optional[str] = None


# ---------------------------------------------------------------------------
# Per-review Claude analysis output
# ---------------------------------------------------------------------------

class SentimentResult(BaseModel):
    overall: str                    # "positive" | "negative" | "neutral"
    score: float                    # -1.0 to 1.0


class CustomerExperience(BaseModel):
    themes: list[str]               # max 5
    positives: list[str]            # max 3
    negatives: list[str]            # max 3
    nps_score: int                  # 0-10


class CROInsights(BaseModel):
    friction_points: list[str]              # max 3
    improvement_opportunities: list[str]    # max 3


class AuditFlags(BaseModel):
    high_priority_issue: bool
    issue_category: str             # product/support/pricing/ux/onboarding/feature_request/other


class ReviewAnalysis(BaseModel):
    review_id: str
    sentiment: SentimentResult
    customer_experience: CustomerExperience
    cro_insights: CROInsights
    audit_flags: AuditFlags


# ---------------------------------------------------------------------------
# Aggregated report models
# ---------------------------------------------------------------------------

class ThemeCount(BaseModel):
    theme: str
    count: int
    percentage: float


class FrictionCount(BaseModel):
    friction: str
    count: int


class SentimentBreakdown(BaseModel):
    positive: int
    neutral: int
    negative: int
    positive_pct: float
    neutral_pct: float
    negative_pct: float


class NPSEstimate(BaseModel):
    score: float            # -100 to 100
    promoters_pct: float    # nps_score >= 9
    passives_pct: float     # nps_score 7-8
    detractors_pct: float   # nps_score <= 6


class IssueCategory(BaseModel):
    category: str
    count: int


class ReportSummary(BaseModel):
    company_slug: str
    trustpilot_url: str
    total_reviews: int
    trustpilot_total_reviews: Optional[int] = None  # Trustpilot-reported total; may exceed scraped count
    avg_rating: float
    sentiment_breakdown: SentimentBreakdown
    avg_sentiment_score: float
    nps_estimate: NPSEstimate
    top_themes: list[ThemeCount]
    top_friction_points: list[FrictionCount]
    top_improvement_opportunities: list[str]
    high_priority_issue_count: int
    issue_categories: list[IssueCategory]
    scraped_at: datetime
    analyzed_at: datetime


class FullReport(BaseModel):
    summary: ReportSummary
    reviews: list[RawReview]
    analyses: list[ReviewAnalysis]


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    PENDING     = "pending"
    SCRAPING    = "scraping"
    ANALYZING   = "analyzing"
    AGGREGATING = "aggregating"
    DONE        = "done"
    ERROR       = "error"


class JobState(BaseModel):
    job_id: str
    status: JobStatus
    trustpilot_url: str
    company_slug: str
    progress_message: str = ""
    pages_scraped: int = 0
    total_reviews: int = 0
    reviews_analyzed: int = 0
    error_message: Optional[str] = None
    report_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# API request/response models
# ---------------------------------------------------------------------------

class StartJobRequest(BaseModel):
    trustpilot_url: str


class StartJobResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress_message: str
    pages_scraped: int
    total_reviews: int
    reviews_analyzed: int
    error_message: Optional[str]
    report_id: Optional[str]
