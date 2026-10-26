"""
app/schemas/auth.py
===================

Request and response shapes for the authentication endpoints.

"Auth" here means the three flows that establish or tear down a session:

  - register — create an account
  - login    — verify credentials and mint a session
  - logout   — revoke the current session

Everything in this file is public API surface. Anything not present here
is not something a client can send or receive on these endpoints. In
particular: no shape in this file exposes `hashed_password`, and no shape
accepts `role`. That's the point of having dedicated auth schemas rather
than reusing `UserRead` / `UserUpdate` for everything.

## On input validation vs business rules

Pydantic validates *shape*: types, ranges, patterns, lengths. It does not
validate *state*: "is this email already taken?", "does this user's
password match?" — those are business rules, enforced in the service
layer against the DB. Keep the split clean:

  - Schema-level: "the email must look like an email."
  - Service-level: "no account with this email exists yet."

If you find yourself wanting to query the DB from a validator, that's
the signal you've crossed the line. Don't.
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field

from app.schemas.user import UserRead


# ===========================================================================
# Register
# ===========================================================================


class RegisterRequest(BaseModel):
    """
    Body of `POST /api/v1/auth/register`.

    Fields are the minimum needed to create an account. Note the absence
    of `role` — every self-registered account is a student. Admin promotion
    is a separate, admin-only operation. This is a small but deliberate
    security decision: the client does not get to choose its own
    authorization level.

    Password rules:

      - Minimum length 8. This is intentionally mild. Modern guidance
        (NIST SP 800-63B) favors length over complexity: a long
        passphrase beats a short "P@ssw0rd!". We don't impose character
        class rules because they push users toward predictable patterns
        ("Password1!") and reduce real-world security.
      - Maximum length 128. Argon2 can handle long inputs, but this caps
        the work an unauthenticated client can force us to do. Without
        it, a client could send a 1 GB password and pin a worker.
      - No normalization. We don't trim, lowercase, or otherwise
        transform the password. What the user typed is what gets hashed.
        Trimming passwords silently is a classic source of "I know I
        typed it right" bugs.

    Email rules:

      - `EmailStr` is a Pydantic type that validates basic email shape.
        It does not verify the domain has an MX record, nor that the
        address exists. Full verification would require sending mail,
        which is out of scope.
      - Case-insensitive uniqueness is enforced by the DB (`CITEXT`
        column), not here. `Alice@X.com` and `alice@x.com` are the same
        account; the schema accepts either.
    """

    email: EmailStr = Field(
        ...,
        description="Email address. Case-insensitive; must be unique.",
        examples=["alice@example.com"],
    )
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Plaintext password. Never logged, never stored as-is.",
    )
    full_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="The user's display name.",
        examples=["Alice Doe"],
    )
    age: int = Field(
        ...,
        ge=0,
        le=150,
        description="Age in years.",
        examples=[22],
    )


# ===========================================================================
# Login
# ===========================================================================


class LoginRequest(BaseModel):
    """
    Body of `POST /api/v1/auth/login`.

    Fields are minimal on purpose: email and password, nothing else.
    Everything else a session needs (user agent, IP) comes from the
    request context, not from the client body. A client can lie about
    those anyway, so they're never trusted input.

    No `EmailStr` here — validation is a waste on this endpoint. If the
    email is malformed we'll simply fail to find a user, which yields the
    same generic "invalid credentials" response as a wrong password. Using
    `EmailStr` would let a client distinguish "malformed email" (422)
    from "valid email, unknown" (401) — a tiny user-enumeration signal
    we don't want to give away for free.
    """

    email: str = Field(
        ...,
        min_length=1,
        max_length=320,  # generous upper bound; addresses don't get longer
        description="Email address as entered by the user.",
        examples=["alice@example.com"],
    )
    password: str = Field(
        ...,
        min_length=1,  # a login with an empty password is obviously wrong
        max_length=128,
        description="Plaintext password.",
    )


# ===========================================================================
# Login response
# ===========================================================================


class LoginResponse(BaseModel):
    """
    Body of a successful `POST /api/v1/auth/login`.

    Contains two things:

      - `token` — the raw session token. The client must keep it
        somewhere it can attach to subsequent requests. Two transports
        are supported:

          * the server also sets an HttpOnly cookie containing the same
            token, so a browser client doesn't need to do anything with
            this field
          * a non-browser client (CLI, mobile, another service) reads it
            from here and sends it as `Authorization: Bearer <token>`

      - `user` — the authenticated user's public shape. Saves the client
        a follow-up call to `/me` after login, which is the common case
        for the UI.

    Why return the token *and* set a cookie? Because the two transports
    serve different clients. Cookies are ergonomic for browsers but
    useless to a CLI; the JSON field is ergonomic for a CLI but
    inconvenient in a browser (XSS-risky if stored in JS-accessible
    storage). Providing both lets each client use what fits.

    Is exposing the token in the JSON body a security issue? Only if a
    browser client reads it and stashes it somewhere unsafe (localStorage,
    a non-HttpOnly cookie, etc.). The server's job is to provide the
    option; the client's job is to use it well. Our own future frontend
    will ignore this field and rely on the cookie.
    """

    token: str = Field(
        ...,
        description=(
            "Raw session token. Send as `Authorization: Bearer <token>` "
            "on subsequent requests, or rely on the cookie set by the "
            "server."
        ),
    )
    user: UserRead = Field(
        ...,
        description="The authenticated user.",
    )


# ===========================================================================
# Register response
# ===========================================================================


class RegisterResponse(BaseModel):
    """
    Body of a successful `POST /api/v1/auth/register`.

    Registration does not automatically log the user in — the client is
    expected to call `login` next. This is a deliberate choice:

      - It keeps the register endpoint's contract narrow: "create an
        account." No side effects beyond that.
      - It forces the register flow to exercise the login path too, which
        means a user whose login would fail (e.g. because of a later
        account-state rule) finds out immediately, not after being told
        they're "logged in" from register.

    The response therefore just echoes the created user, so the client can
    display "Welcome, Alice" on a signup confirmation page without a
    second round trip.
    """

    user: UserRead = Field(
        ...,
        description="The newly created user.",
    )