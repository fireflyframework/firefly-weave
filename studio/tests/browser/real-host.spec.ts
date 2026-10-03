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
// The real local host from this source tree (never an installed `weave`),
// started with a throwaway configuration directory so it cannot read the
// developer's saved platforms or touch their credential store.
import { test, expect, Page } from "@playwright/test";
import { ChildProcess, spawn } from "node:child_process";
import { existsSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const repository = resolve("..");
const python = resolve(repository, ".venv/bin/python");

/**
 * A loopback port nothing listens on. A fixed port can stay blocked for a
 * while by connections the previous host closed (TIME_WAIT).
 */
function freePort(): Promise<number> {
  return new Promise((done, fail) => {
    const server = createServer();
    server.once("error", fail);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => done(port));
    });
  });
}

interface Host {
  child: ChildProcess;
  origin: string;
  output: () => string;
}

/** The parent environment without Weave settings that could point elsewhere. */
function hostEnvironment(configHome: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(process.env))
    if (!key.startsWith("WEAVE_")) env[key] = value;
  return {
    ...env,
    PYTHONPATH: join(repository, "src"),
    WEAVE_CONFIG_HOME: configHome,
  };
}

function startHost(configHome: string, port: number): Host {
  let output = "";
  const child = spawn(
    python,
    [
      "-m",
      "firefly_weave.cli.main",
      "studio",
      "--assets",
      "studio/dist/studio/browser",
      "--port",
      String(port),
      "--no-browser",
    ],
    {
      cwd: repository,
      env: hostEnvironment(configHome),
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout?.on("data", (data) => (output += String(data)));
  child.stderr?.on("data", (data) => (output += String(data)));
  return { child, origin: `http://127.0.0.1:${port}`, output: () => output };
}

async function stopHost(host: Host) {
  if (host.child.exitCode !== null || host.child.signalCode !== null) return;
  const exited = new Promise((done) => host.child.once("exit", done));
  host.child.kill("SIGTERM");
  await exited;
}

/** Waits for the host and pairs `page` with the code it printed. */
async function pair(page: Page, host: Host) {
  await expect
    .poll(() => host.output().match(/Pairing code: (\S+)/)?.[1], {
      timeout: 20000,
    })
    .toBeTruthy();
  const code = host.output().match(/Pairing code: (\S+)/)![1];
  await expect
    .poll(
      async () => {
        try {
          return (
            await page.request.get(`${host.origin}/studio/session`)
          ).status();
        } catch {
          return 0;
        }
      },
      { timeout: 20000 },
    )
    .toBe(200);
  await page.goto(host.origin);
  await page.getByLabel("Pairing code").fill(code);
  await page.getByRole("button", { name: "Pair browser" }).click();
}

test("real PyFly host pairs the production app under CSP and validates local source", async ({
  page,
}) => {
  test.setTimeout(120_000);
  if (process.env.CI) expect(existsSync(python)).toBe(true);
  test.skip(
    !existsSync(python),
    "Create the Python development environment for the local-host integration test.",
  );
  const configHome = realpathSync(
    mkdtempSync(join(tmpdir(), "weave-studio-real-host-")),
  );
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && m.text().includes("Content Security Policy"))
      errors.push(m.text());
  });
  let host = startHost(configHome, await freePort());
  try {
    // A fresh configuration: no saved platform, so Studio asks how to work.
    await pair(page, host);
    await expect(page.locator("#wizard-heading")).toHaveText(
      "How do you want to work?",
    );
    const saved = page.waitForResponse(
      (r) =>
        r.url() === `${host.origin}/studio/preferences` &&
        r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Work locally" }).click();
    const response = await saved;
    expect(response.status()).toBe(200);
    expect(response.request().postDataJSON()).toEqual({ start: "local" });
    expect(await response.json()).toEqual({ preferences: { start: "local" } });
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    expect(existsSync(join(configHome, "studio.json"))).toBe(true);

    // A new host on the same configuration remembers the choice.
    await stopHost(host);
    host = startHost(configHome, await freePort());
    await pair(page, host);
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    await expect(page.locator("weave-connection-wizard")).toHaveCount(0);
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
    // One honest status line: what passed, and what waits for a platform.
    await expect(page.locator(".diagnostics-headline")).toHaveText(
      "Checked locally — Validate to check against the project",
    );
    await expect(page.locator(".toast")).toContainText("No problems found.");
    expect(errors).toEqual([]);
    await page.screenshot({ path: "test-results/real-host-designer.png" });
  } catch (error) {
    console.error(host.output());
    throw error;
  } finally {
    await stopHost(host);
    rmSync(configHome, { recursive: true, force: true });
  }
});
