"""Operator commands, run where the service's environment (HVSR_*) is available:

    python -m hvsr_web.admin migrate
    python -m hvsr_web.admin issue-admin-token --name "Operator name"
    python -m hvsr_web.admin issue-upload-token [--email E] [--quota-bytes N]
    python -m hvsr_web.admin backup create|verify|restore DIR
    python -m hvsr_web.admin banks list
    python -m hvsr_web.admin banks upload MANIFEST.json [DATA.npz]      (needs HVSR_ADMIN_TOKEN)
    python -m hvsr_web.admin banks activate|deactivate BANK_ID          (needs HVSR_ADMIN_TOKEN)
    python -m hvsr_web.admin sweep-staging

Tokens are printed once and stored only as SHA-256 digests. Bank commands read the administrator
token from the HVSR_ADMIN_TOKEN environment variable, never from the command line (shell history).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from hvsr_core.jsonutil import pretty_dumps, strict_loads

from . import backup, banks
from .auth import AuthError, admin_for_token, issue_admin_token, issue_upload_token
from .context import AppContext
from .db import migrate
from .settings import Settings
from .submissions import sweep_staging


def _ctx() -> AppContext:
    return AppContext.from_settings(Settings.from_env())


def _admin_id(ctx: AppContext) -> str:
    with ctx.sessions() as session:
        return admin_for_token(session, os.environ.get("HVSR_ADMIN_TOKEN")).id


def cmd_migrate(_args) -> dict:
    return {"schema_revision": migrate(Settings.from_env().database_url)}


def cmd_admin_token(args) -> dict:
    ctx = _ctx()
    with ctx.sessions() as session, session.begin():
        token, a = issue_admin_token(session, args.name)
        return {"admin_id": a.id, "token": token, "note": "shown once; store it in a password manager"}


def cmd_upload_token(args) -> dict:
    ctx = _ctx()
    with ctx.sessions() as session, session.begin():
        token, c = issue_upload_token(session, private_email=args.email, note=args.note)
        if args.quota_bytes is not None:
            c.monthly_quota_bytes = args.quota_bytes
        return {"contributor_id": c.id, "token": token, "note": "shown once; send it privately"}


def cmd_backup(args) -> dict:
    if args.action == "verify":
        return backup.verify_backup(args.dir)
    ctx = _ctx()
    if args.action == "create":
        return backup.create_backup(ctx, args.dir)
    return backup.restore_backup(ctx, args.dir)


def cmd_banks(args) -> dict | list:
    ctx = _ctx()
    if args.action == "list":
        return banks.listing(ctx)
    admin_id = _admin_id(ctx)
    if args.action == "upload":
        manifest_bytes = args.target.read_bytes()
        data_path = args.data or args.target.parent / strict_loads(manifest_bytes).get("data_file", "")
        bank, created = banks.upload(ctx, admin_id, manifest_bytes, Path(data_path).read_bytes())
        return {**bank, "created": created}
    return banks.set_active(ctx, admin_id, str(args.target), args.action == "activate")


def cmd_sweep(_args) -> dict:
    return sweep_staging(_ctx())


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m hvsr_web.admin", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    a = sub.add_parser("issue-admin-token")
    a.add_argument("--name", required=True, help="private name of the administrator")
    a.set_defaults(fn=cmd_admin_token)
    u = sub.add_parser("issue-upload-token")
    u.add_argument("--email", help="private contact; never published")
    u.add_argument("--note")
    u.add_argument("--quota-bytes", type=int)
    u.set_defaults(fn=cmd_upload_token)
    b = sub.add_parser("backup")
    b.add_argument("action", choices=["create", "verify", "restore"])
    b.add_argument("dir", type=Path)
    b.set_defaults(fn=cmd_backup)
    k = sub.add_parser("banks")
    k.add_argument("action", choices=["list", "upload", "activate", "deactivate"])
    k.add_argument("target", nargs="?", type=Path, help="manifest path (upload) or bank ID")
    k.add_argument("data", nargs="?", type=Path, help="data file (default: the manifest's data_file)")
    k.set_defaults(fn=cmd_banks)
    sub.add_parser("sweep-staging").set_defaults(fn=cmd_sweep)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "action", None) in ("upload", "activate", "deactivate") and args.target is None:
        print("error: a manifest path or bank ID is required", file=sys.stderr)
        return 2
    try:
        out = args.fn(args)
    except (AuthError, banks.BankRejected, backup.BackupError, KeyError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(pretty_dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
