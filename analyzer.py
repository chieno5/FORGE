from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pycparser import c_ast

from models import FunctionAnalysis, LoopRegion
from parser import ParsedFunction


ARITHMETIC_OPS = {"+", "-", "*", "/", "%"}
COMPARISON_OPS = {"<", "<=", ">", ">=", "==", "!="}
LOGIC_OPS = {"&&", "||"}
UNSUPPORTED_CALLS = {
    "malloc": "dynamic_memory",
    "calloc": "dynamic_memory",
    "realloc": "dynamic_memory",
    "free": "dynamic_memory",
    "printf": "stdio",
    "fprintf": "stdio",
    "sprintf": "stdio",
    "puts": "stdio",
    "fopen": "file_io",
    "fclose": "file_io",
    "fread": "file_io",
    "fwrite": "file_io",
    "fscanf": "file_io",
}


@dataclass
class FeatureState:
    function_name: str
    function_names_in_file: set[str]
    loop_count: int = 0
    for_loop_count: int = 0
    while_loop_count: int = 0
    max_loop_depth: int = 0
    array_access_count: int = 0
    regular_array_access_count: int = 0
    assignment_count: int = 0
    arithmetic_op_count: int = 0
    multiplication_count: int = 0
    comparison_op_count: int = 0
    logic_op_count: int = 0
    branch_count: int = 0
    function_call_count: int = 0
    unknown_function_call_count: int = 0
    pointer_deref_count: int = 0
    address_of_count: int = 0
    parameter_pointer_count: int = 0
    unsupported_constructs: set[str] = field(default_factory=set)
    called_functions: Counter[str] = field(default_factory=Counter)
    has_reduction_pattern: bool = False
    has_mac_pattern: bool = False
    has_recursion: bool = False

    def to_features(self) -> dict[str, Any]:
        return {
            "loop_count": self.loop_count,
            "for_loop_count": self.for_loop_count,
            "while_loop_count": self.while_loop_count,
            "max_loop_depth": self.max_loop_depth,
            "array_access_count": self.array_access_count,
            "regular_array_access_count": self.regular_array_access_count,
            "arithmetic_op_count": self.arithmetic_op_count,
            "multiplication_count": self.multiplication_count,
            "assignment_count": self.assignment_count,
            "comparison_op_count": self.comparison_op_count,
            "logic_op_count": self.logic_op_count,
            "branch_count": self.branch_count,
            "function_call_count": self.function_call_count,
            "unknown_function_call_count": self.unknown_function_call_count,
            "pointer_deref_count": self.pointer_deref_count,
            "address_of_count": self.address_of_count,
            "parameter_pointer_count": self.parameter_pointer_count,
            "has_multiplication": self.multiplication_count > 0,
            "has_mac_pattern": self.has_mac_pattern,
            "has_reduction_pattern": self.has_reduction_pattern,
            "has_regular_memory_access": self.regular_array_access_count > 0
            and self.regular_array_access_count >= max(1, self.array_access_count // 2),
            "unsupported_constructs": sorted(self.unsupported_constructs),
            "called_functions": dict(self.called_functions),
            "has_recursion": self.has_recursion,
            "has_complex_pointer_usage": self.pointer_deref_count > 0
            or self.address_of_count > 1
            or self.parameter_pointer_count >= 4,
            "is_compute_heavy": self.arithmetic_op_count >= max(3, self.branch_count * 2),
            "is_control_heavy": self.branch_count > self.arithmetic_op_count,
            "has_simple_loop_based_computation": self.loop_count > 0
            and self.arithmetic_op_count > 0
            and not self.unsupported_constructs,
        }


class FeatureVisitor(c_ast.NodeVisitor):
    def __init__(self, state: FeatureState, initial_loop_depth: int = 0):
        self.state = state
        self.loop_depth = initial_loop_depth

    def visit_For(self, node: c_ast.For) -> None:
        self.state.loop_count += 1
        self.state.for_loop_count += 1
        self._visit_loop(node)

    def visit_While(self, node: c_ast.While) -> None:
        self.state.loop_count += 1
        self.state.while_loop_count += 1
        self._visit_loop(node)

    def visit_DoWhile(self, node: c_ast.DoWhile) -> None:
        self.state.loop_count += 1
        self.state.while_loop_count += 1
        self._visit_loop(node)

    def visit_If(self, node: c_ast.If) -> None:
        self.state.branch_count += 1
        self.generic_visit(node)

    def visit_Switch(self, node: c_ast.Switch) -> None:
        self.state.branch_count += 1
        self.generic_visit(node)

    def visit_Case(self, node: c_ast.Case) -> None:
        self.state.branch_count += 1
        self.generic_visit(node)

    def visit_TernaryOp(self, node: c_ast.TernaryOp) -> None:
        self.state.branch_count += 1
        self.generic_visit(node)

    def visit_Assignment(self, node: c_ast.Assignment) -> None:
        self.state.assignment_count += 1
        if node.op in {"+=", "-=", "*=", "/="}:
            self.state.has_reduction_pattern = True
            if node.op == "+=" and _contains_multiplication(node.rvalue):
                self.state.has_mac_pattern = True
        elif _is_self_accumulation(node.lvalue, node.rvalue):
            self.state.has_reduction_pattern = True
            if _contains_multiplication(node.rvalue):
                self.state.has_mac_pattern = True
        self.generic_visit(node)

    def visit_ArrayRef(self, node: c_ast.ArrayRef) -> None:
        self.state.array_access_count += 1
        if _is_regular_array_ref(node):
            self.state.regular_array_access_count += 1
        self.generic_visit(node)

    def visit_BinaryOp(self, node: c_ast.BinaryOp) -> None:
        if node.op in ARITHMETIC_OPS:
            self.state.arithmetic_op_count += 1
            if node.op == "*":
                self.state.multiplication_count += 1
        elif node.op in COMPARISON_OPS:
            self.state.comparison_op_count += 1
        elif node.op in LOGIC_OPS:
            self.state.logic_op_count += 1
        self.generic_visit(node)

    def visit_FuncCall(self, node: c_ast.FuncCall) -> None:
        call_name = _call_name(node)
        self.state.function_call_count += 1
        if call_name:
            self.state.called_functions[call_name] += 1
            if call_name == self.state.function_name:
                self.state.has_recursion = True
                self.state.unsupported_constructs.add("recursion")
            if call_name in UNSUPPORTED_CALLS:
                self.state.unsupported_constructs.add(UNSUPPORTED_CALLS[call_name])
            elif call_name not in self.state.function_names_in_file:
                self.state.unknown_function_call_count += 1
        self.generic_visit(node)

    def visit_UnaryOp(self, node: c_ast.UnaryOp) -> None:
        if node.op == "*":
            self.state.pointer_deref_count += 1
        elif node.op == "&":
            self.state.address_of_count += 1
        self.generic_visit(node)

    def _visit_loop(self, node: c_ast.Node) -> None:
        self.loop_depth += 1
        self.state.max_loop_depth = max(self.state.max_loop_depth, self.loop_depth)
        self.generic_visit(node)
        self.loop_depth -= 1


def analyze_functions(parsed_functions: list[ParsedFunction]) -> list[FunctionAnalysis]:
    function_names = {function.name for function in parsed_functions}
    return [_analyze_function(function, function_names) for function in parsed_functions]


def _analyze_function(
    function: ParsedFunction,
    function_names: set[str],
) -> FunctionAnalysis:
    state = FeatureState(function.name, function_names)
    state.parameter_pointer_count = sum(1 for param in function.parameters if "*" in param)
    FeatureVisitor(state).visit(function.node.body)

    loop_regions = _extract_loop_regions(function, function_names)
    return FunctionAnalysis(
        name=function.name,
        return_type=function.return_type,
        parameters=function.parameters,
        features=state.to_features(),
        loop_regions=loop_regions,
    )


def _extract_loop_regions(
    function: ParsedFunction,
    function_names: set[str],
) -> list[LoopRegion]:
    collector = LoopCollector(function.name, function_names)
    collector.visit(function.node.body)
    return collector.loop_regions


class LoopCollector(c_ast.NodeVisitor):
    def __init__(self, function_name: str, function_names: set[str]):
        self.function_name = function_name
        self.function_names = function_names
        self.loop_depth = 0
        self.loop_index = 0
        self.loop_regions: list[LoopRegion] = []

    def visit_For(self, node: c_ast.For) -> None:
        self._record_loop(node, "for")

    def visit_While(self, node: c_ast.While) -> None:
        self._record_loop(node, "while")

    def visit_DoWhile(self, node: c_ast.DoWhile) -> None:
        self._record_loop(node, "do_while")

    def _record_loop(self, node: c_ast.Node, kind: str) -> None:
        self.loop_index += 1
        self.loop_depth += 1
        state = FeatureState(self.function_name, self.function_names)
        FeatureVisitor(state, initial_loop_depth=self.loop_depth - 1).visit(node)
        loop_id = f"{self.function_name}.loop_{self.loop_index}"
        self.loop_regions.append(
            LoopRegion(
                id=loop_id,
                kind=kind,
                depth=self.loop_depth,
                features=state.to_features(),
            )
        )
        self.generic_visit(node)
        self.loop_depth -= 1


def _call_name(node: c_ast.FuncCall) -> str | None:
    if isinstance(node.name, c_ast.ID):
        return node.name.name
    return None


def _contains_multiplication(node: c_ast.Node | None) -> bool:
    if node is None:
        return False
    if isinstance(node, c_ast.BinaryOp) and node.op == "*":
        return True
    for _, child in node.children():
        if _contains_multiplication(child):
            return True
    return False


def _is_self_accumulation(lvalue: c_ast.Node | None, rvalue: c_ast.Node | None) -> bool:
    if lvalue is None or rvalue is None:
        return False
    target = _node_key(lvalue)
    if target is None:
        return False
    return _contains_node_key(rvalue, target)


def _contains_node_key(node: c_ast.Node | None, key: str) -> bool:
    if node is None:
        return False
    if _node_key(node) == key:
        return True
    return any(_contains_node_key(child, key) for _, child in node.children())


def _node_key(node: c_ast.Node) -> str | None:
    if isinstance(node, c_ast.ID):
        return node.name
    if isinstance(node, c_ast.ArrayRef):
        return _node_key(node.name)
    return None


def _is_regular_array_ref(node: c_ast.ArrayRef) -> bool:
    if not _is_regular_subscript(node.subscript):
        return False
    parent = node.name
    while isinstance(parent, c_ast.ArrayRef):
        if not _is_regular_subscript(parent.subscript):
            return False
        parent = parent.name
    return isinstance(parent, c_ast.ID)


def _is_regular_subscript(node: c_ast.Node | None) -> bool:
    if node is None:
        return False
    if isinstance(node, (c_ast.ID, c_ast.Constant)):
        return True
    if isinstance(node, c_ast.BinaryOp) and node.op in {"+", "-", "*"}:
        return _is_regular_subscript(node.left) and _is_regular_subscript(node.right)
    if isinstance(node, c_ast.UnaryOp) and node.op in {"+", "-"}:
        return _is_regular_subscript(node.expr)
    return False
