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

# Weave Studio desktop

Choose a matching installer **actually attached** to the
[v0.1.0a6 release](https://github.com/fireflyframework/firefly-weave/releases/tag/v0.1.0a6).
The desktop product version is `0.1.0-alpha.6`; its bundled Python host is
`0.1.0a6`. macOS bundles use **ad-hoc signing** for resource integrity; they are
**not Developer ID signed and not notarized**. Windows installers remain unsigned.
Build and frozen-host smoke checks do not establish interactive GUI testing on
every operating system. Your organization's installation policy still applies.

| Computer | Target | Installer filename |
| --- | --- | --- |
| macOS Apple silicon | `aarch64-apple-darwin` | `firefly-weave-studio-0.1.0-alpha.6-aarch64-apple-darwin.dmg` |
| macOS Intel | `x86_64-apple-darwin` | `firefly-weave-studio-0.1.0-alpha.6-x86_64-apple-darwin.dmg` |
| Windows x64 | `x86_64-pc-windows-msvc` | `firefly-weave-studio-0.1.0-alpha.6-x86_64-pc-windows-msvc.exe` or `.msi` |
| Linux x64 | `x86_64-unknown-linux-gnu` | `firefly-weave-studio-0.1.0-alpha.6-x86_64-unknown-linux-gnu.deb` or `.AppImage` |

Download the chosen installer and its target's checksum inventory,
`weave-studio-TARGET-SHA256SUMS`, from that same release. The matrix describes
filenames, not a promise that every asset is already available. If your matching
asset is absent, use the [browser installation](studio.md#install-the-alpha6-browser-application)
instead. Alpha4 Python releases do not contain Studio or desktop installers.

**Do not use the alpha5 macOS installers.** Their outer application bundle lacked
its resource seal, causing macOS to report that the application was damaged.
Alpha6 adds explicit ad-hoc bundle signing and a strict verification gate for both
the built application and the application enclosed in its DMG. Download the
matching alpha6 asset rather than attempting to bypass the alpha5 failure.
Ad-hoc integrity does not establish publisher trust or Gatekeeper acceptance.

## What is packaged

The application embeds the compiled Angular Studio and a frozen Python host.
End users do not install Python or Node.js. Tauri uses the operating system's
WebView; Windows may install WebView2 through its bundled bootstrapper, and Linux
requires the distribution's WebKitGTK runtime. See the official
[Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) for platform details.

The browser content always comes from an owned `127.0.0.1` host on a random port.
Remote platforms are accessed through the existing narrow Studio bridge; the
native WebView never navigates to the remote API or identity provider. API tokens
stay in the Python host and credential store. The native shell grants no web-content
filesystem, shell or dialog commands. Pairing is performed privately at the exact
owned origin, without placing a pairing code in a URL or writing tokens to logs.
This follows Tauri's [sidecar model](https://v2.tauri.app/develop/sidecar/) and
[capability boundary](https://v2.tauri.app/security/capabilities/).

## Install on macOS

1. Open **Apple menu → About This Mac**. Choose the Apple silicon DMG for an Apple
   chip, or the Intel DMG for an Intel processor.
2. Download the DMG and matching `weave-studio-TARGET-SHA256SUMS` release asset.
3. In Terminal, enter the download directory and calculate the file's checksum.
   For Apple silicon:

   ```bash
   # Inspect the downloaded installer without opening it.
   cd ~/Downloads
   shasum -a 256 firefly-weave-studio-0.1.0-alpha.6-aarch64-apple-darwin.dmg
   # Show the expected digest for this exact filename.
   grep 'firefly-weave-studio-0.1.0-alpha.6-aarch64-apple-darwin.dmg$' weave-studio-aarch64-apple-darwin-SHA256SUMS
   ```

   Both displayed digests must match. For Intel, replace `aarch64-apple-darwin`
   with `x86_64-apple-darwin` in both filenames. Stop if the digest differs.
4. Open the verified DMG, then drag **Firefly Weave Studio** on the left into
   **Applications** on the right. The cream and forest installer shows the
   destination and a gold arrow.
5. After copying, verify the installed bundle’s resource integrity in Terminal:

   ```bash
   # Check the copied app without launching it. Stop if verification fails.
   codesign --verify --deep --strict --verbose=4 "/Applications/Firefly Weave Studio.app"
   ```

6. Open Studio from Applications after verification succeeds, then eject the
   installer volume. The app is ad-hoc signed, not Developer ID signed, and not notarized.
   macOS may still require manual approval allowed by your organization’s policy.
   If that policy does not permit it, use the browser installation. Do not remove
   quarantine or disable system protections to bypass a refusal.

The background is maintained as `desktop/artwork/dmg-background.svg` with its
Finder-compatible PNG alongside it. Tauri's DMG settings define the window and
icon positions; the artwork does not replace the actual app or Applications icons.

## Install on Windows

Choose the x64 **EXE** for an interactive setup, or **MSI** when your organization
uses Windows Installer deployment. Install one format, not both. Download its
matching `weave-studio-x86_64-pc-windows-msvc-SHA256SUMS` inventory first.

```powershell
# Inspect the downloaded EXE before running it.
Set-Location "$HOME\Downloads"
Get-FileHash .\firefly-weave-studio-0.1.0-alpha.6-x86_64-pc-windows-msvc.exe -Algorithm SHA256
# Compare the digest with the line for this exact filename.
Select-String -Path .\weave-studio-x86_64-pc-windows-msvc-SHA256SUMS -Pattern 'firefly-weave-studio-0.1.0-alpha.6-x86_64-pc-windows-msvc.exe$'
```

For MSI, substitute `.msi` in both commands. Compare digests without regard to
letter case; stop on a mismatch. Double-click the verified installer, complete its
setup, then open **Firefly Weave Studio** from the Start menu. The installer may
need network access to provision WebView2. Unsigned-installation warnings remain
subject to your organization's policy. The MSI's internal version is `0.1.6`, a
monotonic Windows Installer counter; the displayed product remains alpha6.

## Install on Linux

Choose the x64 **DEB** for a Debian/Ubuntu system, or **AppImage** for a portable
application where your distribution supports its runtime. Download the matching
`weave-studio-x86_64-unknown-linux-gnu-SHA256SUMS` inventory.

```bash
# Enter the download directory, verify this exact file, then install only on success.
cd ~/Downloads &&
  grep 'firefly-weave-studio-0.1.0-alpha.6-x86_64-unknown-linux-gnu.deb$' weave-studio-x86_64-unknown-linux-gnu-SHA256SUMS | sha256sum --check &&
  sudo apt install ./firefly-weave-studio-0.1.0-alpha.6-x86_64-unknown-linux-gnu.deb
```

Open **Firefly Weave Studio** from your desktop application launcher. For AppImage:

```bash
# Verify this exact portable file; a failed or missing checksum prevents launch.
cd ~/Downloads &&
  grep 'firefly-weave-studio-0.1.0-alpha.6-x86_64-unknown-linux-gnu.AppImage$' weave-studio-x86_64-unknown-linux-gnu-SHA256SUMS | sha256sum --check &&
  chmod +x firefly-weave-studio-0.1.0-alpha.6-x86_64-unknown-linux-gnu.AppImage &&
  ./firefly-weave-studio-0.1.0-alpha.6-x86_64-unknown-linux-gnu.AppImage
```

Linux requires the distribution's WebKitGTK runtime, and AppImage execution may
require its FUSE compatibility package. If the launcher reports a missing runtime,
use the distribution-supported package installation or the browser fallback.
The connection assistant also needs an unlocked Secret Service/keyring; authoring
without a platform connection does not need sign-in.

## First launch and connection

1. Start in **Home**, with **Not connected to a platform** explaining what works on your computer. Create a workflow
   or import an explicit YAML/JSON file; connecting is optional for authoring.
2. Choose **Home → Connect a workspace** or **Settings → Connect a workspace** to open the connection assistant. Import or enter the
   administrator-provided nonsecret OAuth login configuration, verify the exact
   API and identity-provider origins, then explicitly confirm trust. Credentials
   and tokens stay in the host's native credential store.
3. Start sign-in. The provider determines whether the flow uses device authorization
   or a browser PKCE callback. The native app opens the trusted sign-in address in
   your default browser. Follow the sign-in URI and displayed device code
   when applicable; a copyable URI is available when browser opening is unavailable.
   Cancel sign-in in the assistant if needed; closing the host also cancels its flow.
4. Test the connection and choose a workspace, project and environment from the
   authorized list. API actions use the current identity and permissions.

For a saved profile, **Studio → Choose platform profile…** is an alternative
native entry point. Choose the nonsecret profile generated by `weave studio configure`;
export unsaved drafts before changing workspaces. **Studio → Sign in to selected
platform…** supports device authorization and opens the trusted URI in the default
browser. Use **View → Refresh workspace…** afterward. Providers without device
support can use the connection assistant's PKCE flow.

**Studio → Open offline authoring** opens a fresh offline workspace. Profile selection
is not persisted automatically; explicitly reconnect after a new launch.
Closing the native application terminates only its own host/login children. A
closed parent control pipe also stops an orphaned host. Submitted API changes may
already have completed; reconcile server state before retrying an interrupted request.
Linux users need a system Secret Service/keyring for the assistant's native store;
existing saved profiles can explicitly select the private file credential store.

## Build from source

Build on the target operating system and architecture: PyInstaller does not
cross-freeze the host. Install Rust, Node 24 and uv, plus the platform prerequisites
linked above. From the repository root:

```bash
uv sync --locked --extra studio # Install the locked host and Studio dependencies.
uv pip install pyinstaller==6.16.0 # Freeze Python so users need no Python installation.
cd studio # Build the browser assets embedded in the host.
npm ci # Install the exact frontend or desktop tool lockfile.
npm run build # Produce assets or native installers for this operating system.
cd .. # Return to the repository root for host packaging.
uv run --no-sync python desktop/scripts/build_sidecar.py # Freeze the current native host.
```

Verify the frozen binary, replacing TARGET with `rustc -vV`'s host triple
(and adding `.exe` on Windows):

```bash
uv run --no-sync python desktop/scripts/smoke_sidecar.py \
  desktop/src-tauri/binaries/weave-studio-host-TARGET # Verify readiness, pairing and shutdown.
cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml # Verify the native origin policy.
cd desktop # Build the native shell after checking its host.
npm ci # Install the exact frontend or desktop tool lockfile.
npm run build # Produce assets or native installers for this operating system.
```

The standalone smoke test checks ready-only startup, packaged assets, pairing,
compiler availability and owned shutdown. Build outputs are under
`desktop/src-tauri/target/release/bundle`; they are ignored, not source artifacts.
The official Weave symbol is used for application icons.

## Platform artifacts and signing

The Desktop installers GitHub Actions workflow builds each host natively:

| Target | Runner | Installer artifacts |
|---|---|---|
| Apple Silicon macOS | `macos-15` | `.app`, `.dmg` |
| Intel macOS | `macos-15-intel` | `.app`, `.dmg` |
| Windows x64 | `windows-2022` | NSIS `.exe`, WiX `.msi` |
| Linux x64 | `ubuntu-22.04` | `.deb`, `.AppImage` |

Artifacts are retained per workflow run and are not automatically released.
macOS bundles are ad-hoc signed; Windows remains unsigned. A workflow artifact
label containing `unsigned` is not a statement that the macOS resource seal is
absent. Windows/macOS/Linux matrix outcomes must be checked
before claiming those installers work. Building against Ubuntu 22.04 sets a concrete
Linux baseline; an AppImage does not imply compatibility with every distribution.

The macOS integrity gate verifies the outer resource seal and both native
executables using `codesign --verify --deep --strict --verbose=4`. It checks
`Contents/_CodeSignature/CodeResources` and repeats verification against the app
inside a read-only DMG mount before collecting release assets. From a matching
source checkout, you can run that same gate on an explicitly downloaded DMG:

```bash
# Verify resource integrity without launching the application.
# Replace the path with the actual alpha6 installer you downloaded.
.venv/bin/python desktop/scripts/verify_macos_bundle.py /path/to/firefly-weave-studio-0.1.0-alpha.6-aarch64-apple-darwin.dmg
```

The build also smoke-tests the frozen host after Tauri signs it. Hardened runtime
remains enabled, with one library-validation exception so the PyInstaller host can
load its bundled Python library and extensions without an Apple Team ID. The
exception grants no native webview IPC and does not bypass Gatekeeper.

This is a packaging-integrity check, not a Gatekeeper or notarization test. A
source checkout is needed only for this contributor verification command; ordinary
installation follows the checksum and platform steps above.

For a production macOS release, configure the application and sidecar signing
identity plus notarization credentials through the release environment, following
[Tauri macOS signing](https://v2.tauri.app/distribute/sign/macos/).
`APPLE_SIGNING_IDENTITY` is passed to PyInstaller for the frozen host as well as
Tauri's own signing configuration. Use Tauri's documented Apple certificate,
password and notarization environment variables rather than committing credentials.
For Windows, configure a trusted signing certificate or an approved signing service
following [Tauri Windows signing](https://v2.tauri.app/distribute/sign/windows/).
Developer ID signing, Windows publisher signing, and notarization have not been
performed or verified by this implementation. Ad-hoc signing only protects bundle
integrity; it does not make a CI artifact a trusted production download.
