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
class AnalysisReport:
    """Complete static-analysis report."""

    file: str
    threshold: int
    functions: list[FunctionAnalysis]
    parser: str = "pycparser"
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "threshold": self.threshold,
            "parser": self.parser,
            "functions": [function.to_dict() for function in self.functions],
            "limitations": self.limitations,
        }
