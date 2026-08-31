"""Installed `multica-delivery` command surface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Sequence

from multica_delivery import __version__
from multica_delivery.core.contract_audit import audit_contracts
from multica_delivery.core.manifest import ManifestError, load_manifest

from .apply import ApplyService, InteractiveConfirmationReader
from .clock import SystemClock
from .commands.apply import run_apply
from .commands.discover import run_discover
from .commands.doctor import run_doctor
from .commands.init import run_init
from .commands.plan import run_plan
from .commands.upgrade import run_upgrade
from .commands.validate import run_validate
from .confirmation import load_confirmation_text
from .discovery import LocalRepositoryReader, discovery_from_value
from .doctor import DoctorService
from .errors import CliError, ExitCode
from .output import Envelope, SafeMessage
from .plan import PlanStore
from .secrets import DeferredSecretSource, EnvironmentSecretSource, PromptSecretSource
from .services import build_planning_service
from .upgrade import MigrationExecutor, UpgradeService
from .validation import SubprocessVersionReader, validate_control_directory


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliError(
            "cli.arguments",
            "Command-line arguments are invalid",
            ExitCode.VALIDATION,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="multica-delivery")
    parser.add_argument("--output", choices=("human", "json"), default="human")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="inspect explicit repository roots")
    discover.add_argument("paths", nargs="+", type=Path)

    initialize = subparsers.add_parser("init", help="create a confirmed local scaffold")
    initialize.add_argument("--discovery", required=True, type=Path)
    initialize.add_argument("--confirmation", required=True, type=Path)
    initialize.add_argument("--target", required=True, type=Path)

    validate = subparsers.add_parser("validate", help="validate a delivery-control directory")
    validate.add_argument("path", type=Path)

    plan = subparsers.add_parser("plan", help="create a read-only onboarding plan")
    plan.add_argument("path", type=Path)
    plan.add_argument("--plan-path", type=Path)

    apply = subparsers.add_parser("apply", help="apply one exact confirmed plan")
    apply.add_argument("--plan", dest="plan_path", required=True, type=Path)
    apply.add_argument("--confirm", dest="confirmation")
    apply.add_argument("--manifest", dest="manifest_path", type=Path)
    apply.add_argument("--lock", dest="lock_path", type=Path)
    apply.add_argument("--secret-source", choices=("environment", "prompt"), default="environment")

    doctor = subparsers.add_parser("doctor", help="run read-only lifecycle diagnostics")
    doctor.add_argument("path", type=Path)

    upgrade = subparsers.add_parser("upgrade", help="create a framework migration plan")
    upgrade.add_argument("path", type=Path)
    upgrade.add_argument("--plan-path", type=Path)
    return parser


def command_names(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return tuple(action.choices)
    return ()


def _load_discovery(path: Path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CliError(
            "init.discovery_unreadable",
            "Discovery JSON cannot be read safely",
            ExitCode.VALIDATION,
        ) from None
    if (
        isinstance(value, dict)
        and value.get("command") == "discover"
        and value.get("status") == "ok"
        and isinstance(value.get("result"), dict)
    ):
        value = value["result"]
    return discovery_from_value(value)


class DefaultServices:
    """Construct each command's minimum typed capability set on demand."""

    def discover(self, args) -> Envelope:
        return run_discover(
            args,
            SimpleNamespace(repository_reader=LocalRepositoryReader()),
        )

    def init(self, args) -> Envelope:
        try:
            confirmation_text = args.confirmation.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise CliError(
                "init.confirmation_unreadable",
                "Confirmation YAML cannot be read safely",
                ExitCode.VALIDATION,
            ) from None
        return run_init(
            args,
            SimpleNamespace(
                discovery=_load_discovery(args.discovery),
                confirmations=load_confirmation_text(confirmation_text),
            ),
        )

    def validate(self, args) -> Envelope:
        return run_validate(
            args,
            SimpleNamespace(
                version_reader=SubprocessVersionReader(),
                platform_name=sys.platform,
                python_version=(sys.version_info.major, sys.version_info.minor),
            ),
        )

    def plan(self, args) -> Envelope:
        plan_path = args.plan_path or args.path / "plan.json"
        return run_plan(
            SimpleNamespace(path=args.path, plan_path=plan_path, mode="onboard"),
            SimpleNamespace(
                planning=build_planning_service(args.path),
                clock=SystemClock(),
                plan_store=PlanStore(),
            ),
        )

    def apply(self, args) -> Envelope:
        control = args.plan_path.parent
        manifest_path = args.manifest_path or control / "delivery.yaml"
        lock_path = args.lock_path or control / "framework.lock"
        try:
            manifest = load_manifest(manifest_path, strict_commands=True)
        except ManifestError:
            raise CliError(
                "apply.invalid_manifest",
                "Manifest cannot be loaded for apply",
                ExitCode.VALIDATION,
            ) from None
        names = {
            name
            for repository in manifest.repositories.values()
            for name in repository.secret_env
        }
        secret_source = DeferredSecretSource(
            lambda: (
                PromptSecretSource(names)
                if args.secret_source == "prompt"
                else EnvironmentSecretSource(names)
            )
        )
        planning = build_planning_service(control)
        service = ApplyService(
            planning,
            planning.provisioner,
            PlanStore(),
            SystemClock(),
            confirmation_reader=InteractiveConfirmationReader(),
            migration_executor=MigrationExecutor(planning),
        )
        return run_apply(
            SimpleNamespace(
                plan_path=args.plan_path,
                confirmation=args.confirmation,
                manifest_path=manifest_path,
                lock_path=lock_path,
            ),
            SimpleNamespace(apply_service=service, secret_source=secret_source),
        )

    def doctor(self, args) -> Envelope:
        planning = build_planning_service(args.path)

        def validation(path: Path):
            return validate_control_directory(path)

        def contract(path: Path):
            manifest = load_manifest(
                path / "delivery.yaml",
                strict_commands=True,
            )
            return audit_contracts(
                planning.provisioner.multica,
                planning.provisioner.github,
                manifest,
            )

        return run_doctor(
            args,
            SimpleNamespace(doctor=DoctorService(validation, contract, planning)),
        )

    def upgrade(self, args) -> Envelope:
        plan_path = args.plan_path or args.path / "plan.json"
        planning = build_planning_service(args.path)
        return run_upgrade(
            SimpleNamespace(path=args.path, plan_path=plan_path),
            SimpleNamespace(
                upgrade=UpgradeService(planning=planning),
                clock=SystemClock(),
                plan_store=PlanStore(),
            ),
        )


