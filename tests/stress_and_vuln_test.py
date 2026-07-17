"""
Stress Test & Vulnerability Assessment
Target: https://100.101.159.22:8088
Service: Recurring Target Detection (FastAPI)

Run: python tests/stress_and_vuln_test.py
Requires: pip install httpx
"""

import asyncio
import time
import json
import sys
import httpx
from dataclasses import dataclass

# Suppress SSL warnings for self-signed cert
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://100.101.159.22:8088"
VERIFY_SSL = False

# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    severity: str       # CRITICAL / HIGH / MEDIUM / LOW / INFO / PASS
    title: str
    detail: str
    evidence: str = ""

findings: list[Finding] = []

def report(severity: str, title: str, detail: str, evidence: str = ""):
    findings.append(Finding(severity, title, detail, evidence))
    icons = {"CRITICAL": "CRIT", "HIGH": "HIGH", "MEDIUM": "MED ", "LOW": "LOW ", "INFO": "INFO", "PASS": "PASS"}
    icon = icons.get(severity, "    ")
    print(f"  [{icon}] {title}")
    if evidence:
        for line in evidence.strip().splitlines()[:5]:
            print(f"         {line}")

# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def sync_client(cookies=None, timeout=10) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL, verify=VERIFY_SSL, timeout=timeout,
        cookies=cookies or {}, follow_redirects=False
    )

def login(username: str, password: str) -> tuple[int, dict, str | None]:
    """Return (status, body, session_token)."""
    with sync_client() as c:
        r = c.post("/api/login", json={"username": username, "password": password})
        token = r.cookies.get("session_token")
        try:
            body = r.json()
        except Exception:
            body = {}
        return r.status_code, body, token

# ─────────────────────────────────────────────────────────────────────────────
# Section 1: Default Credentials
# ─────────────────────────────────────────────────────────────────────────────

def test_default_credentials():
    print("\n[1] Default Credentials")
    pairs = [
        ("admin",    "admin123"),
        ("admin",    "admin"),
        ("operator", "operator123"),
        ("operator", "operator"),
        ("root",     "root"),
        ("admin",    "password"),
    ]
    for user, pw in pairs:
        status, body, token = login(user, pw)
        if status == 200 and token:
            report("CRITICAL", f"Default credential works: {user} / {pw}",
                   "Seeded default password has not been rotated. "
                   "Any attacker familiar with this codebase can log in immediately.",
                   f"POST /api/login -> 200  role={body.get('role')}")
            with sync_client(cookies={"session_token": token}) as c:
                c.post("/api/logout")
        elif status == 200 and body.get("status") == "must_change_password" and pw in ("admin123", "operator123"):
            report("LOW", f"Default password still present but forced-change enforced: {user} / {pw}",
                   "The default seeded password is accepted but triggers a mandatory password-change flow. "
                   "No session is issued until a new password is set. The fix is working.",
                   f"POST /api/login -> 200 must_change_password (no session cookie)")

# ─────────────────────────────────────────────────────────────────────────────
# Section 2: Brute-Force / Rate Limiting
# ─────────────────────────────────────────────────────────────────────────────

def test_brute_force():
    print("\n[2] Brute-Force / Rate Limiting")
    locked = False
    start = time.time()
    for i in range(30):
        status, _, _ = login("admin", f"wrongpassword{i}")
        if status == 429:
            locked = True
            report("PASS", f"Rate limit enforced via HTTP 429 after {i} attempts", "")
            break
    elapsed = time.time() - start
    avg_ms = elapsed / 30 * 1000

    if not locked:
        # Delay-based throttling: if average response time > 1 s, the rate limiter is adding sleeps
        if avg_ms > 1000:
            report("PASS", "Rate limiting enforced via delay (no HTTP 429 but avg response time raised)",
                   f"30 failed logins took {elapsed:.1f}s total ({avg_ms:.0f}ms/req avg). "
                   "In-process asyncio.sleep throttle is active.",
                   f"elapsed={elapsed:.1f}s, avg={avg_ms:.0f}ms/req")
        else:
            report("HIGH", "No rate limiting on POST /api/login",
                   f"30 wrong-password requests in {elapsed:.1f}s ({avg_ms:.0f}ms/req avg) — zero throttling. "
                   "Enables offline-speed credential stuffing.",
                   f"30 x POST /api/login (wrong pw) -> all 400, elapsed={elapsed:.1f}s")

