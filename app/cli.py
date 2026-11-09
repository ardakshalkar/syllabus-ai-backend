"""Admin commands: `python -m app.cli import-rubric rubric/2026.1.yaml`, `python -m app.cli seed`,
`python -m app.cli create-db`, `python -m app.cli drive-check`, `python -m app.cli drive-setup <shared folder id>`,
`python -m app.cli setup-schools`, `python -m app.cli add-test-users`."""

import argparse
import asyncio
import logging
import re
from pathlib import Path

import sqlalchemy as sa

from app.db import pg
from app.db.mongo import close_mongo, init_mongo
from app.enums import Role

RUBRIC = Path(__file__).resolve().parents[1] / "rubric" / "2026.1.yaml"


async def _import_rubric(path: Path, activate: bool) -> None:
    from app.services.rubric import import_rubric

    async with pg.session_scope() as session:
        rubric = await import_rubric(session, path, activate=activate)
        print(f"imported rubric {rubric.code} (active={rubric.is_active})")


async def _seed() -> None:
    from seed.demo import run

    await run()


async def _drive_check(folder_ids: list[str], write: bool) -> bool:
    """Print the Google Drive check (same as the Schools & Drive screen); True when everything works."""
    from app.services.drive_admin import check

    r = await check(folder_ids or None, write)
    a = r["account"]
    print(f"drive         {r['provider']} (settings from {'the app' if r['source'] == 'app' else '.env'})")
    if not a["ok"]:
        print(f"FAIL credentials: {a['error']}")
        return False
    if r["provider"] == "google":
        print(f"credentials   {a['type']}")
        if a["email"]:
            print(f"account       {a['email']}  <- share the Drive folders with this email")
            print(f"project       {a['project_id']}")
    print(f"acting as     {a['acting_as']}")
    print(f"access        {'read-only' if r['read_only'] else 'read and write'}")
    if not r["folders"]:
        print("\nNo folder to check: pass --folder <id>, or set the shared folder on Schools & Drive")
    for f in r["folders"]:
        print(f"\nfolder {f['id']}")
        if f["error"]:
            print(f"  FAIL {f['error']}")
            continue
        print(f"  name        {f['name']}")
        print(f"  location    {f['location']}")
        print(f"  can upload  {'yes' if f['can_upload'] else 'no'}")
        sample = ": " + ", ".join(f["sample"]) if f["sample"] else ""
        print(f"  contents    {f['items']} items{sample}")
        for warning in f["warnings"]:
            print(f"  WARN {warning}")
        if f["write_test"]:
            ok = f["write_test"] == "ok"
            print(f"  write test  {'OK (a test file was uploaded and deleted)' if ok else f['write_test']}")
    return bool(r["ok"])


async def _setup_schools() -> None:

    from app.models import RubricVersion
    from app.services.demo_users import setup_schools
    from app.services.rubric import import_rubric

    async with pg.session_scope() as session:
        for name, created in await setup_schools(session):
            print(f"{'created ' if created else 'exists  '} {name}")
        if not await session.scalar(sa.select(RubricVersion.id).limit(1)):  # a new database: checks need one
            rubric = await import_rubric(session, RUBRIC)
            print(f"imported rubric {rubric.code}")


async def _add_test_users(instructor: bool) -> None:
    from app.services.demo_users import add_demo_users

    async with pg.session_scope() as session:
        report = await add_demo_users(session, instructor=instructor)
    for email, roles in report.roles.items():
        new = " (new)" if email in report.created else ""
        print(f"{email}{new}\n  token stub:{email}\n  " + "\n  ".join(roles or ["no scope found"]))
    if instructor:
        print(f"\nkarimova@ now co-teaches {report.courses_taught} more course(s)")
    print("\nPick these users in the frontend's user switcher (sidebar).")


async def _requeue_runs(include_sessions: bool) -> None:
    """Send Queued runs to the broker again, e.g. after Redis lost its queue (it keeps no data on restart).
    A run that is already queued or done ignores the extra message (checks.execute_run is idempotent)."""

    from app.enums import RunStatus
    from app.models import AssessmentRun
    from app.workers import dispatch

    async with pg.session_scope() as session:
        query = sa.select(AssessmentRun.id).where(AssessmentRun.status == RunStatus.queued)
        if not include_sessions:
            query = query.where(AssessmentRun.session_id.is_(None))
        run_ids = list(await session.scalars(query.order_by(AssessmentRun.created_at)))
    await dispatch.check_runs(run_ids)
    print(f"sent {len(run_ids)} queued check(s) to the workers again")


async def _drive_setup(args: argparse.Namespace) -> None:
    from app.services.drive_setup import setup_shared_folder
    from app.storage import drive_config

    config = await drive_config.refresh()
    if config.provider == "google" and not await _drive_check([args.root], False):
        raise SystemExit(1)
    report = await setup_shared_folder(
        args.root, args.semester, create=not args.no_create, dry_run=args.dry_run
    )
    print(f"\nSchool folders in {args.root} for {args.semester}:")
    for link in report.links:
        print(
            f"  {link.action:<13} {link.school}"
            + (f"  ->  {link.drive_folder_id}" if link.drive_folder_id else "")
        )
    if args.dry_run:
        print("\nDry run: nothing was created or saved.")
        return
    print(
        f"\nSchools & Drive: {report.folders_updated_in_db} folder(s) updated, "
        f"{report.folders_created_in_db} created."
    )
    if any(link.action == "missing" for link in report.links):
        why = "Drive is read-only" if config.read_only else "--no-create"
        print(
            f"Missing School folders were not created ({why}): create them in Drive or run again without it."
        )
    if config.default_folder_id != args.root:
        print(
            f"\nFor Schools without a folder: set {args.root} as the shared folder on Schools & Drive,"
            f" or DRIVE_DEFAULT_FOLDER_ID={args.root} in .env"
        )


