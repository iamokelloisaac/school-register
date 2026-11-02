#!/usr/bin/env bash
# =============================================================================
# School Register — end-to-end API walkthrough
# =============================================================================
#
# Exercises every endpoint in dependency order. Use it to sanity-check
# that the whole system is wired up after seeding.
#
# Usage:
#     ./scripts/test_api.sh
#     BASE_URL=http://localhost:8000 ./scripts/test_api.sh
#
# Requires:
#     * The app running (uvicorn app.main:app --reload)
#     * A seeded database (python -m scripts.seed)
#     * curl (always present on Unix)
#     * jq (optional; install via apt/dnf/brew if missing)
#
# The script exits non-zero on the first failure, so `./scripts/test_api.sh
# && echo OK` is a useful one-liner.
# =============================================================================

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
API="${BASE_URL}/api/v1"

# Cookies for the admin and the test student, so requests carry sessions.
ADMIN_COOKIE=$(mktemp)
STUDENT_COOKIE=$(mktemp)
trap 'rm -f "$ADMIN_COOKIE" "$STUDENT_COOKIE"' EXIT

# Does jq exist? It makes the output pretty and lets us extract ids.
# The script works without it, but the ids extraction uses grep/sed as
# a fallback — hacky but dependency-free.
if command -v jq >/dev/null 2>&1; then
    HAVE_JQ=1
else
    HAVE_JQ=0
    echo "note: jq not found, output will be raw JSON"
fi

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

section() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  $1"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# Pretty-print a JSON response. Uses jq when available, echoes raw
# otherwise.
pp() {
    if [ "$HAVE_JQ" = "1" ]; then
        jq .
    else
        cat
    fi
}

# Extract a field from a JSON string. jq when available; grep/sed
# otherwise. Not robust for nested objects, but works for flat ids.
extract_id() {
    local field="$1"
    if [ "$HAVE_JQ" = "1" ]; then
        jq -r ".$field // empty"
    else
        sed -n "s/.*\"$field\":\\([0-9]*\\).*/\\1/p" | head -1
    fi
}

# -----------------------------------------------------------------------------
# 1. Health — prove the app is up
# -----------------------------------------------------------------------------

section "1. Health checks"

echo "→ GET /health"
curl -sf "${API}/health" | pp

echo "→ GET /health/db"
curl -sf "${API}/health/db" | pp

# -----------------------------------------------------------------------------
# 2. Register a fresh student
# -----------------------------------------------------------------------------
#
# The seed created student1..student10. This creates an 11th through the
# public API, to exercise the self-registration path (which the seed
# bypasses by inserting directly).

section "2. Register a new student (self-signup)"

REGISTER_EMAIL="walkthrough@example.com"
REGISTER_RESPONSE=$(curl -sf -X POST "${API}/auth/register" \
    -H "Content-Type: application/json" \
    -d "{
        \"email\": \"${REGISTER_EMAIL}\",
        \"password\": \"walkthrough123\",
        \"full_name\": \"Walkthrough User\",
        \"age\": 25
    }")

echo "→ POST /auth/register"
echo "$REGISTER_RESPONSE" | pp

# -----------------------------------------------------------------------------
# 3. Log in as the registered student
# -----------------------------------------------------------------------------

section "3. Log in as the new student"

curl -sf -X POST "${API}/auth/login" \
    -H "Content-Type: application/json" \
    -c "$STUDENT_COOKIE" \
    -d "{\"email\": \"${REGISTER_EMAIL}\", \"password\": \"walkthrough123\"}" \
    | pp

# -----------------------------------------------------------------------------
# 4. Browse the catalog (paginated)
# -----------------------------------------------------------------------------

section "4. Browse programs (paginated)"

echo "→ GET /programs?limit=5&offset=0"
curl -sf "${API}/programs?limit=5&offset=0" -b "$STUDENT_COOKIE" | pp

echo ""
echo "→ GET /programs?limit=5&offset=5  (page 2 — should not overlap page 1)"
curl -sf "${API}/programs?limit=5&offset=5" -b "$STUDENT_COOKIE" | pp

# -----------------------------------------------------------------------------
# 5. Fetch a single program by id
# -----------------------------------------------------------------------------
#
# We grab the first program id from the catalog response to use as the
# enrollment target.

section "5. Fetch a program by id"

FIRST_PROGRAM_RESPONSE=$(curl -sf "${API}/programs?limit=1" -b "$STUDENT_COOKIE")
PROGRAM_ID=$(echo "$FIRST_PROGRAM_RESPONSE" | jq -r '.items[0].id' 2>/dev/null \
    || echo "$FIRST_PROGRAM_RESPONSE" | extract_id "id")

if [ -z "$PROGRAM_ID" ]; then
    echo "ERROR: could not extract a program id"
    exit 1
fi
echo "using program id: ${PROGRAM_ID}"

echo "→ GET /programs/${PROGRAM_ID}"
curl -sf "${API}/programs/${PROGRAM_ID}" -b "$STUDENT_COOKIE" | pp

# -----------------------------------------------------------------------------
# 6. Enroll in the program
# -----------------------------------------------------------------------------

section "6. Enroll in the program"

echo "→ POST /programs/${PROGRAM_ID}/enroll"
curl -sf -X POST "${API}/programs/${PROGRAM_ID}/enroll" -b "$STUDENT_COOKIE" | pp

# -----------------------------------------------------------------------------
# 7. Confirm enrollment appears in "my programs"
# -----------------------------------------------------------------------------

section "7. View my enrollments"

echo "→ GET /me/programs"
curl -sf "${API}/me/programs" -b "$STUDENT_COOKIE" | pp

