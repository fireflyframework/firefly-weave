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

"""Run log entries: written by the holder of a task's current lease, within quotas.

An entry's run, step, attempt, worker, workflow and test flag come from the lease and its run, never from the
request. The insert is one statement conditioned on the lease (active, this worker's, this token's, the latest
generation, not expired), so a lease revoked between the checks writes nothing; nothing locks the run or the task.
A run's writes take the advisory lock ``execution-log:<run>`` to count its quotas: 200 entries or 64 KiB per attempt,
2,000 entries or 1 MiB per run, and the environment's entries per UTC day, counted on its day row. A batch over a
quota keeps what fits and reports the rest as dropped; a batch that finds a quota spent is refused.
"""

import json
import math
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from firefly_weave.contracts.workers import TaskLogAccepted, TaskLogEntry, TaskLogRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.log_redaction import redact_entry
from firefly_weave.persistence.uow import Transaction
from firefly_weave.workers.models import token_hash

ATTEMPT_ENTRIES = 200
ATTEMPT_BYTES = 64 * 1024
RUN_ENTRIES = 2000
RUN_BYTES = 1024 * 1024
_SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
_SAME = "{a}.tenant_id={b}.tenant_id AND {a}.project_id={b}.project_id AND {a}.environment_id={b}.environment_id"
_RUN_OF_LEASE = (
    "SELECT t.run_id FROM task_leases l JOIN task_intents t ON "
    + _SAME.format(a="t", b="l")
    + " AND t.id=l.task_id WHERE l.tenant_id=:tenant AND l.project_id=:project AND l.environment_id=:environment "
    "AND l.task_id=:task AND l.generation=:generation"
)
_COUNTS = (
    "SELECT count(*) AS run_entries,coalesce(sum(bytes),0) AS run_bytes,"
    "count(*) FILTER (WHERE task_id=:task AND generation=:generation) AS attempt_entries,"
    "coalesce(sum(bytes) FILTER (WHERE task_id=:task AND generation=:generation),0) AS attempt_bytes "
    f"FROM execution_logs WHERE {_SCOPE} AND run_id=:run"
)
# Creates the environment's row for the UTC day, or locks the existing one, and says how long the day still runs.
_DAY = (
    "INSERT INTO execution_log_days(tenant_id,project_id,environment_id,day) "
    "VALUES(:tenant,:project,:environment,(clock_timestamp() AT TIME ZONE 'UTC')::date) "
    "ON CONFLICT(tenant_id,project_id,environment_id,day) DO UPDATE SET accepted=execution_log_days.accepted "
    "RETURNING day,accepted,"
    "extract(epoch FROM (day+1)::timestamp-(clock_timestamp() AT TIME ZONE 'UTC')) AS seconds_left"
)
# Answers how many entries it stored: all of them while the lease is current, none otherwise.
_INSERT = (
    "WITH now AS (SELECT clock_timestamp() AS at), stored AS ("
    "INSERT INTO execution_logs(tenant_id,project_id,environment_id,id,run_id,task_id,generation,node_id,"
    "instance_key,worker_id,at,received_at,source,level,code,message,fields,redactions,truncated,bytes,"
    "workflow_name,test) "
    "SELECT l.tenant_id,l.project_id,l.environment_id,e.id,t.run_id,l.task_id,l.generation,f.node_id,"
    "f.instance_key,l.owner,greatest(l.claimed_at,least(e.at,now.at)),now.at,e.source,e.level,e.code,e.message,"
    "e.fields,e.redactions,e.truncated,e.bytes,r.workflow_name,r.test "
    "FROM now,task_leases l "
    "JOIN task_intents t ON " + _SAME.format(a="t", b="l") + " AND t.id=l.task_id "
    "JOIN task_facts f ON " + _SAME.format(a="f", b="l") + " AND f.task_id=l.task_id "
    "JOIN run_facts r ON " + _SAME.format(a="r", b="l") + " AND r.run_id=t.run_id "
    "CROSS JOIN jsonb_to_recordset(cast(:entries AS jsonb)) AS e(id uuid,at timestamptz,source text,level text,"
    "code text,message text,fields jsonb,redactions integer,truncated boolean,bytes integer) "
    "WHERE l.tenant_id=:tenant AND l.project_id=:project AND l.environment_id=:environment "
    "AND l.task_id=:task AND l.generation=:generation AND l.owner=:owner AND l.token_hash=:hash "
    "AND l.status='active' AND l.expires_at>now.at AND l.generation=(SELECT max(m.generation) FROM task_leases m "
    "WHERE " + _SAME.format(a="m", b="l") + " AND m.task_id=l.task_id) RETURNING 1) "
    "SELECT count(*) FROM stored"
)
_COUNTED = (
    f"UPDATE execution_log_days SET accepted=accepted+:accepted,dropped=dropped+:dropped WHERE {_SCOPE} AND day=:day"
)


