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
// The first page stays small: the test data service, its reader, its
// operations and its check load with the test data tools once a workflow
// opens, and the designer waits for them.
import { expect, test, type Page } from "@playwright/test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";
import { repository } from "../python-path";
import {
  fitCanvas,
  openStepFixture,
  withLanguageFeatures,
} from "./step-details-po";
import { connected, defaultCatalog } from "./support";
import {
  banner,
  lookupContract,
  outputPane,
  PROJECT,
  routeDrafts,
  TENANT,
  testDataFixture,
} from "./test-data-po";

/**
 * Text only the test data service and its operations hold. The production
 * build is what a person loads; the dev server bundles differently.
 */
const MARKERS = [
  "couldn't read the test data kept for this workflow",
  "Test data is limited to 1 MB.",
];
const BUILD = join(repository, "studio/dist/studio/browser");

/** Starts connected, as the test data fixture does. */
async function startConnected(page: Page) {
  const result = await connected(page, {
    catalog: [...defaultCatalog, { id: "a3", document: lookupContract }],
    tenantId: TENANT,
    projectId: PROJECT,
  });
  await withLanguageFeatures(page);
  return result;
}

test("the first page of the production build holds no test data code", () => {
  // CI builds the production app before the browser tests (the real-host test serves it too).
  const index = readFileSync(join(BUILD, "index.html"), "utf8");
  const first = [...index.matchAll(/(?:src|href)="([^"]+\.js)"/g)].map(
    (match) => match[1],
  );
  expect(first.length).toBeGreaterThan(0);
  const holding = first.filter((file) => {
    const text = readFileSync(join(BUILD, file), "utf8");
    return MARKERS.some((marker) => text.includes(marker));
  });
  expect(holding).toEqual([]);
  // The service is in the build, in a chunk loaded later.
  const later = readdirSync(BUILD).filter(
    (file) =>
      file.endsWith(".js") &&
      !first.includes(file) &&
      readFileSync(join(BUILD, file), "utf8").includes(MARKERS[0]),
  );
  expect(later.length).toBe(1);
});

/**
 * The first page's bytes on main when this branch last merged it: it must not
 * grow. Counted before compression, because every machine builds the same
 * files while gzip counts depend on the zlib inside Node (Node 24's and the
 * macOS system zlib differ by about 480 bytes on this page).
 */
const FIRST_PAGE_BUDGET = 807_656;
/** The decision grid's chunk, at most (gzip). */
const GRID_BUDGET = 61_440;
const gzipped = (file: string) =>
  gzipSync(readFileSync(join(BUILD, file)), { level: 9 }).length;

test("the decision grid stays out of the first page and under its chunk budget", () => {
  const index = readFileSync(join(BUILD, "index.html"), "utf8");
  const first = [...index.matchAll(/(?:src|href)="([^"]+\.(?:js|css))"/g)].map(
    (match) => match[1],
  );
  const total = first.reduce(
    (sum, file) => sum + statSync(join(BUILD, file)).size,
    0,
  );
  expect(total).toBeLessThanOrEqual(FIRST_PAGE_BUDGET);
  // The grid's own class names live only in its chunk.
  const grid = readdirSync(BUILD).filter(
    (file) =>
      file.endsWith(".js") &&
      readFileSync(join(BUILD, file), "utf8").includes("dg-scroll"),
  );
  expect(grid).toHaveLength(1);
  expect(first).not.toContain(grid[0]);
  expect(gzipped(grid[0])).toBeLessThanOrEqual(GRID_BUDGET);
});

test("the editor's shortcut table loads with the editor, not with the first page", () => {
  // A label only the shortcut table holds.
  const marker = "Extend the selection downstream";
  const index = readFileSync(join(BUILD, "index.html"), "utf8");
  const first = [...index.matchAll(/(?:src|href)="([^"]+\.js)"/g)].map(
    (match) => match[1],
  );
  const holding = readdirSync(BUILD).filter(
    (file) =>
      file.endsWith(".js") &&
      readFileSync(join(BUILD, file), "utf8").includes(marker),
  );
  expect(holding).toHaveLength(1);
  expect(first).not.toContain(holding[0]);
});

test("the Runs tab's code stays out of the first page", () => {
  // Copy only the Runs tab's list holds.
  const marker = "Runs live on a platform.";
  const index = readFileSync(join(BUILD, "index.html"), "utf8");
  const first = [...index.matchAll(/(?:src|href)="([^"]+\.js)"/g)].map(
    (match) => match[1],
  );
  const holding = readdirSync(BUILD).filter(
    (file) =>
      file.endsWith(".js") &&
      readFileSync(join(BUILD, file), "utf8").includes(marker),
  );
  expect(holding).toHaveLength(1);
  expect(first).not.toContain(holding[0]);
});

