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
// On error in a Call an API step's Settings: the three choices with what
// each does, Result on error for Continue with error output, the error path
// that goes (with a toast and Undo) when the step stops continuing with
// error output, and the read-only "Stop the run" on a platform that can't
// continue after errors. The language manifest is the repository's own.
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { repository } from "../python-path";
import type { StepDetailsPage } from "./step-details-po";
import { httpWorkflow, openHttpStep } from "./step-http";

/** The language manifest the Studio host serves, as the repository's Python writes it. */
const manifest = JSON.parse(
  readFileSync(
    join(repository, "studio/tests/fixtures/language/manifest.json"),
    "utf8",
  ),
).manifest as { features: string[] };

/** Serves the manifest, or one without step error handling (an older platform). */
async function withManifest(page: Page, onError = true) {
  const served = onError
    ? manifest
    : {
        ...manifest,
        features: manifest.features.filter((name) => name !== "flow.onError"),
      };
  await page.route(
    /\/studio\/(?:contracts\/language|api\/api\/v1\/tenants\/[^/]+\/projects\/[^/]+\/language)$/,
    (route) => route.fulfill({ json: served }),
  );
}

/** The step's source in the draft Studio keeps on this computer. */
const stored = (page: Page) =>
  page.evaluate(() =>
    Object.keys(localStorage)
      .filter((key) => key.startsWith("local:weave.localDraft."))
      .map(
        (key) =>
          (JSON.parse(localStorage.getItem(key) ?? "{}") as { source?: string })
            .source ?? "",
      )
      .join(),
  );

/** The same workflow, with get-customer-orders already continuing with error output. */
const withErrorPath = httpWorkflow.replace(
  "  output: {literal: {}}\n",
  `      onError: errorOutput
      errorPath:
        steps:
          - id: notify-team
            kind: transform
            value: {ref: /steps/get-customer-orders/error/code}
        output: {literal: null}
  output: {literal: {}}
`,
);

const onError = (details: StepDetailsPage) =>
  details.field("onError").getByRole("combobox", { name: "On error" });

test.use({ viewport: { width: 1440, height: 900 } });

test("On error offers three choices and says what the chosen one does", async ({
  page,
}) => {
  await withManifest(page);
  const details = await openHttpStep(page, undefined, undefined, {
    recipe: { method: "POST" },
  });
  await details.tab("Settings").click();
  const field = details.field("onError");
  await expect(onError(details)).toHaveValue("Stop the run");
  await expect(field).toContainText(
    "The run stops with an incident you can retry or resolve in Operate.",
  );
  // The action changes data: an unknown outcome still stops the run.
  await expect(field).toContainText(
    "If its outcome is unknown, the run still stops with an incident until someone checks it.",
  );
  // Retry on fail and Timeout come first, Notes and About this step after.
  await expect
    .poll(() =>
      details
        .topFields()
        .evaluateAll((items) =>
          items.map((item) => item.getAttribute("data-param")),
        ),
    )
    .toEqual(["retry", "timeout", "onError", "notes", "about"]);
  await onError(details).click();
  await expect(
    page.getByRole("listbox", { name: "On error options" }).getByRole("option"),
  ).toHaveText([
    /^Stop the run\s*The run stops with an incident/,
    /^Continue\s*The run goes on\. This step's output is empty \(null\)/,
    /^Continue with error output\s*Failures take this step's Error output on the canvas/,
  ]);
  await page.keyboard.press("Escape");
  await expect(onError(details)).toBeFocused();
  await expect(onError(details)).toHaveValue("Stop the run");
});

test("Continue with error output writes the setting and an empty error path as one undo step", async ({
  page,
}) => {
  await withManifest(page);
  const details = await openHttpStep(page);
  await details.tab("Settings").click();
  const before = await stored(page);
  // By keyboard: the list opens on the combobox, Down twice, Enter.
  await onError(details).focus();
  await page.keyboard.press("Alt+ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(onError(details)).toHaveValue("Continue with error output");
  await expect(onError(details)).toBeFocused();
  const result = details.field("resultOnError");
  await expect(result).toContainText("Result on error");
  await expect(result).toContainText(
    "What later steps read as this step's output after the error path.",
  );
  // It maps data first: later steps usually read what the error path made.
  await expect(result.getByRole("radio", { name: "Mapped" })).toBeChecked();
  await expect
    .poll(() => stored(page))
    .toMatch(
      /onError: errorOutput\n\s+errorPath:\n\s+steps: \[\]\n\s+output:\n\s+literal: null/,
    );
  // The tab's dot says a setting differs from its default.
  await expect(details.tab("Settings")).not.toHaveAccessibleName("Settings");
  await page.keyboard.press("ControlOrMeta+z");
  await expect(onError(details)).toHaveValue("Stop the run");
  await expect(result).toHaveCount(0);
  await expect.poll(() => stored(page)).toBe(before);
});

test("leaving Continue with error output removes the error path, named in a toast, and Undo brings it back", async ({
  page,
}) => {
  await withManifest(page);
  const details = await openHttpStep(page, undefined, withErrorPath);
  await details.tab("Settings").click();
  await expect(onError(details)).toHaveValue("Continue with error output");
  await onError(details).click();
  await page
    .getByRole("option", { name: /^Continue\s*The run goes on/ })
    .click();
  await expect(onError(details)).toHaveValue("Continue");
  await expect(details.field("onError")).toContainText(
    "The run goes on. This step's output is empty (null), and later steps can read its error.",
  );
  const toast = page.locator(".toast");
  await expect(toast.locator("strong")).toHaveText(
    "Removed the error path (1 step)",
  );
  await expect(toast.locator(".toast-detail")).toHaveText(
    "notify-team was in it.",
  );
  await expect.poll(() => stored(page)).not.toContain("notify-team");
  await expect.poll(() => stored(page)).toContain("onError: continue");
  await toast.getByRole("button", { name: "Undo" }).click();
  await expect(onError(details)).toHaveValue("Continue with error output");
  await expect.poll(() => stored(page)).toContain("- id: notify-team");
  await expect.poll(() => stored(page)).toContain("onError: errorOutput");
});

test("Stop the run stays read-only on a platform that can't continue after errors", async ({
  page,
}) => {
  await withManifest(page, false);
  const details = await openHttpStep(page);
  await details.tab("Settings").click();
  const field = details.field("onError");
  const group = field.getByRole("radiogroup", { name: "On error" });
  await expect(
    group.getByRole("radio", { name: "Stop the run" }),
  ).toBeChecked();
  await expect(
    group.getByRole("radio", { name: "Stop the run" }),
  ).toBeDisabled();
  await expect(field).toContainText(
    "Update the platform to continue after errors.",
  );
  await expect(field.getByRole("combobox")).toHaveCount(0);
});
