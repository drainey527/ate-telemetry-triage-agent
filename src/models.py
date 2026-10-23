import enum
from typing import List, Optional, Dict
from pydantic import BaseModel, Field

class FailureDomain(str, enum.Enum):
    """Categorical classification of post-silicon test fallout."""
    SILICON_LOGIC = "SILICON_LOGIC"               # True gate, flip-flop, or timing defects
    FIXTURE_INTERMITTENT = "FIXTURE_INTERMITTENT" # Pogo pin, socket wear, DIB trace resistance
    THERMAL_EXCURSION = "THERMAL_EXCURSION"       # Thermal runaway, chiller trip, heat-sink lift
    POWER_DELIVERY_DROOP = "POWER_DELIVERY_DROOP" # VDD droop under high-activity ATPG bursts
    TESTER_HARNESS_SYNCH = "TESTER_HARNESS_SYNCH" # Vector timing deskew / strobe alignment fault
    UNKNOWN = "UNKNOWN"

class SeverityLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

class ParsedTelemetry(BaseModel):
    """Pre-processed deterministic metrics extracted from raw ATE output."""
    site_id: int = Field(..., description="Tester physical test site / socket number (1-32)")
    test_phase: str = Field(..., description="Active test phase, e.g., SCAN, ATPG, BIST, RETENTION")
    peak_junction_temp_c: float = Field(..., description="Peak recorded die junction temp in Celsius")
    baseline_temp_c: float = Field(..., description="Baseline chuck / ambient temp in Celsius")
    nominal_vdd_v: float = Field(..., description="Programmed supply voltage in Volts")
    minimum_vdd_v: float = Field(..., description="Minimum sampled instantaneous voltage under load")
    contact_resistance_mohm: float = Field(..., description="Measured socket contact resistance in mOhm")
    error_cycle_offset: Optional[int] = Field(None, description="Vector cycle offset at failure strobe")
    failing_pins: List[str] = Field(default_factory=list, description="Digital pin channels reporting mismatch")
    register_signature: Dict[str, str] = Field(default_factory=dict, description="Captured hex register states")

class TriageAssessment(BaseModel):
    """Validated structured diagnostic payload ready for Jira/Slack ticket routing."""
    failure_domain: FailureDomain = Field(..., description="Root cause domain classification")
    severity: SeverityLevel = Field(..., description="Urgency rating for lab escalation")
    confidence_score: float = Field(..., ge=0.0, le=1.0, description="Confidence metric (0.0 to 1.0)")
    physical_guards_triggered: List[str] = Field(
        default_factory=list,
        description="Deterministic hard rules that overrode or constrained the diagnosis"
    )
    primary_failure_signature: str = Field(..., description="Concise diagnostic summary of failure mechanism")
    evidence_snippets: List[str] = Field(..., description="Extracted log lines supporting diagnosis")
    recommended_bench_actions: List[str] = Field(
        ...,
        description="Prioritized, actionable instructions for ATE test engineers or lab technicians"
    )
    unblock_pathway: str = Field(..., description="Direct path to unblock customer or production floor")