def _requested_output(argv: Sequence[str]) -> str:
    for index, value in enumerate(argv):
        if value == "--output" and index + 1 < len(argv):
            return "json" if argv[index + 1] == "json" else "human"
    return "human"


def _dispatch(command: str, args, services) -> Envelope:
    if command == "discover":
        return services.discover(args)
    if command == "init":
        return services.init(args)
    if command == "validate":
        return services.validate(args)
    if command == "plan":
        return services.plan(args)
    if command == "apply":
        return services.apply(args)
    if command == "doctor":
        return services.doctor(args)
    if command == "upgrade":
        return services.upgrade(args)
    raise CliError("cli.command", "Command is unsupported", ExitCode.VALIDATION)


def _render(envelope: Envelope, output: str) -> None:
    print(envelope.to_json() if output == "json" else envelope.to_human())


def _envelope_exit(envelope: Envelope) -> ExitCode:
    if envelope.command == "doctor":
        value = envelope.result.get("exit_code", 0)
        try:
            return ExitCode(value)
        except (TypeError, ValueError):
            return ExitCode.EXTERNAL
    if envelope.status == "ok":
        return ExitCode.OK
    if envelope.status == "human-block":
        return ExitCode.HUMAN_BLOCK
    if envelope.command == "validate":
        return ExitCode.VALIDATION
    return ExitCode.EXTERNAL


def main(argv: Sequence[str] | None = None, *, services=None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    output = _requested_output(arguments)
    command = next((value for value in arguments if value in command_names(build_parser())), "cli")
    try:
        args = build_parser().parse_args(arguments)
        output = args.output
        envelope = _dispatch(args.command, args, services or DefaultServices())
        _render(envelope, output)
        return int(_envelope_exit(envelope))
    except SystemExit as exit_signal:
        return int(exit_signal.code or 0)
    except CliError as error:
        envelope = Envelope(
            command=command,
            status="failed",
            result={},
            errors=(SafeMessage(error.code, error.safe_message),),
        )
        _render(envelope, output)
        return int(error.exit_code)
    except Exception:
        envelope = Envelope(
            command=command,
            status="failed",
            result={},
            errors=(SafeMessage("cli.internal", "Command failed at a sanitized boundary"),),
        )
        _render(envelope, output)
        return int(ExitCode.EXTERNAL)
