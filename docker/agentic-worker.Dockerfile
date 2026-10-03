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

FROM ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 AS uv
FROM python:3.13-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/weave
COPY --chmod=0644 . /opt/weave/
RUN python image_identity.py --verify-only
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN uv pip install --system --no-cache --require-hashes -r agentic-worker-requirements.txt \
 && uv pip install --system --no-cache --no-deps /opt/weave/*.whl \
 && python image_identity.py
USER 65532:65532
ENTRYPOINT ["weave-agentic-worker"]
