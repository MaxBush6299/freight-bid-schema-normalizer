from __future__ import annotations

import ast
from typing import Any

BANNED_IMPORTS = {
    "os",
    "subprocess",
    "socket",
    "requests",
    "urllib",
}

BANNED_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
}

BANNED_ATTRIBUTE_CALLS = {
    "os.system",
    "subprocess.Popen",
    "subprocess.run",
    "subprocess.call",
    "socket.socket",
    "requests.get",
    "requests.post",
    "urllib.request.urlopen",
}


def _node_to_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        current: ast.AST | None = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


def _is_allowed_toplevel_statement(node: ast.AST) -> bool:
    allowed_types = (
        ast.Import,
        ast.ImportFrom,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Assign,
        ast.AnnAssign,
    )
    if isinstance(node, allowed_types):
        return True
    if isinstance(node, ast.Expr):
        return isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    return False


def evaluate_script_policy(script_source: str) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []

    try:
        parsed = ast.parse(script_source)
    except SyntaxError as error:
        findings.append(
            {
                "code": "syntax_error",
                "severity": "error",
                "message": str(error),
                "line": getattr(error, "lineno", None),
            }
        )
        return {
            "passed": False,
            "error_count": 1,
            "warning_count": 0,
            "findings": findings,
        }

    for node in parsed.body:
        if not _is_allowed_toplevel_statement(node):
            findings.append(
                {
                    "code": "top_level_side_effect",
                    "severity": "error",
                    "message": "Top-level executable statements are not allowed.",
                    "line": getattr(node, "lineno", None),
                }
            )

    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_IMPORTS:
                    findings.append(
                        {
                            "code": "banned_import",
                            "severity": "error",
                            "message": f"Banned import detected: {alias.name}",
                            "line": getattr(node, "lineno", None),
                        }
                    )

        if isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in BANNED_IMPORTS:
                    findings.append(
                        {
                            "code": "banned_import",
                            "severity": "error",
                            "message": f"Banned import detected: {node.module}",
                            "line": getattr(node, "lineno", None),
                        }
                    )

        if isinstance(node, ast.Call):
            call_name = _node_to_name(node.func)
            if call_name in BANNED_CALLS or call_name in BANNED_ATTRIBUTE_CALLS:
                findings.append(
                    {
                        "code": "banned_call",
                        "severity": "error",
                        "message": f"Banned call detected: {call_name}",
                        "line": getattr(node, "lineno", None),
                    }
                )

    transform_functions = [
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name == "transform"
    ]

    if not transform_functions:
        findings.append(
            {
                "code": "missing_transform_entrypoint",
                "severity": "error",
                "message": "Required function transform(context) is missing.",
                "line": None,
            }
        )
    else:
        transform_fn = transform_functions[0]
        arg_count = len(transform_fn.args.args)
        if arg_count != 1 or transform_fn.args.args[0].arg != "context":
            findings.append(
                {
                    "code": "invalid_transform_signature",
                    "severity": "error",
                    "message": "transform function must have exactly one argument named context.",
                    "line": getattr(transform_fn, "lineno", None),
                }
            )

    try:
        compile(script_source, "<planner_script>", "exec")
    except Exception as error:
        findings.append(
            {
                "code": "compile_error",
                "severity": "error",
                "message": str(error),
                "line": None,
            }
        )

    error_count = sum(1 for item in findings if item["severity"] == "error")
    warning_count = sum(1 for item in findings if item["severity"] == "warning")

    return {
        "passed": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "findings": findings,
    }
