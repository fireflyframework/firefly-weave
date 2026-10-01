# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Weave HMAC authenticates timestamp and exact raw bytes before JSON parsing."""

import hashlib
import hmac
import re
import time

from firefly_weave.compiler.parser import parse_source
from firefly_weave.definitions.models import CatalogError


def denied() -> CatalogError:
    return CatalogError(401, "WV-WEBHOOK-AUTH", "Webhook authentication failed")


def authenticate(secret: bytes, raw_body: bytes, headers: dict[str, str], *, tolerance: int) -> str:
    stamp = headers.get("x-weave-timestamp", "")
    header_event = headers.get("x-weave-event-id")
    signature = headers.get("x-weave-signature", "")
    if not re.fullmatch(r"[0-9]{1,12}", stamp) or not re.fullmatch(r"[a-f0-9]{64}", signature):
        raise denied()
    if abs(time.time() - int(stamp)) > tolerance:
        raise denied()
    expected = hmac.new(secret, stamp.encode("ascii") + b"." + raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise denied()
    try:
        envelope = parse_source(raw_body, format="json").value
    except (ValueError, UnicodeError, RecursionError):
        raise denied() from None
    event = envelope.get("eventId")
    if (
        set(envelope) != {"eventId", "payload"}
        or not isinstance(event, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", event)
        or (header_event is not None and header_event != event)
    ):
        raise denied()
    return event
