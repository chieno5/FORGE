from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any

from models import AnalysisReport, LoopRegion, StructuralConstraint


SUPPORTED_TRANSFORMATION = "partial_accumulator_v1"
DEFAULT_PARTIAL_FACTOR = 4


@dataclass(frozen=True)
class SourceTransformation:
    name: str
    function: str
    loop_id: str
    factor: int
    variables: list[dict[str, str]]
    original_source_hash: str
    transformed_source_hash: str
    semantic_contract: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TransformationAttempt:
    applied: bool
    source_text: str
    reason: str
    transformation: SourceTransformation | None = None


def apply_reduction_preflight_transform(
    source_text: str,
    report: AnalysisReport,
    top_function: str,
    factor: int = DEFAULT_PARTIAL_FACTOR,
) -> TransformationAttempt:
    """Apply one conservative integer reduction transform, or leave source unchanged."""

    if factor not in {2, 4, 8}:
        return TransformationAttempt(False, source_text, "unsupported partial factor")
    constraint = _select_constraint(report, top_function)
    if constraint is None:
        return TransformationAttempt(
            False,
            source_text,
            "no supported scalar loop-carried dependency was found in the top function",
        )
    function = next(item for item in report.functions if item.name == top_function)
    loop = next(item for item in function.loop_regions if item.id == constraint.loop_id)
    recurrences = {
        str(item["variable"]): str(item["operation"])
        for item in loop.features.get("scalar_recurrences", [])
    }
    if not recurrences or any(
        operation not in {"add", "max", "min"}
        for operation in recurrences.values()
    ):
        return TransformationAttempt(False, source_text, "the recurrence operation is not supported")

    masked = _mask_non_code(source_text)
    function_span = _function_span(masked, top_function)
    if function_span is None:
        return TransformationAttempt(False, source_text, "top function body could not be located")
    loop_number = _loop_number(constraint.loop_id)
    loop_span = _loop_span(masked, function_span, loop_number)
    if loop_span is None:
        return TransformationAttempt(False, source_text, "target loop could not be located safely")
    loop_start, body_start, body_end, loop_end, iterator = loop_span
    if re.search(r"\b(?:break|continue|goto|return)\b", masked[body_start:body_end]):
        return TransformationAttempt(False, source_text, "target loop has unsupported control flow")

    declaration_specs: dict[str, tuple[str, str]] = {}
    for variable, operation in recurrences.items():
        declaration = _find_integer_declaration(
            source_text,
            masked,
            function_span[0],
            loop_start,
            variable,
        )
        if declaration is None:
            return TransformationAttempt(
                False,
                source_text,
                f"integer accumulator declaration is not safely transformable: {variable}",
            )
        declaration_specs[variable] = declaration
        if not _has_supported_loop_use(masked[body_start:body_end], variable, operation):
            return TransformationAttempt(
                False,
                source_text,
                f"accumulator has unsupported uses in the target loop: {variable}",
            )

    partial_names = {
        variable: f"{variable}__forge_partial" for variable in recurrences
    }
    if any(re.search(rf"\b{re.escape(name)}\b", masked) for name in partial_names.values()):
        return TransformationAttempt(False, source_text, "generated partial name already exists")

    line_start = source_text.rfind("\n", 0, loop_start) + 1
    indent = source_text[line_start:loop_start]
    if indent.strip():
        return TransformationAttempt(False, source_text, "target loop is not at a stable line boundary")

    declaration_lines: list[str] = []
    combine_lines: list[str] = []
    transformed_variables: list[dict[str, str]] = []
    for variable, operation in recurrences.items():
        c_type, _initializer = declaration_specs[variable]
        partial = partial_names[variable]
        if operation == "add":
            initializer = "{0}"
        else:
            initializer = "{" + ", ".join([variable] * factor) + "}"
        declaration_lines.extend(
            [
                f"{indent}{c_type} {partial}[{factor}] = {initializer};",
                f"{indent}#pragma HLS ARRAY_PARTITION variable={partial} complete dim=1",
            ]
        )
        for lane in range(factor):
            if operation == "add":
                combine_lines.append(f"{indent}{variable} += {partial}[{lane}];")
            elif operation == "max":
                combine_lines.append(
                    f"{indent}if ({partial}[{lane}] > {variable}) {variable} = {partial}[{lane}];"
                )
            else:
                combine_lines.append(
                    f"{indent}if ({partial}[{lane}] < {variable}) {variable} = {partial}[{lane}];"
                )
        transformed_variables.append({"name": variable, "operation": operation})

    transformed_body = source_text[body_start:body_end]
    lane_index = f"[({iterator}) & {factor - 1}]"
    for variable, partial in partial_names.items():
        transformed_body = re.sub(
            rf"\b{re.escape(variable)}\b",
            partial + lane_index,
            transformed_body,
        )

    declarations = "\n".join(declaration_lines) + "\n"
    combines = "\n" + "\n".join(combine_lines)
    transformed = (
        source_text[:line_start]
        + declarations
        + source_text[line_start:body_start]
        + transformed_body
        + source_text[body_end:loop_end]
        + combines
        + source_text[loop_end:]
    )
    original_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    transformed_hash = hashlib.sha256(transformed.encode("utf-8")).hexdigest()
    transformation = SourceTransformation(
        name=SUPPORTED_TRANSFORMATION,
        function=top_function,
        loop_id=constraint.loop_id,
        factor=factor,
        variables=transformed_variables,
        original_source_hash=original_hash,
        transformed_source_hash=transformed_hash,
        semantic_contract=(
            "Integer additive/min/max reduction only; top function interface is unchanged; "
            "the frozen comparison-lineage testbench must still pass."
        ),
    )
    return TransformationAttempt(
        True,
        transformed,
        f"applied {SUPPORTED_TRANSFORMATION} to {constraint.loop_id}",
        transformation,
    )


