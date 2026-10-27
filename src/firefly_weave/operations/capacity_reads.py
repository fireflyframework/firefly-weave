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


"""Statements that measure shared capacity, read by the summary and sampled by run statistics.

Every statement takes the environment as ``:tenant``, ``:project`` and ``:environment`` and the observation time as
``:now``.
"""

SCOPE = "{alias}tenant_id=:tenant AND {alias}project_id=:project AND {alias}environment_id=:environment"
SAME_SCOPE = "{a}.tenant_id={b}.tenant_id AND {a}.project_id={b}.project_id AND {a}.environment_id={b}.environment_id"
NOW = "cast(:now AS timestamptz)"
LIVE_LEASES = (
    "SELECT owner,count(*) AS leases FROM task_leases WHERE "
    + SCOPE.format(alias="")
    + f" AND status='active' AND expires_at>{NOW} AND deadline>{NOW} GROUP BY owner"
)
RECENT = f"w.last_seen_at>{NOW}-interval '60 seconds'"
CURRENT = f"NOT w.revoked AND coalesce(w.last_seen_at,f.registered_at)>{NOW}-interval '1 hour'"
WORKER_FACTS = f"LEFT JOIN worker_facts f ON {SAME_SCOPE.format(a='f', b='w')} AND f.worker_id=w.id"
# Active test runs, exactly as the run usage counter counts runs: by status, so a finished run without a recorded end
# is not active. The partial index run_facts_active_test has this predicate.
ACTIVE_TEST = "test AND (status IS NULL OR status NOT IN ('succeeded','failed','cancelled','timed_out'))"
ACTIVE_TESTS = f"SELECT count(*) AS active FROM run_facts WHERE {SCOPE.format(alias='')} AND {ACTIVE_TEST}"
READY_COUNTS = (
    "SELECT count(*) AS ready,extract(epoch FROM (cast(:now AS timestamptz)-min(ready_since))) AS oldest "
    f"FROM task_facts WHERE {SCOPE.format(alias='')} AND status='ready' AND ready_since<=cast(:now AS timestamptz)"
)
# One grouped statement over the environment's workers, counted as the Workers page lists current ones.
WORKER_COUNTS = (
    f"WITH live AS ({LIVE_LEASES}) SELECT "
    f"count(*) FILTER (WHERE {CURRENT} AND NOT w.draining AND {RECENT}) AS online,"
    f"count(*) FILTER (WHERE {CURRENT} AND NOT w.draining AND NOT coalesce({RECENT},false)) AS offline,"
    f"count(*) FILTER (WHERE {CURRENT} AND w.draining) AS draining,"
    f"coalesce(sum((w.payload->>'capacity')::integer) FILTER (WHERE {CURRENT} AND NOT w.draining "
    f"AND {RECENT}),0) AS capacity,coalesce(sum(a.leases),0) AS active_leases "
    f"FROM worker_instances w {WORKER_FACTS} LEFT JOIN live a ON a.owner=w.id "
    f"WHERE {SCOPE.format(alias='w.')}"
)
