"""No print() in the connector modules that run inside the frozen exe: its stdout is None (console=False), so a print is
silently lost. The allowed prints are the CLI outputs of main.py flags and the comstress benchmark's JSON line."""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "connector"

# (file, enclosing function) where print() is the intended CLI output
ALLOWED = {
    ("main.py", "log"),           # make_logger's inner stdout mirror, guarded by `if sys.stdout`
    ("main.py", "selftest"),      # --selftest report
    ("main.py", "e2e"),           # --e2e report
    ("main.py", "main"),          # --version
    ("comstress.py", "main"),     # JSON result on the last line (test_com_stress.py parses it)
}


def _prints():
    for path in sorted(PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        stack = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == "print":
                    yield_items.append((path.name, stack[-1] if stack else "<module>", node.lineno))
                self.generic_visit(node)
        yield_items = []
        V().visit(tree)
        yield from yield_items


def test_no_print_outside_cli_outputs():
    bad = [(f, fn, ln) for f, fn, ln in _prints() if (f, fn) not in ALLOWED]
    assert not bad, f"print() would be lost in the frozen exe, use the app logger: {bad}"


def test_allowlist_is_not_stale():
    found = {(f, fn) for f, fn, _ in _prints()}
    assert ALLOWED <= found, f"allowlisted print() no longer exists: {ALLOWED - found}"