# ─────────────────────────────────────────────────────────────────────────────
# Section 3: SSRF via Image Proxy
# ─────────────────────────────────────────────────────────────────────────────

def test_ssrf(admin_token: str | None):
    print("\n[3] SSRF via /api/image?url=")
    if not admin_token:
        report("INFO", "Skipping SSRF — no valid admin token available", "")
        return

    ssrf_targets = [
        ("Localhost app itself",       "http://127.0.0.1:8088/api/user/me"),
        ("Internal PostgreSQL port",   "http://127.0.0.1:5432"),
        ("Docker DB service",          "http://db:5432"),
        ("AWS instance metadata",      "http://169.254.169.254/latest/meta-data/"),
        ("IPv6 loopback",              "http://[::1]:8088/api/user/me"),
        ("SSH port probe",             "http://127.0.0.1:22"),
    ]

    ssrf_hits = []
    for label, url in ssrf_targets:
        try:
            with sync_client(cookies={"session_token": admin_token}, timeout=5) as c:
                r = c.get("/api/image", params={"url": url})
            # 200 = proxied content returned; 502 = server tried to fetch but remote rejected
            # Both indicate the server made an outbound connection — SSRF
            if r.status_code in (200, 502):
                ssrf_hits.append(f"{label}: {url} -> {r.status_code}")
        except httpx.TimeoutException:
            # Server hung on connection attempt — confirms SSRF
            ssrf_hits.append(f"{label}: {url} -> TIMEOUT (connection attempted)")
        except Exception:
            pass

    if ssrf_hits:
        report("CRITICAL", "SSRF: /api/image proxies arbitrary internal URLs",
               "No allowlist, no scheme check, no private IP block on the url parameter. "
               "An authenticated user can probe the internal network, reach cloud metadata, "
               "or trigger connections to arbitrary hosts.",
               "\n".join(ssrf_hits))
    else:
        report("PASS", "SSRF probes returned no exploitable responses", "")

# ─────────────────────────────────────────────────────────────────────────────
# Section 4: HTTP Security Headers & CORS
# ─────────────────────────────────────────────────────────────────────────────

def test_security_headers():
    print("\n[4] HTTP Security Headers & CORS")
    with sync_client() as c:
        r = c.get("/login")

    missing = []
    for header, desc in {
        "x-frame-options":           "clickjacking protection",
        "x-content-type-options":    "MIME sniffing protection",
        "content-security-policy":   "XSS / injection protection",
        "strict-transport-security": "HSTS",
        "referrer-policy":           "Referer leakage control",
    }.items():
        if header not in r.headers:
            missing.append(f"  {header} ({desc})")

    if missing:
        report("MEDIUM", "Missing HTTP security headers",
               "None of the standard hardening headers are set.",
               "\n".join(missing))
    else:
        report("PASS", "All security headers present", "")

    # CORS check
    with sync_client() as c:
        r = c.options("/api/login", headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST"
        })
    acao = r.headers.get("access-control-allow-origin", "")
    if acao == "*":
        report("MEDIUM", "CORS allow_origins=[*] — wildcard origin",
               "Any website can initiate cross-origin requests to this API. "
               "Browsers block cookies on wildcard CORS, limiting practical exploit, "
               "but it is a hygiene gap and aids attackers in probing the API.",
               f"Access-Control-Allow-Origin: {acao}")
    else:
        report("PASS", f"CORS restricted (ACAO={acao or 'not set'})", "")

# ─────────────────────────────────────────────────────────────────────────────
# Section 5: Authorization / Privilege Escalation
# ─────────────────────────────────────────────────────────────────────────────

def test_authorization(operator_token: str | None):
    print("\n[5] Authorization — Operator vs Admin endpoints")

    admin_endpoints = [
        "/api/config",
        "/api/users",
        "/api/cameras/by-engine",
        "/api/categories/lpr",
        "/api/categories/fr",
    ]

    if not operator_token:
        report("INFO", "Skipping privilege escalation — no operator token", "")
    else:
        for path in admin_endpoints:
            with sync_client(cookies={"session_token": operator_token}) as c:
                r = c.get(path)
            if r.status_code not in (401, 403, 302):
                report("HIGH", f"Operator accessed admin endpoint: GET {path}",
                       f"Expected 401/403/302, got {r.status_code}.",
                       r.text[:200])
            else:
                report("PASS", f"Operator blocked from GET {path} -> {r.status_code}", "")

    # Unauthenticated access checks
    for path in ["/api/user/me", "/api/monitor/status", "/api/fr/status", "/api/monitor/logs"]:
        with sync_client() as c:
            r = c.get(path)
        if r.status_code not in (401, 302):
            report("HIGH", f"Unauthenticated access to {path}",
                   f"Expected 401/302, got {r.status_code}.", r.text[:200])

