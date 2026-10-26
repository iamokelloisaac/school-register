"""
tests/services/test_auth_service.py
===================================

Tests for the auth service layer — `register`, `login`, `logout`, and
`logout_all`.

## Why test the service layer separately

The API tests already exercise these functions through HTTP, but the
service layer has logic that isn't obvious from the outside:

  - **Registration always forces `role="student"`**, even if a caller
    somehow passed a different role. This is a security decision that
    lives in the service, not the schema.
  - **`login` returns a tuple `(raw_token, user_without_hash)`**, and
    the user in the tuple must not carry the hash even though the
    fetched row did.
  - **`login` collapses three failure modes** (no user, wrong password,
    inactive user) into the same `Unauthorized` exception with the same
    code. Verifying that all three produce identical failures is a
    service-level concern.
  - **`register` translates `UniqueViolation` into `Conflict`** with a
    specific code. The translation is a service-layer contract.

Testing these directly lets us assert on the *domain* exceptions (not
HTTP status codes) and on the return shapes. It's the layer where a
future refactor is most likely to introduce subtle bugs, and where a
test suite pays off most.

## What we don't test here

  - HTTP behavior — that's `tests/api/test_auth.py`.
  - SQL correctness — that's `tests/db/test_queries.py`.
  - Pydantic validation — handled by the schema and covered via API.

The focus is: does the service do the *right thing* with the pieces it
composes, in the right order, with the right outcomes?
"""

from __future__ import annotations

import pytest
from psycopg import AsyncConnection

from app.core import security
from app.core.errors import Conflict, Unauthorized
from app.core.config import settings
from app.services import auth_service


# ===========================================================================
# register
# ===========================================================================


async def test_register_creates_user_with_hashed_password(
    db_conn: AsyncConnection,
) -> None:
    """
    Registration hashes the password before storage.

    We verify by fetching the row *with* the hash and checking it
    against the plaintext. If the service ever stored the plaintext
    (a catastrophic bug), this test would fail because `verify_password`
    would find a mismatched hash — and the stored value wouldn't start
    with `$argon2id$`.
    """
    from app.db.queries import users as users_q

    user = await auth_service.register(
        db_conn,
        email="svc-reg@example.com",
        password="plaintext-password",
        full_name="Service Register",
        age=25,
    )

    # The returned user has no hash, as designed.
    assert "hashed_password" not in user
    assert user["email"] == "svc-reg@example.com"
    assert user["role"] == "student"

    # Now check the stored hash.
    row = await users_q.get_user_by_email(db_conn, "svc-reg@example.com")
    assert row is not None
    assert row["hashed_password"].startswith("$argon2")
    assert security.verify_password("plaintext-password", row["hashed_password"])


async def test_register_always_creates_student(
    db_conn: AsyncConnection,
) -> None:
    """
    Registration ignores any attempt to set the role.

    The service's `register` signature doesn't take a role parameter
    at all — the role is hardcoded to `"student"` inside. This test
    doesn't try to pass a role (it can't); it just verifies the
    hardcoding holds for a normal call. If someone later adds a
    `role=` parameter to `register`, they'd need to update this test
    and think about whether admin self-registration should be allowed.
    """
    user = await auth_service.register(
        db_conn,
        email="svc-role@example.com",
        password="hunter2hunter2",
        full_name="Role Check",
        age=25,
    )

    assert user["role"] == "student"


async def test_register_duplicate_email_raises_conflict(
    db_conn: AsyncConnection,
) -> None:
    """
    Duplicate email → `Conflict` with code `email_taken`.

    The service catches `UniqueViolation` from the query layer and
    re-raises as a domain exception. This is the translation the API
    handler relies on to produce a 409 instead of a 500.
    """
    await auth_service.register(
        db_conn,
        email="svc-dup@example.com",
        password="hunter2hunter2",
        full_name="First",
        age=25,
    )

    with pytest.raises(Conflict) as exc_info:
        await auth_service.register(
            db_conn,
            email="svc-dup@example.com",
            password="hunter2hunter2",
            full_name="Second",
            age=25,
        )

    assert exc_info.value.code == "email_taken"


