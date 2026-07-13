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
        source_line=function.node.decl.coord.line if function.node.decl.coord else 0,
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
        features = state.to_features()
        features.update(_loop_memory_dependency_features(node))
        self.loop_regions.append(
            LoopRegion(
                id=loop_id,
                kind=kind,
                depth=self.loop_depth,
                features=features,
                source_line=node.coord.line if node.coord else 0,
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


class _LoopMemoryAccessVisitor(c_ast.NodeVisitor):
    """Collect direct array reads and writes from one loop body."""

    def __init__(self, iterator_name: str | None):
        self.iterator_name = iterator_name
        self.reads: list[tuple[str, int | None]] = []
        self.writes: list[tuple[str, int | None]] = []

    def visit_Assignment(self, node: c_ast.Assignment) -> None:
        self._record_write(node.lvalue)
        if node.op != "=":
            self._record_reads(node.lvalue)
        self._record_reads(node.rvalue)

    def visit_Decl(self, node: c_ast.Decl) -> None:
        self._record_reads(node.init)

    def _record_write(self, node: c_ast.Node | None) -> None:
        access = _array_access(node, self.iterator_name)
        if access is not None:
            self.writes.append(access)

    def _record_reads(self, node: c_ast.Node | None) -> None:
        if node is None:
            return
        for array_ref in _array_references(node):
            access = _array_access(array_ref, self.iterator_name)
            if access is not None:
                self.reads.append(access)


def _loop_memory_dependency_features(node: c_ast.Node) -> dict[str, Any]:
    iterator_name = _loop_iterator_name(node)
    visitor = _LoopMemoryAccessVisitor(iterator_name)
    body = getattr(node, "stmt", None)
    if body is not None:
        visitor.visit(body)

    read_arrays = {name for name, _ in visitor.reads}
    write_arrays = {name for name, _ in visitor.writes}
    shared_arrays = read_arrays & write_arrays
    carried_arrays = {
        name
        for name, offset in visitor.reads
        if name in write_arrays and offset is not None and offset < 0
    }
    has_dependency = bool(carried_arrays)
    has_neighbor_access = any(
        offset is not None and offset != 0
        for _, offset in [*visitor.reads, *visitor.writes]
    )
    notes: list[str] = []
    if shared_arrays:
        notes.append(f"same array read and write: {', '.join(sorted(shared_arrays))}")
    if carried_arrays:
        notes.append(
            "loop-carried read after previous write: "
            f"{', '.join(sorted(carried_arrays))}"
        )
    if _has_variable_trip_count(node, iterator_name):
        notes.append("variable loop trip count")
    return {
        "memory_read_arrays": sorted(read_arrays),
        "memory_write_arrays": sorted(write_arrays),
        "has_same_array_read_write": bool(shared_arrays),
        "has_neighbor_index_access": has_neighbor_access,
        "has_loop_carried_dependency": has_dependency,
        "dependency_arrays": sorted(carried_arrays),
        "has_variable_trip_count": _has_variable_trip_count(node, iterator_name),
        "pipeline_eligible": not has_dependency,
        "unroll_eligible": not has_dependency,
        "dependency_notes": notes,
    }


def _loop_iterator_name(node: c_ast.Node) -> str | None:
    if not isinstance(node, c_ast.For):
        return None
    if isinstance(node.init, c_ast.DeclList) and node.init.decls:
        return node.init.decls[0].name
    if isinstance(node.init, c_ast.Assignment) and isinstance(node.init.lvalue, c_ast.ID):
        return node.init.lvalue.name
    return None


def _has_variable_trip_count(node: c_ast.Node, iterator_name: str | None) -> bool:
    if not isinstance(node, c_ast.For) or node.cond is None:
        return True
    return any(
        identifier.name != iterator_name
        for identifier in _nodes_of_type(node.cond, c_ast.ID)
    )


def _array_references(node: c_ast.Node) -> list[c_ast.ArrayRef]:
    return list(_nodes_of_type(node, c_ast.ArrayRef))


def _nodes_of_type(node: c_ast.Node, node_type: type[c_ast.Node]) -> list[c_ast.Node]:
    found: list[c_ast.Node] = []

    class Visitor(c_ast.NodeVisitor):
        def generic_visit(self, current: c_ast.Node) -> None:
            if isinstance(current, node_type):
                found.append(current)
            super().generic_visit(current)

    Visitor().visit(node)
    return found


def _array_access(
    node: c_ast.Node | None,
    iterator_name: str | None,
) -> tuple[str, int | None] | None:
    if not isinstance(node, c_ast.ArrayRef):
        return None
    base = node
    while isinstance(base, c_ast.ArrayRef):
        base = base.name
    if not isinstance(base, c_ast.ID):
        return None
    return base.name, _index_offset(node.subscript, iterator_name)


def _index_offset(node: c_ast.Node | None, iterator_name: str | None) -> int | None:
    if not iterator_name or node is None:
        return None
    if isinstance(node, c_ast.ID) and node.name == iterator_name:
        return 0
    if isinstance(node, c_ast.BinaryOp) and node.op in {"+", "-"}:
        if isinstance(node.left, c_ast.ID) and node.left.name == iterator_name:
            if isinstance(node.right, c_ast.Constant) and node.right.type == "int":
                try:
                    value = int(node.right.value, 0)
                except ValueError:
                    return None
                return value if node.op == "+" else -value
    return None
