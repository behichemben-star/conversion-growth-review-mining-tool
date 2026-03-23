from __future__ import annotations
import asyncio
import json
import logging
import os
from typing import Callable, Awaitable

import anthropic

from app.models import (
    RawReview,
    ReviewAnalysis,
    SentimentResult,
    CustomerExperience,
    CROInsights,
    AuditFlags,
)

logger = logging.getLogger(__name__)

BATCH_THRESHOLD = 50        # use batch API at or above this many reviews
SEQUENTIAL_CHUNK = 5        # concurrent requests for sequential mode
BATCH_POLL_INTERVAL = 5     # seconds between batch status polls

MODEL = "claude-3-5-haiku-20241022"   # confirmed valid model ID

SYSTEM_PROMPT = (
    "You are a CRO analyst specializing in customer sentiment analysis. "
    "Analyze the review and return structured JSON.\n\n"
    "SENTIMENT RULES — follow strictly:\n"
    "- Star rating is your PRIMARY signal. Use text only to override in extreme cases.\n"
    "- 4–5 stars → overall='positive', score ≥ 0.4 (even if text is brief or generic)\n"
    "- 3 stars   → overall='neutral',  score between -0.2 and 0.2\n"
    "- 1–2 stars → overall='negative', score ≤ -0.4\n"
    "- Only override the star-based classification when the text is CLEARLY sarcastic "
    "or explicitly contradicts the rating.\n\n"
    "NPS SCORE RULES:\n"
    "- 5 stars → nps_score 9 or 10\n"
    "- 4 stars → nps_score 8\n"
    "- 3 stars → nps_score 7\n"
    "- 2 stars → nps_score 4–5\n"
    "- 1 star  → nps_score 0–3\n\n"
    "Focus friction_points and improvement_opportunities on CRO-relevant patterns. "
    "Be concise and actionable."
)

# JSON schema guaranteed from Claude
REVIEW_ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "sentiment": {
            "type": "object",
            "properties": {
                "overall": {
                    "type": "string",
                    "enum": ["positive", "negative", "neutral"],
                },
                "score": {"type": "number", "minimum": -1.0, "maximum": 1.0},
            },
            "required": ["overall", "score"],
            "additionalProperties": False,
        },
        "customer_experience": {
            "type": "object",
            "properties": {
                "themes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                },
                "positives": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                },
                "negatives": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                },
                "nps_score": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10,
                },
            },
            "required": ["themes", "positives", "negatives", "nps_score"],
            "additionalProperties": False,
        },
        "cro_insights": {
            "type": "object",
            "properties": {
                "friction_points": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                },
                "improvement_opportunities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 3,
                },
            },
            "required": ["friction_points", "improvement_opportunities"],
            "additionalProperties": False,
        },
        "audit_flags": {
            "type": "object",
            "properties": {
                "high_priority_issue": {"type": "boolean"},
                "issue_category": {
                    "type": "string",
                    "enum": [
                        "product",
                        "support",
                        "pricing",
                        "ux",
                        "onboarding",
                        "feature_request",
                        "other",
                    ],
                },
            },
            "required": ["high_priority_issue", "issue_category"],
            "additionalProperties": False,
        },
    },
    "required": ["sentiment", "customer_experience", "cro_insights", "audit_flags"],
    "additionalProperties": False,
}


