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
// Times Studio's fingerprints() in the page under test: the performance spec
// bundles fingerprint-workload.ts into the page, as formula-calibration.ts
// does for the formula code, and runs it on the page's main thread.
import { buildSync } from "esbuild";
import { resolve } from "node:path";
import type { Page } from "@playwright/test";
import type { FingerprintWork, timeFingerprints } from "./fingerprint-workload";

let bundle: string | undefined;

function workloadScript(): string {
  bundle ??= buildSync({
    stdin: {
      contents:
        'import { timeFingerprints } from "./fingerprint-workload";\n' +
        "globalThis.__weaveFingerprintWork = timeFingerprints;\n",
      resolveDir: resolve("tests/browser"),
      loader: "ts",
    },
    bundle: true,
    format: "iife",
    platform: "browser",
    write: false,
  }).outputFiles[0].text;
  return bundle;
}

/** Each measured fingerprints() call over `work.steps` steps, timed in `page`. */
export async function timeFingerprintWork(page: Page, work: FingerprintWork) {
  await page.evaluate(workloadScript());
  return page.evaluate((input) => {
    const time = (
      globalThis as unknown as {
        __weaveFingerprintWork: typeof timeFingerprints;
      }
    ).__weaveFingerprintWork;
    return time(input);
  }, work);
}
