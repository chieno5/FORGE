from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class LoopRegion:
    """Analysis result for one loop region."""

    id: str
    kind: str
    depth: int
    features: dict[str, Any]
    source_line: int = 0
    score: int = 0
    classification: str = ""
    is_candidate: bool = False
    reasoning: str = ""
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FunctionAnalysis:
    """Analysis result for one C function."""

    name: str
    return_type: str
    parameters: list[str]
    features: dict[str, Any]
    source_line: int = 0
    score: int = 0
    classification: str = ""
    is_candidate: bool = False
    reasoning: str = ""
    recommendations: list[str] = field(default_factory=list)
    loop_regions: list[LoopRegion] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["loop_regions"] = [region.to_dict() for region in self.loop_regions]
        return data


@dataclass
class StructuralConstraint:
    """A source structure that can limit HLS scheduling or pragma effectiveness."""

    id: str
    constraint_type: str
    function: str
    loop_id: str
    source_line: int
    variables: list[str]
    evidence: str
    confidence: float
    affected_pragmas: list[str] = field(default_factory=list)
    supported_transformations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalysisReport:
    """Complete static-analysis report."""

    file: str
    threshold: int
    functions: list[FunctionAnalysis]
    parser: str = "pycparser"
    limitations: list[str] = field(default_factory=list)
    structural_constraints: list[StructuralConstraint] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "threshold": self.threshold,
            "parser": self.parser,
            "functions": [function.to_dict() for function in self.functions],
            "limitations": self.limitations,
            "structural_constraints": [
                constraint.to_dict() for constraint in self.structural_constraints
            ],
        }