def lease_gone() -> CatalogError:
    return CatalogError(409, "WV-LOG-LEASE", "This worker holds no current lease on the task, so it writes no entry")


def stored_entry(entry: TaskLogEntry) -> dict[str, Any]:
    """The entry as stored: redacted again here, with its weight in bytes (message plus fields as JSON)."""
    redacted = redact_entry(entry.message, entry.fields, known=frozenset())
    fields = json.dumps(redacted.fields, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return {
        "id": str(uuid4()),
        "at": entry.at.isoformat(),
        "source": entry.source,
        "level": entry.level,
        "code": entry.code,
        "message": redacted.message,
        "fields": redacted.fields,
        "redactions": entry.redactions + redacted.redactions,
        "truncated": entry.truncated or redacted.truncated,
        "bytes": len(redacted.message.encode()) + len(fields.encode()),
    }


class ExecutionLogWriter:
    """Appends a worker's entries in the caller's transaction, which the caller authorized for ``task.log``."""

    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        scope = tx.scope
        self.params = {"tenant": scope.tenant_id, "project": scope.project_id, "environment": scope.environment_id}

    async def _rows(self, statement: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(statement), {**self.params, **values})
        return [dict(row) for row in result.mappings().all()]

    async def append(self, request: TaskLogRequest, *, daily_limit: int) -> TaskLogAccepted:
        proof = request.lease
        lease = {"task": proof.task_id, "generation": proof.generation}
        runs = await self._rows(_RUN_OF_LEASE, **lease)
        if not runs:
            raise lease_gone()
        run = runs[0]["run_id"]
        await self.tx.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('execution-log:' || cast(:run AS text),0))"),
            {"run": str(run)},
        )
        entries = [stored_entry(entry) for entry in request.entries]
        (counts,) = await self._rows(_COUNTS, run=run, **lease)
        (day,) = await self._rows(_DAY)
        attempt_entries, attempt_bytes = counts["attempt_entries"], counts["attempt_bytes"]
        run_entries, run_bytes, today = counts["run_entries"], counts["run_bytes"], day["accepted"]
        kept, daily_only = 0, False
        for entry in entries:
            size = entry["bytes"]
            within_task = (
                attempt_entries < ATTEMPT_ENTRIES
                and attempt_bytes + size <= ATTEMPT_BYTES
                and run_entries < RUN_ENTRIES
                and run_bytes + size <= RUN_BYTES
            )
            if not within_task or today >= daily_limit:
                daily_only = within_task
                break
            kept += 1
            attempt_entries, attempt_bytes = attempt_entries + 1, attempt_bytes + size
            run_entries, run_bytes, today = run_entries + 1, run_bytes + size, today + 1
        if kept == 0:
            retry_after = max(1, math.ceil(day["seconds_left"])) if daily_only else None
            message = (
                "This environment wrote its log entries for today"
                if daily_only
                else "This task wrote all the log entries it may"
            )
            raise CatalogError(429, "WV-LOG-QUOTA", message, retry_after=retry_after)
        try:
            inserted = await self.tx.session.scalar(
                text(_INSERT),
                {
                    **self.params,
                    **lease,
                    "owner": proof.owner,
                    "hash": token_hash(proof.token),
                    "entries": json.dumps(entries[:kept], ensure_ascii=False),
                },
            )
        except IntegrityError as error:
            # The run went (a purge) after the lease was read.
            cause = getattr(error.orig, "__cause__", None)
            if "23503" in (getattr(error.orig, "sqlstate", None), getattr(cause, "sqlstate", None)):
                raise lease_gone() from None
            raise
        if inserted != kept:
            raise lease_gone()
        dropped = len(entries) - kept
        await self.tx.session.execute(
            text(_COUNTED),
            {**self.params, "day": day["day"], "accepted": kept, "dropped": dropped + request.dropped},
        )
        return TaskLogAccepted(accepted=kept, dropped=dropped)
