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
import { defineConfig } from "@playwright/test";
import { join } from "node:path";

// Studio journeys against the real platform that scripts/acceptance.py starts
// (tests/acceptance/README.md). No web server: each journey starts the Studio
// host from this source tree. One worker, no retries; traces stay under the
// run's private folder and are never uploaded. Without WEAVE_ACC_RUN_DIR every
// journey skips.
const run = process.env["WEAVE_ACC_RUN_DIR"]?.trim() ?? "";

export default defineConfig({
  testDir: "tests/acceptance",
  testMatch: "*.spec.ts",
  outputDir: run
    ? join(run, "private", "playwright", "journeys")
    : "test-results/acceptance",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 900_000,
  use: {
    browserName: "chromium",
    launchOptions: {
      executablePath: process.env["PLAYWRIGHT_CHROMIUM_EXECUTABLE"],
    },
    locale: "en-US",
    timezoneId: "UTC",
    actionTimeout: 30_000,
    navigationTimeout: 30_000,
    trace: "retain-on-failure",
    video: "off",
    screenshot: "off",
  },
  projects: [
    { name: "desktop-1440", use: { viewport: { width: 1440, height: 900 } } },
    { name: "compact-600", use: { viewport: { width: 600, height: 500 } } },
  ],
  reporter: run
    ? [
        ["list"],
        ["json", { outputFile: join(run, "evidence", "playwright.json") }],
      ]
    : [["list"]],
});
