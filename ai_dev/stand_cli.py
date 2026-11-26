"""Worker interface for stand capabilities registered by the host operator."""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile

from . import gitio, policy, stands
from .redact import sanitize
from .store import CardError, record

ACTIONS = ("start", "status", "test", "install", "repair", "reset", "reinstall", "restore-previous", "shutdown")


def operations(driver):
    code, output = stands.call(driver, "operations", timeout=10)
    try:
        result = json.loads(output) if code == 0 else None
    except ValueError:
        result = None
    if not isinstance(result, list) or not all(isinstance(v, str) for v in result):
        raise CardError(f"Stand {driver.name} does not advertise its operations. Check its registered driver.")
    return [v for v in ACTIONS if v in result]


def run(args, card, workspace):
    # Repair cards have scratch workspaces. Their parent project board identifies the catalog.
    try:
        repo = gitio.repo_root(workspace)
    except CardError:
        repo = None
    settings = policy.load(repo=gitio.main_repo(repo), origin=gitio.origin(repo)) if repo else policy.load()
    project = settings["project"] or card.board
    if args.action == "list":
        if args.name or args.arguments:
            raise CardError("Use stand list without a driver name.")
        catalog = {}
        for name in stands.available(project):
            try:
                catalog[name] = operations(stands.registered(project, name))
            except (CardError, OSError, subprocess.TimeoutExpired) as error:
                catalog[name] = {"problem": str(error)}
        return {"project": project, "stands": catalog}, 0
    if not args.name:
        raise CardError("Choose a registered driver from stand list.")
    driver = stands.registered(project, args.name)
    if args.action not in operations(driver):
        raise CardError(f"This stand does not grant {args.action}. See stand list.")
    arguments = args.arguments
    if args.action == "test":
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        if not arguments:
            raise CardError("Run stand test NAME -- COMMAND [ARG...].")
    elif args.action == "install":
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        if len(arguments) != 2:
            raise CardError("Run stand install NAME -- PACKAGE_URL SHA256.")
    elif arguments:
        raise CardError("Only stand test and stand install accept arguments.")
    script = None
    if args.action == "repair":
        if sys.stdin.isatty():
            raise CardError("Pass the guest repair script via standard input.")
        script = sys.stdin.buffer.read(262145)
        if not 0 < len(script) <= 262144:
            raise CardError("The guest repair script must contain 1 to 262144 bytes.")
    fields = {"stand": driver.name, "operation": args.action, "argv": arguments}
    if script:
        fields["script_sha256"] = hashlib.sha256(script).hexdigest()
    with card.locked() as state:
        record(state, "stand.requested", **fields)
        request = len(state["log"])
    argv = [str(driver), args.action, *(["--", *arguments] if arguments else [])]
    with tempfile.TemporaryFile() as log:
        try:
            with stands.exclusive(project, driver.name):
                code = subprocess.run(argv, cwd=driver.parent, input=script,
                                      stdin=None if script else subprocess.DEVNULL,
                                      stdout=log, stderr=subprocess.STDOUT, timeout=stands.PREPARE_TIMEOUT).returncode
            log.seek(max(0, log.tell() - 8192))
            output = sanitize(log.read().decode(errors="replace"))
        except CardError as error:
            code, output = 1, str(error)
        except (OSError, subprocess.TimeoutExpired) as error:
            code, output = 1, f"{type(error).__name__} during {args.action}."
    with card.locked() as state:
        record(state, "stand.finished", request=request, exit_code=code, output=output, **fields)
    return {"request": request, "stand": driver.name, "operation": args.action,
            "exit_code": code, "output": output}, int(code != 0)


def add_command(commands, get_card, get_workspace):
    sub = commands.add_parser("stand", help="Inspect and use operations granted on registered guest VMs.")
    sub.add_argument("action", choices=("list", *ACTIONS))
    sub.add_argument("name", nargs="?")
    sub.add_argument("arguments", nargs=argparse.REMAINDER)
    sub.set_defaults(run=lambda args: run(args, get_card(args), get_workspace(args)))