# -----------------------------------------------------------------------------
# 8. View own profile, then edit it
# -----------------------------------------------------------------------------

section "8. View and update own profile"

echo "→ GET /me"
curl -sf "${API}/me" -b "$STUDENT_COOKIE" | pp

echo ""
echo "→ PATCH /me  { full_name: 'Updated Walkthrough User' }"
curl -sf -X PATCH "${API}/me" \
    -H "Content-Type: application/json" \
    -b "$STUDENT_COOKIE" \
    -d '{"full_name": "Updated Walkthrough User"}' | pp

# -----------------------------------------------------------------------------
# 9. Change password
# -----------------------------------------------------------------------------

section "9. Change password"

echo "→ POST /me/password"
curl -sf -o /dev/null -w "status: %{http_code}\n" -X POST "${API}/me/password" \
    -H "Content-Type: application/json" \
    -b "$STUDENT_COOKIE" \
    -d '{"current_password": "walkthrough123", "new_password": "newpassword456"}'

echo "→ GET /me  (current session should still work)"
curl -sf "${API}/me" -b "$STUDENT_COOKIE" | pp

# -----------------------------------------------------------------------------
# 10. Log out the student
# -----------------------------------------------------------------------------

section "10. Log out the student"

echo "→ POST /auth/logout"
curl -sf -o /dev/null -w "status: %{http_code}\n" -X POST "${API}/auth/logout" \
    -b "$STUDENT_COOKIE"

echo "→ GET /me  (should now return 401)"
curl -s -o /dev/null -w "status: %{http_code}\n" "${API}/me" -b "$STUDENT_COOKIE"

# -----------------------------------------------------------------------------
# 11. Log in as admin
# -----------------------------------------------------------------------------

section "11. Log in as admin"

curl -sf -X POST "${API}/auth/login" \
    -H "Content-Type: application/json" \
    -c "$ADMIN_COOKIE" \
    -d '{"email": "admin@school.local", "password": "changeme123"}' \
    | pp

# -----------------------------------------------------------------------------
# 12. Admin: list users (paginated)
# -----------------------------------------------------------------------------

section "12. Admin: list users"

echo "→ GET /admin/students?limit=5"
curl -sf "${API}/admin/students?limit=5" -b "$ADMIN_COOKIE" | pp

# -----------------------------------------------------------------------------
# 13. Admin: create a program
# -----------------------------------------------------------------------------

section "13. Admin: create a program"

echo "→ POST /admin/programs"
NEW_PROGRAM_RESPONSE=$(curl -sf -X POST "${API}/admin/programs" \
    -H "Content-Type: application/json" \
    -b "$ADMIN_COOKIE" \
    -d '{"name": "Philosophy", "code": "PHIL", "description": "Ethics, logic, metaphysics."}')
echo "$NEW_PROGRAM_RESPONSE" | pp
NEW_PROGRAM_ID=$(echo "$NEW_PROGRAM_RESPONSE" | jq -r '.id' 2>/dev/null \
    || echo "$NEW_PROGRAM_RESPONSE" | extract_id "id")

# -----------------------------------------------------------------------------
# 14. Admin: archive the new program
# -----------------------------------------------------------------------------

section "14. Admin: archive the program"

echo "→ POST /admin/programs/${NEW_PROGRAM_ID}/archive"
curl -sf -o /dev/null -w "status: %{http_code}\n" \
    -X POST "${API}/admin/programs/${NEW_PROGRAM_ID}/archive" \
    -b "$ADMIN_COOKIE"

echo "→ GET /programs  (archived program should be hidden from public catalog)"
curl -sf "${API}/programs?limit=100" -b "$STUDENT_COOKIE" \
    | (jq '.items | map(.code)' 2>/dev/null || cat)

echo "→ GET /admin/programs  (archived program should still appear for admin)"
curl -sf "${API}/admin/programs?limit=100" -b "$ADMIN_COOKIE" \
    | (jq '.items | map(.code)' 2>/dev/null || cat)

# -----------------------------------------------------------------------------
# 15. Admin: list students in a specific program
# -----------------------------------------------------------------------------

section "15. Admin: roster of a program"

echo "→ GET /admin/programs/${PROGRAM_ID}/students"
curl -sf "${API}/admin/programs/${PROGRAM_ID}/students" -b "$ADMIN_COOKIE" | pp

# -----------------------------------------------------------------------------
# 16. Admin: reset a student's password
# -----------------------------------------------------------------------------

section "16. Admin: reset a student's password"

# Find the walkthrough user's id.
WALKTHROUGH_ID=$(curl -sf "${API}/admin/students?limit=100" -b "$ADMIN_COOKIE" \
    | jq -r '.items[] | select(.email == "walkthrough@example.com") | .id' 2>/dev/null \
    || echo "1")

echo "→ POST /admin/students/${WALKTHROUGH_ID}/password"
curl -sf -o /dev/null -w "status: %{http_code}\n" \
    -X POST "${API}/admin/students/${WALKTHROUGH_ID}/password" \
    -H "Content-Type: application/json" \
    -b "$ADMIN_COOKIE" \
    -d '{"new_password": "admin-set-this-789"}'

# -----------------------------------------------------------------------------
# 17. Admin: log out everywhere
# -----------------------------------------------------------------------------

section "17. Admin: log out everywhere"

echo "→ POST /auth/logout-all"
curl -sf -o /dev/null -w "status: %{http_code}\n" \
    -X POST "${API}/auth/logout-all" -b "$ADMIN_COOKIE"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  All endpoint checks passed."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"