async def test_register_duplicate_email_case_insensitive(
    db_conn: AsyncConnection,
) -> None:
    """
    The duplicate detection respects CITEXT semantics.

    `Svc-Dup-Case@example.com` and `svc-dup-case@example.com` are the
    same account. The check exists because it's the kind of thing that
    could silently break if the column type were ever changed to TEXT.
    """
    await auth_service.register(
        db_conn,
        email="svc-dup-case@example.com",
        password="hunter2hunter2",
        full_name="First",
        age=25,
    )

    with pytest.raises(Conflict):
        await auth_service.register(
            db_conn,
            email="Svc-Dup-Case@EXAMPLE.com",
            password="hunter2hunter2",
            full_name="Second",
            age=25,
        )


# ===========================================================================
# login
# ===========================================================================


async def test_login_returns_token_and_user(
    db_conn: AsyncConnection,
) -> None:
    """
    Successful login returns `(raw_token, user_dict)`.

    The user dict must not include the hash — even though
    `get_user_by_email` fetched it (to verify the password). The
    service strips it before returning.
    """
    await auth_service.register(
        db_conn,
        email="svc-login@example.com",
        password="correct-password",
        full_name="Login User",
        age=30,
    )

    raw_token, user = await auth_service.login(
        db_conn,
        email="svc-login@example.com",
        password="correct-password",
    )

    assert isinstance(raw_token, str) and len(raw_token) > 20
    assert user["email"] == "svc-login@example.com"
    assert "hashed_password" not in user


async def test_login_creates_session_row(
    db_conn: AsyncConnection,
) -> None:
    """
    Login creates a session row whose hash matches the returned token.

    This is the round-trip contract: whatever token comes out of
    `login`, hashing it produces the `token_hash` that was stored.
    Without this property, session lookup on the next request would
    fail. The test verifies both halves.
    """
    from app.db.queries import sessions as sessions_q

    await auth_service.register(
        db_conn,
        email="svc-login-row@example.com",
        password="correct-password",
        full_name="Login Row",
        age=30,
    )

    raw_token, user = await auth_service.login(
        db_conn,
        email="svc-login-row@example.com",
        password="correct-password",
    )

    expected_hash = security.hash_token(raw_token)
    row = await sessions_q.get_session_by_token_hash(db_conn, expected_hash)

    assert row is not None
    assert row["user_id"] == user["id"]
    assert row["user_agent"] is None  # no UA passed
    assert row["ip_address"] is None


async def test_login_wrong_password_raises_unauthorized(
    db_conn: AsyncConnection,
) -> None:
    """Wrong password → `Unauthorized` with code `invalid_credentials`."""
    await auth_service.register(
        db_conn,
        email="svc-badpw@example.com",
        password="correct-password",
        full_name="Bad PW",
        age=30,
    )

    with pytest.raises(Unauthorized) as exc_info:
        await auth_service.login(
            db_conn,
            email="svc-badpw@example.com",
            password="wrong-password",
        )

    assert exc_info.value.code == "invalid_credentials"


async def test_login_unknown_email_raises_unauthorized(
    db_conn: AsyncConnection,
) -> None:
    """Unknown email → `Unauthorized`, same code as wrong password."""
    with pytest.raises(Unauthorized) as exc_info:
        await auth_service.login(
            db_conn,
            email="svc-nobody@example.com",
            password="anything",
        )

    assert exc_info.value.code == "invalid_credentials"