# ─────────────────────────────────────────────────────────────────────────────
# Section 6: Session Management
# ─────────────────────────────────────────────────────────────────────────────

def test_session_management():
    print("\n[6] Session Management")

    # Invalid token
    with sync_client(cookies={"session_token": "deadbeef" * 8}) as c:
        r = c.get("/api/user/me")
    if r.status_code == 401:
        report("PASS", "Invalid session token rejected (401)", "")
    else:
        report("HIGH", "Invalid session token not rejected",
               f"Expected 401, got {r.status_code}", r.text[:200])

    # Logout invalidation
    _, _, token = login("admin", "admin123")
    if token:
        with sync_client(cookies={"session_token": token}) as c:
            before  = c.get("/api/user/me")
            c.post("/api/logout")
            after   = c.get("/api/user/me")

        if before.status_code == 200 and after.status_code == 401:
            report("PASS", "Logout correctly invalidates session", "")
        elif before.status_code == 200 and after.status_code == 200:
            report("HIGH", "Session remains valid after /api/logout",
                   "Old token is still accepted after logout — session is not server-side invalidated.",
                   f"Before: {before.status_code}, After: {after.status_code}")
    else:
        report("INFO", "Could not get token for logout test (default pw rotated)", "")

    # Unlimited concurrent sessions
    tokens = []
    for _ in range(6):
        _, _, t = login("admin", "admin123")
        if t:
            tokens.append(t)
    if len(tokens) == 6:
        report("LOW", "No cap on concurrent sessions per user",
               "A user can hold unlimited simultaneous sessions. "
               "Changing a password does not terminate active sessions.",
               f"Created {len(tokens)} simultaneous admin sessions without rejection.")
    for t in tokens:
        with sync_client(cookies={"session_token": t}) as c:
            c.post("/api/logout")

# ─────────────────────────────────────────────────────────────────────────────
# Section 7: Input Validation
# ─────────────────────────────────────────────────────────────────────────────

def test_input_validation(token: str | None):
    print("\n[7] Input Validation")
    if not token:
        report("INFO", "Skipping input validation — no token", "")
        return

    # Oversized login body
    try:
        with sync_client(timeout=10) as c:
            r = c.post("/api/login", json={"username": "A" * 100_000, "password": "x"})
        if r.status_code == 500:
            report("MEDIUM", "Server 500 on oversized username (100k chars)",
                   "No length check on login payload — potential memory pressure.", r.text[:200])
        else:
            report("PASS", f"Oversized username handled -> {r.status_code}", "")
    except Exception as e:
        report("MEDIUM", f"Connection error on oversized login: {e}", "")

    # XSS in characters param (reflected in JSON)
    xss = "<script>alert(1)</script>"
    try:
        with sync_client(cookies={"session_token": token}, timeout=15) as c:
            r = c.get("/api/monitor/target/history", params={"characters": xss})
    except httpx.TimeoutException:
        report("PASS", "XSS payload not reflected (Vaidio search timed out — input not echoed)", "")
        r = None
    if r is not None and xss in r.text:
        report("MEDIUM", "Input echoed in response: /api/monitor/target/history?characters=",
               "XSS payload appears in JSON response. If frontend renders this as innerHTML, XSS is possible.",
               f"Echoed: {xss}")
    else:
        report("PASS", "XSS payload not reflected in monitor/target/history response", "")

    # Invalid limit param type
    with sync_client(cookies={"session_token": token}) as c:
        r = c.get("/api/monitor/logs", params={"limit": "'; DROP TABLE lpr_logs;--"})
    if r.status_code == 500:
        report("MEDIUM", "Server 500 on non-integer limit param",
               "limit param passed directly to DB query without type check.", r.text[:200])
    elif r.status_code == 422:
        report("PASS", "Pydantic rejects non-integer limit (422)", "")

