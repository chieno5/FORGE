from __future__ import annotations

import re
from dataclasses import dataclass

from models import FunctionAnalysis


DEFAULT_TEST_SIZE = 64


@dataclass(frozen=True)
class TestbenchSource:
    filename: str
    source: str


@dataclass(frozen=True)
class ParameterSpec:
    raw: str
    name: str
    c_type: str
    dimensions: list[str]
    is_pointer: bool = False

    @property
    def is_array(self) -> bool:
        return bool(self.dimensions)

    @property
    def storage_type(self) -> str:
        return _strip_type_qualifiers(self.c_type)


def generate_local_testbench(
    source_text: str,
    source_stem: str,
    top_function: FunctionAnalysis,
) -> TestbenchSource:
    specs = [_parse_parameter(parameter) for parameter in top_function.parameters]
    typedefs = _extract_typedefs(source_text)
    defines = _extract_defines(source_text)
    lines: list[str] = [
        "/* Auto-generated smoke testbench for Vitis HLS. */",
        "#include <stdint.h>",
    ]
    lines.extend(_extract_local_includes(source_text))
    lines.append("")
    if defines:
        lines.extend(defines)
        lines.append("")
    if typedefs:
        lines.extend(typedefs)
        lines.append("")
    lines.append(f"#define FORGE_TB_DEFAULT_SIZE {DEFAULT_TEST_SIZE}")
    lines.append("")
    lines.extend(_prototype_lines(top_function))
    lines.append("")
    lines.extend(_storage_lines(specs))
    lines.append("")
    lines.append("int main(void) {")
    lines.extend(_initialization_lines(specs))
    lines.extend(_scalar_lines(specs))
    lines.extend(_call_lines(top_function, specs))
    lines.append("    return 0;")
    lines.append("}")
    return TestbenchSource(
        filename=f"{source_stem}_tb.c",
        source="\n".join(lines) + "\n",
    )


def _prototype_lines(function: FunctionAnalysis) -> list[str]:
    if not function.parameters:
        return [f"{function.return_type} {function.name}(void);"]
    lines = [f"{function.return_type} {function.name}("]
    for index, parameter in enumerate(function.parameters):
        suffix = "," if index < len(function.parameters) - 1 else ""
        lines.append(f"    {parameter}{suffix}")
    lines.append(");")
    return lines


def _storage_lines(specs: list[ParameterSpec]) -> list[str]:
    lines: list[str] = []
    for spec in specs:
        if spec.is_array:
            dimensions = "".join(f"[{_dimension_or_default(dim)}]" for dim in spec.dimensions)
            lines.append(f"static {spec.storage_type} {spec.name}{dimensions};")
        elif spec.is_pointer:
            lines.append(f"static {spec.storage_type} {spec.name}[FORGE_TB_DEFAULT_SIZE];")
    return lines


def _initialization_lines(specs: list[ParameterSpec]) -> list[str]:
    lines: list[str] = []
    for spec in specs:
        if spec.is_array:
            count = _element_count(spec.dimensions)
            lines.append(f"    for (int i = 0; i < {count}; ++i) {{")
            lines.append(
                f"        (({spec.storage_type} *){spec.name})[i] = "
                f"({spec.storage_type})((i * 17 + 3) & 255);"
            )
            lines.append("    }")
        elif spec.is_pointer:
            lines.append("    for (int i = 0; i < FORGE_TB_DEFAULT_SIZE; ++i) {")
            lines.append(
                f"        {spec.name}[i] = ({spec.storage_type})((i * 17 + 3) & 255);"
            )
            lines.append("    }")
    return lines


def _scalar_lines(specs: list[ParameterSpec]) -> list[str]:
    return [
        f"    {spec.storage_type} {spec.name} = {_scalar_value(spec)};"
        for spec in specs
        if not spec.is_array and not spec.is_pointer
    ]


def _call_lines(function: FunctionAnalysis, specs: list[ParameterSpec]) -> list[str]:
    args = ", ".join(spec.name for spec in specs)
    if function.return_type.strip() == "void":
        return [f"    {function.name}({args});" if args else f"    {function.name}();"]
    return [
        f"    {function.return_type} forge_result = {function.name}({args});"
        if args
        else f"    {function.return_type} forge_result = {function.name}();",
        "    (void)forge_result;",
    ]


def _parse_parameter(parameter: str) -> ParameterSpec:
    text = " ".join(parameter.strip().split())
    array_match = re.fullmatch(
        r"(?P<type>.+?)\s+(?P<name>[A-Za-z_]\w*)\s*(?P<dims>(?:\[[^\]]*\])+)",
        text,
    )
    if array_match:
        return ParameterSpec(
            raw=parameter,
            name=array_match.group("name"),
            c_type=array_match.group("type"),
            dimensions=re.findall(r"\[([^\]]*)\]", array_match.group("dims")),
        )

    pointer_match = re.fullmatch(
        r"(?P<type>.+?)\s*\*\s*(?P<name>[A-Za-z_]\w*)",
        text,
    )
    if pointer_match:
        return ParameterSpec(
            raw=parameter,
            name=pointer_match.group("name"),
            c_type=pointer_match.group("type"),
            dimensions=[],
            is_pointer=True,
        )

    scalar_match = re.fullmatch(r"(?P<type>.+?)\s+(?P<name>[A-Za-z_]\w*)", text)
    if scalar_match:
        return ParameterSpec(
            raw=parameter,
            name=scalar_match.group("name"),
            c_type=scalar_match.group("type"),
            dimensions=[],
        )

    safe_name = re.sub(r"\W+", "_", text).strip("_") or "arg"
    return ParameterSpec(raw=parameter, name=safe_name, c_type="int", dimensions=[])


def _extract_typedefs(source_text: str) -> list[str]:
    return re.findall(r"^\s*typedef\b[^;]*;", source_text, flags=re.MULTILINE)


def _extract_defines(source_text: str) -> list[str]:
    return re.findall(
        r"^\s*#define\s+[A-Za-z_]\w*(?:\([^\n)]*\))?[^\n]*",
        source_text,
        flags=re.MULTILINE,
    )


def _extract_local_includes(source_text: str) -> list[str]:
    return re.findall(r"^\s*(#include\s+\"[^\"]+\")", source_text, flags=re.MULTILINE)


def _dimension_or_default(dimension: str) -> str:
    value = dimension.strip()
    return value if value else "FORGE_TB_DEFAULT_SIZE"


def _element_count(dimensions: list[str]) -> str:
    values = [_dimension_or_default(dimension) for dimension in dimensions]
    if not values:
        return "FORGE_TB_DEFAULT_SIZE"
    return " * ".join(values)


def _scalar_value(spec: ParameterSpec) -> str:
    name = spec.name.lower()
    c_type = spec.storage_type.lower()
    if "float" in c_type:
        return "1.0f"
    if "double" in c_type:
        return "1.0"
    if "width" in name or "height" in name or "row" in name or "col" in name:
        return "16"
    if "pixel" in name or "size" in name or "length" in name or name in {"n", "count"}:
        return "FORGE_TB_DEFAULT_SIZE"
    if "threshold" in name:
        return "80"
    if "max" in name or "limit" in name:
        return "256"
    return "1"


def _strip_type_qualifiers(c_type: str) -> str:
    words = [word for word in c_type.split() if word not in {"const", "volatile"}]
    return " ".join(words) or "int"
