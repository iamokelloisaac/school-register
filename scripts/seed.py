"""
scripts/seed.py
===============

Bootstrap script: create the initial admin, ten students, ten programs,
and a realistic spread of enrollments.

Run once, after migrations, on a fresh database. Idempotent — every
insert checks existence first, so re-running is safe. To start truly
fresh: `python -m scripts.reset_db --yes` then `python -m scripts.seed`.

## What gets created

    1 admin        admin@school.local       / changeme123
    10 students    student1@example.com ... / student123
    10 programs    CS, MATH, PHYS, CHEM, BIO, ENG, HIST, GEO, ART, MUS
    ~30 enrollments — each student in 2-4 programs, variety of overlaps

## Why these numbers

Ten is enough to exercise pagination (`?limit=5` splits into two pages,
`?limit=20` fits one page), realistic enough to eyeball ordering, and
small enough to keep seed time under a second. Realistic enrollments
mean real reports are non-trivial — attendance percentages, "who's in
this program" rosters, and "my programs" lists all return meaningful
data instead of empty shells.

## How to run

    python -m scripts.seed

The `-m` flag matters. Running `scripts/seed.py` directly would break
the `from app...` imports because `sys.path[0]` would be `scripts/`
instead of the project root.

## What this script does NOT do

  - It does not run migrations. Apply them first with `make migrate`
    (or the psql loop). The script assumes the schema exists.
  - It does not reset anything. For a clean slate, run reset_db first.
  - It does not seed attendance or marks. Neither exists yet.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.core import security
from app.core.config import settings
from app.core.logging import configure_logging
from app.db.pool import close_pool, get_pool, open_pool
from app.db.queries import enrollments as enrollments_q
from app.db.queries import programs as programs_q
from app.db.queries import users as users_q

logger = logging.getLogger("scripts.seed")


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
#
# Names, ages, and emails are deliberately spread across a realistic
# range — different ages, different name lengths, different local parts
# — so that ordering, sorting, and display bugs have somewhere to
# surface. If everything were "Alice, 22, alice@example.com" the data
# would look uniform in ways production never does.

ADMIN = {
    "email": "admin@school.local",
    "password": "changeme123",
    "full_name": "System Administrator",
    "age": 34,
}

STUDENTS = [
    {"email": "student1@example.com",  "full_name": "Alice Ngugi",       "age": 19},
    {"email": "student2@example.com",  "full_name": "Brian Otieno",      "age": 20},
    {"email": "student3@example.com",  "full_name": "Chloe Wanjiru",     "age": 18},
    {"email": "student4@example.com",  "full_name": "David Kimani",      "age": 21},
    {"email": "student5@example.com",  "full_name": "Esther Mwangi",     "age": 22},
    {"email": "student6@example.com",  "full_name": "Frank Achieng",     "age": 20},
    {"email": "student7@example.com",  "full_name": "Grace Mutua",       "age": 19},
    {"email": "student8@example.com",  "full_name": "Hassan Abdalla",    "age": 23},
    {"email": "student9@example.com",  "full_name": "Irene Chebet",      "age": 21},
    {"email": "student10@example.com", "full_name": "James Mburu",       "age": 18},
]
STUDENT_PASSWORD = "student123"

PROGRAMS = [
    {"name": "Computer Science", "code": "CS",   "description": "Algorithms, data structures, systems, and software engineering."},
    {"name": "Mathematics",      "code": "MATH", "description": "Calculus, linear algebra, discrete math, and probability."},
    {"name": "Physics",          "code": "PHYS", "description": "Mechanics, electromagnetism, thermodynamics, and quantum foundations."},
    {"name": "Chemistry",        "code": "CHEM", "description": "Organic, inorganic, physical, and analytical chemistry."},
    {"name": "Biology",          "code": "BIO",  "description": "Cell biology, genetics, ecology, and evolution."},
    {"name": "English",          "code": "ENG",  "description": "Literature, composition, and linguistics."},
    {"name": "History",          "code": "HIST", "description": "World history, regional history, and historiography."},
    {"name": "Geography",        "code": "GEO",  "description": "Physical geography, human geography, and cartography."},
    {"name": "Art",              "code": "ART",  "description": "Drawing, painting, sculpture, and art history."},
    {"name": "Music",            "code": "MUS",  "description": "Theory, composition, performance, and music history."},
]

# Enrollment plan. Each student is in 2-4 programs, with a lot of
# overlap in the popular ones (CS, MATH) and less in the niche ones
# (ART, MUS). Realistic distributions mean realistic queries later —
# "which program has the most students?" has an interesting answer
# instead of everything tied at 3.
#
# The keys are student emails; the values are lists of program codes.
# This shape is easier to read and edit than a matrix.

ENROLLMENT_PLAN: dict[str, list[str]] = {
    "student1@example.com":  ["CS", "MATH", "PHYS"],
    "student2@example.com":  ["CS", "MATH"],
    "student3@example.com":  ["CS", "ENG"],
    "student4@example.com":  ["CS", "MATH", "PHYS", "CHEM"],
    "student5@example.com":  ["BIO", "CHEM"],
    "student6@example.com":  ["MATH", "HIST", "GEO"],
    "student7@example.com":  ["ENG", "HIST"],
    "student8@example.com":  ["CS", "MATH", "BIO"],
    "student9@example.com":  ["ART", "MUS"],
    "student10@example.com": ["CS", "GEO"],
}


# ---------------------------------------------------------------------------
# Seeding functions
# ---------------------------------------------------------------------------


async def seed_admin() -> int:
    """
    Create the admin user if missing, return their id.

    Idempotent — if `admin@school.local` already exists, we return
    their id without touching the row. The same admin is the
    `created_by` for every seeded program, so the script can't run
    without this.
    """
    async with get_pool().connection() as conn:
        existing = await users_q.get_user_by_email(conn, ADMIN["email"])
        if existing is not None:
            logger.info("admin exists: %s (id=%s)", ADMIN["email"], existing["id"])
            return existing["id"]

        hashed = security.hash_password(ADMIN["password"])
        admin = await users_q.create_user(
            conn,
            email=ADMIN["email"],
            hashed_password=hashed,
            full_name=ADMIN["full_name"],
            age=ADMIN["age"],
            role="admin",
        )
        logger.info("created admin: %s (id=%s)", ADMIN["email"], admin["id"])
        return admin["id"]


async def seed_students() -> dict[str, int]:
    """
    Create the ten students if missing. Returns a map of email → user id.

    Returning the map (rather than a list of ids) makes the enrollment
    step readable — `students_by_email["student1@example.com"]` reads
    better than `students[0]`, and it can't be fooled by a reordered
    list.
    """
    students_by_email: dict[str, int] = {}
    hashed = security.hash_password(STUDENT_PASSWORD)

    async with get_pool().connection() as conn:
        for spec in STUDENTS:
            existing = await users_q.get_user_by_email(conn, spec["email"])
            if existing is not None:
                students_by_email[spec["email"]] = existing["id"]
                logger.info("student exists: %s (id=%s)", spec["email"], existing["id"])
                continue

            user = await users_q.create_user(
                conn,
                email=spec["email"],
                hashed_password=hashed,
                full_name=spec["full_name"],
                age=spec["age"],
                role="student",
            )
            students_by_email[spec["email"]] = user["id"]
            logger.info("created student: %s (id=%s)", spec["email"], user["id"])

    return students_by_email


async def seed_programs(admin_id: int) -> dict[str, int]:
    """
    Create the ten programs if missing. Returns a map of code → program id.
    """
    programs_by_code: dict[str, int] = {}

    async with get_pool().connection() as conn:
        for spec in PROGRAMS:
            existing = await programs_q.get_program_by_code(conn, spec["code"])
            if existing is not None:
                programs_by_code[spec["code"]] = existing["id"]
                logger.info("program exists: %s (id=%s)", spec["code"], existing["id"])
                continue

            program = await programs_q.create_program(
                conn,
                name=spec["name"],
                code=spec["code"],
                description=spec["description"],
                created_by=admin_id,
            )
            programs_by_code[spec["code"]] = program["id"]
            logger.info("created program: %s (id=%s)", spec["code"], program["id"])

    return programs_by_code


async def seed_enrollments(
    students_by_email: dict[str, int],
    programs_by_code: dict[str, int],
) -> None:
    """
    Create the enrollment plan.

    Idempotent at the row level. Duplicate enrollments raise
    `UniqueViolation`, which we catch and skip. But there's a subtlety:
    in Postgres, a constraint violation *aborts the current
    transaction* — every subsequent statement on the same connection
    fails with `InFailedSqlTransaction` until the transaction is
    rolled back or recovered.

    So we wrap each insert in its own `conn.transaction()` block. When
    called inside an outer transaction, psycopg 3 treats this as a
    *savepoint*: a failure inside rolls back only to the savepoint, and
    the outer transaction continues in a healthy state. Without the
    savepoint, the first "already enrolled" catch would poison the rest
    of the loop.

    This is the correct general pattern for "insert many, skip
    duplicates" in Postgres. Pre-checking with `is_enrolled` would also
    work but costs an extra round trip per row and is racier.
    """
    from psycopg import errors as pg_errors

    async with get_pool().connection() as conn:
        for email, program_codes in ENROLLMENT_PLAN.items():
            student_id = students_by_email[email]
            for code in program_codes:
                program_id = programs_by_code[code]
                try:
                    # The `async with conn.transaction():` here creates
                    # a savepoint. On success, the savepoint is
                    # released (the outer transaction still commits at
                    # the end). On UniqueViolation, only the savepoint
                    # is rolled back — the outer transaction stays
                    # healthy and the next iteration can proceed.
                    async with conn.transaction():
                        await enrollments_q.enroll(
                            conn,
                            student_id=student_id,
                            program_id=program_id,
                        )
                    logger.info("enrolled: %s -> %s", email, code)
                except pg_errors.UniqueViolation:
                    logger.info("already enrolled: %s -> %s", email, code)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def main() -> None:
    """
    Full seed run: logging, pool, admin, students, programs, enrollments,
    pool close.

    Order matters: admin first (so programs have a `created_by`),
    students next (so enrollments have a `student_id`), programs after
    that, enrollments last. Each step returns the ids the next step
    needs.
    """
    configure_logging()
    logger.info("seeding database: %s", settings.DATABASE_URL.split("@")[-1])

    await open_pool()
    try:
        admin_id = await seed_admin()
        students = await seed_students()
        programs = await seed_programs(admin_id)
        await seed_enrollments(students, programs)
        logger.info("seed complete")
    finally:
        await close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        logging.getLogger("scripts.seed").exception("seed failed: %s", exc)
        sys.exit(1)