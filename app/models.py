"""Pydantic response / request models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    error: dict
    details: dict = Field(default_factory=dict)


class ConceptRef(BaseModel):
    prefix: str
    local: str
    qname: str


class Equation(BaseModel):
    id: str
    role: str
    summation: str
    items: list[dict]  # [{"concept": qname, "weight": 1|-1}]
    coefficients: dict[str, int]
    concept_count: int


class PackageRevisionOut(BaseModel):
    package_id: str
    revision: int
    concepts: list[str]
    equations: list[Equation]
    created_at: str


class PackageOut(BaseModel):
    package_id: str
    name: str
    latest_revision: int
    created_at: str
    updated_at: str
    latest: Optional[PackageRevisionOut] = None


class Fact(BaseModel):
    concept: str
    value: float
    decimals: object  # int or the string "INF"
    lower: float
    upper: float


class InstanceRevisionOut(BaseModel):
    instance_id: str
    package_id: str
    revision: int
    entity: str
    period: str
    currency: str
    facts: list[Fact]
    created_at: str


class InstanceOut(BaseModel):
    instance_id: str
    package_id: str
    latest_revision: int
    created_at: str
    updated_at: str
    latest: Optional[InstanceRevisionOut] = None


class WitnessFact(BaseModel):
    concept: str
    reported_value: float
    decimals: object
    true_value: float
    lower: float
    upper: float


class Conflict(BaseModel):
    equation: Equation
    residual_at_witness: Optional[float] = None
    facts: list[WitnessFact]


class ReportOut(BaseModel):
    report_id: str
    instance_id: str
    package_id: str
    instance_revision: int
    package_revision: int
    status: Literal["feasible", "infeasible", "pending"]
    is_correction: bool
    superseded: bool
    late: bool
    witness: Optional[list[WitnessFact]] = None
    conflicts: Optional[list[Conflict]] = None
    missing_facts: Optional[list[str]] = None
    detail: dict
    created_at: str
    has_instance_snapshot: bool = True
    has_xsd_snapshot: bool = True
    has_calc_snapshot: bool = True


class CheckAck(BaseModel):
    report: ReportOut
    superseded_report_ids: list[str] = Field(default_factory=list)


class ReportList(BaseModel):
    instance_id: str
    current: Optional[ReportOut] = None
    history: list[ReportOut]


class SensitiveConcept(BaseModel):
    concept: str = Field(..., description="敏感科目 QName，例如 acct:Inventory")
    min_width: float = Field(..., gt=0,
                             description="该科目可推断区间的最小允许宽度（正数）")


class ExportRequest(BaseModel):
    report_id: str = Field(..., description="当前可行的报告 ID")
    expected_instance_revision: int = Field(..., ge=1)
    expected_package_revision: int = Field(..., ge=1)
    retained_concepts: list[str] = Field(
        ..., min_length=1, description="公开（保留）科目 QNames"
    )
    sensitive_concepts: list[SensitiveConcept] = Field(
        ..., min_length=1, description="敏感科目及各自最小允许推断宽度"
    )


class RangeOut(BaseModel):
    """Achieved inference width for one sensitive concept; never its location."""
    width: Optional[float]
    min_width: float
    unbounded: bool
    satisfied: bool


class ExportOut(BaseModel):
    export_id: str
    report_id: str
    instance_id: str
    package_id: str
    instance_revision: int
    package_revision: int
    retained_concepts: list[str]
    sensitive_concepts: list[SensitiveConcept]
    ranges: dict[str, RangeOut]
    row_count: int
    sha256: str
    created_at: str
    download_path: str
