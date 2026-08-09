"""
Migration missing-import investigator (v2 — scope-aware).

For every .py under predictx/app, reports names that are *used* (Load) but are
NOT:
  - a Python builtin,
  - defined anywhere in the same file (def/class/assign/param/comprehension),
  - imported by the file (including `from x import *`, resolved via AST),
  - declared `global` / `nonlocal`.

This isolates genuine "called-but-never-imported" bugs (the silent NameError
class introduced by the folder migration) from same-file helpers.

Usage:  python _check_imports.py
"""
from __future__ import annotations
import ast
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
BUILTINS = set(dir(__builtins__)) | {
    "self", "cls", "True", "False", "None", "Ellipsis", "NotImplemented",
    "__name__", "__file__", "__doc__", "__package__", "__loader__",
    "__spec__", "__annotations__", "__builtins__", "__class__", "__dict__",
    "__slots__", "__weakref__", "__module__", "__qualname__", "__func__",
    "__self__", "__code__", "__defaults__", "__kwdefaults__", "__globals__",
    "__closure__",
}

# Cache parsed ASTs of modules we resolve `from x import *` against.
_AST_CACHE: dict[str, ast.Module | None] = {}


def _module_file_for(import_str, level, current_file):
    """Resolve a (possibly relative) module import string to a .py path."""
    if level is None:
        level = 0
    parts = import_str.split(".") if import_str else []
    if level == 0:
        base = ROOT
        rel_parts = parts
    else:
        cur_dir = os.path.dirname(current_file)
        for _ in range(level - 1):
            cur_dir = os.path.dirname(cur_dir)
        rel_parts = parts
        base = cur_dir
    if not rel_parts:
        cand = os.path.join(base, "__init__.py")
    else:
        cand = os.path.join(base, *rel_parts) + ".py"
        if not os.path.exists(cand):
            cand2 = os.path.join(base, *rel_parts, "__init__.py")
            if os.path.exists(cand2):
                cand = cand2
    if os.path.exists(cand):
        return cand
    return None


def _public_names_of(module_path):
    """Top-level def/class names of a module (approximates `import *`)."""
    if module_path in _AST_CACHE:
        tree = _AST_CACHE[module_path]
    else:
        try:
            with open(module_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=module_path)
        except Exception:
            tree = None
        _AST_CACHE[module_path] = tree
    if tree is None:
        return set()
    names = set()
    # Check __all__ first
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                names.add(elt.value)
    if names:
        return names
    # Fall back to all top-level defs/classes/assigns
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in _assign_names(t):
                    names.add(n)
    return names


def _assign_names(node):
    out = []
    if isinstance(node, ast.Name):
        out.append(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            out.extend(_assign_names(e))
    elif isinstance(node, ast.Starred):
        out.extend(_assign_names(node.value))
    return out


def _arg_names(args):
    """Extract all parameter names from an ast.arguments node."""
    names = []
    for a in args.posonlyargs:
        names.append(a.arg)
    for a in args.args:
        names.append(a.arg)
    if args.vararg:
        names.append(args.vararg.arg)
    for a in args.kwonlyargs:
        names.append(a.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return names


def _collect_file_defs(tree):
    """All names defined in the file at any scope.

    Includes: def/class names, assignments, function parameters,
    lambda parameters, comprehension targets, with-as, except-as,
    for-loop targets, global/nonlocal declarations.
    """
    defined = set()

    class DefCollector(ast.NodeVisitor):
        def visit_FunctionDef(self, n):
            defined.add(n.name)
            for nm in _arg_names(n.args):
                defined.add(nm)
            self.generic_visit(n)

        def visit_AsyncFunctionDef(self, n):
            defined.add(n.name)
            for nm in _arg_names(n.args):
                defined.add(nm)
            self.generic_visit(n)

        def visit_Lambda(self, n):
            for nm in _arg_names(n.args):
                defined.add(nm)
            self.generic_visit(n)

        def visit_ClassDef(self, n):
            defined.add(n.name)
            self.generic_visit(n)

        def visit_Assign(self, n):
            for t in n.targets:
                for nm in _assign_names(t):
                    defined.add(nm)
            self.generic_visit(n)

        def visit_AnnAssign(self, n):
            for nm in _assign_names(n.target):
                defined.add(nm)
            self.generic_visit(n)

        def visit_For(self, n):
            for nm in _assign_names(n.target):
                defined.add(nm)
            self.generic_visit(n)

        def visit_AsyncFor(self, n):
            self.visit_For(n)

        def visit_With(self, n):
            for item in n.items:
                if item.optional_vars:
                    for nm in _assign_names(item.optional_vars):
                        defined.add(nm)
            self.generic_visit(n)

        def visit_AsyncWith(self, n):
            self.visit_With(n)

        def visit_ExceptHandler(self, n):
            if n.name:
                defined.add(n.name)
            self.generic_visit(n)

        def visit_Global(self, n):
            for nm in n.names:
                defined.add(nm)
            self.generic_visit(n)

        def visit_Nonlocal(self, n):
            for nm in n.names:
                defined.add(nm)
            self.generic_visit(n)

        def visit_comprehension(self, n):
            """Comprehension targets are in their own scope but the variable
            is used within the comprehension. We add them to defined so they
            don't get flagged as undefined."""
            for nm in _assign_names(n.target):
                defined.add(nm)
            self.generic_visit(n)

    DefCollector().visit(tree)
    return defined


def _collect_imported_names(tree, current_file):
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.names and node.names[0].name == "*":
                mod_file = _module_file_for(node.module or "", node.level, current_file)
                if mod_file:
                    imported |= _public_names_of(mod_file)
            else:
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imported.add(alias.asname or alias.name)
    return imported


def main():
    total = 0
    files_with_issues = 0
    for dirpath, _, filenames in os.walk(ROOT):
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    src = f.read()
            except Exception:
                continue
            try:
                tree = ast.parse(src, filename=path)
            except SyntaxError as e:
                print("[SYNTAX ERROR] %s:%d: %s" % (os.path.relpath(path, ROOT), e.lineno, e.msg))
                total += 1
                files_with_issues += 1
                continue
            defined = _collect_file_defs(tree)
            imported = _collect_imported_names(tree, path)
            safe = BUILTINS | defined | imported
            issues = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    if node.id in safe:
                        continue
                    issues.add((node.lineno, node.id))
            if issues:
                files_with_issues += 1
                print("\n=== %s ===" % os.path.relpath(path, ROOT))
                for lineno, name in sorted(issues):
                    print("  line %d: '%s'" % (lineno, name))
                    total += 1
    print("\n----------------------------------------")
    print("Potential missing-import / undefined names: %d across %d files" % (total, files_with_issues))
    if total == 0:
        print("No undefined names found via static analysis.")


if __name__ == "__main__":
    main()