async def test_login_failures_are_indistinguishable(
    db_conn: AsyncConnection,
) -> None:
    """
    Wrong password and unknown email produce identical failures.

    This is the anti-enumeration guarantee at the service layer. We
    call both failure paths and compare the exceptions' code and
    message. Any divergence — a different message, a different code —
    would leak "does this email exist?" to the caller.

    Same principle as the API test, but at the domain layer. The API
    test verifies the HTTP shape; this one verifies the exception
    shape. Both matter.
    """
    await auth_service.register(
        db_conn,
        email="svc-known@example.com",
        password="correct-password",
        full_name="Known",
        age=30,
    )

    with pytest.raises(Unauthorized) as wrong_pw:
        await auth_service.login(
            db_conn, email="svc-known@example.com", password="wrong"
        )

    with pytest.raises(Unauthorized) as no_user:
        await auth_service.login(
            db_conn, email="svc-unknown@example.com", password="whatever"
        )

    assert wrong_pw.value.code == no_user.value.code
    assert wrong_pw.value.message == no_user.value.message


async def test_login_inactive_user_raises_unauthorized(
    db_conn: AsyncConnection,
) -> None:
    """
    Inactive user → `Unauthorized`, same shape as a bad password.

    The user exists and the password is correct — but the account is
    suspended. The service rejects it with the same generic error so
    the caller can't distinguish "suspended" from "nonexistent."
    """
    from app.db.queries import users as users_q

    user = await auth_service.register(
        db_conn,
        email="svc-inactive@example.com",
        password="correct-password",
        full_name="Inactive",
        age=30,
    )

    await users_q.set_user_active(db_conn, user_id=user["id"], is_active=False)

    with pytest.raises(Unauthorized) as exc_info:
        await auth_service.login(
            db_conn,
            email="svc-inactive@example.com",
            password="correct-password",
        )

    assert exc_info.value.code == "invalid_credentials"


async def test_login_stores_user_agent_and_ip(
    db_conn: AsyncConnection,
) -> None:
    """
    Login records the client's user-agent and IP in the session row.

    These are metadata, not authentication — a client can lie about
    both. But the service is supposed to pass them through, and the
    test verifies the wiring.
    """
    from app.db.queries import sessions as sessions_q

    await auth_service.register(
        db_conn,
        email="svc-meta@example.com",
        password="correct-password",
        full_name="Meta",
        age=30,
    )

    raw_token, _ = await auth_service.login(
        db_conn,
        email="svc-meta@example.com",
        password="correct-password",
        user_agent="Mozilla/5.0 (test)",
        ip_address="192.0.2.1",
    )

    row = await sessions_q.get_session_by_token_hash(
        db_conn, security.hash_token(raw_token)
    )
    assert row is not None
    assert row["user_agent"] == "Mozilla/5.0 (test)"
    # `ip_address` comes back as an `ipaddress` object or string,
    # depending on the driver. Compare as a string to be safe.
    assert str(row["ip_address"]) == "192.0.2.1"


async def test_login_uses_configured_ttls(
    db_conn: AsyncConnection,
) -> None:
    """
    The session's expiries are set from settings, not hardcoded.

    We check that `expires_at - created_at` is roughly the idle TTL
    and `absolute_expires_at - created_at` is roughly the absolute
    TTL. "Roughly" because a fraction of a second elapses during the
    two DB calls — we allow a generous window.

    If someone hardcoded a TTL in the service, this test would fail
    when the settings changed.
    """
    from app.db.queries import sessions as sessions_q

    await auth_service.register(
        db_conn,
        email="svc-ttl@example.com",
        password="correct-password",
        full_name="TTL",
        age=30,
    )

    raw_token, _ = await auth_service.login(
        db_conn,
        email="svc-ttl@example.com",
        password="correct-password",
    )

    row = await sessions_q.get_session_by_token_hash(
        db_conn, security.hash_token(raw_token)
    )
    assert row is not None

    idle_delta = (row["expires_at"] - row["created_at"]).total_seconds()
    absolute_delta = (row["absolute_expires_at"] - row["created_at"]).total_seconds()

    # Allow 5 seconds of slack for the round-trip between INSERT and
    # the SELECT that reads it back.
    assert abs(idle_delta - settings.SESSION_IDLE_TTL_SECONDS) < 5
    assert abs(absolute_delta - settings.SESSION_ABSOLUTE_TTL_SECONDS) < 5


# ===========================================================================
# logout
# ===========================================================================


