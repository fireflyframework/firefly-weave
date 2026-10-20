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
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS base
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/weave
COPY --chmod=0644 *.whl *-requirements.txt Dockerfile worker.Dockerfile image_identity.py main.py manifest.json operations-worker.py operations-worker-manifest.json LICENSE NOTICE release.json /opt/weave/
RUN python image_identity.py --verify-only
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN uv pip install --system --no-cache --require-hashes -r worker-requirements.txt \
 && uv pip install --system --no-cache --no-deps /opt/weave/firefly_weave-*.whl \
 && python image_identity.py \
 && mkdir -p /opt/weave/examples/worker \
 && cp main.py /opt/weave/examples/worker/main.py
USER 65532:65532
CMD ["python", "-I", "/opt/weave/examples/worker/main.py"]

# The sleep fixture worker the local platform runs for Operations.
FROM base AS operations-worker
USER root
RUN mkdir -p /opt/weave/operations-worker \
 && cp /opt/weave/operations-worker.py /opt/weave/operations-worker/main.py \
 && cp /opt/weave/operations-worker-manifest.json /opt/weave/operations-worker/operations-worker-manifest.json
LABEL org.opencontainers.image.title="Weave sleep fixture worker"
USER 65532:65532
CMD ["python", "-I", "/opt/weave/operations-worker/main.py"]
