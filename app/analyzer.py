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
    BrandInsight,
    BrandSynthesis,
)

logger = logging.getLogger(__name__)

CONCURRENCY      = 15   # parallel Claude requests (no batch API)
DEEP_ANALYSIS_CAP = 150  # max reviews sent to Claude; rest use star-based defaults

MODEL = "claude-haiku-4-5-20251001"

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
    Two-tier analysis strategy:

    Tier 1 — Star-based classification (free, instant, applied to ALL reviews):
        5★ → positive/0.9   4★ → positive/0.7
        3★ → neutral/0.0
        2★ → negative/-0.7  1★ → negative/-0.9

    Tier 2 — Claude deep analysis (up to DEEP_ANALYSIS_CAP reviews):
        Priority: all 1-2★ → all 3★ → random sample of 4-5★
        Extracts: friction_points, improvement_opportunities, issue_category,
                  themes, positives, negatives (used by synthesis)

    Result: ALL reviews have sentiment. Up to 150 have deep Claude insights.
    Synthesis then reads all review texts directly, so themes/friction are
    always comprehensive regardless of which reviews got deep analysis.
    """
    import random
    random.seed(42)

    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ── Tier 1: instant star-based defaults for every review ─────────────────
    base_map: dict[str, ReviewAnalysis] = {
        r.id: _star_based_default(r) for r in reviews
    }
    await progress_callback(0, len(reviews))

    # ── Tier 2: build smart sample for Claude deep analysis ──────────────────
    low_star  = [r for r in reviews if r.rating <= 2]
    mid_star  = [r for r in reviews if r.rating == 3]
    high_star = [r for r in reviews if r.rating >= 4]

    # Fill budget: low-star first (most CRO-relevant), then mid, then high
    budget = DEEP_ANALYSIS_CAP
    sample: list[RawReview] = []

    take_low  = min(len(low_star),  int(budget * 0.50))   # up to 50%
    take_mid  = min(len(mid_star),  int(budget * 0.25))   # up to 25%
    take_high = min(len(high_star), budget - take_low - take_mid)  # remainder

    sample.extend(random.sample(low_star,  take_low)  if take_low  < len(low_star)  else low_star)
    sample.extend(random.sample(mid_star,  take_mid)  if take_mid  < len(mid_star)  else mid_star)
    sample.extend(random.sample(high_star, take_high) if take_high < len(high_star) else high_star)

    logger.info(
        "Deep analysis sample: %d reviews (from %d total) — %d low★, %d mid★, %d high★",
        len(sample), len(reviews), take_low, take_mid, take_high,
    )

    # ── Run Claude on the sample (concurrent, no batch API) ──────────────────
    if sample:
        try:
            deep_results = await _analyze_concurrent(sample, client, progress_callback, len(reviews))
            for analysis in deep_results:
                base_map[analysis.review_id] = analysis
        except Exception as exc:
            logger.warning("Deep analysis failed (%s) — star-based defaults used for all", exc)

    await progress_callback(len(reviews), len(reviews))

    # Return in original order
    return [base_map.get(r.id, _star_based_default(r)) for r in reviews]


# ---------------------------------------------------------------------------
# Concurrent analysis (replaces batch API — faster, no polling loop)
# ---------------------------------------------------------------------------

async def _analyze_concurrent(
    reviews: list[RawReview],
    client: anthropic.AsyncAnthropic,
    progress_callback: Callable[[int, int], Awaitable[None]],
    total_reviews: int,
) -> list[ReviewAnalysis]:
    """
    Run CONCURRENCY Claude calls in parallel, chunked.
    150 reviews at 15 concurrent = 10 chunks ≈ 20 seconds.
    progress_callback reports against total_reviews (full dataset size).
    """
    results: list[ReviewAnalysis] = []
    done = 0

    for i in range(0, len(reviews), CONCURRENCY):
        chunk = reviews[i : i + CONCURRENCY]
        chunk_results = await asyncio.gather(
            *[_analyze_single(r, client) for r in chunk],
            return_exceptions=False,
        )
        results.extend(chunk_results)
        done += len(chunk)
        await progress_callback(done, len(reviews))
        logger.info("Deep analysis: %d/%d reviews done", done, len(reviews))

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


# ---------------------------------------------------------------------------
# Global brand synthesis (one call, holistic CRO insights)
# ---------------------------------------------------------------------------

SYNTHESIS_MODEL = "claude-haiku-4-5-20251001"

async def synthesize_brand_insights(
    reviews: list[RawReview],
    analyses: list[ReviewAnalysis],
) -> BrandSynthesis:
    """
    Makes ONE Claude call across all reviews to produce holistic CRO insights:
    top themes, friction points, improvement opportunities, key insights.

    This fixes the "None detected" problem — per-review analysis returns empty
    friction arrays for 4-5 star reviews. The synthesis sees the full picture.
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Build review_id → analysis map
    analysis_map = {a.review_id: a for a in analyses}

    # Separate by sentiment for focused synthesis
    negative_reviews = [
        r for r in reviews
        if r.rating <= 2 or (
            analysis_map.get(r.id) and
            analysis_map[r.id].sentiment.overall == "negative"
        )
    ]
    neutral_reviews  = [r for r in reviews if r.rating == 3]
    positive_reviews = [r for r in reviews if r.rating >= 4]

    # Build the review digest — all negatives + sample of others
    def _fmt(r: RawReview) -> str:
        title = f" | Title: {r.title}" if r.title else ""
        text  = (r.text or "").strip()[:300]
        return f"[{r.rating}★]{title}\n{text}"

    import random
    random.seed(42)

    neg_block  = "\n\n".join(_fmt(r) for r in negative_reviews[:60])
    neu_block  = "\n\n".join(_fmt(r) for r in neutral_reviews[:20])
    pos_sample = random.sample(positive_reviews, min(40, len(positive_reviews)))
    pos_block  = "\n\n".join(_fmt(r) for r in pos_sample)

    total      = len(reviews)
    avg_rating = round(sum(r.rating for r in reviews) / total, 2) if total else 0
    pos_count  = sum(1 for a in analyses if a.sentiment.overall == "positive")
    neg_count  = sum(1 for a in analyses if a.sentiment.overall == "negative")

    prompt = f"""You are a senior CRO analyst. Analyze these Trustpilot reviews for a brand and produce actionable CRO insights.

=== STATS ===
Total reviews analyzed: {total}
Average rating: {avg_rating}/5
Positive: {round(pos_count/total*100,1) if total else 0}% | Negative: {round(neg_count/total*100,1) if total else 0}%

=== NEGATIVE REVIEWS (most important for friction analysis) ===
{neg_block or "(none)"}

=== NEUTRAL REVIEWS ===
{neu_block or "(none)"}

=== SAMPLE OF POSITIVE REVIEWS ===
{pos_block or "(none)"}

Return a JSON object with exactly this structure:
{{
  "top_themes": [
    {{"title": "short theme name", "description": "1-2 sentence explanation of what customers say", "mentions": <integer>}}
  ],
  "friction_points": [
    {{"title": "specific friction label", "description": "what goes wrong and why it hurts conversions", "mentions": <integer>}}
  ],
  "improvement_opportunities": [
    {{"title": "actionable fix", "description": "what to change and expected CRO impact", "mentions": <integer>}}
  ],
  "key_insights": ["insight 1", "insight 2", "insight 3"]
}}

Rules:
- top_themes: 5 items — what customers mention most (positive AND negative)
- friction_points: up to 5 items — ONLY if real friction exists in the reviews. If the brand is overwhelmingly positive, list real minor friction or leave as empty array []. Never invent friction.
- improvement_opportunities: up to 5 actionable items tied to actual review evidence
- key_insights: exactly 3 high-level CRO observations about this brand's customer experience
- mentions: realistic estimate based on review count
- Be specific and evidence-based. Do not invent issues not present in the reviews."""

    try:
        response = await client.messages.create(
            model=SYNTHESIS_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        data = json.loads(raw)

        def _parse_insights(items: list) -> list[BrandInsight]:
            result = []
            for item in (items or []):
                if isinstance(item, dict) and item.get("title"):
                    result.append(BrandInsight(
                        title=item["title"],
                        description=item.get("description", ""),
                        mentions=int(item.get("mentions", 0)),
                    ))
            return result

        return BrandSynthesis(
            top_themes=_parse_insights(data.get("top_themes", [])),
            friction_points=_parse_insights(data.get("friction_points", [])),
            improvement_opportunities=_parse_insights(data.get("improvement_opportunities", [])),
            key_insights=[str(i) for i in data.get("key_insights", []) if i],
        )

    except Exception as exc:
        logger.warning("Brand synthesis failed: %s", exc)
        return BrandSynthesis(
            top_themes=[], friction_points=[],
            improvement_opportunities=[], key_insights=[],
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
