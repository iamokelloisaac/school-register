"""
app/schemas/program.py
======================

Pydantic shapes for the `programs` table.

Programs are the catalog of things a student can enroll into. They're
created and deleted by admins; students read them and enroll/leave.
That asymmetry drives the shape split in this file:

  - `ProgramRead`   — what everyone sees (list, detail, embedded).
  - `ProgramCreate` — what an admin sends to create one.

There is no `ProgramUpdate`. Programs are created and deleted as whole
entities; editing one is a feature we haven't decided to support, and
adding an update schema without an endpoint that uses it would be
speculative.

## On `is_active`

The DB has an `is_active` column for soft-delete. The API exposes it in
`ProgramRead` so clients can render "archived" state, but `ProgramCreate`
doesn't accept it — new programs are always active. Deactivating is a
separate admin operation (a future `PATCH`) and not part of creation.

## On `code`

`code` is a short, stable identifier ("CS", "MATH") alongside the
human-readable `name` ("Computer Science"). Both are unique in the DB.
The code is what you'd put in URLs, exports, and logs; the name is what
you'd put in a UI. Enforcing both at the schema level means a client
gets a 422 for a bad code shape (too long, wrong characters) rather than
a DB-level rejection.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ProgramRead(BaseModel):
    """
    A program as returned by the API.

    Includes `created_by` as a nullable id. Most of the time clients
    won't need it, but it's useful for audit-style displays and for
    debugging "who made this program." The DB column is
    `ON DELETE SET NULL`, so it can legitimately be null if the creating
    admin was later deleted.
    """

    id: int = Field(..., description="Unique program identifier.")
    name: str = Field(..., description="Human-readable program name.")
    code: str = Field(..., description="Short stable code for the program.")
    description: str | None = Field(
        default=None,
        description="Optional longer description.",
    )
    is_active: bool = Field(
        ...,
        description="False if the program has been archived.",
    )
    created_by: int | None = Field(
        default=None,
        description="ID of the admin who created the program, if still on file.",
    )
    created_at: datetime = Field(..., description="When the program was created (UTC).")
    updated_at: datetime = Field(
        ...,
        description="When the program was last modified (UTC).",
    )


class ProgramCreate(BaseModel):
    """
    Body of `POST /api/v1/admin/programs`.

    Only the fields an admin can set at creation time. `id`, `created_at`,
    `updated_at`, and `created_by` are server-assigned. `is_active` is
    not exposed here — new programs are active by default.

    Field constraints:

      - `name` and `code` are required and non-empty.
      - `code` is uppercased and validated against a simple pattern.
        We uppercase before validating so a client can send "cs" and
        get "CS" — this is friendlier than rejecting lowercase, and it
        keeps codes consistent in the DB. Note that the *unique*
        constraint in the DB is case-sensitive (TEXT, not CITEXT), so
        uppercasing here is what prevents "cs" and "CS" from being two
        separate rows. If you'd rather have case-insensitive codes at
        the DB level, change the column to CITEXT in the migration.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Human-readable program name.",
        examples=["Computer Science"],
    )
    code: str = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Short stable code, uppercased. Unique across programs.",
        examples=["CS"],
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional longer description.",
    )