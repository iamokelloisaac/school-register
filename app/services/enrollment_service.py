"""
app/services/enrollment_service.py
==================================

Orchestration for enrollment operations: join a program, leave a
program, list one's enrollments.

This is the layer where the business decisions we deferred live. The
query layer just reads and writes rows; the service layer decides what
those rows *mean* and what should happen when the rules aren't met.

## Decisions made here (changeable, but named)

1. **A student can enroll in any active program.** No prerequisites, no
   quotas, no waiting lists. If the program exists and isn't archived,
   enrollment succeeds.

2. **Enrolling twice is a `Conflict`.** Not a silent no-op. The client
   wanted to know "am I enrolled?" — the answer is "you already were,"
   and that's information worth returning as a distinct response from
   "just enrolled."

3. **Leaving is always allowed.** A student can drop any program at any
   time, no admin approval needed. This matches the "lean" scope we
   settled on.

4. **Leaving a program you weren't in is a `NotFound`.** This is
   slightly stricter than "idempotent delete," and it's a choice: if
   you POST a delete for something that isn't there, you made a
   mistake, and a 404 is more informative than a 204. If we later
   decide leaving should be fully idempotent, we'd relax this to a
   204 unconditionally.

5. **Enrolling in an archived program is rejected.** The public catalog
   only shows active programs, so a client shouldn't be able to enroll
   in one it never saw. If it does — via a stale cached page, a
   bookmark, or a direct API call — we reject with a `Conflict`.
   Archiving is the admin's way of saying "don't let new students in,"
   and this is where that instruction is honored.

6. **Archived programs stay visible in a student's existing
   enrollments.** See the note in `list_enrolled_programs`. Enrolling
   *new* students is blocked; *existing* enrollments are preserved.

These are the kinds of rules that live in a service layer rather than
a query layer. The query layer is a mechanism; the service layer is a
policy. Keeping them separate means the policy can change without
touching SQL, and vice versa.

## The transaction story

Each public function is a single unit of work. psycopg's implicit
transaction (see `deps.get_db`) wraps it: on clean return, commit; on
exception, rollback. We don't manage transactions explicitly here
because nothing in this service needs nested or partial scoping.

If we ever needed to do "insert enrollment AND update a counter AND
send an event" as an atomic unit with a checkpoint in the middle, that
would be the moment to introduce an explicit `async with conn.transaction():`.
For now, the default is right.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import AsyncConnection
from psycopg import errors as pg_errors

from app.core.errors import Conflict, NotFound
from app.db.queries import enrollments as enrollments_q
from app.db.queries import programs as programs_q

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enroll
# ---------------------------------------------------------------------------


async def enroll(
    conn: AsyncConnection,
    *,
    student_id: int,
    program_id: int,
) -> dict[str, Any]:
    """
    Enroll a student into a program.

    Steps:

      1. Confirm the program exists and is active. If it doesn't exist,
         raise `NotFound`. If it exists but is archived, raise
         `Conflict` — the program is real, but not currently enrollable.
      2. Attempt the insert. If the composite PK constraint fires, the
         student is already enrolled; raise `Conflict`.

    ## Why we check the program exists and is active *before* inserting

    Not for the program-existence check itself — that would be a race
    condition and the FK constraint would catch it anyway. It's for the
    *archived* check. There's no DB constraint that says "you can't
    enroll into an inactive program" (a CHECK constraint can't reference
    another table), so this rule has to be enforced here. Once we're
    already querying the program, checking existence in the same call is
    free.

    This means there's a race: an admin could archive the program
    between our check and our insert. That's fine — the worst outcome
    is a student enrolled in a program archived a millisecond later,
    which is indistinguishable from a student enrolled a millisecond
    before it was archived. Both are historical enrollment, both are
    harmless.

    ## Why the UniqueViolation is translated here

    Because it means "already enrolled" *in this context*. The same
    exception on a different insert (say, a future "student can only
    have one primary program" constraint) would mean something else.
    Translation happens at the layer that knows what the constraint
    means.

    Returns:
        The created enrollment row.

    Raises:
        NotFound: if no program exists with that id.
        Conflict: if the program is archived, or the student is already
            enrolled.
    """
    program = await programs_q.get_program_by_id(conn, program_id)

    if program is None:
        raise NotFound(
            code="program_not_found",
            message="No such program.",
        )

    if not program["is_active"]:
        logger.info(
            "enroll rejected: program %s is inactive (student=%s)",
            program_id,
            student_id,
        )
        raise Conflict(
            code="program_inactive",
            message="This program is no longer accepting enrollments.",
        )

    try:
        enrollment = await enrollments_q.enroll(
            conn,
            student_id=student_id,
            program_id=program_id,
        )
    except pg_errors.UniqueViolation as exc:
        logger.info(
            "enroll rejected: already enrolled (student=%s, program=%s)",
            student_id,
            program_id,
        )
        raise Conflict(
            code="already_enrolled",
            message="You are already enrolled in this program.",
        ) from exc

    logger.info("enrolled: student=%s program=%s", student_id, program_id)
    return enrollment


# ---------------------------------------------------------------------------
# Leave
# ---------------------------------------------------------------------------


async def leave(
    conn: AsyncConnection,
    *,
    student_id: int,
    program_id: int,
) -> None:
    """
    Remove a student from a program.

    Steps:

      1. Attempt the delete.
      2. If no row was removed, the student wasn't enrolled — raise
         `NotFound`.

    ## Why we don't check the program exists first

    If the student wasn't enrolled, the delete removes nothing, and we
    can't distinguish "program doesn't exist" from "program exists but
    you weren't in it." Do we care? No — from the student's perspective,
    either way the enrollment they tried to leave isn't there. The
    generic "not enrolled" error is honest about that ambiguity.

    If we wanted a more specific error ("this program doesn't exist" vs
    "you weren't enrolled in it"), we'd add a program lookup before the
    delete. That's a UX choice, not a correctness one. We're going with
    the simpler path and a single generic error.

    ## Why `NotFound` and not idempotent success

    Because the caller asked to remove a specific enrollment, and it
    wasn't there. For an admin-facing "DELETE /programs/X/students/Y"
    endpoint, a 404 makes sense: something the caller believes exists
    doesn't. If we later decide leave should be idempotent from the
    student's point of view, the change is trivial and this docstring
    should be updated to explain why we changed it.

    Raises:
        NotFound: if the student wasn't enrolled in that program.
    """
    removed = await enrollments_q.leave(
        conn,
        student_id=student_id,
        program_id=program_id,
    )

    if not removed:
        logger.info(
            "leave rejected: not enrolled (student=%s, program=%s)",
            student_id,
            program_id,
        )
        raise NotFound(
            code="not_enrolled",
            message="You are not enrolled in this program.",
        )

    logger.info("left program: student=%s program=%s", student_id, program_id)


# ---------------------------------------------------------------------------
# List — my enrollments
# ---------------------------------------------------------------------------


async def list_my_programs(
    conn: AsyncConnection,
    *,
    student_id: int,
) -> list[dict[str, Any]]:
    """
    Return every program the given student is enrolled in.

    This is a straight pass-through to the query layer. It exists as a
    service function for two reasons:

      1. Consistency: routes always go through the service layer, never
         directly to queries. That rule makes it easy to add a policy
         later ("students can't see archived programs in their list")
         without hunting for direct query calls.

      2. Future-proofing: if we later want to add a "decorate with
         current attendance percentage" or "attach the assigned teacher"
         enrichment, this is where it goes. The route won't need to
         change.

    Right now the function has no logic beyond delegation. That's fine —
    a thin service layer is still a service layer, and the indirection
    pays for itself the first time a rule changes.
    """
    return await enrollments_q.list_enrolled_programs(conn, student_id=student_id)