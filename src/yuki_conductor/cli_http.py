"""Calling the running daemon's HTTP API from a CLI command."""

import json
import sys
import urllib.error
import urllib.request


def api_request(method: str, path: str, payload: dict | None = None, *, what: str = "Request"):
    """Send a JSON request to the local daemon and return the decoded reply.

    Exits the process with a readable message on any failure; `what` names
    the action in it ("Send failed (404): ...").
    """
    from yuki_conductor.web_server import WEB_PORT

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{WEB_PORT}{path}",
        method=method,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except (ValueError, AttributeError):
            pass
        print(f"{what} failed ({exc.code}): {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Daemon not reachable on port {WEB_PORT}: {exc.reason}", file=sys.stderr)
        sys.exit(1)
