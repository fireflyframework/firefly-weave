# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Owned local mail fixtures verify validation and durable boundaries."""

import pytest

from firefly_weave.connectors.email import EmailConnector
from firefly_weave.contracts.connectors import ConnectorFailure


async def test_connector_rejects_unfenced_context():
    from types import SimpleNamespace

    with pytest.raises(ConnectorFailure) as caught:
        await EmailConnector().execute({"text": "hi"}, SimpleNamespace(email_submit=None))
    assert caught.value.outcome == "not_started"
