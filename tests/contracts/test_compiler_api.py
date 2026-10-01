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

"""HTTP representation preserves the canonical pure compiler result."""

import importlib
import json

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot

pytestmark = pytest.mark.integration


def test_compile_wire_result_is_canonical():
    module = importlib.import_module("firefly_weave.api.compiler")
    result = compile_source("kind: Unknown", format="yaml", catalog=CatalogSnapshot.empty())
    assert module.compile_payload(result) == json.loads(result.to_bytes())
