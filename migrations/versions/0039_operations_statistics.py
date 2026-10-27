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


"""Run statistics: duration histograms per workflow, version and step, minute samples of shared capacity, rollup
cursors, and the bucket and histogram functions the rollup and the reads share. Bounded by caps and retention, so
none of these tables counts toward usage.
"""

from alembic import op

revision = "0039_operations_statistics"
down_revision = "0036_editor_studio_documents"

KEY = "tenant_id,project_id,environment_id"
SCOPE_COLUMNS = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
EXACT = " AND ".join(f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid" for name in KEY.split(","))
GRANULARITIES = "'5m','1h','1d'"
SAMPLE_GRANULARITIES = "'1m','1h'"
METRICS = (
    "'tasks_ready','oldest_ready_seconds','runs_active','waits_active','outbox_pending','provider_pending',"
    "'worker_capacity','worker_active_leases','workers_online'"
)
# Lower edges, in whole microseconds, of histogram buckets 1–63: bucket i starts at 2^((i−1)/2) ms. Frozen here;
# firefly_weave.operations.statistics_histogram.THRESHOLDS_US must stay equal to it.
THRESHOLDS = (
    "1000,1415,2000,2829,4000,5657,8000,11314,16000,22628,32000,45255,64000,90510,128000,181020,256000,362039,"
    "512000,724078,1024000,1448155,2048000,2896310,4096000,5792619,8192000,11585238,16384000,23170476,32768000,"
    "46340951,65536000,92681901,131072000,185363801,262144000,370727601,524288000,741455201,1048576000,1482910401,"
    "2097152000,2965820801,4194304000,5931641602,8388608000,11863283204,16777216000,23726566407,33554432000,"
    "47453132813,67108864000,94906265625,134217728000,189812531249,268435456000,379625062498,536870912000,"
    "759250124995,1073741824000,1518500249989,2147483648000"
)
EMPTY = "{" + ",".join(["0"] * 64) + "}"


def secure(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON {table} TO weave_app USING ({EXACT}) WITH CHECK ({EXACT})")
    # The operations loop replaces recomputed buckets and prunes expired rows, so the app may delete.
    op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO weave_app")


def upgrade() -> None:
    op.execute(
        "CREATE FUNCTION weave_duration_bucket(microseconds bigint) RETURNS integer LANGUAGE sql IMMUTABLE "
        f"PARALLEL SAFE AS $$SELECT width_bucket(microseconds,ARRAY[{THRESHOLDS}]::bigint[])$$"
    )
    op.execute(
        "CREATE FUNCTION weave_histogram_add(total int4[],more int4[]) RETURNS int4[] LANGUAGE sql IMMUTABLE STRICT "
        "PARALLEL SAFE AS $$SELECT array_agg(total[i]+more[i] ORDER BY i) FROM generate_series(1,64) i$$"
    )
    op.execute(
        "CREATE AGGREGATE weave_histogram_sum(int4[]) "
        f"(SFUNC=weave_histogram_add,STYPE=int4[],INITCOND='{EMPTY}',PARALLEL=SAFE)"
    )
    # Counting durations into a histogram: unknown durations are skipped (STRICT), negative ones (clock skew) count
    # as under 1 ms.
    op.execute(
        "CREATE FUNCTION weave_histogram_count(counts int4[],microseconds bigint) RETURNS int4[] LANGUAGE plpgsql "
        "IMMUTABLE STRICT PARALLEL SAFE AS $$DECLARE slot integer := weave_duration_bucket(greatest(microseconds,0))+1;"
        "BEGIN counts[slot] := counts[slot]+1; RETURN counts; END$$"
    )
    op.execute(
        "CREATE AGGREGATE weave_histogram(bigint) "
        f"(SFUNC=weave_histogram_count,STYPE=int4[],INITCOND='{EMPTY}',PARALLEL=SAFE)"
    )
    op.execute("GRANT EXECUTE ON FUNCTION weave_histogram_count(int4[],bigint) TO weave_app")
    op.execute("GRANT EXECUTE ON FUNCTION weave_histogram(bigint) TO weave_app")
    op.execute("GRANT EXECUTE ON FUNCTION weave_duration_bucket(bigint) TO weave_app")
    op.execute("GRANT EXECUTE ON FUNCTION weave_histogram_add(int4[],int4[]) TO weave_app")
    op.execute("GRANT EXECUTE ON FUNCTION weave_histogram_sum(int4[]) TO weave_app")
    histogram = "CHECK(cardinality({column})=64 AND array_position({column},NULL) IS NULL)"
    op.execute(f"""CREATE TABLE run_stat_buckets(
        {SCOPE_COLUMNS},granularity text NOT NULL CHECK(granularity IN ({GRANULARITIES})),
        bucket_start timestamptz NOT NULL,workflow_name text NOT NULL,workflow_version text NOT NULL,
        node_id text NOT NULL DEFAULT '',kind text NOT NULL DEFAULT '',
        started integer NOT NULL DEFAULT 0 CHECK(started>=0),succeeded integer NOT NULL DEFAULT 0 CHECK(succeeded>=0),
        failed integer NOT NULL DEFAULT 0 CHECK(failed>=0),cancelled integer NOT NULL DEFAULT 0 CHECK(cancelled>=0),
        timed_out integer NOT NULL DEFAULT 0 CHECK(timed_out>=0),
        duration_hist int4[] NOT NULL {histogram.format(column="duration_hist")},
        queue_hist int4[] CHECK(queue_hist IS NULL OR (cardinality(queue_hist)=64
            AND array_position(queue_hist,NULL) IS NULL)),
        duration_sum_ms bigint NOT NULL DEFAULT 0 CHECK(duration_sum_ms>=0),last_ended_at timestamptz,
        CHECK((node_id='')=(kind='') AND (node_id='')=(queue_hist IS NULL)),
        PRIMARY KEY({KEY},granularity,bucket_start,workflow_name,workflow_version,node_id),
        FOREIGN KEY({KEY}) REFERENCES environments(tenant_id,project_id,id) ON DELETE CASCADE)""")
    op.execute(
        f"CREATE INDEX run_stat_buckets_workflow ON run_stat_buckets({KEY},granularity,workflow_name,bucket_start)"
    )
    op.execute(f"""CREATE TABLE operations_samples(
        {SCOPE_COLUMNS},granularity text NOT NULL CHECK(granularity IN ({SAMPLE_GRANULARITIES})),
        at timestamptz NOT NULL,metric text NOT NULL CHECK(metric IN ({METRICS})),label text NOT NULL DEFAULT '',
        value_avg double precision NOT NULL CHECK(value_avg>=0),value_max double precision NOT NULL
            CHECK(value_max>=value_avg),samples integer NOT NULL CHECK(samples>0),
        PRIMARY KEY({KEY},granularity,at,metric,label),
        FOREIGN KEY({KEY}) REFERENCES environments(tenant_id,project_id,id) ON DELETE CASCADE)""")
    op.execute(f"""CREATE TABLE operations_statistics_cursors(
        {SCOPE_COLUMNS},covered_from timestamptz,rolled_until timestamptz,hours_until timestamptz,days_until timestamptz,
        sampled_until timestamptz,complete_until timestamptz,
        PRIMARY KEY({KEY}),FOREIGN KEY({KEY}) REFERENCES environments(tenant_id,project_id,id) ON DELETE CASCADE)""")
    for table in ("run_stat_buckets", "operations_samples", "operations_statistics_cursors"):
        secure(table)
        op.execute(f"ANALYZE {table}")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
