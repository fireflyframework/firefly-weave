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
// How fast this machine runs Studio's own formula code: the performance spec
// bundles formula-workload.ts into the page under test and times it there,
// so the formula preview budget can account for a slow shared runner.
import { buildSync } from "esbuild";
import { resolve } from "node:path";
import type { Page } from "@playwright/test";
import type { timeFormulaWork, Workload } from "./formula-workload";

let bundle: string | undefined;

/** The workload and the formula modules it imports, as one browser script. */
function calibrationScript(): string {
  bundle ??= buildSync({
    stdin: {
      contents:
        'import { timeFormulaWork } from "./formula-workload";\n' +
        "globalThis.__weaveFormulaCalibration = timeFormulaWork;\n",
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

/** Times the workload in `page`, on its main thread. */
export async function calibrateFormulaWork(page: Page, work: Workload) {
  await page.evaluate(calibrationScript());
  return page.evaluate((input) => {
    const time = (
      globalThis as unknown as {
        __weaveFormulaCalibration: typeof timeFormulaWork;
      }
    ).__weaveFormulaCalibration;
    return time(input);
  }, work);
}
