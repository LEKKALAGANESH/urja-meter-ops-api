"""HMAC request signing for the portal's bulk export endpoint.

Reverse-engineered from the portal's own client bundle
(``_app/immutable/nodes/4.*.js``) and confirmed against the live service. The scheme:

    message   = "\\n".join([METHOD, PATH, QUERY, TIMESTAMP])
    signature = hex(HMAC_SHA256(signing_secret, message))
    headers   = {"x-timestamp": TIMESTAMP, "x-signature": signature}

where ``TIMESTAMP`` is whole seconds since the Unix epoch, as a decimal string.

Measured constraints (see PROTOCOL.md for the probe output):
  * accepted clock skew is +/-300 s inclusive; +/-301 s is rejected
  * the signature is bound to the *exact* query string - a signature minted for
    ``page=1`` is rejected on ``page=2``
  * hex case is not significant
  * a valid signature does **not** replace the session cookie; both are required
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

#: Server-side tolerance, measured empirically. We do not rely on it - we sign each
#: request immediately before sending - but it defines how much local clock drift is
#: survivable before exports start failing.
MAX_CLOCK_SKEW_SECONDS = 300


@dataclass(frozen=True, slots=True)
class SignedRequest:
    timestamp: str
    signature: str

    def as_headers(self) -> dict[str, str]:
        return {"x-timestamp": self.timestamp, "x-signature": self.signature}


def build_message(method: str, path: str, query: str, timestamp: str) -> str:
    """Assemble the exact byte string the portal signs.

    Isolated from :func:`sign_request` so it can be asserted directly in tests against
    the vector captured from the portal's own bundle.
    """
    return "\n".join([method, path, query, timestamp])


def sign_request(
    secret: str,
    method: str,
    path: str,
    query: str,
    *,
    timestamp: int | None = None,
) -> SignedRequest:
    """Sign one request.

    Args:
        secret: value of ``data.signingSecret`` from ``GET /portal/keys``.
        method: HTTP verb, upper-case, e.g. ``"GET"``.
        path: path only, no query and no host, e.g. ``"/portal/export"``.
        query: raw query string *without* the leading ``?``. Empty string if none.
        timestamp: Unix seconds; defaults to now. Injectable so tests are deterministic.

    Returns:
        The timestamp/signature pair to send as headers.
    """
    ts = str(int(time.time()) if timestamp is None else int(timestamp))
    message = build_message(method, path, query, ts)
    signature = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return SignedRequest(timestamp=ts, signature=signature)
