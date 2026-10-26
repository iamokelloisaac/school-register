"""
app/api/v1/api.py
=================

Aggregates the v1 routers into a single router.

`main.py` mounts this under `/api/v1`. Adding a new router means importing
it here and calling `include_router`. Nothing else in the app needs to
know about individual routers.

## On the order of `include_router` calls

FastAPI matches routes in the order they're registered. If two routers
declare the same path, the first one wins. We don't have any collisions
right now, but the rule is worth knowing: **specific routes must come
before parameterized ones** if their paths could overlap.

For example, if `programs.py` had a `GET /programs/mine` route and a
`GET /programs/{program_id}` route, the `mine` route must be declared
first, or it would be swallowed by the `{program_id}` matcher (which
would try to parse "mine" as an int and 422). We sidestepped this by
putting "my programs" under `/me/programs` instead. But the ordering rule
still applies within individual routers, so it's worth internalizing.

## Import paths

Every internal import uses the `app.` prefix (e.g. `from app.api.v1.routers
import admin`). This matches the layout — `app` is the top-level package
inside the project root — and it works regardless of which directory
Python is invoked from, as long as the project root is on `sys.path`
(which it is after `pip install -e .`, and which PyCharm also sets up
when you mark `app/` as a sources root).

Do not use bare imports like `from api.v1.routers import admin`. They
only work if the `app/` directory itself is on `sys.path`, which is not
what we've configured. Consistency here prevents a whole class of
"works in my shell, breaks in CI" headaches.
"""

from fastapi import APIRouter

from app.api.v1.routers import admin, auth, health, me, programs

api_router = APIRouter()

# Health endpoints have no prefix of their own — they respond at
# `/api/v1/health` and `/api/v1/health/db`.
api_router.include_router(health.router)

# Everything else is namespaced by its router's `prefix`.
api_router.include_router(auth.router)
api_router.include_router(programs.router)
api_router.include_router(me.router)
api_router.include_router(admin.router)