async def test_logout_deletes_session(
    db_conn: AsyncConnection,
) -> None:
    """
    Logout deletes the session row.

    We create a session (via login), grab its id, call logout, and
    verify the row is gone.
    """
    from app.db.queries import sessions as sessions_q

    await auth_service.register(
        db_conn,
        email="svc-logout@example.com",
        password="correct-password",
        full_name="Logout",
        age=30,
    )

    raw_token, _ = await auth_service.login(
        db_conn,
        email="svc-logout@example.com",
        password="correct-password",
    )

    row = await sessions_q.get_session_by_token_hash(
        db_conn, security.hash_token(raw_token)
    )
    assert row is not None
    session_id = row["id"]

    await auth_service.logout(db_conn, session_id=session_id)

    assert await sessions_q.get_session_by_token_hash(
        db_conn, security.hash_token(raw_token)
    ) is None


async def test_logout_idempotent(
    db_conn: AsyncConnection,
) -> None:
    """
    Logout twice doesn't raise.

    Deleting an already-deleted row is a no-op, and the service
    doesn't try to make it an error. A client calling logout twice
    (say, after a network retry) should not get a 500.
    """
    await auth_service.register(
        db_conn,
        email="svc-logout-idem@example.com",
        password="correct-password",
        full_name="Idem",
        age=30,
    )

    raw_token, _ = await auth_service.login(
        db_conn,
        email="svc-logout-idem@example.com",
        password="correct-password",
    )

    from app.db.queries import sessions as sessions_q

    row = await sessions_q.get_session_by_token_hash(
        db_conn, security.hash_token(raw_token)
    )
    assert row is not None

    await auth_service.logout(db_conn, session_id=row["id"])
    # Second call — no exception.
    await auth_service.logout(db_conn, session_id=row["id"])


# ===========================================================================
# logout_all
# ===========================================================================


async def test_logout_all_deletes_every_session(
    db_conn: AsyncConnection,
) -> None:
    """
    Logout-all deletes every session for the user.

    Two sessions created (two logins), then logout_all called once,
    then verify both are gone.
    """
    from app.db.queries import sessions as sessions_q

    user = await auth_service.register(
        db_conn,
        email="svc-logout-all@example.com",
        password="correct-password",
        full_name="All",
        age=30,
    )

    # Two logins = two sessions.
    await auth_service.login(
        db_conn,
        email="svc-logout-all@example.com",
        password="correct-password",
    )
    await auth_service.login(
        db_conn,
        email="svc-logout-all@example.com",
        password="correct-password",
    )

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = %(id)s",
            {"id": user["id"]},
        )
        assert (await cur.fetchone())["n"] == 2

    await auth_service.logout_all(db_conn, user_id=user["id"])

    async with db_conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = %(id)s",
            {"id": user["id"]},
        )
        assert (await cur.fetchone())["n"] == 0


async def test_logout_all_does_not_affect_other_users(
    db_conn: AsyncConnection,
) -> None:
    """
    Logout-all for one user doesn't touch another user's sessions.

    This is the identity-boundary check at the service layer. Alice's
    logout-all must leave Bob's sessions intact.
    """
    from app.db.queries import sessions as sessions_q

    alice = await auth_service.register(
        db_conn,
        email="svc-all-alice@example.com",
        password="correct-password",
        full_name="Alice",
        age=30,
    )
    bob = await auth_service.register(
        db_conn,
        email="svc-all-bob@example.com",
        password="correct-password",
        full_name="Bob",
        age=30,
    )

    alice_token, _ = await auth_service.login(
        db_conn, email="svc-all-alice@example.com", password="correct-password"
    )
    bob_token, _ = await auth_service.login(
        db_conn, email="svc-all-bob@example.com", password="correct-password"
    )

    await auth_service.logout_all(db_conn, user_id=alice["id"])

    # Alice's session gone.
    assert await sessions_q.get_session_by_token_hash(
        db_conn, security.hash_token(alice_token)
    ) is None

    # Bob's session survives.
    assert await sessions_q.get_session_by_token_hash(
        db_conn, security.hash_token(bob_token)
    ) is not None