test("the Operate pages, Settings and the record lists load outside the first page", () => {
  // The shell shows them through one outlet: the first page names none of
  // them, neither their code nor a deferred block for each.
  const index = readFileSync(join(BUILD, "index.html"), "utf8");
  const first = [...index.matchAll(/(?:src|href)="([^"]+\.js)"/g)].map(
    (match) => match[1],
  );
  const pages = [
    "overview-page",
    "runs-page",
    "incidents-page",
    "workers-page",
    "clusters-page",
    "metrics-page",
    "logs-page",
    "settings-page",
    "records-view",
  ].map((name) => `weave-${name}`);
  const named = first.flatMap((file) => {
    const code = readFileSync(join(BUILD, file), "utf8");
    return pages
      .filter((page) => code.includes(page))
      .map((page) => `${file}: ${page}`);
  });
  expect(named).toEqual([]);
});

/** The Runs tab's list chunk, at most (gzip). */
const RUNS_BUDGET = 40_960;

test("the Runs tab's chunk stays under its budget, out of the first page", () => {
  const index = readFileSync(join(BUILD, "index.html"), "utf8");
  const first = [...index.matchAll(/(?:src|href)="([^"]+\.(?:js|css))"/g)].map(
    (match) => match[1],
  );
  // The list's own element name lives only in its chunk.
  const runs = readdirSync(BUILD).filter(
    (file) =>
      file.endsWith(".js") &&
      readFileSync(join(BUILD, file), "utf8").includes("weave-runs-list"),
  );
  expect(runs).toHaveLength(1);
  expect(first).not.toContain(runs[0]);
  const size = gzipped(runs[0]);
  console.log(JSON.stringify({ runsChunkGzip: size }));
  expect(size).toBeLessThanOrEqual(RUNS_BUDGET);
});

test("the designer opens once the test data service loads, and step details read it", async ({
  page,
}) => {
  const details = await openStepFixture(page, testDataFixture, startConnected);
  await details.open("lookup");
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(banner(pane)).toBeVisible();
});

test("a pin set right after opening is kept and follows a rename", async ({
  page,
}) => {
  // On a cold load the designer waits for the test data chunk; pins behave as before.
  const details = await openStepFixture(page, testDataFixture, startConnected);
  const drafts = await routeDrafts(page);
  await details.open("lookup");
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(banner(pane)).toBeVisible();
  await details.close();
  // A change to the workflow saves the draft (and the pin with it) on the platform.
  await fitCanvas(page);
  await page.locator('[data-step="notify"]').first().click();
  await page.keyboard.press("ControlOrMeta+d");
  await expect.poll(() => drafts.size, { timeout: 15000 }).toBe(1);
  await page.reload();
  await details.open("lookup");
  await expect(banner(await outputPane(details))).toBeVisible();
  await details.dialog.getByRole("button", { name: "Rename lookup" }).click();
  await details.dialog.getByRole("textbox", { name: "Step name" }).fill("find");
  await page.keyboard.press("Enter");
  await expect(details.dialogFor("find")).toBeVisible();
  await expect(banner(await outputPane(details))).toBeVisible();
});

test("the code for connections and real step runs stays out of the first page", () => {
  const index = readFileSync(join(BUILD, "index.html"), "utf8");
  const first = [...index.matchAll(/(?:src|href)="([^"]+\.js)"/g)].map(
    (match) => match[1],
  );
  // Strings only that code holds: the kept bindings, the step test client,
  // the Execute in local copy and the connection sheet.
  const markers = [
    "weave.devBindings.v1",
    "/step-tests",
    "Studio stopped waiting for",
    "Loading the connection",
  ];
  const chunks = readdirSync(BUILD).filter((file) => file.endsWith(".js"));
  for (const marker of markers) {
    const holding = chunks.filter((file) =>
      readFileSync(join(BUILD, file), "utf8").includes(marker),
    );
    expect(holding.length, marker).toBeGreaterThan(0);
    expect(
      holding.filter((file) => first.includes(file)),
      marker,
    ).toEqual([]);
  }
});

test("the activation steps, trigger setup and test listening stay out of the first page", () => {
  const index = readFileSync(join(BUILD, "index.html"), "utf8");
  const first = [...index.matchAll(/(?:src|href)="([^"]+\.js)"/g)].map(
    (match) => match[1],
  );
  // Strings only that code holds: the dialog's checks and its setting up of
  // triggers, the test activation client, and the Test event pane's listening.
  const markers = [
    "Checking what this workflow needs",
    "Setting up the triggers",
    "/test-activations",
    "Make a POST request to:",
  ];
  const chunks = readdirSync(BUILD).filter((file) => file.endsWith(".js"));
  for (const marker of markers) {
    const holding = chunks.filter((file) =>
      readFileSync(join(BUILD, file), "utf8").includes(marker),
    );
    expect(holding.length, marker).toBeGreaterThan(0);
    expect(
      holding.filter((file) => first.includes(file)),
      marker,
    ).toEqual([]);
  }
});
