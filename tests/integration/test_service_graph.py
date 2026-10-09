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

"""Constructor order and guarded native PostgreSQL graph acceptance."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from firefly_weave.app import make_app
from firefly_weave.connections.service import ConnectionService
from firefly_weave.definitions.ports import ConnectionBindingPort, WorkerAdmissionPort
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.workers.service import WorkerService


@pytest.mark.parametrize("first", [DefinitionService, ConnectionService, WorkerService])
def test_deferred_ports_allow_each_resolution_order(services, first):
    graph = services(async_sessionmaker())
    initial = graph.resolve(first)
    definitions = graph.resolve(DefinitionService)
    connections = graph.resolve(ConnectionService)
    workers = graph.resolve(WorkerService)
    assert graph.resolve(first) is initial
    assert definitions.connections.get() is graph.resolve(ConnectionBindingPort) is connections
    assert definitions.workers.get() is graph.resolve(WorkerAdmissionPort) is workers
    assert workers.definitions is connections.definitions is definitions
    assert workers.connections is connections


@pytest.mark.integration
async def test_real_database_native_service_graph(settings):
    from firefly_weave.access.authentication import AuthenticationFilter, AuthenticationService
    from firefly_weave.access.authorization import AuthorizationService
    from firefly_weave.access.service import AccessService
    from firefly_weave.api.workers import WorkerController
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.workers.leases import TaskService

    app = make_app(settings)
    async with app.router.lifespan_context(app):
        context = app.state.pyfly.context
        definitions = context.get_bean(DefinitionService)
        tasks = context.get_bean(TaskService)
        assert tasks is context.get_bean(WorkerController).tasks
        assert tasks.workers is context.get_bean(WorkerService) is definitions.workers.get()
        assert tasks.runtime is context.get_bean(RuntimeService)
        assert tasks.connections is context.get_bean(ConnectionService) is definitions.connections.get()
        assert tasks.runtime.definitions is tasks.workers.definitions is definitions
        access = context.get_bean(AccessService)
        assert access.authorization is definitions.authorization is context.get_bean(AuthorizationService)
        assert access.uow is definitions.uow is tasks.connections.uow is context.get_bean(UnitOfWork)
        assert context.get_bean(AuthenticationFilter).authentication is context.get_bean(AuthenticationService)
        from firefly_weave.api.operations import OperationsController
        from firefly_weave.api.runs import RunController
        from firefly_weave.operations.history import HistoryService
        from firefly_weave.operations.incidents import IncidentService
        from firefly_weave.runtime.deadlines import DeadlineService
        from firefly_weave.runtime.recovery import RecoveryService
        from firefly_weave.runtime.signals import SignalService

        for cls in (SignalService, DeadlineService, RecoveryService, IncidentService, HistoryService):
            assert context.get_bean(cls) is context.get_bean(cls)
            assert context.container.get_registration(cls).factory is None
            assert cls.__pyfly_stereotype__ == "service"
        assert context.get_bean(RunController).signals is context.get_bean(SignalService)
        assert context.get_bean(RecoveryService).tasks is tasks
        assert context.get_bean(RecoveryService).deadlines is context.get_bean(DeadlineService)
        incidents = context.get_bean(IncidentService)
        assert context.get_bean(OperationsController).incidents is incidents
        assert incidents.runtime is tasks.runtime and incidents.tasks is tasks and incidents.access is access
        history = context.get_bean(HistoryService)
        assert context.get_bean(OperationsController).history is history
        assert history.runtime is tasks.runtime and history.access is access
        from firefly_weave.api.ai import AIController
        from firefly_weave.connections.registry import ConnectorRegistry
        from firefly_weave.operations.ai_connections import AIConnectionService
        from firefly_weave.operations.lumi_gateway import LumiGatewayClient

        # Generic and dedicated AI connection tests reach one gateway and take slots from one limit.
        ai = context.get_bean(AIConnectionService)
        assert context.get_bean(AIController).service is ai and ai.connections is tasks.connections
        assert tasks.connections.test_admission == ai.admit
        tester = context.get_bean(ConnectorRegistry).get("weave-agentic-provider").tester
        assert tester.gateway is ai.gateway is context.get_bean(LumiGatewayClient)
        assert await app.state.resources.is_ready()
