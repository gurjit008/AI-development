"""Dependency-free local/CI verification; never report zero tests as success."""
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.version_info < (3, 13):
        print("Python 3.13 or newer is required.", file=sys.stderr)
        return 1
    required = ("app/server.py", "app/database.py", "app/schema.py",
                "app/importer.py", "web/index.html", "web/styles.css", "web/app.js")
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        print("Missing implementation files: " + ", ".join(missing), file=sys.stderr)
        return 1
    sources = sorted(path for directory in ("app", "scripts", "tests")
                     for path in (ROOT / directory).rglob("*.py"))
    for path in sources:
        try:
            compile(path.read_bytes(), str(path.relative_to(ROOT)), "exec")
        except (SyntaxError, ValueError) as error:
            print(f"Python syntax failed: {error}", file=sys.stderr)
            return 1
    print(f"Python syntax: {len(sources)} files passed.", flush=True)
    node = shutil.which("node")
    if not node:
        print("Node.js is required for JavaScript syntax verification.", file=sys.stderr)
        return 1
    result = subprocess.run([node, "--check", "web/app.js"], cwd=ROOT, check=False)
    if result.returncode:
        return result.returncode
    print("JavaScript syntax: web/app.js passed.", flush=True)
    frontend_tests = subprocess.run([node, "--test", "web/app.test.cjs"], cwd=ROOT, check=False)
    if frontend_tests.returncode:
        return frontend_tests.returncode
    if not (ROOT / "tests").is_dir():
        print("Missing tests directory; verification cannot pass.", file=sys.stderr)
        return 1
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    count = suite.countTestCases()
    if count == 0:
        print("No tests discovered; verification cannot pass.", file=sys.stderr)
        return 1
    print(f"Discovered {count} tests; running unittest discovery.", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT, check=False,
    )
    if result.returncode == 0:
        print("Verification passed. Browser accessibility and production readiness require separate review.")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