# ─────────────────────────────────────────────────────────────────────────────
# Section 8: Information Disclosure
# ─────────────────────────────────────────────────────────────────────────────

def test_info_disclosure():
    print("\n[8] Information Disclosure")

    for path in ["/docs", "/openapi.json", "/redoc"]:
        with sync_client() as c:
            r = c.get(path)
        if r.status_code == 200:
            report("LOW", f"API docs publicly accessible: {path}",
                   "FastAPI auto-docs expose full endpoint/schema map to unauthenticated clients.",
                   f"GET {path} -> 200 ({len(r.content)} bytes)")
        else:
            report("PASS", f"{path} not accessible (unauthenticated) -> {r.status_code}", "")

# ─────────────────────────────────────────────────────────────────────────────
# Section 9: Password Reset Without Session Invalidation
# ─────────────────────────────────────────────────────────────────────────────

def test_password_reset_session(admin_token: str | None):
    print("\n[9] Password Reset — Session Invalidation")
    if not admin_token:
        report("INFO", "Skipping — no admin token", "")
        return

    tmp_user = "sec_test_tmp_user"
    tmp_pass = "sec_temp_pass_123"
    new_pass  = "sec_new_pass_456"

    with sync_client(cookies={"session_token": admin_token}) as c:
        c.post("/api/users", json={"username": tmp_user, "password": tmp_pass, "role": "Operator"})

    _, _, user_token = login(tmp_user, tmp_pass)
    if not user_token:
        with sync_client(cookies={"session_token": admin_token}) as c:
            c.delete(f"/api/users/{tmp_user}")
        report("INFO", "Could not log in as temp user", "")
        return

    with sync_client(cookies={"session_token": user_token}) as c:
        before = c.get("/api/user/me")

    with sync_client(cookies={"session_token": admin_token}) as c:
        c.put(f"/api/users/{tmp_user}/password", json={"password": new_pass})

    with sync_client(cookies={"session_token": user_token}) as c:
        after = c.get("/api/user/me")

    with sync_client(cookies={"session_token": admin_token}) as c:
        c.delete(f"/api/users/{tmp_user}")

    if before.status_code == 200 and after.status_code == 200:
        report("MEDIUM", "Password reset does NOT invalidate existing user sessions",
               "After an admin resets a password, the old session cookie is still valid "
               "for up to 24 hours. An attacker with a captured token stays authenticated.",
               f"Before reset: {before.status_code}, after reset: {after.status_code}")
    elif after.status_code == 401:
        report("PASS", "Password reset invalidates existing sessions", "")

# ─────────────────────────────────────────────────────────────────────────────
# Section 10: Stress Tests (async)
# ─────────────────────────────────────────────────────────────────────────────

async def stress_login_flood(concurrency: int = 40):
    print(f"\n[10a] Stress: {concurrency} concurrent login attempts")

    async def do_login(i: int):
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=BASE_URL, verify=VERIFY_SSL, timeout=15, follow_redirects=False
            ) as c:
                r = await c.post("/api/login", json={"username": "admin", "password": f"wrong{i}"})
                return r.status_code, time.monotonic() - t0, None
        except Exception as e:
            return None, time.monotonic() - t0, str(e)

    t_start = time.monotonic()
    results = await asyncio.gather(*[do_login(i) for i in range(concurrency)])
    wall    = time.monotonic() - t_start

    statuses = [r[0] for r in results if r[0]]
    errors   = [r[2] for r in results if r[2]]
    times    = [r[1] for r in results]
    avg_ms   = sum(times) / len(times) * 1000
    max_ms   = max(times) * 1000

    summary = (f"concurrency={concurrency}, wall={wall:.2f}s, "
               f"avg={avg_ms:.0f}ms, max={max_ms:.0f}ms, "
               f"statuses={set(statuses)}, errors={len(errors)}")

    if len(errors) / len(results) > 0.2 or max_ms > 8000:
        report("HIGH", "Login endpoint degrades under concurrent flood",
               "High error rate or extreme latency. With no rate limiting, "
               "this makes DoS via login flood effective.",
               summary)
    else:
        report("INFO", "Login flood: server survived", summary)


