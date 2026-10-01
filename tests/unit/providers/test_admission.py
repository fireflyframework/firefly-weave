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
"""Whole-batch semantic classification precedes transactional extension effects."""

import importlib

import pytest

from firefly_weave.contracts.providers import ProviderEvent
from firefly_weave.definitions.models import CatalogError


def classify():
    return importlib.import_module("firefly_weave.providers.admission").classify


def event(identity, value=1):
    return ProviderEvent(event_id=identity, kind="message", payload={"value": value})


def test_only_new_semantic_events_reach_hook():
    old, new = event("old"), event("new")
    assert classify()((old, new, new), {(old.kind, old.event_id): old.fingerprint}) == (new,)


def test_conflict_rejects_whole_batch_before_new_events():
    with pytest.raises(CatalogError) as conflict:
        classify()((event("new"), event("old", 2)), {("message", "old"): event("old").fingerprint})
    assert conflict.value.status == 409
    with pytest.raises(CatalogError):
        classify()((event("new"), event("new", 2)), {})


def test_normalized_batch_limit_is_absolute():
    with pytest.raises(CatalogError) as too_large:
        classify()(tuple(event(str(i)) for i in range(101)), {})
    assert too_large.value.status == 413