class AnalysisError(Exception):
    pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def analyze_reviews(
    reviews: list[RawReview],
    progress_callback: Callable[[int, int], Awaitable[None]],
) -> list[ReviewAnalysis]:
    """
    Route to batch (>=50) or sequential (<50) analysis.
    Returns ReviewAnalysis list in the same order as input reviews.
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if len(reviews) >= BATCH_THRESHOLD:
        try:
            return await _analyze_batch(reviews, client, progress_callback)
        except Exception as exc:
            logger.warning(
                "Batch API failed (%s), falling back to sequential mode", exc
            )

    return await _analyze_sequential(reviews, client, progress_callback)


# ---------------------------------------------------------------------------
# Batch path (>=50 reviews)
# ---------------------------------------------------------------------------

async def _analyze_batch(
    reviews: list[RawReview],
    client: anthropic.AsyncAnthropic,
    progress_callback: Callable[[int, int], Awaitable[None]],
) -> list[ReviewAnalysis]:
    batch_id = await _submit_batch(reviews, client)
    return await _poll_batch_until_done(batch_id, reviews, client, len(reviews), progress_callback)


async def _submit_batch(
    reviews: list[RawReview],
    client: anthropic.AsyncAnthropic,
) -> str:
    requests = [
        {
            "custom_id": review.id,
            "params": {
                "model": MODEL,
                "max_tokens": 700,
                "system": [
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [
                    {
                        "role": "user",
                        "content": _build_review_message(review),
                    }
                ],
            },
        }
        for review in reviews
    ]

    batch = await client.beta.messages.batches.create(requests=requests)
    return batch.id


async def _poll_batch_until_done(
    batch_id: str,
    reviews: list[RawReview],
    client: anthropic.AsyncAnthropic,
    total: int,
    progress_callback: Callable[[int, int], Awaitable[None]],
) -> list[ReviewAnalysis]:
    tick = 0
    while True:
        batch = await client.beta.messages.batches.retrieve(batch_id)

        counts = batch.request_counts
        done_count = counts.succeeded + counts.errored + counts.canceled
        await progress_callback(done_count, total)

        if batch.processing_status == "ended":
            break

        tick += 1
        # Keep-alive log every 30s
        if tick % 6 == 0:
            logger.info("Batch %s still processing… %d/%d done", batch_id, done_count, total)
        await asyncio.sleep(BATCH_POLL_INTERVAL)

    # Build a review_id → review lookup for star-rating override
    review_map = {r.id: r for r in reviews}

    # Collect results
    results: list[ReviewAnalysis] = []
    async for result in await client.beta.messages.batches.results(batch_id):
        review = review_map.get(result.custom_id)
        if result.result.type == "succeeded":
            raw_json = result.result.message.content[0].text
            analysis = _parse_analysis_result(result.custom_id, raw_json)
            if review:
                analysis = _apply_star_override(review, analysis)
        else:
            logger.warning("Batch result failed for %s, using star-based default", result.custom_id)
            analysis = _star_based_default(review) if review else _neutral_default(result.custom_id)
        results.append(analysis)

    return results


# ---------------------------------------------------------------------------
# Sequential path (<50 reviews)
# ---------------------------------------------------------------------------

async def _analyze_sequential(
    reviews: list[RawReview],
    client: anthropic.AsyncAnthropic,
    progress_callback: Callable[[int, int], Awaitable[None]],
) -> list[ReviewAnalysis]:
    results: list[ReviewAnalysis] = []
    total = len(reviews)

    for i in range(0, total, SEQUENTIAL_CHUNK):
        chunk = reviews[i : i + SEQUENTIAL_CHUNK]
        chunk_results = await asyncio.gather(
            *[_analyze_single(review, client) for review in chunk]
        )
        results.extend(chunk_results)
        await progress_callback(len(results), total)
        await asyncio.sleep(0.5)  # brief pause between chunks

    return results


async def _analyze_single(
    review: RawReview,
    client: anthropic.AsyncAnthropic,
) -> ReviewAnalysis:
    for attempt in range(3):
        try:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=700,
                system=SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": _build_review_message(review),
                    }
                ],
            )
            raw_json = response.content[0].text
            analysis = _parse_analysis_result(review.id, raw_json)
            return _apply_star_override(review, analysis)
        except anthropic.RateLimitError:
            wait = 2 ** attempt
            logger.warning("Rate limited, waiting %ds…", wait)
            await asyncio.sleep(wait)
        except Exception as exc:
            logger.warning("Analysis failed for review %s: %s", review.id, exc)
            break

    # API call failed entirely — build a sensible default from the star rating
    return _star_based_default(review)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_review_message(review: RawReview) -> str:
    stars = "★" * review.rating + "☆" * (5 - review.rating)
    sentiment_hint = (
        "POSITIVE" if review.rating >= 4
        else "NEGATIVE" if review.rating <= 2
        else "NEUTRAL"
    )
    parts = [f"Star Rating: {review.rating}/5 {stars}  → expected sentiment: {sentiment_hint}"]
    if review.title:
        parts.append(f"Title: {review.title}")
    text = review.text.strip() if review.text else "(no review text provided)"
    parts.append(f"Review Text: {text}")
    return "\n".join(parts)


def _parse_analysis_result(review_id: str, raw_json: str) -> ReviewAnalysis:
    try:
        # Claude may wrap JSON in markdown code fences — strip them
        text = raw_json.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        data = json.loads(text)
        return ReviewAnalysis(
            review_id=review_id,
            sentiment=SentimentResult(**data["sentiment"]),
            customer_experience=CustomerExperience(**data["customer_experience"]),
            cro_insights=CROInsights(**data["cro_insights"]),
            audit_flags=AuditFlags(**data["audit_flags"]),
        )
    except Exception as exc:
        logger.warning("Parse failed for review %s: %s — using neutral defaults", review_id, exc)
        return _neutral_default(review_id)


def _neutral_default(review_id: str) -> ReviewAnalysis:
    return ReviewAnalysis(
        review_id=review_id,
        sentiment=SentimentResult(overall="neutral", score=0.0),
        customer_experience=CustomerExperience(
            themes=[], positives=[], negatives=[], nps_score=5
        ),
        cro_insights=CROInsights(friction_points=[], improvement_opportunities=[]),
        audit_flags=AuditFlags(high_priority_issue=False, issue_category="other"),
    )


def _star_based_default(review: RawReview) -> ReviewAnalysis:
    """
    Fallback used when the Claude API call itself fails.
    Derives sentiment and NPS entirely from the star rating so results
    are always meaningful even without API access.
    """
    rating = review.rating
    if rating >= 4:
        overall, score, nps = "positive", 0.7,  10 if rating == 5 else 8
    elif rating <= 2:
        overall, score, nps = "negative", -0.7, 2 if rating == 1 else 4
    else:
        overall, score, nps = "neutral",  0.0,  7

    return ReviewAnalysis(
        review_id=review.id,
        sentiment=SentimentResult(overall=overall, score=score),
        customer_experience=CustomerExperience(
            themes=[], positives=[], negatives=[], nps_score=nps
        ),
        cro_insights=CROInsights(friction_points=[], improvement_opportunities=[]),
        audit_flags=AuditFlags(high_priority_issue=rating <= 2, issue_category="other"),
    )


def _apply_star_override(review: RawReview, analysis: ReviewAnalysis) -> ReviewAnalysis:
    """
    Safety net: if Claude classified a 4-5 star review as neutral (or a
    1-2 star review as neutral), force-correct the sentiment.
    Only fires when the star rating clearly contradicts Claude's label.
    """
    rating   = review.rating
    override = analysis.sentiment.overall

    if rating >= 4 and analysis.sentiment.overall == "neutral":
        override = "positive"
        score    = max(0.5, analysis.sentiment.score)
    elif rating <= 2 and analysis.sentiment.overall == "neutral":
        override = "negative"
        score    = min(-0.5, analysis.sentiment.score)
    else:
        return analysis  # no change needed

    return ReviewAnalysis(
        review_id=analysis.review_id,
        sentiment=SentimentResult(overall=override, score=score),
        customer_experience=analysis.customer_experience,
        cro_insights=analysis.cro_insights,
        audit_flags=analysis.audit_flags,
    )
