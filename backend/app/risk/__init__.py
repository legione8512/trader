"""Applying the risk rules to stored state.

The rules themselves are pure and live in ``app.domain.risk``. This package is
the wiring: it reads the configuration a trading day was opened under, assembles
the context from the database, evaluates, and records the verdict.
"""

from app.risk.assessor import AssessmentResult, RiskAssessor, approved_assessment_id
from app.risk.mapping import DayStateSources, day_state_from_records, limits_from_configuration

__all__ = [
    "AssessmentResult",
    "DayStateSources",
    "RiskAssessor",
    "approved_assessment_id",
    "day_state_from_records",
    "limits_from_configuration",
]
