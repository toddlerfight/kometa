import time
import jwt
import httpx
import logging

log = logging.getLogger(__name__)

APNS_HOST = "https://api.push.apple.com"
APNS_HOST_SANDBOX = "https://api.sandbox.push.apple.com"


def _make_token(key_pem: str, key_id: str, team_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": team_id, "iat": now},
        key_pem,
        algorithm="ES256",
        headers={"kid": key_id},
    )


def send_push(
    *,
    tokens: list[str],
    title: str,
    body: str,
    data: dict | None = None,
    key_pem: str,
    key_id: str,
    team_id: str,
    bundle_id: str,
    sandbox: bool = False,
) -> dict[str, str]:
    """Send APNs push to a list of tokens. Returns {token: 'ok'|error_reason}."""
    host = APNS_HOST_SANDBOX if sandbox else APNS_HOST
    token = _make_token(key_pem, key_id, team_id)
    headers = {
        "authorization": f"bearer {token}",
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-topic": bundle_id,
    }
    payload = {
        "aps": {"alert": {"title": title, "body": body}, "sound": "default"},
        **(data or {}),
    }

    results: dict[str, str] = {}
    # HTTP/2 client — APNs requires it
    with httpx.Client(http2=True) as client:
        for device_token in tokens:
            url = f"{host}/3/device/{device_token}"
            try:
                resp = client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    results[device_token] = "ok"
                else:
                    reason = resp.json().get("reason", str(resp.status_code))
                    log.warning("APNs rejected %s: %s", device_token[:8], reason)
                    results[device_token] = reason
            except Exception as exc:
                log.error("APNs send failed for %s: %s", device_token[:8], exc)
                results[device_token] = str(exc)
    return results