def _select_constraint(
    report: AnalysisReport,
    top_function: str,
) -> StructuralConstraint | None:
    return next(
        (
            item
            for item in report.structural_constraints
            if item.function == top_function
            and SUPPORTED_TRANSFORMATION in item.supported_transformations
        ),
        None,
    )


def _loop_number(loop_id: str) -> int:
    match = re.search(r"\.loop_(\d+)$", loop_id)
    return int(match.group(1)) if match else -1


def _function_span(masked: str, function_name: str) -> tuple[int, int] | None:
    match = re.search(
        rf"\b{re.escape(function_name)}\s*\([^;{{}}]*\)\s*\{{",
        masked,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    opening = masked.find("{", match.start())
    closing = _matching(masked, opening, "{", "}")
    return (opening + 1, closing) if closing is not None else None


def _loop_span(
    masked: str,
    function_span: tuple[int, int],
    loop_number: int,
) -> tuple[int, int, int, int, str] | None:
    if loop_number < 1:
        return None
    start, end = function_span
    loop_matches = list(re.finditer(r"\b(?:for|while)\s*\(", masked[start:end]))
    if loop_number > len(loop_matches):
        return None
    match = loop_matches[loop_number - 1]
    loop_start = start + match.start()
    if not masked[loop_start:start + match.end()].lstrip().startswith("for"):
        return None
    opening_paren = masked.find("(", loop_start, end)
    closing_paren = _matching(masked, opening_paren, "(", ")")
    if closing_paren is None:
        return None
    header = masked[opening_paren + 1:closing_paren]
    iterator_match = re.match(
        r"\s*(?:[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*\s+)?([A-Za-z_]\w*)\s*=",
        header,
    )
    if iterator_match is None:
        return None
    iterator = iterator_match.group(1)
    body_open = closing_paren + 1
    while body_open < end and masked[body_open].isspace():
        body_open += 1
    if body_open >= end or masked[body_open] != "{":
        return None
    body_close = _matching(masked, body_open, "{", "}")
    if body_close is None or body_close > end:
        return None
    return loop_start, body_open + 1, body_close, body_close + 1, iterator


def _matching(text: str, opening: int, left: str, right: str) -> int | None:
    if opening < 0 or opening >= len(text) or text[opening] != left:
        return None
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == left:
            depth += 1
        elif text[index] == right:
            depth -= 1
            if depth == 0:
                return index
    return None


def _find_integer_declaration(
    source: str,
    masked: str,
    function_start: int,
    loop_start: int,
    variable: str,
) -> tuple[str, str] | None:
    prefix = masked[function_start:loop_start]
    pattern = re.compile(
        rf"(?m)^\s*(?P<type>(?:(?:unsigned|signed)\s+)?"
        rf"(?:char|short|int|long(?:\s+long)?|ap_[u]?int\s*<\s*\d+\s*>))"
        rf"\s+{re.escape(variable)}\s*=\s*(?P<init>[^;]+);"
    )
    matches = list(pattern.finditer(prefix))
    if len(matches) != 1:
        return None
    match = matches[0]
    tail = prefix[match.end():]
    if re.search(rf"\b{re.escape(variable)}\b", tail):
        return None
    original = source[function_start + match.start():function_start + match.end()]
    original_match = pattern.search(original)
    if original_match is None:
        return None
    return " ".join(original_match.group("type").split()), original_match.group("init").strip()


def _has_supported_loop_use(loop_body: str, variable: str, operation: str) -> bool:
    occurrences = len(re.findall(rf"\b{re.escape(variable)}\b", loop_body))
    if operation == "add":
        updates = len(re.findall(rf"\b{re.escape(variable)}\s*\+=", loop_body))
        return occurrences == 1 and updates == 1
    assignments = len(
        re.findall(rf"\b{re.escape(variable)}\s*=(?!=)", loop_body)
    )
    return occurrences == 2 and assignments == 1


def _mask_non_code(source: str) -> str:
    chars = list(source)
    index = 0
    state = "code"
    quote = ""
    while index < len(chars):
        current = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code" and current == "/" and following == "/":
            chars[index] = chars[index + 1] = " "
            state = "line_comment"
            index += 2
            continue
        if state == "code" and current == "/" and following == "*":
            chars[index] = chars[index + 1] = " "
            state = "block_comment"
            index += 2
            continue
        if state == "code" and current in {'"', "'"}:
            quote = current
            chars[index] = " "
            state = "string"
            index += 1
            continue
        if state == "line_comment":
            if current == "\n":
                state = "code"
            else:
                chars[index] = " "
        elif state == "block_comment":
            if current == "*" and following == "/":
                chars[index] = chars[index + 1] = " "
                state = "code"
                index += 1
            elif current != "\n":
                chars[index] = " "
        elif state == "string":
            if current == "\\" and following:
                chars[index] = chars[index + 1] = " "
                index += 1
            elif current == quote:
                chars[index] = " "
                state = "code"
            elif current != "\n":
                chars[index] = " "
        index += 1
    return "".join(chars)
