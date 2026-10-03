/*
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
*/
// Single seam for files Studio hands to the person. Browsers and the desktop
// shell both receive a blob download; the desktop shell saves it to the
// Downloads folder itself and shows a native dialog when saving fails. Callers
// offer the in-app copy fallback only when exportFile could not start a
// download at all.
//
// WebKit (Safari and the macOS desktop webview) keeps only the last of several
// downloads a page starts before the webview has answered for the previous
// one, so downloads start one at a time with a short pause between them. A
// hidden frame per download would avoid the pause, but the host's Content
// Security Policy (frame-src 'self') rightly refuses blob: frames.

export interface ExportedFile {
  name: string;
  mime: string;
  content: string;
}
export type ExportDelivery = "downloaded" | "unconfirmed";

const downloadGapMs = 400;
// Long enough for the webview to hand the file to its download machinery.
const releaseAfterMs = 60_000;
let nextStart = 0;

/** True inside the Tauri desktop shell (WKWebView, WebView2 or WebKitGTK). */
export function isDesktopShell(): boolean {
  const host = window as unknown as Record<string, unknown>;
  return !!host["isTauri"] || "__TAURI_INTERNALS__" in host;
}

function startDownload(url: string, name: string) {
  const anchor = document.createElement("a");
  try {
    anchor.href = url;
    anchor.download = name;
    anchor.rel = "noopener";
    anchor.style.display = "none";
    document.body.append(anchor);
    anchor.click();
  } finally {
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), releaseAfterMs);
  }
}

/**
 * Starts a file download. "unconfirmed" means no download could be started,
 * so the caller should offer the contents another way.
 */
export function exportFile(
  name: string,
  mime: string,
  bytes: string | Uint8Array,
): ExportDelivery {
  let url = "";
  try {
    const body =
      typeof bytes === "string" ? bytes : new Uint8Array(bytes).buffer;
    url = URL.createObjectURL(new Blob([body], { type: mime }));
    const now = performance.now();
    const wait = Math.max(0, nextStart - now);
    nextStart = now + wait + downloadGapMs;
    const download = url;
    if (wait === 0) startDownload(download, name);
    else
      setTimeout(() => {
        try {
          startDownload(download, name);
        } catch {
          // Nothing else can deliver the file once the export has returned.
        }
      }, wait);
    return "downloaded";
  } catch {
    if (url) URL.revokeObjectURL(url);
    return "unconfirmed";
  }
}

/** Plain-language notice once exportFile started every download. */
export function exportNotice(names: readonly string[]): string {
  const list =
    names.length < 3
      ? names.join(" and ")
      : `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
  return isDesktopShell()
    ? `Saved ${list} to your Downloads folder.`
    : `Exported ${list}.`;
}
