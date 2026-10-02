<!--
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
-->
# Desktop release versions

The alpha5 product version is `0.1.0-alpha.5` in npm, Cargo and Tauri. It corresponds
to Python `0.1.0a5` and the repository prerelease tag `v0.1.0a5`.

Windows MSI uses the explicit numeric version `0.1.5`. Tauri's pinned bundler rejects
nonnumeric prerelease identifiers when deriving MSI versions; its `windows.wix.version`
override supplies the valid numeric installer version while filenames retain the product
version. MSI compares only the first three fields. Future desktop alpha and stable
releases must advance this numeric installer counter: the stable product `0.1.0`
must not reset the MSI counter to `0.1.0`. The macOS internal bundle version also uses
numeric `0.1.5`; its displayed product version remains the alpha product version.

The Desktop installers workflow builds on four native runners and is dispatchable by
an exact branch/tag ref. Versioned, target-specific unsigned artifacts contain only
installers named `firefly-weave-studio-PRODUCT_VERSION-TARGET.EXT` plus target-specific build provenance, host manifest and SHA256SUMS.
The provenance records the product version, Python version and exact Git commit.

After every matrix job passes, download artifacts from that same workflow run, verify
the target checksum files, and attach the installers and provenance to the matching
GitHub prerelease. Never combine outputs from different commits or claim that a
platform installer exists before its corresponding job and upload succeed. The workflow
has read-only repository permissions and does not create or publish releases itself.
Signing and notarization remain separate release verification requirements.
