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

"""Live checks against a real Ollama; skipped unless WEAVE_LIVE_OLLAMA_URL names one.

WEAVE_LIVE_OLLAMA_URL is the endpoint's origin, such as http://127.0.0.1:11434, with an IP address host. Start
Ollama with OLLAMA_CONTEXT_LENGTH=32768 (WEAVE_LIVE_OLLAMA_CONTEXT if different). WEAVE_LIVE_OLLAMA_MODELS lists the
models to evaluate, comma separated (qwen3:4b by default); each must already be pulled, since a check never pulls.
"""

import os

import pytest

URL = "WEAVE_LIVE_OLLAMA_URL"


@pytest.fixture
def ollama_url() -> str:
    url = os.environ.get(URL, "")
    if not url:
        pytest.skip(f"Set {URL} to run the live Ollama checks.")
    return url.rstrip("/")


@pytest.fixture
def ollama_models() -> list[str]:
    return [name.strip() for name in os.environ.get("WEAVE_LIVE_OLLAMA_MODELS", "qwen3:4b").split(",") if name.strip()]


@pytest.fixture
def ollama_context() -> int:
    return int(os.environ.get("WEAVE_LIVE_OLLAMA_CONTEXT", "32768"))
