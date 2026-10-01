<!--
Copyright 2026 Firefly Software Foundation.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->
# e2-inbox-fixture

This local read-only echo example demonstrates a trusted native connector.
It does not implement or certify an external provider.

- Edit `connector.json`, then copy the reviewed declaration into `src/e2_inbox_fixture/connector.json`.
- Validate data with `weave connector validate connector.json` (no package code runs).
- Build with `weave connector package . --directory dist` in an author-controlled environment containing `build`.
- Install the wheel and the matching Weave/PyFly artifacts into an isolated environment.
- Run `pytest tests/test_conformance.py`, then `weave connector test 'e2-inbox-fixture:e2-inbox-fixture:e2_inbox_fixture:package'`.
- Configure that exact identity in the operator's `WEAVE_CONNECTOR_PACKAGES` JSON array to enable it.

`connector test` executes installed trusted declaration, lifecycle, and fixture code, records
`installed-fixture-contract`, and does not call a real provider. Add explicit
fixture tests for protected headers, credential redaction, cancellation, response
bounds and unknown outcomes when replacing echo with an external operation.
Never change an uncertain external outcome into a safe automatic retry.


## Read the fixture's boundaries

![Installed package, immutable definition, and environment admission layers](../../../docs/diagrams/integrations-admission.svg)

**How to read this diagram:** This directory supplies the first layer's test
package. Publishing its Connector, creating a connection, and binding an admitted
release remain separate test-harness or deployment operations.

The package contains two native services with different responsibilities:

| Component | What it exercises | Boundary |
| --- | --- | --- |
| `Echo` in [the installed module](src/e2_inbox_fixture/__init__.py) | Bounded read-only Action input/output and native composition | It returns local JSON and performs no provider send |
| `FixtureVerifier` in that module | Test-only HMAC verification, configured account matching, normalized events, and a transactional admission hook | Its protocol and failure switches exist for the controlled provider-inbox tests |
| [provider_verifier.py](examples/provider_verifier.py) | A fail-closed design skeleton for future provider authoring | It is excluded from installed service discovery and raises `NotImplementedError` |

The root [connector.json](connector.json) pins `FixtureVerifier` and declares
`message` and `secret` as dispatchable kinds. Lifecycle kinds exercise ignored
admission; the secret-marked kind exercises classification rejection. None of
these names advertise a commercial provider. The registered test verifier and
the unregistered design skeleton are distinct implementations.

Run the commands above from this directory in the isolated test environment.
The package build output must be absent or empty. The echo conformance command
checks installed trusted code; it does not by itself exercise a running inbox,
its database transaction, or a real provider. The integration harness supplies
its disposable database, fixture effects table, authority, and source setup.
Do not copy the challenge token, signature protocol, or failure switches into
a production provider adapter.

For a production package, start with [connector authoring](../../../docs/connectors/authoring.md)
and the [provider-source contract](../../../docs/reference/provider-sources.md).
Implement and verify the provider's actual raw-byte authentication, installation
policy, stable event identity, schemas, and admission behavior before enabling it.
