"""
app/core/security.py
====================

Password hashing and session-token primitives.

This module is deliberately the *bottom* of the dependency graph. It knows
nothing about HTTP, nothing about the database, nothing about FastAPI. It
takes strings and returns strings. That purity is what makes it easy to
reason about and easy to test in isolation — if you can't test a security
primitive without spinning up a web server, something is wrong with the
designs

Two distinct concerns live here, and they are kept separate on purpose:

  1. Password hashing — for user credentials. Slow, memory-hard (Argon2id).
  2. Session-token hashing — for opaque auth tokens. Fast (SHA-256).

They are *not* the same problem, so they do not use the same algorithm.
That distinction is the single most important idea in this file, and it's
worth understanding before you read a line of the code.

## Why passwords and tokens use different hashes

**Passwords** are low-entropy. A human picks "hunter2", "P@ssw0rd", or
their dog's name. An attacker who steals the hash database can try billions
of candidate passwords per second on a GPU. The only defense is to make
each *guess* expensive, which is what Argon2id does: it's tuned to take
tens or hundreds of milliseconds per hash and to require a chunk of RAM
per attempt. That's the correct trade-off — the cost only hits on login,
which is rare.

**Session tokens** are high-entropy. We generate them ourselves with 256
bits of randomness from the OS CSPRNG. There is no "guessable password"
attack, because there's nothing to guess — you'd need to enumerate
2^256 possibilities. The reason we hash tokens at all is different: it's
to protect against a *database leak*. If raw tokens sat in the DB, a leaked
dump would give an attacker a live session for every logged-in user. Hashing
them means a leaked DB is useless to the attacker.

Because the attack being defended against is "use the leak," not "guess
the input," a fast hash is not just acceptable — it's the right choice.
SHA-256 is fast, deterministic, and gives us a directly indexed equality
lookup (`WHERE token_hash = $1`). Argon2, by contrast, is slow and salted,
which would make every authenticated request pay a ~50ms hashing cost *and*
make indexed lookup impossible (the same token hashes to a different string
each time, because of the salt).

So: Argon2id for passwords, SHA-256 for tokens. Same codebase, two tools,
each matched to its job. This is what "use the right tool for the job"
actually looks like in practice.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings


# ===========================================================================
# PASSWORDS — Argon2id
# ===========================================================================
#
# Argon2id is the current OWASP-recommended password-hashing algorithm. It
# is *memory-hard*, meaning each guess requires a meaningful chunk of RAM,
# which makes large-scale GPU/ASIC cracking expensive in a way that older
# algorithms (MD5, SHA-*, bcrypt to a lesser extent) are not.
#
# Argon2 has three tunable parameters:
#
#   time_cost      — number of passes over memory. Higher = slower = better
#                    against brute force, but also slower for legit logins.
#   memory_cost    — kibibytes of RAM per hash. The main defense against GPU
#                    farms, since GPUs have limited memory per core.
#   parallelism    — number of lanes. 1 is correct for a single-threaded web
#                    app; do not raise it without a specific reason.
#
# We read all three from settings so they can be tuned per environment
# without a code change. Production should use higher values than dev.
#
# A single module-level instance means the parameters are decided exactly
# once, at import time, from the environment. No accidental divergence
# between callers.
_password_hasher = PasswordHasher(
    time_cost=settings.ARGON2_TIME_COST,
    memory_cost=settings.ARGON2_MEMORY_COST,
    parallelism=settings.ARGON2_PARALLELISM,
)


def hash_password(password: str) -> str:
    """
    Hash a plaintext password for storage.

    The returned string is self-describing: it encodes the algorithm
    (argon2id), the parameters used, a random salt, and the derived key.
    Nothing needs to be stored separately alongside it.

    Every call returns a *different* string even for the same password,
    because Argon2 generates a fresh random salt each time. This is
    intentional: two users with the same password do not have identical
    rows in the database, and precomputed rainbow tables are useless.

    Args:
        password: The plaintext password the user typed.

    Returns:
        An Argon2id-encoded hash string, safe to store as-is.
    """
    return _password_hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """
    Check a plaintext password against a stored Argon2id hash.

    Returns True on match, False otherwise. Never raises for the caller.

    Three different argon2-cffi exceptions can mean "the check failed":

      - VerifyMismatchError: hash is valid, password doesn't match.
      - VerificationError:   hash is valid but verification failed for
                             another reason (e.g. corrupted state).
      - InvalidHashError:    the stored string isn't a valid Argon2 hash.

    We collapse all three into `False` here because, from the caller's
    perspective (and the user's), "wrong password" is the only outcome
    that matters. If the stored hash is malformed, that's a server-side
    problem to log, not to surface as a distinct auth error.
    """
    try:
        _password_hasher.verify(hashed, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ===========================================================================
# SESSION TOKENS — generation + SHA-256 hashing
# ===========================================================================
#
# The raw session token is what the client receives: via an HttpOnly cookie
# in a browser, or an `Authorization: Bearer <token>` header for API
# clients. It is never stored in the database. We store its SHA-256 digest
# and, on each request, hash the incoming token and look *that* up.
#
# Because SHA-256 is deterministic, `sha256(incoming)` produces exactly the
# same hex string that was stored at login. That's what makes indexed
# equality lookup possible — a simple `WHERE token_hash = $1`, backed by
# the unique index we created in `002_sessions.sql`.
#
# If instead we used Argon2 for tokens, each hash would include a fresh
# random salt, so `argon2(incoming)` would *never* equal the stored value,
# and lookup would require loading and comparing every session row in the
# table. That's not just slow; it's an entirely different (and worse)
# algorithm for the job. This is the concrete reason the two hashes differ.
#
# Is SHA-256 "too fast" to be secure here? No — and it's worth being
# precise about why. The security of a session token comes entirely from
# its entropy (256 bits of randomness), not from the cost of hashing it.
# An attacker who somehow obtains the digest still faces 2^256 guesses to
# recover the token. Slowing the hash would not help against that; the
# entropy already makes it infeasible. The hash exists to prevent the
# *leak* from being directly usable, not to make guessing harder.


def generate_token() -> str:
    """
    Generate a fresh, high-entropy session token.

    Uses `secrets.token_urlsafe(32)`, which draws 32 random bytes (256 bits)
    from the OS CSPRNG and encodes them as URL-safe base64. The result is a
    43-character string that is safe to put in a URL, a cookie, or an HTTP
    header without escaping.

    `secrets` is the correct module here, not `random`. `random` is seeded
    deterministically and is not cryptographically secure; an attacker who
    learns the internal state can predict future outputs. `secrets` reads
    from the OS entropy source and is designed exactly for this.

    The raw string returned here is the *only* moment it exists in
    plaintext as far as the server is concerned. The client receives it,
    the DB stores its hash, and the plaintext is discarded.

    Returns:
        A URL-safe random string suitable for use as a session token.
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """
    Compute the SHA-256 digest of a session token, as a hex string.

    Deterministic: the same token always produces the same digest. That
    property is required for indexed lookup and is exactly why we don't
    use Argon2 here.

    The output is a 64-character hex string (32 bytes, hex-encoded). We
    store it in a TEXT column; nothing about the schema cares that it's
    hex rather than raw bytes, and hex is easier to eyeball in a debugger.

    Note on encode(): SHA-256 operates on bytes, not str. We encode as
    UTF-8, which is safe because `token_urlsafe` only produces ASCII.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    """
    Constant-time comparison of a raw token's digest against a stored one.

    For a *hash* comparison, timing attacks are not a realistic concern
    (the attacker would have to learn the stored hash, at which point
    timing is the least of your problems). But `hmac.compare_digest` is
    the idiomatic, habit-forming way to compare secrets of any kind, and
    it costs nothing here. Using `==` would work; using `compare_digest`
    signals intent and is what you'd want if this were a raw-secret
    comparison instead of a hash comparison.

    Returns True if the digests match, False otherwise.
    """
    return hmac.compare_digest(hash_token(token), stored_hash)