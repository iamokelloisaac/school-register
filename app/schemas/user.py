"""
app/schemas/user.py
===================

Pydantic shapes for the `users` table, as seen from the API.

This module is the public contract for "what a user looks like" on the
wire. It's deliberately separate from the DB row shape (which lives in
`db/queries/users.py` as a plain dict), because the two aren't the same
thing and shouldn't be forced to be.

Examples of how they differ:

  - The DB row sometimes includes `hashed_password`; the API shape never
    does.
  - The DB row uses whatever the `_USER_COLUMNS` string selects; the API
    shape is a curated subset in a specific order.
  - If we later rename a DB column, the API shape can stay stable and just
    map at the boundary.

Keeping them separate also means the OpenAPI schema is generated from
these models — so the docs describe exactly what clients get, not the raw
table layout.

## A quick Pydantic reminder

  - `BaseModel` + type annotations = validation + serialization + schema.
  - `model_config = ConfigDict(from_attributes=True)` lets a model be
    built from an object with attributes (not just a dict). Not needed
    here, since our rows are dicts, but you'll see it in ORM-based
    codebases.
  - `Field(...)` adds descriptions and examples that show up in `/docs`.

We use plain dicts from the query layer, and Pydantic can validate them
directly: `UserRead.model_validate(row)`. Because the dict keys match the
field names, no aliasing is needed.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# ===========================================================================
# Role enum
# ===========================================================================
#
# The DB has a `user_role` enum with values 'student' and 'admin'. We
# mirror it as a Python `str`-based Enum so that:
#
#   1. Invalid role values are caught by Pydantic before they reach the DB.
#   2. The OpenAPI schema lists the allowed values, so Swagger shows a
#      dropdown rather than a free-text field.
#   3. Code that checks `user.role == UserRole.admin` is checked by the
#      type checker and by grep — a stringly-typed `"admin"` is neither.
#
# Inheriting from `str` (not just `Enum`) means the values serialize as
# plain strings in JSON. Without it, Pydantic would emit `"UserRole.admin"`
# in some contexts. This is a small but important detail.


class UserRole(str, Enum):
    student = "student"
    admin = "admin"


# ===========================================================================
# Read shape
# ===========================================================================


class UserRead(BaseModel):
    """
    A user as returned by the API.

    Note what is *absent*: `hashed_password`. This model is the response
    shape for `/me`, admin listings, and anywhere a user is embedded in
    another resource. Because the field simply doesn't exist here, it's
    impossible to accidentally serialize the hash — the safety comes from
    the shape, not from remembering to exclude it.

    `is_active` is included so clients can render "suspended" state.
    `created_at` and `updated_at` are included for audit-style display
    and for sorting client-side.
    """

    id: int = Field(..., description="Unique user identifier.")
    email: str = Field(..., description="Email address on file.")
    full_name: str = Field(..., description="Display name.")
    age: int = Field(..., description="Age in years.", ge=0, le=150)
    role: UserRole = Field(..., description="Authorization role.")
    is_active: bool = Field(
        ...,
        description="False if the account has been suspended by an admin.",
    )
    created_at: datetime = Field(..., description="When the account was created (UTC).")
    updated_at: datetime = Field(
        ...,
        description="Last time the account was modified (UTC).",
    )


# ===========================================================================
# Update shape
# ===========================================================================
#
# Updates are partial — every field is optional, and only the fields
# present in the request body are changed. That's what makes PATCH
# different from PUT: PATCH means "modify these fields," PUT means
# "replace the resource with this."
#
# There is no `UpdateUser` for changing email or role yet. Those are
# privileged operations (email is identity, role is authorization), and
# they'll live on admin-only endpoints with their own shapes when we build
# them. The user's self-service update here is limited to the fields a
# user can reasonably change about themselves.


class UserUpdate(BaseModel):
    """
    Fields a user can change about their own profile.

    All optional. Only fields present in the request are updated.

    Excluded deliberately: `email` (identity change — needs verification),
    `role` (privilege escalation), `is_active` (suspension is admin-only),
    `age` (would need its own verification flow if we were strict about
    it — for now, leave it admin-controlled via a future endpoint, or
    allow it here if the business ever decides so).
    """

    full_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="New display name.",
    )