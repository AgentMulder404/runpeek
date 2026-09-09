"""Identifier minting and the identifier vocabulary.

Identifier *kinds* are distinct namespaces of evidence. A provider's
response-object id (``chatcmpl-…``) and its HTTP request id (``x-request-id``)
are different things and are never compared to each other.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

PROVIDER_OBJECT_ID = "provider_object_id"
HTTP_REQUEST_ID = "http_request_id"
HARNESS_OPERATION_ID = "harness_operation_id"
HARNESS_ATTEMPT_ID = "harness_attempt_id"
PROXY_REQUEST_ID = "proxy_request_id"

# Identifier kinds that are strong enough evidence that two source
# observations describe the SAME provider request. A harness operation id is
# deliberately absent: it groups attempts, it does not prove identity.
REQUEST_IDENTITY_KINDS = frozenset({PROVIDER_OBJECT_ID, HTTP_REQUEST_ID, PROXY_REQUEST_ID})


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def monotonic_ms() -> float:
    return time.perf_counter() * 1000.0
