"""AST-first symbol and dependency extraction with graceful fallbacks."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ParsedCode:
    symbols: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    parser: str = "none"


_TREE_SITTER_LANGUAGE = {
    "javascript": "javascript",
    "typescript": "typescript",
    "react": "javascript",
    "react-ts": "tsx",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "kotlin": "kotlin",
    "csharp": "c_sharp",
    "ruby": "ruby",
    "c": "c",
    "cpp": "cpp",
}


def parse_code(content: str, language: str) -> ParsedCode:
    """Return semantic orientation data without making parsers mandatory."""
    if language == "python":
        parsed = _parse_python(content)
        if parsed:
            return parsed
    parsed = _parse_tree_sitter(content, language)
    return parsed or ParsedCode()


def _parse_python(content: str) -> ParsedCode | None:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    symbols: list[str] = []
    imports: list[str] = []
    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(node.name)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, ast.Call):
            name = _python_call_name(node.func)
            if name:
                calls.append(name)
    return ParsedCode(
        symbols=_unique(symbols, 100),
        imports=_unique(imports, 60),
        calls=_unique(calls, 100),
        parser="python-ast",
    )


def _python_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _python_call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _parse_tree_sitter(content: str, language: str) -> ParsedCode | None:
    grammar = _TREE_SITTER_LANGUAGE.get(language)
    if not grammar:
        return None
    try:
        from tree_sitter_language_pack import get_parser

        parser = get_parser(grammar)
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return None

    source = content.encode("utf-8")
    symbols: list[str] = []
    imports: list[str] = []
    calls: list[str] = []
    stack: list[Any] = [tree.root_node]
    symbol_nodes = {
        "function_declaration", "function_definition", "method_definition",
        "method_declaration", "class_declaration", "class_definition",
        "interface_declaration", "struct_item", "enum_item", "trait_item",
    }
    import_nodes = {"import_statement", "import_declaration", "use_declaration"}
    call_nodes = {"call_expression", "call"}
    while stack:
        node = stack.pop()
        if node.type in symbol_nodes:
            name_node = node.child_by_field_name("name")
            if name_node:
                symbols.append(source[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace"))
        elif node.type in import_nodes:
            imports.append(source[node.start_byte:node.end_byte].decode("utf-8", "replace")[:240])
        elif node.type in call_nodes:
            name_node = node.child_by_field_name("function") or node.child_by_field_name("name")
            if name_node:
                calls.append(source[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")[:120])
        stack.extend(reversed(node.children))
    return ParsedCode(
        symbols=_unique(symbols, 100),
        imports=_unique(imports, 60),
        calls=_unique(calls, 100),
        parser=f"tree-sitter:{grammar}",
    )


def _unique(values: list[str], limit: int) -> list[str]:
    result = []
    seen = set()
    for value in values:
        clean = str(value).strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
        if len(result) >= limit:
            break
    return result
