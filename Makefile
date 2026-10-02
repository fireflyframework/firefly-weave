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

.PHONY: check source test lint type build docs docs-serve check-integration check-e2e check-release

check:
	uv run --locked --no-editable --reinstall-package firefly-weave --all-extras --group docs python scripts/check.py

source:
	python3 scripts/source_coverage.py --strict

test:
	uv run --locked --no-editable --reinstall-package firefly-weave --all-extras --group docs pytest tests/unit tests/contracts

lint:
	uv run --locked --no-editable --reinstall-package firefly-weave --all-extras ruff check src tests examples scripts desktop/scripts
	uv run --locked --no-editable --reinstall-package firefly-weave --all-extras ruff format --check src tests examples scripts desktop/scripts

type:
	uv run --locked --no-editable --reinstall-package firefly-weave --all-extras mypy

build:
	uv build

docs:
	uv run --locked --group docs --extra openapi mkdocs build --strict

docs-serve:
	uv run --locked --group docs --extra openapi mkdocs serve --dev-addr 127.0.0.1:8000

check-integration:
	uv run --locked --no-editable --reinstall-package firefly-weave --all-extras python scripts/check.py --integration

check-e2e:
	uv run pytest tests/e2e -q -m e2e --tb=short --show-capture=no

check-release:
	uv run --locked --no-editable --reinstall-package firefly-weave --all-extras --group docs python scripts/check.py --release --docker-context "$(WEAVE_TEST_DOCKER_CONTEXT)"

# Build Studio separately so Python-only users do not need Node.js.
.PHONY: studio-check studio-build
studio-check:
	cd studio && npm ci && npm run check && npm test && npm run test:browser

studio-build:
	cd studio && npm ci && npm run build
	uv run --locked python scripts/build_studio.py