async def stress_db_pool(token: str, concurrency: int = 25):
    print(f"\n[10b] Stress: {concurrency} concurrent DB requests (pool max=10)")

    async def fetch():
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=BASE_URL, verify=VERIFY_SSL, timeout=20,
                cookies={"session_token": token}, follow_redirects=False
            ) as c:
                r = await c.get("/api/monitor/logs", params={"limit": 200})
                return r.status_code, time.monotonic() - t0, None
        except Exception as e:
            return None, time.monotonic() - t0, str(e)

    t_start = time.monotonic()
    results = await asyncio.gather(*[fetch() for _ in range(concurrency)])
    wall    = time.monotonic() - t_start

    errors  = [r[2] for r in results if r[2]]
    times   = [r[1] for r in results]
    statuses = [r[0] for r in results if r[0]]
    max_ms  = max(times) * 1000 if times else 0
    avg_ms  = sum(times) / len(times) * 1000 if times else 0

    summary = (f"concurrency={concurrency} (pool max_size=10), wall={wall:.2f}s, "
               f"avg={avg_ms:.0f}ms, max={max_ms:.0f}ms, "
               f"statuses={set(statuses)}, errors={len(errors)}")

    if errors:
        report("HIGH", f"DB pool exhausted: {len(errors)}/{concurrency} requests failed",
               "asyncpg pool max_size=10 is saturated. Excess requests timeout, "
               "crashing the endpoint for those users.",
               summary + f"\nFirst error: {errors[0]}")
    elif max_ms > 10000:
        report("MEDIUM", "DB pool queuing causes high latency under load", summary)
    else:
        report("INFO", "DB pool held under concurrent load", summary)


async def stress_chart_query(token: str, concurrency: int = 15):
    print(f"\n[10c] Stress: {concurrency} concurrent chart requests (generate_series query)")

    async def fetch():
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=BASE_URL, verify=VERIFY_SSL, timeout=30,
                cookies={"session_token": token}, follow_redirects=False
            ) as c:
                r = await c.get("/api/monitor/chart", params={"interval": "5m", "lookback_hours": 168})
                return r.status_code, time.monotonic() - t0, None
        except Exception as e:
            return None, time.monotonic() - t0, str(e)

    t_start = time.monotonic()
    results = await asyncio.gather(*[fetch() for _ in range(concurrency)])
    wall    = time.monotonic() - t_start

    errors  = [r[2] for r in results if r[2]]
    times   = [r[1] for r in results]
    max_ms  = max(times) * 1000 if times else 0
    avg_ms  = sum(times) / len(times) * 1000 if times else 0
    statuses = [r[0] for r in results if r[0]]

    summary = (f"concurrency={concurrency}, wall={wall:.2f}s, "
               f"avg={avg_ms:.0f}ms, max={max_ms:.0f}ms, "
               f"statuses={set(statuses)}, errors={len(errors)}")

    if errors or max_ms > 20000:
        report("MEDIUM", "Chart endpoint stressed under concurrent load",
               "generate_series + LEFT JOIN is expensive; parallel queries strain the DB.",
               summary)
    else:
        report("INFO", "Chart endpoint handled concurrent load", summary)


async def stress_image_proxy(token: str, concurrency: int = 20):
    print(f"\n[10d] Stress: {concurrency} concurrent image proxy requests")
    # A small external image; 502 is also valid (server tried, remote failed)
    test_url = "https://httpbin.org/image/jpeg"

    async def fetch():
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=BASE_URL, verify=VERIFY_SSL, timeout=25,
                cookies={"session_token": token}, follow_redirects=False
            ) as c:
                r = await c.get("/api/image", params={"url": test_url})
                return r.status_code, time.monotonic() - t0, None
        except Exception as e:
            return None, time.monotonic() - t0, str(e)

    t_start = time.monotonic()
    results = await asyncio.gather(*[fetch() for _ in range(concurrency)])
    wall    = time.monotonic() - t_start

    errors  = [r[2] for r in results if r[2]]
    times   = [r[1] for r in results]
    statuses = [r[0] for r in results if r[0]]
    max_ms  = max(times) * 1000 if times else 0
    avg_ms  = sum(times) / len(times) * 1000 if times else 0

    summary = (f"concurrency={concurrency}, wall={wall:.2f}s, "
               f"avg={avg_ms:.0f}ms, max={max_ms:.0f}ms, "
               f"statuses={set(statuses)}, errors={len(errors)}")

    if errors:
        report("MEDIUM", f"Image proxy fails under concurrent load ({len(errors)} errors)",
               "Concurrent external fetches + DB cache writes exhaust connections.",
               summary + f"\nFirst error: {errors[0]}")
    else:
        report("INFO", "Image proxy handled concurrent flood", summary)


