"""Admin commands: `python -m app.cli import-rubric rubric/2026.1.yaml`, `seed`, `add-user`."""

import argparse
import asyncio
import logging
from pathlib import Path

import sqlalchemy as sa

from app.db import pg
from app.db.mongo import close_mongo, init_mongo
from app.enums import Role


async def _import_rubric(path: Path, activate: bool) -> None:
    from app.services.rubric import import_rubric

    async with pg.session_scope() as session:
        rubric = await import_rubric(session, path, activate=activate)
        print(f"imported rubric {rubric.code} (active={rubric.is_active})")


async def _add_user(args: argparse.Namespace) -> None:
    """There is no API for provisioning people, so staff are added here (see README)."""
    from app.models import Program, RoleAssignment, School, User

    async with pg.session_scope() as session:
        user = await session.scalar(sa.select(User).where(sa.func.lower(User.email) == args.email.lower()))
        if user is None:
            user = User(email=args.email, name=args.name, title=args.title)
            session.add(user)
            await session.flush()
        school_id = program_id = None
        if args.school:
            school_id = await session.scalar(sa.select(School.id).where(School.slug == args.school))
            if school_id is None:
                raise SystemExit(f"no School with slug {args.school!r}")
        if args.program:
            program_id = await session.scalar(sa.select(Program.id).where(Program.slug == args.program))
            if program_id is None:
                raise SystemExit(f"no program with slug {args.program!r}")
        role = Role(args.role)
        already = await session.scalar(
            sa.select(RoleAssignment.id).where(
                RoleAssignment.user_id == user.id,
                RoleAssignment.role == role,
                RoleAssignment.school_id == school_id,
                RoleAssignment.program_id == program_id,
            )
        )
        if already is None:
            session.add(
                RoleAssignment(user_id=user.id, role=role, school_id=school_id, program_id=program_id)
            )
        scope = args.school or args.program
        print(f"{args.email}: {role.value}" + (f" scoped to {scope}" if scope else ""))


async def _seed() -> None:
    from seed.demo import run

    await run()


async def _main(args: argparse.Namespace) -> None:
    await init_mongo()
    try:
        if args.command == "import-rubric":
            await _import_rubric(Path(args.path), not args.no_activate)
        elif args.command == "seed":
            await _seed()
        elif args.command == "add-user":
            await _add_user(args)
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
    person = sub.add_parser("add-user", help="create a user, or give an existing one another role")
    person.add_argument("email")
    person.add_argument("name")
    person.add_argument("--role", required=True, choices=[r.value for r in Role])
    person.add_argument("--title", default=None)
    person.add_argument("--school", default=None, help="School slug, for school_admin")
    person.add_argument("--program", default=None, help="program slug, for program_head")

    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
