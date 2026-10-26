"""
app/schemas/enrollment.py
=========================

Pydantic shapes for the `enrollments` join table.

An enrollment is a link between a student and a program. The table has
no surrogate id — the composite primary key `(student_id, program_id)`
is the identity. That design choice shows up here: the schemas return
the pair, not a single `id`.

## Two shapes, two use cases

  - `EnrollmentRead` — the raw join row. Useful when a client already
    knows the program and just wants to confirm the link plus its
    timestamp.

  - `EnrolledProgram` — a *joined* view: the program's full details,
    plus the enrollment timestamp. This is what a student's "my
    programs" list actually returns, because the client wants to see
    the program name and code, not just its id.

The two coexist on purpose. The raw shape is the honest representation
of the table; the joined shape is what the UI needs. Rather than force
one endpoint to return the other's shape, we let each endpoint pick.

## Why not embed `EnrollmentRead` inside `EnrolledProgram`?

Because JSON doesn't need nesting where a flat structure will do. A
client reading a list of `EnrolledProgram` wants `{id, name, code, ..., enrolled_at}`,
not `{program: {...}, enrollment: {...}}`. Flattening at the schema
level keeps the client code simpler. This is a design preference, not a
rule — some APIs nest for clarity, and that's fine too.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class EnrollmentRead(BaseModel):
    """
    A raw enrollment row.

    Useful for debugging, for an admin endpoint that returns "who is
    enrolled in program X" with minimal data, and for future audit
    features. Most student-facing endpoints will use `EnrolledProgram`
    instead, because it carries the program details a UI needs.
    """

    student_id: int = Field(..., description="Student's user id.")
    program_id: int = Field(..., description="Program's id.")
    enrolled_at: datetime = Field(..., description="When the enrollment was created (UTC).")


class EnrolledProgram(BaseModel):
    """
    A program the current user is enrolled in.

    This is the shape of each item in a student's "my programs" list.
    It's a flattened join between the `programs` table and the
    `enrollments` join row, keeping only the fields a client needs to
    render the row and to know when it was created.

    Fields deliberately omitted from the join:

      - `created_by` — internal audit data, irrelevant to a student.
      - `updated_at` — program-level, not enrollment-level; the
        enrollment-specific timestamp is `enrolled_at`.
      - `is_active` — a student viewing their own enrollments probably
        shouldn't be shown "archived" state; if a program is archived,
        the enrollment is effectively historical. If we later want to
        show this, we add the field back and the schema grows.

    Keeping response shapes minimal is a small discipline that pays off
    over time: fewer fields to document, fewer fields to keep in sync,
    fewer things for clients to accidentally depend on.
    """

    id: int = Field(..., description="Program id.")
    name: str = Field(..., description="Program name.")
    code: str = Field(..., description="Program code.")
    description: str | None = Field(
        default=None,
        description="Program description, if any.",
    )
    enrolled_at: datetime = Field(
        ...,
        description="When the current user enrolled in this program (UTC).",
    )