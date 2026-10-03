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

"""Store bounded file content separately from workflow JSON and event history."""

from alembic import op

revision = "0028_files"
down_revision = "0027_lumi_configuration"


def upgrade():
    scope = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
    key = "tenant_id,project_id,environment_id"
    op.execute(f"""CREATE TABLE weave_files(
        id uuid PRIMARY KEY,{scope},owner_id uuid NOT NULL REFERENCES principals(id),
        task_id uuid,human_task_id uuid,run_id uuid,payload jsonb NOT NULL,state text NOT NULL DEFAULT 'uploading'
        CHECK(state IN ('uploading','ready','deleted')),
        size_bytes integer NOT NULL CHECK(size_bytes BETWEEN 0 AND 26214400),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE({key},id),
        FOREIGN KEY({key}) REFERENCES environments(tenant_id,project_id,id),
        CHECK(task_id IS NULL OR human_task_id IS NULL),
        FOREIGN KEY({key},human_task_id) REFERENCES human_tasks({key},id) ON DELETE SET NULL(human_task_id),
        FOREIGN KEY({key},task_id) REFERENCES task_intents({key},id) ON DELETE SET NULL(task_id),
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id) ON DELETE SET NULL(run_id))""")
    op.execute(f"""CREATE TABLE weave_file_chunks(
        {scope},file_id uuid NOT NULL,chunk_index integer NOT NULL CHECK(chunk_index BETWEEN 0 AND 99),
        content bytea NOT NULL CHECK(octet_length(content) BETWEEN 1 AND 262144),
        PRIMARY KEY(file_id,chunk_index),FOREIGN KEY({key},file_id) REFERENCES weave_files({key},id))""")
    op.execute(f"""CREATE TABLE weave_run_files(
        {scope},run_id uuid NOT NULL,file_id uuid NOT NULL,PRIMARY KEY(run_id,file_id),
        FOREIGN KEY({key},run_id) REFERENCES runs({key},id) ON DELETE CASCADE,
        FOREIGN KEY({key},file_id) REFERENCES weave_files({key},id))""")
    condition = (
        "tenant_id=nullif(current_setting('weave.tenant_id',true),'')::uuid AND "
        "project_id=nullif(current_setting('weave.project_id',true),'')::uuid AND "
        "environment_id=nullif(current_setting('weave.environment_id',true),'')::uuid"
    )
    for table in ("weave_files", "weave_file_chunks", "weave_run_files"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY exact_scope ON {table} TO weave_app USING ({condition}) WITH CHECK ({condition})")
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON {table} TO weave_app")
    op.execute("GRANT DELETE ON weave_file_chunks TO weave_app")
    op.execute("UPDATE weave_schema_version SET version='0028_files'")


def downgrade():
    raise RuntimeError("Destructive downgrades are not supported")
