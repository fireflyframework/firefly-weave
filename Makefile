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

.PHONY: check source test lint type build check-integration check-e2e check-release

check:
	uv run --locked --no-editable --all-extras python scripts/check.py

source:
	python3 scripts/source_coverage.py --strict

test:
	uv run --locked --no-editable --all-extras pytest tests/unit tests/contracts

lint:
	uv run --locked --no-editable --all-extras ruff check src tests examples scripts
	uv run --locked --no-editable --all-extras ruff format --check src tests examples scripts

type:
	uv run --locked --no-editable --all-extras mypy

build:
	uv build

check-integration:
	uv run --locked --no-editable --all-extras python scripts/check.py --integration

check-e2e:
	uv run pytest tests/e2e -q -m e2e --tb=short --show-capture=no

check-release:
	uv run --locked --no-editable --all-extras python scripts/check.py --release --docker-context "$(WEAVE_TEST_DOCKER_CONTEXT)"
