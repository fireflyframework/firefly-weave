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

"""Canonical capabilities. Provider role names never participate in authorization."""

from types import MappingProxyType

ROLE_CAPABILITIES = MappingProxyType(
    {
        "platform_admin": frozenset({"provider.manage", "tenant.create", "grant.admin"}),
        "tenant_admin": frozenset({"project.manage", "environment.manage", "grant.manage", "connection.manage"}),
        "developer": frozenset({"definition.write", "definition.publish", "compile", "simulate", "catalog.read"}),
        "deployer": frozenset(
            {"release.activate", "release.retire", "connection.bind", "trigger.manage", "subscription.manage"}
        ),
        "operator": frozenset(
            {
                "retention.plan",
                "retention.apply",
                "compatibility.check",
                "run.start",
                "run.signal",
                "run.cancel",
                "run.pause",
                "run.resume",
                "run.archive",
                "incident.read",
                "incident.resolve",
                "run.retry",
                "delivery.read",
                "delivery.retry",
            }
        ),
        "viewer": frozenset({"catalog.read", "run.read", "status.read", "delivery.read"}),
        # Business decisions and message content require purpose-specific grants.
        "task_participant": frozenset(
            {"human_task.read", "human_task.claim", "human_task.release", "human_task.complete", "assignment.read"}
        ),
        "task_manager": frozenset({"human_task.read", "human_task.manage", "assignment.read", "assignment.manage"}),
        "email_reader": frozenset({"email.read"}),
        "email_sender": frozenset({"email.read", "email.send"}),
        "email_manager": frozenset({"email.read", "email.send", "email.manage"}),
        "execution_manager": frozenset({"run.read", "run.archive", "run.purge"}),
        "lumi_user": frozenset({"lumi.use"}),
        "lumi_manager": frozenset({"lumi.use", "lumi.manage"}),
        "file_reader": frozenset({"file.read"}),
        "file_manager": frozenset({"file.read", "file.manage"}),
        "deployment_reader": frozenset({"deployment.read"}),
        "deployment_planner": frozenset({"deployment.read", "target.manage", "deployment.plan"}),
        "deployment_approver": frozenset({"deployment.read", "deployment.approve"}),
        "deployment_operator": frozenset({"deployment.read", "deployment.apply", "deployment.cancel"}),
        "deployment_runner": frozenset({"runner.register", "runner.claim", "runner.renew", "runner.report"}),
        "worker_operator": frozenset({"status.read", "worker.drain"}),
        "worker": frozenset({"worker.register", "task.claim", "task.heartbeat", "task.complete", "credential.lease"}),
    }
)
