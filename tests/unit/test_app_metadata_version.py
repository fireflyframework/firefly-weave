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

"""The application reports the package version, not a stale literal."""

import ast
from pathlib import Path

import firefly_weave

SOURCE = Path(firefly_weave.__file__).parent / "app.py"


def test_application_version_is_the_package_version():
    tree = ast.parse(SOURCE.read_text())
    decorators = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "pyfly_application"
    ]
    assert len(decorators) == 1
    version = next(keyword.value for keyword in decorators[0].keywords if keyword.arg == "version")
    assert isinstance(version, ast.Name) and version.id == "__version__"