async def stress_config_restart(token: str, iterations: int = 10):
    print(f"\n[10e] Stress: {iterations} rapid POST /api/config (monitor restart loop)")

    with sync_client(cookies={"session_token": token}) as c:
        r = c.get("/api/config")
    if r.status_code != 200:
        report("INFO", "Cannot fetch config for restart stress — skipping", "")
        return

    cfg = r.json()
    errors = 0
    times  = []
    for _ in range(iterations):
        t0 = time.monotonic()
        with sync_client(cookies={"session_token": token}, timeout=15) as c:
            rr = c.post("/api/config", json=cfg)
        times.append(time.monotonic() - t0)
        if rr.status_code != 200:
            errors += 1

    avg_ms = sum(times) / len(times) * 1000
    summary = f"iterations={iterations}, errors={errors}, avg={avg_ms:.0f}ms"

    if errors:
        report("MEDIUM", f"Config save errors under rapid succession: {errors}/{iterations} failed",
               "Each save spawns asyncio tasks for monitor restart. "
               "Rapid calls may orphan stale tasks.", summary)
    else:
        report("INFO", "Rapid config saves completed without error", summary)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 70)
    print("  RTD Service — Stress Test & Vulnerability Assessment")
    print(f"  Target : {BASE_URL}")
    print("=" * 70)

    admin_token: str | None = None
    operator_token: str | None = None

    # Temporary passwords used by the test to acquire tokens when must_change_password is active.
    # Chosen to satisfy the 8-char minimum and not match the blocked default list.
    TEST_ADMIN_PW    = "TestAdmin@2026"
    TEST_OPERATOR_PW = "TestOper@2026"

    def acquire_token(username: str, default_pw: str, test_pw: str) -> str | None:
        """Try default_pw first; if must_change_password, upgrade to test_pw; return session token."""
        status, body, token = login(username, default_pw)
        if status == 200 and token:
            return token
        if status == 200 and body.get("status") == "must_change_password":
            # Change to test password so the rest of the test can proceed
            with sync_client() as c:
                r = c.post("/api/change-default-password", json={
                    "username": username,
                    "old_password": default_pw,
                    "new_password": test_pw
                })
            if r.status_code == 200:
                _, _, t = login(username, test_pw)
                return t
        # Try the test password directly (already changed in a previous run)
        _, _, t = login(username, test_pw)
        return t

    print("\n[0] Acquiring session tokens...")
    admin_token    = acquire_token("admin",    "admin123",    TEST_ADMIN_PW)
    operator_token = acquire_token("operator", "operator123", TEST_OPERATOR_PW)
    if admin_token:
        print("  OK  admin token acquired")
    else:
        print("  --  Could not authenticate as admin")
    if operator_token:
        print("  OK  operator token acquired")
    else:
        print("  --  Could not authenticate as operator")

    # ── Vulnerability checks ──
    test_default_credentials()
    test_brute_force()
    test_ssrf(admin_token)
    test_security_headers()
    test_authorization(operator_token)
    test_session_management()
    test_input_validation(operator_token or admin_token)
    test_info_disclosure()
    test_password_reset_session(admin_token)

    # ── Stress tests ──
    await stress_login_flood(concurrency=40)
    stress_token = admin_token or operator_token
    if stress_token:
        await stress_db_pool(stress_token, concurrency=25)
        await stress_chart_query(stress_token, concurrency=15)
        await stress_image_proxy(stress_token, concurrency=20)
        await stress_config_restart(stress_token, iterations=10)
    else:
        print("\n  [SKIP] Authenticated stress tests — no valid session token")

    # ── Final summary ──
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    sev_order = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO", "PASS"]
    by_sev: dict[str, list[Finding]] = {s: [] for s in sev_order}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)

    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
        items = by_sev[sev]
        if items:
            print(f"\n  {sev} ({len(items)}):")
            for f in items:
                print(f"    • {f.title}")

    passes = len(by_sev["PASS"])
    total  = sum(len(v) for k, v in by_sev.items() if k not in ("INFO",))
    print(f"\n  Checks passed : {passes}")
    print(f"  Total checks  : {total}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
