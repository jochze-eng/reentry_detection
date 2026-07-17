# Security & Stress Assessment Report

**Target:** `https://100.101.159.22:8088`  
**Service:** Recurring Target Detection (FastAPI)  
**Date:** 2026-06-27  
**Test Script:** `tests/stress_and_vuln_test.py`

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 3 |
| HIGH     | 1 |
| MEDIUM   | 3 |
| LOW      | 4 |
| PASS     | 10 |

---

## Vulnerability Findings

### CRITICAL

#### 1. Default credentials active — `admin / admin123`
- **Location:** `services/db.py:251` (seed block)
- **Detail:** The seeded default Administrator password has never been rotated. Any attacker familiar with this codebase can authenticate immediately with full admin access.
- **Evidence:** `POST /api/login {"username":"admin","password":"admin123"}` → `200 OK, role=Administrator`
- **Fix:** Force a password change on first login, or remove the hardcoded seed and require the operator to set credentials at deployment time.

#### 2. Default credentials active — `operator / operator123`
- **Location:** `services/db.py:257` (seed block)
- **Detail:** Same issue for the Operator account.
- **Evidence:** `POST /api/login {"username":"operator","password":"operator123"}` → `200 OK, role=Operator`
- **Fix:** Same as above.

#### 3. SSRF via `/api/image?url=`
- **Location:** `api/routes.py:299` — `httpx.AsyncClient(verify=False, timeout=10)` with no URL validation
- **Detail:** The image proxy endpoint accepts any `url` query parameter and forwards the request from the server. There is no scheme allowlist, no private IP block, and no domain restriction. An authenticated user can use this to probe internal services, reach cloud metadata endpoints, or trigger outbound connections to arbitrary hosts.
- **Evidence (confirmed outbound attempts):**

  | Target | Result |
  |--------|--------|
  | `http://127.0.0.1:8088/api/user/me` | 502 — server tried to fetch |
  | `http://127.0.0.1:5432` (PostgreSQL) | 502 — server tried to fetch |
  | `http://db:5432` (Docker DB service) | 502 — server tried to fetch |
  | `http://169.254.169.254/latest/meta-data/` | TIMEOUT — server hung connecting (5 s) |
  | `http://[::1]:8088/api/user/me` | 502 — server tried to fetch |

- **Fix:** Validate the `url` parameter before fetching. Reject non-`https` schemes, block private IP ranges (RFC 1918, loopback, link-local), and enforce a domain allowlist matching the configured `vaidio_base_url`.

---

### HIGH

#### 4. No rate limiting on `POST /api/login`
- **Location:** `api/routes.py:53` — no throttle, lockout, or CAPTCHA
- **Detail:** 30 wrong-password requests were sent in 2.5 seconds with zero server-side throttling. An attacker can run full-speed credential stuffing or dictionary attacks against any username.
- **Evidence:** `30 × POST /api/login (wrong password)` → all `400`, elapsed = 2.5 s
- **Fix:** Add a slowdown or lockout after N consecutive failures per username/IP (e.g. using `slowapi` or a Redis-backed counter). Even a 1-second delay after 5 failures eliminates brute-force viability.

---

### MEDIUM

#### 5. Missing HTTP security headers
- **Location:** `main.py` — no header middleware configured
- **Detail:** None of the standard browser hardening headers are set on any response.

  | Header | Purpose |
  |--------|---------|
  | `X-Frame-Options` | Prevents clickjacking |
  | `X-Content-Type-Options` | Prevents MIME sniffing |
  | `Content-Security-Policy` | Blocks XSS and resource injection |
  | `Strict-Transport-Security` | Enforces HTTPS (HSTS) |
  | `Referrer-Policy` | Controls Referer header leakage |

- **Fix:** Add a FastAPI middleware to inject all five headers on every response.

#### 6. CORS wildcard `allow_origins=["*"]`
- **Location:** `main.py:126–130`
- **Detail:** Any origin can make cross-origin requests to this API. Browsers block cookies on wildcard CORS, limiting the direct risk, but the permissive policy removes a layer of defence and aids attackers in probing the API from arbitrary web pages.
- **Evidence:** `Access-Control-Allow-Origin: *`
- **Fix:** Restrict `allow_origins` to the specific host (e.g. `["https://100.101.159.22:8088"]`) or to a known set of front-end origins.

#### 7. Password reset does not invalidate existing sessions
- **Location:** `api/routes.py:121` (`PUT /api/users/{username}/password`) — no session purge
- **Detail:** After an admin resets a user's password, all of that user's active session cookies remain valid until they naturally expire (24 hours). If a compromised account's password is changed as a security response, the attacker retains access for the full remaining session lifetime.
- **Evidence:** Session cookie issued before password reset returned `200` on `/api/user/me` after the password was changed.
- **Fix:** In `api_reset_password()`, call `db_manager.delete_sessions_for_user(username)` before returning. This helper does not yet exist and needs to be added to `DbManager` (`DELETE FROM user_sessions WHERE username = $1`).

---

### LOW

#### 8. No cap on concurrent sessions per user
- **Location:** `services/db.py:849` (`create_session`) — no count constraint
- **Detail:** A single user can hold an unlimited number of simultaneous active sessions. Combined with finding #7, revoking a compromised account requires identifying and deleting every token individually.
- **Evidence:** 6 simultaneous `admin` sessions created without rejection.
- **Fix:** Either limit concurrent sessions to N per user (delete oldest on overflow) or purge all existing sessions for a user when a new one is created.

#### 9–11. FastAPI auto-docs publicly accessible
- **Location:** FastAPI default routes
- **Detail:** The full OpenAPI schema (all endpoints, parameters, request/response shapes) is accessible without authentication. This gives an attacker a complete map of the attack surface.

  | Path | Status |
  |------|--------|
  | `/docs` | 200 OK |
  | `/openapi.json` | 200 OK (13 238 bytes) |
  | `/redoc` | 200 OK |

- **Fix:** Disable in production: `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`.

---

## Stress Test Results

All endpoints held up under the tested concurrency levels. No crashes, pool exhaustion errors, or sustained degradation were observed.

| Test | Concurrency | Errors | Result |
|------|------------|--------|--------|
| Login flood (`POST /api/login`) | 40 | 0 | Passed |
| DB pool saturation (`GET /api/monitor/logs?limit=200`) | 25 (pool max = 10) | 0 | Passed |
| Chart query with `generate_series` | 15 | 0 | Passed |
| Image proxy flood (`GET /api/image`) | 20 | 0 | Passed |
| Config save / monitor restart loop | 10 rapid saves | 0 | Passed |

**Notes:**
- The asyncpg connection pool (`max_size=10`) queues requests gracefully when saturated; no requests were dropped at 2.5× pool capacity.
- The `generate_series` chart query scales acceptably at 15 concurrent requests even with a 168-hour lookback window.
- No task leaks were observed from the rapid config-save monitor restart loop.

---

## Authorization Audit (All Passed)

| Check | Result |
|-------|--------|
| Operator blocked from `GET /api/config` | PASS (403) |
| Operator blocked from `GET /api/users` | PASS (403) |
| Operator blocked from `GET /api/cameras/by-engine` | PASS (403) |
| Operator blocked from `GET /api/categories/lpr` | PASS (403) |
| Operator blocked from `GET /api/categories/fr` | PASS (403) |
| Invalid session token rejected | PASS (401) |
| Logout invalidates session server-side | PASS |
| Pydantic rejects non-integer `limit` param | PASS (422) |
| Oversized username (100 000 chars) handled gracefully | PASS (400) |
| XSS payload not reflected in API responses | PASS |
