"""Stdlib HTTP client for the local VPN rotation service."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


def _request(
    method: str,
    url: str,
    *,
    timeout: float = 30,
    data: bytes | None = None,
) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body) if body else {}
            return response.status, payload
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"error": body or str(exc)}
        payload.setdefault("ok", False)
        payload.setdefault("status", exc.code)
        return exc.code, payload
    except Exception as exc:
        return 0, {"ok": False, "error": str(exc)}


def is_available(base_url: str, timeout: float = 3) -> bool:
    status, payload = _request("GET", f"{base_url.rstrip('/')}/health", timeout=timeout)
    return status == 200 and payload.get("ok") is True


def get_status(base_url: str, timeout: float = 3) -> dict | None:
    status, payload = _request("GET", f"{base_url.rstrip('/')}/status", timeout=timeout)
    if status == 200:
        return payload
    return None


def report_scraper(
    base_url: str,
    client_id: str,
    status: str = "running",
    message: str = "",
    timeout: float = 3,
) -> dict | None:
    body = json.dumps(
        {"client_id": client_id, "status": status, "message": message}
    ).encode("utf-8")
    status_code, payload = _request(
        "POST",
        f"{base_url.rstrip('/')}/scraper/heartbeat",
        timeout=timeout,
        data=body,
    )
    if status_code == 200:
        return payload
    return payload if payload else None


def stop_scraper(
    base_url: str,
    client_id: str,
    message: str = "finished",
    timeout: float = 3,
) -> dict | None:
    body = json.dumps({"client_id": client_id, "message": message}).encode("utf-8")
    status_code, payload = _request(
        "POST",
        f"{base_url.rstrip('/')}/scraper/stop",
        timeout=timeout,
        data=body,
    )
    if status_code == 200:
        return payload
    return payload if payload else None


def current_ip_from_status(status: dict | None) -> str | None:
    """Best-effort current VPN IP from /status."""
    if not status:
        return None
    result = status.get("last_result") or {}
    if result.get("ok") and result.get("new_ip"):
        return str(result["new_ip"])
    if result.get("old_ip"):
        return str(result["old_ip"])
    return None


# The VPN app reports ages rounded to 0.1 s; allow a little clock slack.
_SWITCH_TIME_TOLERANCE_SECONDS = 1.0


def switch_finished_before(
    age_seconds: float | None,
    not_before: float | None,
    *,
    now: float | None = None,
) -> bool:
    """True when a server switch that finished ``age_seconds`` ago happened
    before ``not_before`` (a ``time.time()`` value).

    Used so a block is never "fixed" by reusing the switch that put us on the
    failing server in the first place: only a switch that finished *after* the
    failing request started can have cured it. Unknown age/limit -> False
    (keep the old behaviour).
    """
    if age_seconds is None or not_before is None:
        return False
    now = time.time() if now is None else now
    finished_at = now - float(age_seconds)
    return finished_at < float(not_before) - _SWITCH_TIME_TOLERANCE_SECONDS


def recent_rotate_from_status(
    status: dict | None,
    *,
    max_age_seconds: float = 180,
    baseline_ip: str | None = None,
    not_before: float | None = None,
) -> dict | None:
    """Return last successful rotate from /status if IP changed recently.

    ``not_before``: ignore a switch that finished before this time (the
    failing request was already running on that switch's server).
    """
    if not status:
        return None

    result = status.get("last_result") or {}
    if not result.get("ok"):
        return None

    old_ip = result.get("old_ip")
    new_ip = result.get("new_ip")
    if not new_ip or (old_ip is not None and new_ip == old_ip):
        return None

    age = status.get("last_rotate_age_seconds")
    if age is not None:
        if float(age) > max_age_seconds:
            return None
        if switch_finished_before(age, not_before):
            return None
    elif baseline_ip is not None:
        # Old automation app has no age field — detect change vs baseline IP.
        if str(new_ip) == str(baseline_ip):
            return None
        if status.get("rotating"):
            return None
    else:
        return None

    payload = dict(result)
    payload["coalesced"] = True
    payload["coalesce_age_seconds"] = age
    return payload


def rotate_vpn(
    base_url: str,
    timeout: float = 300,
    *,
    client_id: str = "unknown",
    trigger: str = "default",
    poll_seconds: float = 2.0,
    request_timeout: float = 45.0,
    coalesce_seconds: float = 180.0,
    not_before: float | None = None,
) -> dict | None:
    """
    Request VPN rotation without duplicate POST storms.

    Waits on /status while another client rotates, sends at most one POST /rotate,
    then polls /status until the IP changes or the deadline expires.

    ``not_before`` (``time.time()`` when the failing request started): a recent
    switch is only reused if it finished after that moment. The VPN app itself
    also reuses switches younger than its own coalesce window; when it hands
    back such an older switch the payload is marked ``stale_reuse=True`` (the
    "new" server is the one that is failing) and the caller decides what to do.
    """
    deadline = time.monotonic() + timeout
    url = f"{base_url.rstrip('/')}/rotate"
    body = json.dumps({"client_id": client_id, "trigger": trigger}).encode("utf-8")
    per_request = max(5.0, min(float(request_timeout), 60.0))
    post_sent = False
    last_payload: dict | None = None
    baseline_ip = current_ip_from_status(get_status(base_url, timeout=min(3.0, per_request)))

    def recover_from_status() -> dict | None:
        status = get_status(base_url, timeout=min(3.0, per_request))
        recovered = recent_rotate_from_status(
            status,
            max_age_seconds=coalesce_seconds,
            baseline_ip=baseline_ip,
            not_before=not_before,
        )
        if recovered:
            recovered["client_id"] = client_id
        return recovered

    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        status_payload = get_status(base_url, timeout=min(3.0, per_request))

        if status_payload and status_payload.get("rotating") and not post_sent:
            time.sleep(min(poll_seconds, remaining, 2.0))
            continue

        recovered = recover_from_status()
        if recovered:
            return recovered

        if not post_sent:
            post_sent = True
            http_status, payload = _request(
                "POST",
                url,
                timeout=min(remaining, per_request),
                data=body,
            )
            last_payload = payload

            if http_status == 200 and payload.get("ok"):
                if payload.get("coalesced") and switch_finished_before(
                    payload.get("coalesce_age_seconds"), not_before
                ):
                    payload = dict(payload)
                    payload["stale_reuse"] = True
                return payload
            if http_status in (400, 503):
                return payload

        time.sleep(min(poll_seconds, max(0.1, remaining)))

    recovered = recover_from_status()
    if recovered:
        return recovered

    if last_payload is not None:
        last_payload.setdefault("ok", False)
        last_payload.setdefault("error", "Timed out waiting for VPN rotate")
        return last_payload
    return {"ok": False, "error": "Timed out waiting for VPN rotate", "client_id": client_id}
