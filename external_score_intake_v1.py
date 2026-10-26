"""Fail-closed authenticated read-only snapshot intake for CC.

This is an isolated library, NOT wired to production. Each source has its own
shared secret and HTTPS URL. No source credentials or payloads are logged.
"""
import hmac
import json
import urllib.parse
import urllib.request
from external_score_format_v1 import format_external_score

MAX_BYTES = 4096

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward source bearer tokens to a redirect target."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_score(url, bearer_token, expected_system, *, opener=None):
    if not isinstance(url, str):
        raise ValueError("HTTPS_REQUIRED")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HTTPS_REQUIRED")
    if parsed.fragment:
        raise ValueError("URL_FRAGMENT_NOT_ALLOWED")
    if not isinstance(bearer_token, str) or len(bearer_token) < 24:
        raise ValueError("INVALID_SOURCE_AUTH")
    if expected_system not in ("DRJ", "JJ Jame"):
        raise ValueError("UNKNOWN_SOURCE")
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + bearer_token,
                      "Accept": "application/json"}, method="GET")
    open_request = opener or urllib.request.build_opener(_NoRedirectHandler()).open
    with open_request(request, timeout=8) as response:
        if response.status != 200:
            raise ValueError("SOURCE_NOT_READY")
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("OVERSIZED_SCORE")
    snapshot = json.loads(body.decode("utf-8"))
    if not isinstance(snapshot, dict) or not hmac.compare_digest(
        str(snapshot.get("system", "")), expected_system
    ):
        raise ValueError("SOURCE_ID_MISMATCH")
    # Strictly validates classification and counts before any delivery.
    message = format_external_score(snapshot)
    return message
