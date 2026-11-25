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

"""Runs one item of a scan in a savepoint and puts it off when it fails."""

import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from traceback import walk_tb
from typing import Final
from uuid import UUID

from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.loop_health import raise_if_cancelled
from firefly_weave.persistence.uow import Transaction, refused_for_now, savepoint
from firefly_weave.runtime.capacity import RuntimeCapacityError
from firefly_weave.runtime.kernel import KernelError

FIRST_WAIT: Final = 5.0
LONGEST_WAIT: Final = 300.0
FAILED_ITEMS: Final = "weave_failed_items"
SOURCES: Final = f"{Path(__file__).parents[2]}{os.sep}"
PACKAGE: Final = f"{SOURCES}firefly_weave{os.sep}"


def unexpected(error: Exception) -> bool:
    """Whether an error is the item's own, so only that item is put off.

    It is, unless it says that the database, the network, the platform's capacity or the scan's authority failed. A
    refusal of the item's own data is the item's: a 404, 409 or 422 of its records, and a value (SQLSTATE class 22),
    constraint (23), size or depth (54) or reserve (WQ002) PostgreSQL refuses.
    """
    if isinstance(error, DBAPIError):
        state = getattr(error.orig, "sqlstate", None) or ""
        return not error.connection_invalidated and (state[:2] in {"22", "23", "54"} or state == "WQ002")
    if isinstance(error, CatalogError):
        return error.status in {404, 409, 422}
    return not isinstance(error, (SQLAlchemyError, OSError, AccessDenied, AuthenticationFailed, RuntimeCapacityError))


def refused(error: Exception) -> bool:
    """Whether PostgreSQL refuses the item's write for now: its project's quota or its reserve is spent, or a row busy.

    A platform with no capacity left for any item is not one item's refusal.
    """
    return isinstance(error, DBAPIError) and not error.connection_invalidated and refused_for_now(error)


def cause(error: Exception) -> str:
    """Name an error by its class, its code and the innermost line of Weave it came through, never by its message."""
    code = ""
    if isinstance(error, DBAPIError):
        code = getattr(error.orig, "sqlstate", None) or ""
    elif isinstance(error, CatalogError):
        code = error.code
    elif isinstance(error, KernelError):
        # A kernel error's message is its code.
        code = str(error)
    place = ""
    for frame, line in walk_tb(error.__traceback__):
        if frame.f_code.co_filename.startswith(PACKAGE):
            place = f"at {frame.f_code.co_filename.removeprefix(SOURCES)}:{line}"
    return " ".join(part for part in (type(error).__name__, code, place) if part)


class RunFailures:
    """Which items of each kind of work failed lately, and when each may be tried again.

    It is kept in this process only: another replica, or a restart, tries an item again and starts its own wait. An
    item that still waits is never forgotten, however many fail, so the items behind it are reached.
    One not tried again ``LONGEST_WAIT`` after its wait is forgotten, so only the failures of the last minutes are kept.
    """

    def __init__(self) -> None:
        self.clock: Callable[[], float] = time.monotonic
        # By environment and kind of work: each item's failures in a row and when its wait ends.
        self._items: dict[tuple[UUID | None, str], dict[UUID, tuple[int, float]]] = {}

    def waiting(self, tx: Transaction, kind: str) -> list[UUID]:
        """Items a scan leaves out for now, so those behind them are reached."""
        now, key = self.clock(), (tx.scope.environment_id, kind)
        kept = {
            item: (failed, due) for item, (failed, due) in self._items.pop(key, {}).items() if now < due + LONGEST_WAIT
        }
        if kept:
            self._items[key] = kept
        return [item for item, (_, due) in kept.items() if due > now]

    async def guard[T](
        self,
        tx: Transaction,
        kind: str,
        identifier: UUID | None,
        work: Callable[[], Awaitable[T]],
        *,
        name: str | None = None,
    ) -> T | None:
        """Do one item's work in a savepoint; None, with its writes and effects undone, when it failed or was refused.

        Without an ``identifier`` the item is the run the work locked. An item that is not a run has a ``name`` for
        the log that leads an operator to its run or trigger.
        """
        info = tx.session.info
        item = [identifier]
        result: list[T] = []

        async def observed() -> None:
            try:
                result.append(await work())
            finally:
                # Read before the savepoint's end restores the marker.
                item[0] = identifier or info.get("weave_runtime_candidate")

        error = await savepoint(tx, observed, lambda error: unexpected(error) or refused(error))
        if error is not None:
            raise_if_cancelled(error)
        found = item[0]
        if found is None:
            if error is not None:
                raise error
            return result[0]
        items = self._items.setdefault((tx.scope.environment_id, kind), {})
        failed, _ = items.pop(found, (0, 0.0))
        if error is None:
            return result[0]
        wait = min(LONGEST_WAIT, FIRST_WAIT * 2 ** min(failed, 10))
        items[found] = (failed + 1, self.clock() + wait)
        info[FAILED_ITEMS] = info.get(FAILED_ITEMS, 0) + 1
        for_now = refused(error)
        logging.getLogger(__name__).log(
            logging.WARNING if for_now else logging.ERROR,
            "Recovery %s: %s of %s in environment %s (%s); the scan continues and tries it again in %d s",
            "was refused for now" if for_now else "failed",
            kind,
            name or f"run {found}",
            tx.scope.environment_id,
            cause(error),
            wait,
        )
        tx.record("recovery_failure", f"recovery.{kind}", "rejected" if for_now else "failed")
        return None
