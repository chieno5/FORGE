from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from models import AnalysisReport


@dataclass(frozen=True)
class ApplicationClassification:
    key: str
    label: str
    rationale: str
    pragma_focus: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


APPLICATIONS: dict[str, tuple[str, list[str]]] = {
    "vector_saxpy": (
        "Vector add / SAXPY",
        ["PIPELINE", "memory bandwidth", "ARRAY_PARTITION"],
    ),
    "matrix_multiply": (
        "Matrix multiply",
        ["UNROLL", "ARRAY_PARTITION", "DSP trade-off"],
    ),
    "fir_filter": (
        "FIR filter",
        ["PIPELINE", "DATAFLOW", "shift register", "II"],
    ),
    "reduction_dot": (
        "Reduction / Dot product",
        ["reduction dependency", "UNROLL", "accumulator dependency"],
    ),
    "conv2d_3x3": (
        "2D convolution",
        ["line buffer", "ARRAY_PARTITION", "data reuse", "BRAM trade-off"],
    ),
    "unclassified": (
        "Unclassified",
        ["PIPELINE", "UNROLL", "ARRAY_PARTITION"],
    ),
}


def classify_application(
    source_path: str | Path,
    source_text: str,
    report: AnalysisReport,
) -> ApplicationClassification:
    """Classify known HLS kernels from stable source and function-name signals."""

    signals = " ".join(
        [
            Path(source_path).stem.lower(),
            source_text.lower(),
            " ".join(function.name.lower() for function in report.functions),
        ]
    )

    if _contains_any(signals, ("convolution", "conv2d", "sobel", "line_buffer", "kernel_2d")):
        return _classification("conv2d_3x3", "Detected 2D image-window or convolution signals.")
    if _contains_any(signals, ("fir", "filter_tap", "taps", "shift_register")):
        return _classification("fir_filter", "Detected FIR/filter tap signals.")
    if _contains_any(signals, ("matmul", "matrix_multiply", "matrixmultiply", "gemm")):
        return _classification("matrix_multiply", "Detected matrix multiplication signals.")
    if _contains_any(signals, ("dot_product", "dotproduct", "reduction", "accumulator", "sum_reduce")):
        return _classification("reduction_dot", "Detected reduction or dot-product signals.")
    if _contains_any(signals, ("saxpy", "vector_add", "vectoradd", "axpy")):
        return _classification("vector_saxpy", "Detected vector add or SAXPY signals.")
    return _classification(
        "unclassified",
        "No supported application pattern was detected; generic HLS history will be used.",
    )


def _classification(key: str, rationale: str) -> ApplicationClassification:
    label, focus = APPLICATIONS[key]
    return ApplicationClassification(key, label, rationale, focus)


def _contains_any(text: str, values: tuple[str, ...]) -> bool:
    return any(value in text for value in values)
