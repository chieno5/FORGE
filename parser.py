from __future__ import annotations

import re
from pathlib import Path

try:
    from pycparser import c_ast, c_parser
except ImportError as exc:  # pragma: no cover - exercised only without dependency
    c_ast = None
    c_parser = None
    _PYCARSER_IMPORT_ERROR = exc
else:
    _PYCARSER_IMPORT_ERROR = None


class CParserError(RuntimeError):
    """Raised when a C file cannot be parsed."""


class ParsedFunction:
    def __init__(self, node: "c_ast.FuncDef", source: str):
        self.node = node
        self.source = source
        self.name = node.decl.name
        self.return_type = _decl_to_text(node.decl.type.type)
        self.parameters = _extract_parameters(node.decl.type.args)


class ParsedCFile:
    def __init__(self, path: Path, ast: "c_ast.FileAST", functions: list[ParsedFunction]):
        self.path = path
        self.ast = ast
        self.functions = functions


def parse_c_file(path: str | Path) -> ParsedCFile:
    if c_parser is None:
        raise CParserError(
            "pycparser is required. Install it with: pip install -r requirements.txt"
        ) from _PYCARSER_IMPORT_ERROR

    source_path = Path(path)
    if not source_path.exists():
        raise CParserError(f"Input file does not exist: {source_path}")

    raw_source = source_path.read_text(encoding="utf-8")
    cleaned_source = _prepare_source_for_pycparser(raw_source)

    parser = c_parser.CParser()
    try:
        ast = parser.parse(cleaned_source, filename=str(source_path))
    except Exception as exc:
        raise CParserError(
            "Failed to parse C file. This tool supports a simplified HLS-style C subset. "
            "Try removing unsupported preprocessor directives, system includes, or compiler extensions."
        ) from exc

    functions = [
        ParsedFunction(ext, raw_source)
        for ext in ast.ext
        if isinstance(ext, c_ast.FuncDef)
    ]
    return ParsedCFile(source_path, ast, functions)


def _prepare_source_for_pycparser(source: str) -> str:
    source = _replace_hls_specific_tokens(source)
    source = _remove_comments(source)
    source = _remove_preprocessor_lines(source)
    source = _remove_common_c_qualifiers(source)
    return source


def _remove_comments(source: str) -> str:
    source = re.sub(
        r"/\*.*?\*/",
        lambda match: "\n" * match.group(0).count("\n"),
        source,
        flags=re.DOTALL,
    )
    return re.sub(r"//.*", "", source)


def _remove_preprocessor_lines(source: str) -> str:
    return "\n".join(
        "" if line.lstrip().startswith("#") else line
        for line in source.splitlines()
    )


def _remove_common_c_qualifiers(source: str) -> str:
    replacements = [
        (r"\bstatic\s+inline\b", "static"),
        (r"\bvolatile\b", ""),
        (r"\brestrict\b", ""),
        (r"\b__restrict\b", ""),
        (r"\b__restrict__\b", ""),
        (r"\binline\b", ""),
    ]
    for pattern, replacement in replacements:
        source = re.sub(pattern, replacement, source)
    return source


def _replace_hls_specific_tokens(source: str) -> str:
    source = re.sub(r"#pragma\s+HLS[^\n]*", "", source)
    source = re.sub(r"\bap_(u)?int\s*<\s*\d+\s*>", "int", source)
    source = re.sub(r"\bhls::stream\s*<[^>]+>", "int", source)
    return source


def _extract_parameters(args: "c_ast.ParamList | None") -> list[str]:
    if args is None:
        return []
    return [_decl_to_text(param) for param in args.params]


def _decl_to_text(node: object | None, name: str = "") -> str:
    if node is None:
        return ""
    if c_ast is None:
        return str(node)
    if isinstance(node, c_ast.Decl):
        return _decl_to_text(node.type, node.name or name)
    if isinstance(node, c_ast.TypeDecl):
        base_type = _decl_to_text(node.type)
        qualifiers = " ".join(getattr(node, "quals", []) or [])
        prefix = f"{qualifiers} {base_type}".strip()
        return f"{prefix} {name}".strip()
    if isinstance(node, c_ast.IdentifierType):
        return " ".join(node.names)
    if isinstance(node, c_ast.PtrDecl):
        pointer_name = f"*{name}" if name else "*"
        return _decl_to_text(node.type, pointer_name)
    if isinstance(node, c_ast.ArrayDecl):
        dim = _expr_to_text(node.dim)
        array_name = f"{name}[{dim}]" if name else f"[{dim}]"
        return _decl_to_text(node.type, array_name)
    if isinstance(node, c_ast.FuncDecl):
        return _decl_to_text(node.type, name)
    return node.__class__.__name__


def _expr_to_text(node: object | None) -> str:
    if node is None:
        return ""
    if c_ast is not None and isinstance(node, c_ast.Constant):
        return node.value
    if c_ast is not None and isinstance(node, c_ast.ID):
        return node.name
    return node.__class__.__name__
