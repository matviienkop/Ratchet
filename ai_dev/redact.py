"""Redact credentials from text and structured values before they are stored or published."""

import os
import re


REDACTED = "[redacted]"
_CREDENTIAL = r"(?:[a-z0-9]+[-_])*(?:password|passwd|pwd|passphrase|(?:client|consumer)?secret(?:[-_]key)?|(?:auth|access|refresh|id)?token|api[-_]?key|access[-_]?key(?:[-_]?id)?|private[-_]?key|credentials?|authorization|cookie)"
_KEY = re.compile(rf"^{_CREDENTIAL}$", re.I)
_ASSIGNMENT = re.compile(
    rf"""(?ix)(?<![\w-])(?P<prefix>["']?{_CREDENTIAL}["']?\s*[:=]\s*)
    (?P<value>\[redacted\]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s"'&,;}}\]]+)"""
)
_YAML_SECRET = re.compile(
    rf"""(?imx)^(?P<header>(?P<diff>[+-]?)(?P<indent>[\t ]*)(?:-[\t ]+)?
        ["']?{_CREDENTIAL}["']?[\t ]*:[\t ]*)[|>][+1-9-]{{0,2}}[\t ]*(?:\#[^\r\n]*)?
        (?P<body>(?:\r?\n(?P=diff)(?:(?P=indent)[\t ]+[^\r\n]*|[\t ]*(?=\r?$)))*)"""
)
_AUTH = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=~.:-]+")
_HEADER = re.compile(r"(?im)^(?P<header>[ \t]*(?:authorization|proxy-authorization|(?:set-)?cookie)\s*:\s*)[^\r\n]+")
_PEM = re.compile(r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----[\s\S]*?(?:-----END \1-----|\Z)")
_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@")
_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[\w-]{10,}|github_pat_[\w-]{10,}|glpat-[\w-]{8,}|"
    r"sk-(?:proj-|ant-)?[\w-]{10,}|xox[baprs]-[\w-]{8,}|ya29\.[\w.-]{10,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[\w-]{35}|eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+)"
)
_OPTION = re.compile(rf"""(?ix)(?P<flag>--{_CREDENTIAL}\s+)(?P<value>"[^"]*"|'[^']*'|[^\s]+)""")
_BINARY_PATCH = re.compile(r"(?ms)^GIT binary patch\n.*?(?=^diff --git |\Z)")

def _credential(key):
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).replace("-", "_")
    return bool(_KEY.fullmatch(normalized))


def _known_values(value):
    # The shell exports its working directory as PWD. Explicit password fields
    # and other sources of the same value still participate in redaction.
    shell_directory = os.environ.get("PWD")
    try:
        if shell_directory != os.getcwd():
            shell_directory = None
    except OSError:
        shell_directory = None
    found = {v for k, v in os.environ.items() if _credential(k) and len(v) >= 4
             and (k != "PWD" or v != shell_directory)}

    def add(raw):
        if len(raw) >= 4 and raw.lower() not in ("bearer", "basic", REDACTED):
            found.add(raw)

    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if _credential(str(key)) and isinstance(child, str):
                    add(child)
                visit(child)
        elif isinstance(item, (list, tuple)):
            for i, child in enumerate(item):
                if (i and isinstance(item[i-1], str) and item[i-1].startswith("--")
                        and _credential(item[i-1][2:]) and isinstance(child, str) and len(child) >= 4):
                    add(child)
                visit(child)
        elif isinstance(item, str):
            for block in _YAML_SECRET.finditer(item):
                for line in block["body"].splitlines():
                    add(line[len(block["diff"]):].strip())
            for pattern in (_ASSIGNMENT, _OPTION):
                for match in pattern.finditer(item):
                    add(match["value"].strip("\"'"))
            for match in _AUTH.finditer(item):
                add(match[0].split(None, 1)[1])
            for match in _USERINFO.finditer(item):
                userinfo = match[0][len(match[1]):-1]
                add(userinfo.split(":", 1)[-1])
        elif isinstance(item, bytes):
            visit(item.decode("utf-8", errors="replace"))

    visit(value)
    return sorted(found - {REDACTED}, key=len, reverse=True)


def _clean(value, known):
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return "[binary evidence omitted]"
        if "\0" in value:
            return "[binary evidence omitted]"
    if isinstance(value, str):
        if "\0" in value:
            return "[binary evidence omitted]"
        value = _YAML_SECRET.sub(lambda m: m["header"] + REDACTED, value)
        value = _PEM.sub(REDACTED, value)
        value = _BINARY_PATCH.sub("[binary patch omitted]\n", value)
        value = _HEADER.sub(lambda m: m["header"] + REDACTED, value)
        value = _AUTH.sub(REDACTED, value)
        value = _USERINFO.sub(r"\1[redacted]@", value)
        value = _TOKEN.sub(REDACTED, value)
        value = _OPTION.sub(lambda m: m["flag"] + REDACTED, value)
        value = _ASSIGNMENT.sub(lambda m: m["prefix"] + REDACTED, value)
        for secret in known:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {_clean(str(k), known): REDACTED if _credential(str(k)) else _clean(v, known)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [REDACTED if i and isinstance(value[i-1], str) and value[i-1].startswith("--")
                and _credential(value[i-1][2:]) else _clean(v, known) for i, v in enumerate(value)]
    return value


def sanitize(value):
    """Sanitize captured text, bytes, or structured JSON before persistence or display."""
    return _clean(value, _known_values(value))
