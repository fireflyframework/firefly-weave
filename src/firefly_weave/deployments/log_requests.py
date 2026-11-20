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

"""Container log requests: a person asks for a snapshot of a target's worker containers, the target's runner reads it
with a fixed command and reports it, and the server keeps it, redacted again, for one hour.

Requests live apart from deployment jobs, so a request never waits for or blocks the target's one active job. Every
person-facing operation needs ``deployment_log.read`` and ``deployment.read`` on the target, on the token principal
and the current principal; the runner's operations need its own capabilities and its registration. The server never
logs what a runner read.
"""

import json
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import (
    LOG_LINE_BYTES,
    DeploymentRunner,
    LogClaim,
    LogLeaseProof,
    LogReport,
    LogReportAccepted,
    LogRequestCreate,
    LogRequestView,
    MemberLog,
    RunnerClaimRequest,
    cut_line,
    newest_lines,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.leases import DeploymentLeases
from firefly_weave.deployments.repository import SCOPE, DeploymentRepository
from firefly_weave.deployments.service import DeploymentService
from firefly_weave.operations.log_redaction import redact_text
from firefly_weave.persistence.idempotency import Idempotency, lock
from firefly_weave.persistence.uow import Transaction, UnitOfWork

READ = ("deployment_log.read", "deployment.read")
# A request and its snapshot are kept this long after the request.
LIFETIME = timedelta(hours=1)
# A request no runner reported on within this time reads failed (no_answer) and is never claimed again.
ANSWER = timedelta(minutes=2)
# A claim the runner hasn't reported on within this time may be claimed again: reading logs changes nothing.
CLAIM = timedelta(seconds=60)
# At most this many requests per target within RATE_WINDOW.
RATE = 10
RATE_WINDOW = timedelta(minutes=1)
# Expired requests deleted per operations loop visit; their snapshots go with them.
EXPIRY_BATCH = 100
_COLUMNS = (
    "id,target_id,component,members,since_seconds,tail_lines,state,failure,runner_id,claim_token,"
    "claim_expires_at,created_at,expires_at,read_at"
)


def stored_members(members: Sequence[MemberLog]) -> tuple[list[MemberLog], bool]:
    """A report's lines as the server keeps them: redacted again by pattern, each cut to 2 KiB, and only the newest
    lines within 256 KiB; True when anything was cut or left out."""
    cut = False
    redone = []
    for member in members:
        lines = []
        for line in member.lines:
            redacted = redact_text(line.text, limit=LOG_LINE_BYTES)
            kept, shortened = cut_line(redacted.text)
            cut = cut or redacted.truncated or shortened
            lines.append(line if kept == line.text else line.model_copy(update={"text": kept}))
        redone.append(member.model_copy(update={"lines": lines}))
    kept_members, older = newest_lines(redone)
    return kept_members, cut or older


def request_view(row: dict[str, Any], snapshot: dict[str, Any] | None, now: datetime) -> LogRequestView:
    """What a request reads as at `now`: expired after its hour, failed once its runner is two minutes late."""
    state, failure = row["state"], row["failure"]
    members: list[Any] = []
    truncated = False
    if now >= row["expires_at"]:
        state, failure = "expired", None
    elif state in {"pending", "claimed"}:
        state, failure = ("failed", "no_answer") if now >= row["created_at"] + ANSWER else ("pending", None)
    elif state == "ready" and snapshot is not None:
        # Stored as JSON: each member is read back the way the wire reads it.
        members = [MemberLog.model_validate_json(json.dumps(item)) for item in snapshot["members"]]
        truncated = snapshot["truncated"]
    return LogRequestView.model_validate(
        {
            "id": row["id"],
            "target_id": row["target_id"],
            "component": row["component"],
            "requested_members": row["members"],
            "since_seconds": row["since_seconds"],
            "tail_lines": row["tail_lines"],
            "state": state,
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "read_at": row["read_at"] if state == "ready" else None,
            "members": members,
            "truncated": truncated,
            "failure": failure,
        }
    )


async def expire(repo: DeploymentRepository, now: datetime, limit: int = EXPIRY_BATCH) -> int:
    """Delete requests whose hour ended, oldest first, with their snapshots; the count it deleted."""
    result = await repo.tx.session.execute(
        text(
            f"DELETE FROM deployment_log_requests WHERE {SCOPE} AND id=ANY(ARRAY(SELECT id "
            f"FROM deployment_log_requests WHERE {SCOPE} AND expires_at<=:now ORDER BY expires_at,id LIMIT :limit))"
        ),
        {**repo.bound, "now": now, "limit": limit},
    )
    return int(getattr(result, "rowcount", 0) or 0)


@service
class LogRequestService:
    def __init__(self, uow: UnitOfWork, deployments: DeploymentService, authorization: AuthorizationService):
        self.uow, self.deployments, self.authorization = uow, deployments, authorization

    async def able(self, tx: Transaction, actor: Principal, capabilities: Sequence[str], context: AuditContext) -> None:
        """Refuse before any lookup unless both principals hold every capability for some target here."""
        if tx.scope.environment_id is None:
            raise AccessDenied()
        current = await load_principal(tx.session, actor.id)
        for principal in (actor, current):
            for capability in capabilities:
                self.authorization.require_somewhere(principal, tx.scope, capability, context=context)

    async def authorized(self, tx: Transaction, actor: Principal, target_id: UUID, context: AuditContext) -> Principal:
        """Both capabilities on the target, for the token principal and the current principal."""
        current = await self.deployments.authorize(tx, actor, READ[0], context, target_id)
        await self.deployments.authorize(tx, actor, READ[1], context, target_id)
        return current

    async def create(
        self, request: LogRequestCreate, key: str, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> LogRequestView:
        async with self.uow.open(scope) as tx:
            await self.able(tx, actor, READ, context)
            current = await self.authorized(tx, actor, request.target_id, context)
            idem = Idempotency(
                tx,
                current.id,
                "deployment_log_requests.create",
                key,
                {"scope": scope.model_dump(mode="json"), **request.model_dump(mode="json")},
            )
            if saved := await idem.replay():
                return LogRequestView.model_validate_json(json.dumps(saved))
            repo = DeploymentRepository(tx)
            target = await self.deployments.target(repo, request.target_id)
            unsupported = CatalogError(422, "WV-DEPLOYMENT-UNSUPPORTED", "This target's runner can't fetch logs")
            if "read_logs" not in target.capabilities:
                raise unsupported
            now = await repo.now()
            runners = [
                DeploymentRunner.model_validate_json(json.dumps(row["payload"]))
                for row in await repo.rows(
                    f"SELECT payload FROM deployment_runners WHERE {SCOPE} AND target_id=:target", target=target.id
                )
            ]
            live = [runner for runner in runners if not runner.revoked and runner.expires_at > now]
            if not live:
                seen = max((runner.last_seen for runner in runners if not runner.revoked), default=None)
                raise CatalogError(
                    409,
                    "WV-RUNNER-OFFLINE",
                    "The target's runner hasn't been in touch within 90 seconds, so its logs can't be fetched",
                    result={"last_seen": seen.isoformat() if seen else None},
                )
            if not any("read_logs" in runner.capabilities for runner in live):
                raise unsupported
            # Requests for one target are counted one at a time, so ten at once never become eleven.
            await lock(tx, f"deployment-log-requests:{scope.environment_id}:{target.id}")
            recent = await repo.rows(
                f"SELECT created_at FROM deployment_log_requests WHERE {SCOPE} AND target_id=:target "
                "AND created_at>:since ORDER BY created_at LIMIT :limit",
                target=target.id,
                since=now - RATE_WINDOW,
                limit=RATE,
            )
            if len(recent) >= RATE:
                wait = (recent[0]["created_at"] + RATE_WINDOW - now).total_seconds()
                raise CatalogError(
                    429,
                    "WV-LOG-RATE",
                    "This target takes at most 10 log requests a minute; try again shortly",
                    retry_after=max(1, math.ceil(wait)),
                )
            identifier = uuid4()
            row = {
                "id": identifier,
                "target_id": target.id,
                "component": request.component,
                "members": request.members,
                "since_seconds": request.since_seconds,
                "tail_lines": request.tail_lines,
                "state": "pending",
                "failure": None,
                "created_at": now,
                "expires_at": now + LIFETIME,
                "read_at": None,
            }
            await tx.session.execute(
                text(
                    "INSERT INTO deployment_log_requests(tenant_id,project_id,environment_id,id,target_id,requested_by,"
                    "component,members,since_seconds,tail_lines,state,created_at,expires_at) VALUES(:tenant,:project,"
                    ":environment,:id,:target_id,:requested_by,:component,cast(:members AS jsonb),:since_seconds,"
                    ":tail_lines,'pending',:created_at,:expires_at)"
                ),
                {**repo.bound, **row, "members": json.dumps(request.members), "requested_by": current.id},
            )
            result = request_view(row, None, now)
            await idem.save(result.model_dump(mode="json"))
            await self.deployments.record(tx, current, READ[0], identifier, context)
            return result

    async def read(self, identifier: UUID, *, actor: Principal, scope: Scope, context: AuditContext) -> LogRequestView:
        async with self.uow.open(scope, mutation=False) as tx:
            await self.able(tx, actor, READ, context)
            repo = DeploymentRepository(tx)
            row = await self.row(repo, identifier)
            await self.authorized(tx, actor, row["target_id"], context)
            snapshots = await repo.rows(
                f"SELECT members,truncated FROM deployment_log_snapshots WHERE {SCOPE} AND request_id=:id",
                id=identifier,
            )
            return request_view(row, snapshots[0] if snapshots else None, await repo.now())

    @staticmethod
    async def row(repo: DeploymentRepository, identifier: UUID, *, lock: bool = False) -> dict[str, Any]:
        rows = await repo.rows(
            f"SELECT {_COLUMNS} FROM deployment_log_requests WHERE {SCOPE} AND id=:id"
            + (" FOR UPDATE" if lock else ""),
            id=identifier,
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Log request not found")
        return rows[0]

    async def runner_able(self, tx: Transaction, actor: Principal, capability: str, context: AuditContext) -> None:
        await self.able(tx, actor, (capability,), context)

    async def claim(
        self, request: RunnerClaimRequest, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> LogClaim | None:
        async with self.uow.open(scope) as tx:
            await self.runner_able(tx, actor, "runner.claim", context)
            repo = DeploymentRepository(tx)
            leases = DeploymentLeases(self.deployments)
            runner, target = await leases.runner(repo, request.runner_id, actor, "runner.claim", context)
            if "read_logs" not in runner.capabilities or "read_logs" not in target.capabilities:
                return None
            now = await repo.now()
            rows = await repo.rows(
                f"SELECT {_COLUMNS} FROM deployment_log_requests WHERE {SCOPE} AND target_id=:target "
                "AND created_at>:answered AND (state='pending' OR (state='claimed' AND claim_expires_at<=:now)) "
                "ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED",
                target=target.id,
                answered=now - ANSWER,
                now=now,
            )
            if not rows:
                return None
            row = rows[0]
            token = uuid4()
            expires = min(now + CLAIM, row["created_at"] + ANSWER)
            await tx.session.execute(
                text(
                    f"UPDATE deployment_log_requests SET state='claimed',runner_id=:runner,claim_token=:token,"
                    f"claim_expires_at=:expires WHERE {SCOPE} AND id=:id"
                ),
                {**repo.bound, "runner": runner.id, "token": token, "expires": expires, "id": row["id"]},
            )
            return LogClaim(
                proof=LogLeaseProof(request_id=row["id"], runner_id=runner.id, token=token),
                expires_at=expires,
                target=leases.for_runner(target, runner),
                component=row["component"],
                members=row["members"],
                since_seconds=row["since_seconds"],
                tail_lines=row["tail_lines"],
            )

    async def report(
        self, request: LogReport, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> LogReportAccepted:
        proof = request.proof
        async with self.uow.open(scope) as tx:
            await self.runner_able(tx, actor, "runner.report", context)
            repo = DeploymentRepository(tx)
            runner, target = await DeploymentLeases(self.deployments).runner(
                repo, proof.runner_id, actor, "runner.report", context
            )
            # read_logs may have been taken away since the claim: then nothing the runner read is kept.
            if "read_logs" not in runner.capabilities or "read_logs" not in target.capabilities:
                raise CatalogError(409, "WV-RUNNER-FENCE", "This log claim is no longer the runner's")
            row = await self.row(repo, proof.request_id, lock=True)
            if row["target_id"] != target.id or row["runner_id"] != runner.id or row["claim_token"] != proof.token:
                raise CatalogError(409, "WV-RUNNER-FENCE", "This log claim is no longer the runner's")
            if row["state"] in {"ready", "failed"}:
                # The same claim reported again: its first report stands.
                return LogReportAccepted(request_id=row["id"], state=row["state"])
            now = await repo.now()
            if row["claim_expires_at"] <= now or row["expires_at"] <= now:
                raise CatalogError(409, "WV-RUNNER-FENCE", "The log claim expired before its report")
            asked = set(row["members"])
            if asked and not {item.member for item in request.members} <= asked:
                raise CatalogError(422, "WV-VALIDATION", "A report names only the members its request asked for")
            state: Literal["ready", "failed"]
            if request.failure is not None:
                await tx.session.execute(
                    text(
                        f"UPDATE deployment_log_requests SET state='failed',failure=:failure WHERE {SCOPE} AND id=:id"
                    ),
                    {**repo.bound, "failure": request.failure, "id": row["id"]},
                )
                state = "failed"
            else:
                members, cut = stored_members(request.members)
                await tx.session.execute(
                    text(
                        "INSERT INTO deployment_log_snapshots(tenant_id,project_id,environment_id,request_id,members,"
                        "truncated,bytes,read_at) VALUES(:tenant,:project,:environment,:id,cast(:members AS jsonb),"
                        ":truncated,:bytes,:now)"
                    ),
                    {
                        **repo.bound,
                        "id": row["id"],
                        "members": json.dumps([item.model_dump(mode="json") for item in members]),
                        "truncated": cut or request.truncated,
                        "bytes": sum(len(line.text.encode()) for item in members for line in item.lines),
                        "now": now,
                    },
                )
                await tx.session.execute(
                    text(f"UPDATE deployment_log_requests SET state='ready',read_at=:now WHERE {SCOPE} AND id=:id"),
                    {**repo.bound, "now": now, "id": row["id"]},
                )
                state = "ready"
            await self.deployments.record(tx, actor, "runner.report", row["id"], context)
            return LogReportAccepted(request_id=row["id"], state=state)
