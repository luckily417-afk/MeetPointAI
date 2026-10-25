"""算法层：候选发现、确定性排序、AI 校验与主流程编排。"""

from .ai_validator import (
    AIValidationError,
    ValidationResult,
    build_ai_messages,
    candidate_payload,
    validate_ai_payload,
)
from .candidates import CancelToken, SearchStats, discover_pois, plan_routes
from .pipeline import (
    AI_FALLBACK_NOTICE,
    NO_RESULT_MESSAGE,
    RecommendRequest,
    RecommendResult,
    RecommendationError,
    RecommendationPipeline,
)
from .ranking import RankContext, rank_candidates, score_candidates, to_recommendations

__all__ = [
    "AIValidationError",
    "ValidationResult",
    "build_ai_messages",
    "candidate_payload",
    "validate_ai_payload",
    "CancelToken",
    "SearchStats",
    "discover_pois",
    "plan_routes",
    "AI_FALLBACK_NOTICE",
    "NO_RESULT_MESSAGE",
    "RecommendRequest",
    "RecommendResult",
    "RecommendationError",
    "RecommendationPipeline",
    "RankContext",
    "rank_candidates",
    "score_candidates",
    "to_recommendations",
]
