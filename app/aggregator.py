from __future__ import annotations
from collections import Counter
from datetime import datetime

from typing import Optional
from app.models import (
    RawReview,
    ReviewAnalysis,
    ReportSummary,
    FullReport,
    SentimentBreakdown,
    NPSEstimate,
    ThemeCount,
    FrictionCount,
    IssueCategory,
    BrandSynthesis,
)


def aggregate_report(
    company_slug: str,
    trustpilot_url: str,
    reviews: list[RawReview],
    analyses: list[ReviewAnalysis],
    scraped_at: datetime,
    analyzed_at: datetime,
    trustpilot_total_reviews: int = 0,
    synthesis: Optional[BrandSynthesis] = None,
) -> FullReport:
    total = len(reviews)

    summary = ReportSummary(
        company_slug=company_slug,
        trustpilot_url=trustpilot_url,
        total_reviews=total,
        trustpilot_total_reviews=trustpilot_total_reviews if trustpilot_total_reviews > total else None,
        avg_rating=_compute_avg_rating(reviews),
        sentiment_breakdown=_compute_sentiment_breakdown(analyses),
        avg_sentiment_score=_compute_avg_sentiment_score(analyses),
        nps_estimate=_compute_nps_estimate(analyses),
        top_themes=_rank_themes(analyses),
        top_friction_points=_rank_friction_points(analyses),
        top_improvement_opportunities=_deduplicate_improvements(analyses),
        high_priority_issue_count=sum(
            1 for a in analyses if a.audit_flags.high_priority_issue
        ),
        issue_categories=_count_issue_categories(analyses),
        scraped_at=scraped_at,
        analyzed_at=analyzed_at,
    )

    return FullReport(summary=summary, reviews=reviews, analyses=analyses, synthesis=synthesis)


# ---------------------------------------------------------------------------
# Sub-aggregators
# ---------------------------------------------------------------------------

def _compute_avg_rating(reviews: list[RawReview]) -> float:
    if not reviews:
        return 0.0
    return round(sum(r.rating for r in reviews) / len(reviews), 2)


def _compute_sentiment_breakdown(analyses: list[ReviewAnalysis]) -> SentimentBreakdown:
    total = len(analyses) or 1
    pos = sum(1 for a in analyses if a.sentiment.overall == "positive")
    neg = sum(1 for a in analyses if a.sentiment.overall == "negative")
    neu = total - pos - neg
    return SentimentBreakdown(
        positive=pos,
        neutral=neu,
        negative=neg,
        positive_pct=round(pos / total * 100, 1),
        neutral_pct=round(neu / total * 100, 1),
        negative_pct=round(neg / total * 100, 1),
    )


def _compute_avg_sentiment_score(analyses: list[ReviewAnalysis]) -> float:
    if not analyses:
        return 0.0
    return round(sum(a.sentiment.score for a in analyses) / len(analyses), 3)


def _compute_nps_estimate(analyses: list[ReviewAnalysis]) -> NPSEstimate:
    total = len(analyses) or 1
    promoters = sum(1 for a in analyses if a.customer_experience.nps_score >= 9)
    detractors = sum(1 for a in analyses if a.customer_experience.nps_score <= 6)
    passives = total - promoters - detractors

    prom_pct = round(promoters / total * 100, 1)
    det_pct = round(detractors / total * 100, 1)
    pass_pct = round(passives / total * 100, 1)
    nps = round(prom_pct - det_pct, 1)

    return NPSEstimate(
        score=nps,
        promoters_pct=prom_pct,
        passives_pct=pass_pct,
        detractors_pct=det_pct,
    )


def _rank_themes(analyses: list[ReviewAnalysis], top_n: int = 10) -> list[ThemeCount]:
    all_themes = [
        t.lower().strip()
        for a in analyses
        for t in a.customer_experience.themes
        if t.strip()
    ]
    total = len(all_themes) or 1
    counter = Counter(all_themes)
    return [
        ThemeCount(theme=theme, count=count, percentage=round(count / total * 100, 1))
        for theme, count in counter.most_common(top_n)
    ]


def _rank_friction_points(
    analyses: list[ReviewAnalysis], top_n: int = 10
) -> list[FrictionCount]:
    all_friction = [
        f.lower().strip()
        for a in analyses
        for f in a.cro_insights.friction_points
        if f.strip()
    ]
    counter = Counter(all_friction)
    return [
        FrictionCount(friction=friction, count=count)
        for friction, count in counter.most_common(top_n)
    ]


def _deduplicate_improvements(
    analyses: list[ReviewAnalysis], top_n: int = 10
) -> list[str]:
    all_improvements = [
        imp.lower().strip()
        for a in analyses
        for imp in a.cro_insights.improvement_opportunities
        if imp.strip()
    ]
    counter = Counter(all_improvements)
    return [imp for imp, _ in counter.most_common(top_n)]


def _count_issue_categories(analyses: list[ReviewAnalysis]) -> list[IssueCategory]:
    high_priority = [a for a in analyses if a.audit_flags.high_priority_issue]
    counter = Counter(a.audit_flags.issue_category for a in high_priority)
    return [
        IssueCategory(category=cat, count=count)
        for cat, count in counter.most_common()
    ]