async def _users(args: argparse.Namespace) -> None:
    from app.services import users

    async with pg.session_scope() as session:
        try:
            if args.command == "add-user":
                user, created = await users.add_user(
                    session,
                    args.email,
                    Role(args.role),
                    name=args.name or args.name_arg,
                    title=args.title,
                    school=args.school,
                    program=args.program,
                )
                print(f"{'created' if created else 'updated'} {user.email}")
            elif args.command == "disable-user":
                user = await users.disable_user(session, args.email)
                print(f"disabled {user.email}: their sign-ins are refused from now on")
        except users.UserError as exc:
            raise SystemExit(f"error: {exc}") from exc
        for user, roles in await users.list_users(session):
            if args.command == "list-users" or user.email == args.email.strip().lower():
                state = "" if user.is_active else "  [disabled]"
                print(f"{user.email}{state}\n  " + ("\n  ".join(roles) or "no roles"))


async def _create_db() -> None:
    """Create the database named in DATABASE_URL if it does not exist yet (the separate one for real data)."""
    import asyncpg
    from sqlalchemy.engine import make_url

    from app.config import get_settings

    url = make_url(get_settings().database_url)
    name = url.database or ""
    if not re.fullmatch(r"\w+", name):
        raise SystemExit(f"unexpected database name {name!r}")
    conn = await asyncpg.connect(
        user=url.username, password=url.password, host=url.host, port=url.port or 5432, database="postgres"
    )
    try:
        if await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            print(f"database {name} exists")
        else:
            await conn.execute(f'CREATE DATABASE "{name}"')
            print(f"created database {name}")
    finally:
        await conn.close()


async def _main(args: argparse.Namespace) -> None:
    await init_mongo()
    try:
        if args.command == "import-rubric":
            await _import_rubric(Path(args.path), not args.no_activate)
        elif args.command == "seed":
            await _seed()
        elif args.command == "drive-setup":
            await _drive_setup(args)
        elif args.command == "requeue-runs":
            await _requeue_runs(args.all)
        elif args.command in ("add-user", "list-users", "disable-user"):
            await _users(args)
        elif args.command == "setup-schools":
            await _setup_schools()
        elif args.command == "add-test-users":
            await _add_test_users(args.instructor)
        elif args.command == "drive-check":
            if not await _drive_check(args.folder, args.write):
                raise SystemExit(1)
    finally:
        await close_mongo()
        await pg.get_engine().dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    rubric = sub.add_parser("import-rubric", help="import a rubric YAML as a new version")
    rubric.add_argument("path")
    rubric.add_argument(
        "--no-activate", action="store_true", help="import without making it the active rubric"
    )
    sub.add_parser("seed", help="load the prototype demo data and run its checks")
    sub.add_parser("create-db", help="create the PostgreSQL database in DATABASE_URL if it is missing")
    setup = sub.add_parser(
        "drive-setup",
        help="use a shared Drive folder: link (and create) one subfolder per School as its upload folder",
    )
    setup.add_argument(
        "root", help="id of the shared folder (with DRIVE_PROVIDER=stub: its path under DRIVE_STUB_ROOT)"
    )
    setup.add_argument("--semester", required=True, help='upload folders of this semester, e.g. "Fall 2026"')
    setup.add_argument("--no-create", action="store_true", help="only link School folders that already exist")
    setup.add_argument("--dry-run", action="store_true", help="only show what would be linked or created")
    requeue = sub.add_parser(
        "requeue-runs",
        help="send Queued checks to the workers again (after Redis lost its queue); single checks only by default",
    )
    requeue.add_argument("--all", action="store_true", help="also the checks of sessions")
    sub.add_parser(
        "setup-schools",
        help="create the University's four Schools, and the rubric in a new database (idempotent)",
    )
    add_user = sub.add_parser(
        "add-user", help="give a person a role (creates the user); they sign in with their Google account"
    )
    add_user.add_argument("email")
    # Also accepted positionally (`add-user EMAIL "Full Name" --role …`), the form PR #1 introduced.
    add_user.add_argument("name_arg", nargs="?", metavar="name", help="display name (same as --name)")
    add_user.add_argument(
        "--role", required=True, choices=["provost", "kok", "school_admin", "program_head", "professor"]
    )
    add_user.add_argument(
        "--school", help="school_admin / program_head: economics, humanities, law, digital (or slug / name)"
    )
    add_user.add_argument("--name", help="display name (default: the email, replaced by the Google name)")
    add_user.add_argument("--title", help="job title, e.g. Associate Professor")
    add_user.add_argument(
        "--program", help="program_head: the program's slug (default: the School's program)"
    )
    sub.add_parser("list-users", help="every user with their roles")
    disable = sub.add_parser("disable-user", help="refuse this person's sign-ins from now on")
    disable.add_argument("email")
    users = sub.add_parser(
        "add-test-users",
        help="add KOK, School admin, Program head and professor users to this database (not in prod)",
    )
    users.add_argument(
        "--instructor",
        action="store_true",
        help="also make the professor a co-instructor of every course (to try the non-lead view)",
    )
    check = sub.add_parser(
        "drive-check", help="check the Google Drive credentials and folders (GOOGLE_DRIVE_SETUP.md)"
    )
    check.add_argument(
        "--folder",
        action="append",
        default=[],
        metavar="ID",
        help="folder id to check (repeat for more); default: DRIVE_DEFAULT_FOLDER_ID and the Schools & Drive folders",
    )
    check.add_argument(
        "--write", action="store_true", help="also upload a small test file to each folder and delete it"
    )
    args = parser.parse_args()
    if args.command == "create-db":
        asyncio.run(_create_db())
        return
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
