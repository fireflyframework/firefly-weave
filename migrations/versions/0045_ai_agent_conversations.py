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

"""Conversation memory for AI agents: what an agent remembers across runs, and the loads that read it.

``agent_conversations`` holds one conversation per environment, workflow and key: the remembered exchanges (each
prompt scrubbed, each answer redacted before it is written), a version that each append increments, the last run that
appended, and when it expires. The key is never stored: ``key_hash`` is the SHA-256 of the workflow's name, a zero
byte and the key's value. ``last_run_id`` has no foreign key, so purging a run never touches a conversation.

``agent_memory_loads`` records each load an agent asks for: the recovery scan delivers the pending ones (its partial
index is the scan's marker), the delivered row is the provenance replay checks, and it answers which runs loaded a
conversation when one is deleted. Loads go with their run (a purge cascades), and both tables go with their
environment.

Both are scoped by row-level security and charged to usage. No role is added, and ``runs`` and ``weave_purge_run`` are
unchanged.
"""

from alembic import op

revision = "0045_ai_agent_conversations"
down_revision = "0044_editor_trigger_retarget"

KEY = "tenant_id,project_id,environment_id"
SCOPE_COLUMNS = "tenant_id uuid NOT NULL,project_id uuid NOT NULL,environment_id uuid NOT NULL"
ENVIRONMENT = f"FOREIGN KEY({KEY}) REFERENCES environments(tenant_id,project_id,id) ON DELETE CASCADE"
EXACT = " AND ".join(f"{name}=nullif(current_setting('weave.{name}',true),'')::uuid" for name in KEY.split(","))
HASHED = "key_hash text NOT NULL CHECK(key_hash ~ '^[0-9a-f]{64}$')"
WORKFLOW = "workflow text NOT NULL CHECK(char_length(workflow) BETWEEN 1 AND 200)"


def secure(table: str, keys: tuple[str, ...], grants: str) -> None:
    """Scope ``table`` to the session's environment and charge each row to usage under its primary key ``keys``."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY exact_scope ON {table} TO weave_app USING ({EXACT}) WITH CHECK ({EXACT})")
    op.execute(f"GRANT {grants} ON {table} TO weave_app")
    arguments = ",".join(f"'{key}'" for key in keys)
    op.execute(
        f"CREATE TRIGGER operation_usage AFTER INSERT OR UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION weave_usage_charge({arguments})"
    )


def conversations() -> None:
    """One row per conversation, read by key and listed newest update first; the sweep reads by expiry."""
    # The platform keeps at most 256 KiB of compact JSON per conversation; jsonb's text form adds a space after each
    # colon and comma (at most 1.5 times the compact size), so the row's ceiling is a backstop well above it.
    op.execute(f"""CREATE TABLE agent_conversations(
        {SCOPE_COLUMNS},{WORKFLOW},{HASHED},
        messages jsonb NOT NULL CHECK(jsonb_typeof(messages)='array' AND jsonb_array_length(messages)<=200
          AND octet_length(messages::text)<=1048576),
        version bigint NOT NULL CHECK(version>0),last_run_id uuid NOT NULL,
        created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL,expires_at timestamptz NOT NULL,
        CHECK(expires_at>updated_at),
        PRIMARY KEY({KEY},workflow,key_hash),{ENVIRONMENT})""")
    op.execute(f"CREATE INDEX agent_conversations_expiry ON agent_conversations({KEY},expires_at)")
    # The listing's order, newest update first, with and without a workflow filter: each page walks an index.
    op.execute(
        f"CREATE INDEX agent_conversations_recent ON agent_conversations({KEY},workflow,updated_at DESC,key_hash DESC)"
    )
    op.execute(
        f"CREATE INDEX agent_conversations_updated ON agent_conversations({KEY},updated_at DESC,workflow DESC,"
        "key_hash DESC)"
    )
    secure(
        "agent_conversations",
        ("tenant_id", "project_id", "environment_id", "workflow", "key_hash"),
        "SELECT,INSERT,UPDATE(messages,version,last_run_id,updated_at,expires_at),DELETE",
    )


def loads() -> None:
    """One row per load an agent asked for; pending until the recovery scan delivers it or drops it."""
    op.execute(f"""CREATE TABLE agent_memory_loads(
        {SCOPE_COLUMNS},id uuid NOT NULL,run_id uuid NOT NULL,
        node_id text NOT NULL CHECK(char_length(node_id) BETWEEN 1 AND 512),
        issued_sequence bigint NOT NULL CHECK(issued_sequence>0),{WORKFLOW},{HASHED},
        requested_at timestamptz NOT NULL,delivered_at timestamptz,version bigint CHECK(version>=0),
        expired boolean,test boolean,dropped boolean NOT NULL DEFAULT false,
        PRIMARY KEY({KEY},id),UNIQUE(run_id,node_id,issued_sequence),
        FOREIGN KEY({KEY},run_id) REFERENCES runs({KEY},id) ON DELETE CASCADE)""")
    # The recovery scan's marker: a run with a load still to deliver.
    op.execute("CREATE INDEX agent_memory_loads_pending ON agent_memory_loads(run_id) WHERE delivered_at IS NULL")
    # The runs that loaded one conversation, for a delete's answer.
    op.execute(f"CREATE INDEX agent_memory_loads_conversation ON agent_memory_loads({KEY},workflow,key_hash,run_id)")
    secure(
        "agent_memory_loads",
        ("tenant_id", "project_id", "environment_id", "id"),
        "SELECT,INSERT,UPDATE(delivered_at,version,expired,test,dropped)",
    )


def upgrade() -> None:
    conversations()
    loads()
    # Fresh tables get statistics at once, so the first scans plan with the indexes above.
    for table in ("agent_conversations", "agent_memory_loads"):
        op.execute(f"ANALYZE {table}")
    op.execute(f"UPDATE weave_schema_version SET version='{revision}'")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are not supported")
