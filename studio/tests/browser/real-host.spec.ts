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
import { test, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
test("real PyFly host pairs the production app under CSP and validates local source", async ({
  page,
}) => {
  const executable = resolve("../.venv/bin/weave");
  if (process.env.CI) expect(existsSync(executable)).toBe(true);
  test.skip(
    !existsSync(executable),
    "Create the Python development environment for the local-host integration test.",
  );
  const host = spawn(
    executable,
    [
      "studio",
      "--assets",
      "studio/dist/studio/browser",
      "--port",
      "8879",
      "--no-browser",
    ],
    { cwd: resolve(".."), stdio: ["ignore", "pipe", "pipe"] },
  );
  let output = "";
  host.stdout.on("data", (data) => (output += String(data)));
  host.stderr.on("data", (data) => (output += String(data)));
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && m.text().includes("Content Security Policy"))
      errors.push(m.text());
  });
  try {
    await expect
      .poll(() => output.match(/Pairing code: (\S+)/)?.[1], { timeout: 15000 })
      .toBeTruthy();
    const code = output.match(/Pairing code: (\S+)/)![1];
    await expect
      .poll(
        async () => {
          try {
            return (
              await page.request.get("http://127.0.0.1:8879/studio/session")
            ).status();
          } catch {
            return 0;
          }
        },
        { timeout: 15000 },
      )
      .toBe(200);
    await page.goto("http://127.0.0.1:8879");
    await page.getByLabel("Pairing code").fill(code);
    await page.getByRole("button", { name: "Connect to Studio" }).click();
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    await page.screenshot({ path: "test-results/real-host-home.png" });
    await page
      .getByRole("button", { name: "New workflow", exact: true })
      .click();
    await page
      .locator(".palette-step")
      .filter({ hasText: "Transform" })
      .click();
    await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
    await page.getByRole("button", { name: "Validate", exact: true }).click();
    await expect(
      page.getByText("Catalog checks pending", { exact: true }),
    ).toBeVisible();
    await expect(page.locator(".diagnostics")).toContainText("0 errors");
    expect(errors).toEqual([]);
    await page.screenshot({ path: "test-results/real-host-designer.png" });
  } catch (error) {
    console.error(output);
    throw error;
  } finally {
    host.kill("SIGTERM");
  }
});
