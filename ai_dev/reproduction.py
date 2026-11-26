"""Structured interpretation of a single, explicitly selected Go test."""

import json
from pathlib import Path
import re
import tempfile

PACKAGE = re.compile(r"(?:[.]/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)|[.]")
TEST = re.compile(r"Test[A-Z0-9_][A-Za-z0-9_]*")
GO_ENV = {"GOENV": "off", "GOFLAGS": "", "GOWORK": "off", "GOTOOLCHAIN": "local",
          "GOPROXY": "off", "GOSUMDB": "off"}


def is_go(argv):
    return len(argv) > 1 and Path(argv[0]).name == "go" and argv[1] == "test"


def command(package, test, executable="go"):
    return _command(package, f"^{test}$", executable, 1)


def _command(package, selector, executable, count):
    return [executable, "test", "-json", f"-count={count}", "-run", selector, package]


# These options affect execution or the build, not which test is selected.
# A tracked supersession uses the default build only; its declaration check
# and its test run must use the same configuration.
_GO_VALUE_OPTIONS = frozenset({"-count", "-timeout", "-tags", "-parallel", "-shuffle", "-vet"})
_GO_BOOL_OPTIONS = frozenset({"-short", "-race", "-cover"})


def structured(argv, *, ordinary=False):
    """Build a structured run for one named test in one local package."""
    args = iter(argv[2:])
    package = selector = None
    options = {}
    for arg in args:
        if arg in ("-v", "-json") or (not ordinary and arg == "-count=1"):
            continue
        if arg == "-run" or arg.startswith("-run="):
            if selector is not None:
                return None
            selector = next(args, "") if arg == "-run" else arg[5:]
        elif ordinary and (flag := arg.partition("=")[0]) in _GO_VALUE_OPTIONS | _GO_BOOL_OPTIONS:
            if flag in options:
                return None
            _, equals, value = arg.partition("=")
            if flag in _GO_VALUE_OPTIONS:
                value = value if equals else next(args, "")
                if not value or value.startswith("-"):
                    return None
            else:
                value = value if equals else "true"
                if value not in ("true", "false"):
                    return None
            options[flag] = value
        elif PACKAGE.fullmatch(arg) and ".." not in Path(arg).parts and package is None:
            package = arg
        else:
            return None
    if selector and selector.startswith("^") and selector.endswith("$"):
        test = selector[1:-1]
    else:
        test = selector if ordinary else None
    if not test or not TEST.fullmatch(test):
        return None
    count = options.pop("-count", "1")
    if not re.fullmatch(r"[1-9][0-9]{0,17}", count):
        return None
    run = _command(package or ".", selector, argv[0], count)
    run[-1:-1] = [f"{flag}={value}" for flag, value in options.items()]
    return run, test, int(count)


# Go's own parser distinguishes package-level functions from methods, comments and strings.
# Inspect only files selected by the same Go build configuration as the test runner.
_DECLARATION_CHECK = """package main
import (
    "go/ast"
    "go/parser"
    "go/token"
    "os"
)
func main() {
    test, target := os.Args[2], os.Args[3]
    count, inTarget := 0, false
    for _, path := range os.Args[4:] {
        file, err := parser.ParseFile(token.NewFileSet(), path, nil, parser.SkipObjectResolution)
        if err != nil { os.Exit(1) }
        for _, d := range file.Decls {
            if fn, ok := d.(*ast.FuncDecl); ok && fn.Recv == nil && fn.Name.Name == test {
                count++
                inTarget = inTarget || path == target
            }
        }
    }
    if count != 1 || !inTarget { os.Exit(1) }
}
"""


def go_declared_in(run, executable, root, package, test, source, timeout):
    """True only for a unique top-level declaration in the requested active build file."""
    with tempfile.TemporaryDirectory(prefix="ai-dev-go-source-") as directory:
        scratch = Path(directory)
        listing = scratch / "list.json"
        _, _, klass, _ = run([executable, "list", "-json", package], root, listing, timeout, GO_ENV)
        if klass != "pass":
            return False
        try:
            metadata = json.loads(listing.read_bytes())
            if Path(metadata["Dir"]) != source.parent:
                return False
            names = metadata.get("TestGoFiles", []) + metadata.get("XTestGoFiles", [])
            if source.name not in names or not all(isinstance(n, str) and Path(n).name == n for n in names):
                return False
        except (KeyError, ValueError, TypeError, OSError):
            return False
        helper = scratch / "check.go"
        helper.write_text(_DECLARATION_CHECK)
        _, _, klass, _ = run([executable, "run", str(helper), "--", test, str(source),
                             *(str(source.parent / n) for n in names)], scratch, scratch / "parse.log", timeout, GO_ENV)
        return klass == "pass"


def go_result(log, code, test, count=1):
    """A zero exit with no named test, setup/build errors and skips are inconclusive."""
    if code not in (0, 1):
        return "environment"
    tests, packages = [], []
    for line in log.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            # Older Go versions can write compiler diagnostics outside the JSON stream.
            if line.strip():
                return "environment"
            continue
        if not isinstance(event, dict):
            return "environment"
        action, package = event.get("Action"), event.get("Package")
        if action in ("build-fail", "build-output") or event.get("FailedBuild"):
            return "environment"
        name = event.get("Test")
        if name and action in ("run", "pass", "fail", "skip"):
            if not isinstance(name, str) or name.split("/", 1)[0] != test:
                return "environment"
            if name == test:
                tests.append((package, action))
        if not name and action in ("pass", "fail", "skip"):
            packages.append((package, action))
    result = "pass" if code == 0 else "fail"
    if len(packages) == 1 and isinstance(packages[0][0], str) and packages[0][1] == result:
        package = packages[0][0]
        if len(tests) == 2 * count:
            starts, ends = tests[::2], tests[1::2]
            if all(event == (package, "run") for event in starts) and all(
                    name == package and action in ("pass", "fail") for name, action in ends):
                observed = "fail" if (package, "fail") in ends else "pass"
                if observed == result:
                    return result
    return "environment"


def legacy_compile_only(data, destination):
    """Recognize plain Go compile logs only, with a diagnostic in the stored overlay.

    Refuse legacy logs with any unrecognized output, including assertion/suite output.
    This deliberately does not handle setup failures or other runner formats.
    """
    lines = data.decode(errors="replace").splitlines()
    if len(lines) < 4 or not lines[0].startswith("# ") or lines[-1] != "FAIL":
        return False
    failure = re.fullmatch(r"FAIL\s+(\S+) \[build failed\]", lines[-2])
    if not failure or lines[0] not in (f"# {failure[1]}", f"# {failure[1]} [{failure[1]}.test]"):
        return False
    diagnostic = re.compile(r"(?:[.]/)?" + re.escape(destination) + r":[0-9]+:[0-9]+: .+")
    return all(diagnostic.fullmatch(line) for line in lines[1:-2])
