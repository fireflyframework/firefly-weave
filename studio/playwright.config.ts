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

// Parallel lanes run their own dev server: STUDIO_TEST_PORT picks the port and
// STUDIO_TEST_OUTPUT the artifacts folder. Without them, nothing changes.
const DEFAULT_PORT = 4200;
const requested = process.env.STUDIO_TEST_PORT?.trim();
const port = requested ? Number(requested) : DEFAULT_PORT;
if (!Number.isInteger(port) || port < 1024 || port > 65535)
  throw Error(
    `STUDIO_TEST_PORT must be a whole number from 1024 to 65535, not "${requested}".`,
  );
const baseURL = `http://127.0.0.1:${port}`;
// Playwright empties outputDir before every run. A lane without its own folder
// gets one, so it never clears test-results/ under another lane's feet.
const output =
  process.env.STUDIO_TEST_OUTPUT?.trim() ||
  (port === DEFAULT_PORT ? undefined : `test-results/port-${port}`);

export default defineConfig({
  testDir: "tests/browser",
  outputDir: output,
  fullyParallel: false,
  // Shared CI runners are slow and noisy; a test that passes on its retry is
  // reported as flaky instead of failing the job. Local runs never retry.
  retries: process.env.GITHUB_ACTIONS === "true" ? 2 : 0,
  use: {
    baseURL,
    browserName: "chromium",
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    },
  },
  webServer: {
    command: `npm start -- --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    // A second dev server must not share the persistent Angular/Vite cache
    // with the default one; the CLI turns that cache off under CI=1.
    env: port === DEFAULT_PORT ? {} : { CI: "1" },
  },
  reporter: "list",
});
