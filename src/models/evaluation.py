"""Pydantic models for LLM evaluation results."""

from __future__ import annotations

from typing import Optional, Union
from pydantic import BaseModel, Field, field_validator


class SLAEntry(BaseModel):
    value_minutes: Optional[float] = None
    threshold_minutes: float
    status: str  # MET | BREACHED | NOT_APPLICABLE


class SLAStatus(BaseModel):
    first_response_time: SLAEntry
    resolution_time: SLAEntry


class MetricResult(BaseModel):
    metric_id: str
    metric_name: str
    rating: Union[int, str]  # 1–4 or "N/A"
    rating_label: str = ""   # computed by sla.py — LLM need not supply this
    evidence: str = ""
    reasoning: str = ""
    improvement_note: str = ""

    @field_validator("rating")
    @classmethod
    def clamp_rating(cls, v: Union[int, str]) -> Union[int, str]:
        """Ensure numeric rating is within 1–4; clamp 0 → 1."""
        if isinstance(v, int):
            if v == 0:
                return 1
            if v < 0:
                return 1
            if v > 4:
                return 4
        return v


class AggregateScore(BaseModel):
    numeric: float
    out_of: float = 4.0
    band: str = ""  # computed by sla.py — LLM need not supply this
    metrics_rated: int
    metrics_na: int


class EvaluationResult(BaseModel):
    ticket_id: str
    evaluation_date: str
    agent_name: str
    ticket_summary: str = ""
    sla_status: Optional[SLAStatus] = None
    metrics: list[MetricResult] = Field(default_factory=list)
    aggregate_score: Optional[AggregateScore] = None
    flags: list[str] = Field(default_factory=list)
    evaluator_confidence: str = "HIGH"  # HIGH | MEDIUM | LOW
    confidence_note: str = ""
    # Internal fields added by the pipeline (not from LLM)
    prompt_version: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None

    def get_metric(self, metric_id: str) -> Optional[MetricResult]:
        for m in self.metrics:
            if m.metric_id == metric_id:
                return m
        return None
