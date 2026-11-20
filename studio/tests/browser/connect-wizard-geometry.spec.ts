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
// The Kubernetes connect wizard measured at a wide desktop, a desktop, a
// tablet with a fine pointer and a phone: the form at most 720 px with "What
// Studio sees" beside it or below the connect command, commands fully
// visible (or scrolling on their own line on a phone) with Copy always in
// view, 36 px controls with a fine pointer and 44 px on touch, and no
// sideways page scroll. Each state is also saved under connect-wizard/<size>/
// in the run's output folder, for the design review.
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { box, boxes, density } from "./step-geometry";
import {
  clusterRunner,
  clusterTarget,
  command,
  copyButton,
  lookedFor,
  observation,
  openClusters,
  openWizard,
  openWizardAndLook,
  settled,
  status,
  type Backend,
} from "./connect-wizard-fixture";

const SIZES = [
  { tag: "1920x1080", use: { viewport: { width: 1920, height: 1080 } } },
  { tag: "1440x900", use: { viewport: { width: 1440, height: 900 } } },
  { tag: "768x1024", use: { viewport: { width: 768, height: 1024 } } },
  {
    tag: "360x740",
    use: {
      viewport: { width: 360, height: 740 },
      hasTouch: true,
      isMobile: true,
    },
  },
];

/** The page never scrolls sideways, and every icon has a drawing. */
async function expectSound(page: Page) {
  const [scroll, width] = await page.evaluate(() => [
    document.scrollingElement!.scrollWidth,
    innerWidth,
  ]);
  expect(scroll, "page width").toBeLessThanOrEqual(width);
  await expect(page.locator("weave-icon[data-icon-missing]")).toHaveCount(0);
}
const near = (actual: number, expected: number, what: string) =>
  expect(Math.abs(actual - expected), `${what}: ${actual}`).toBeLessThanOrEqual(
    0.5,
  );

/**
 * Saves what the window shows, and the whole page when it is taller: the
 * window grows to the page's height for that picture, then shrinks back.
 */
async function shot(page: Page, tag: string, name: string) {
  await page.mouse.move(0, 0);
  await page.evaluate(() => document.fonts.ready);
  // The output folder is the run's own (STUDIO_TEST_OUTPUT), so runs on other
  // ports never overwrite these pictures.
  const folder = join(test.info().project.outputDir, "connect-wizard");
  const file = join(folder, tag, `${name}.png`);
  mkdirSync(dirname(file), { recursive: true });
  await page.screenshot({ path: file });
  const size = page.viewportSize()!;
  const extra = await page.evaluate(() =>
    Math.max(
      0,
      ...[...document.querySelectorAll<HTMLElement>("body *")]
        .filter((e) => /(auto|scroll)/.test(getComputedStyle(e).overflowY))
        .map((e) => e.scrollHeight - e.clientHeight),
    ),
  );
  if (extra > 4) {
    await page.setViewportSize({ ...size, height: size.height + extra });
    await page.screenshot({ path: join(folder, tag, `${name}-full.png`) });
    await page.setViewportSize(size);
  }
}

/** The form and "What Studio sees": beside each other, or one under the other. */
async function expectLayout(page: Page) {
  const form = await box(page.locator(".connect-form"));
  const aside = await box(status(page));
  const content = await box(page.locator(".connect"));
  expect(form.width, "form width").toBeLessThanOrEqual(720.5);
  if (content.width >= 1064) {
    near(form.width, 720, "form width");
    expect(aside.x, "status beside the form").toBeGreaterThanOrEqual(
      form.right + 23.5,
    );
    near(aside.y, form.y, "status top");
    expect(aside.width, "status width").toBeGreaterThanOrEqual(320);
  } else {
    near(form.width, content.width, "form width");
    near(aside.width, content.width, "status width");
    expect(aside.y, "status below the form").toBeGreaterThanOrEqual(
      form.bottom + 23.5,
    );
  }
}

/**
 * From 768 px a command wraps between its arguments, fully visible; on a
 * phone it keeps one line and scrolls on its own, with Copy beside it.
 */
async function expectCommand(page: Page, block: Locator) {
  const pre = block.locator("pre");
  const { scrollWidth, clientWidth } = await pre.evaluate((e) => ({
    scrollWidth: e.scrollWidth,
    clientWidth: e.clientWidth,
  }));
  const copy = await box(block.getByRole("button"));
  const frame = await box(block);
  expect(copy.x, "Copy inside its command").toBeGreaterThanOrEqual(frame.x);
  expect(copy.right, "Copy inside its command").toBeLessThanOrEqual(
    frame.right,
  );
  expect(copy.right, "Copy in the window").toBeLessThanOrEqual(
    page.viewportSize()!.width,
  );
  if (page.viewportSize()!.width < 768) {
    expect(scrollWidth, "the command scrolls").toBeGreaterThan(clientWidth);
    return;
  }
  expect(scrollWidth, "the command is fully visible").toBeLessThanOrEqual(
    clientWidth,
  );
  // A flag stays on one line with its value, unless it is wider than the box.
  for (const part of await boxes(pre.locator(".connect-arg"))) {
    if (part.width < clientWidth - 1)
      expect(part.height, "an argument on one line").toBeLessThanOrEqual(21);
  }
}

/**
 * A name inside a sentence never breaks at its hyphens: it moves to the next
 * line whole, unless it is longer than the line.
 */
async function expectWholeNames(page: Page) {
  const names = page.locator(".connect-name, .connect-target-line code");
  expect(await names.count()).toBeGreaterThan(0);
  const broken = await names.evaluateAll((elements) =>
    elements
      .filter((element) => {
        const lines = new Set(
          [...element.getClientRects()].map((rect) => Math.round(rect.top)),
        );
        const room = element.parentElement!.clientWidth;
        return lines.size > 1 && element.scrollWidth < room;
      })
      .map((element) => element.textContent),
  );
  expect(broken, "names broken across lines").toEqual([]);
}

/**
 * Punctuation right after a name stays on the name's line: a long name moves
 * to the next line with its comma or period, never leaving it to start a line.
 */
async function expectPunctuationKept(page: Page) {
  const loose = await page.locator(".connect").evaluate((root) => {
    const found: string[] = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.textContent ?? "";
      const at = text.search(/\S/);
      if (at < 0 || !/[.,…]/.test(text[at])) continue;
      let before = node.previousSibling;
      while (before && before.nodeType === Node.COMMENT_NODE)
        before = before.previousSibling;
      if (!(before instanceof Element)) continue;
      const range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + 1);
      const mark = range.getClientRects()[0];
      const rects = before.getClientRects();
      const last = rects[rects.length - 1];
      // Lines are 20 px: centres more than half a line apart are on
      // different lines (mono and sans text start a few px apart on one).
      const centre = (rect: DOMRect) => rect.top + rect.height / 2;
      if (mark && last && Math.abs(centre(mark) - centre(last)) > 10)
        found.push(`${before.textContent}${text[at]}`);
    }
    return found;
  });
  expect(loose, "punctuation that starts a line").toEqual([]);
}

/** Every control is 36 px with a fine pointer and 44 px on touch. */
async function expectControls(page: Page) {
  const { touch } = await density(page);
  const control = touch ? 44 : 36;
  near(
    (await box(page.getByRole("button", { name: "Back to Clusters" }))).height,
    control,
    "Back to Clusters",
  );
  for (const name of ["Cluster name", "Namespace"])
    near(
      (await box(page.getByRole("textbox", { name }))).height,
      control,
      name,
    );
  const radios = await boxes(page.locator(".connect-segment"));
  expect(radios).toHaveLength(2);
  if (touch)
    for (const radio of radios)
      expect(radio.height, "a provider radio").toBeGreaterThanOrEqual(44);
  else
    near((await box(page.locator(".connect-segments"))).height, 36, "Provider");
  for (const which of ["create", "connect"] as const)
    near(
      (await box(copyButton(page, which))).height,
      touch ? 44 : 32,
      `Copy ${which}`,
    );
  for (const action of await boxes(status(page).getByRole("button")))
    near(action.height, control, "status action");
}

async function connectedLater(page: Page, backend: Backend) {
  await openWizard(page);
  await settled(page);
  backend.targets = [clusterTarget()];
  backend.runners = [clusterRunner(true)];
  backend.observations = [observation(12_000)];
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(status(page).getByRole("status")).toContainText(
    "Cluster connected",
  );
}

for (const size of SIZES)
  test.describe(`The Kubernetes connect wizard at ${size.tag}`, () => {
    test.use(size.use);

    test("Add target with the Kubernetes option", async ({ page }) => {
      await openClusters(page);
      await page.getByRole("button", { name: "Add target" }).click();
      const { touch } = await density(page);
      near(
        (await box(page.getByRole("button", { name: "Connect a cluster" })))
          .height,
        touch ? 44 : 36,
        "Connect a cluster",
      );
      const add = await box(page.getByRole("button", { name: "Add target" }));
      near(add.height, touch ? 44 : 36, "Add target");
      // Add target and the choices it opened keep apart.
      expect(
        (await box(page.locator("#add-target-choices"))).y - add.bottom,
        "Add target to its choices",
      ).toBeGreaterThanOrEqual(11.5);
      // Other is a control too, and shows that it opens.
      const other = page.locator("#add-target-choices summary");
      near((await box(other)).height, touch ? 44 : 36, "Other");
      const chevron = other.locator("weave-icon");
      await expect(chevron).toHaveCount(1);
      await expectSound(page);
      await shot(page, size.tag, "01-chooser");
      const turn = () => chevron.evaluate((e) => getComputedStyle(e).rotate);
      expect(await turn(), "Other's chevron, closed").toBe("none");
      await other.click();
      await expect
        .poll(turn, { message: "Other's chevron, open" })
        .toBe("90deg");
    });

    test("the form as it opens, waiting", async ({ page }) => {
      await openClusters(page);
      await openWizard(page);
      await settled(page);
      await expectLayout(page);
      await expectCommand(page, command(page, "Create"));
      await expectCommand(page, command(page, "Connect"));
      await expectControls(page);
      await expectWholeNames(page);
      await expectPunctuationKept(page);
      // What each command changes avoids a one-word last line.
      for (const paragraph of [".connect-says", ".connect-changes"])
        expect(
          await page
            .locator(paragraph)
            .evaluate((e) =>
              getComputedStyle(e).getPropertyValue("text-wrap-style"),
            ),
          paragraph,
        ).toBe("pretty");
      // Each command comes first, then what it changes, so the connect
      // command stays high on the page.
      expect(
        (await box(page.locator(".connect-changes"))).y,
        "what connecting changes, under its command",
      ).toBeGreaterThan((await box(command(page, "Connect"))).bottom);
      // The connect command is in view without scrolling on a desktop.
      if (size.tag === "1440x900" || size.tag === "1920x1080")
        expect(
          (await box(command(page, "Connect"))).bottom,
          "connect command bottom",
        ).toBeLessThanOrEqual(page.viewportSize()!.height);
      await expectSound(page);
      await shot(page, size.tag, "02-form-waiting");
    });

    test("copied", async ({ page, context }) => {
      await context.grantPermissions(["clipboard-read", "clipboard-write"]);
      await openClusters(page);
      await openWizard(page);
      await settled(page);
      await copyButton(page, "connect").click();
      const note = page
        .getByRole("status")
        .filter({ hasText: "Copied. Run it" });
      await expect(note).toBeVisible();
      if (size.tag === "1440x900" || size.tag === "1920x1080")
        expect(
          (await box(note)).bottom,
          "Copied note bottom",
        ).toBeLessThanOrEqual(page.viewportSize()!.height);
      near(
        (await box(copyButton(page, "connect"))).height,
        (await density(page)).touch ? 44 : 32,
        "Copied",
      );
      // Copied keeps Copy's box.
      near(
        (await box(copyButton(page, "connect"))).width,
        (await box(copyButton(page, "create"))).width,
        "Copied width",
      );
      await expectSound(page);
      await shot(page, size.tag, "03-copied");
    });

    test("connected", async ({ page }) => {
      const backend = await openClusters(page);
      await connectedLater(page, backend);
      await expectLayout(page);
      await expectControls(page);
      await expectSound(page);
      await shot(page, size.tag, "04-connected");
    });

    test("connected, k3d in namespace team-a", async ({ page }) => {
      const backend = await openClusters(page);
      await openWizard(page);
      await lookedFor(page, () =>
        page.getByRole("radio", { name: "k3d" }).check(),
      );
      await lookedFor(page, () =>
        page.getByRole("textbox", { name: "Namespace" }).fill("team-a"),
      );
      backend.targets = [
        clusterTarget({ name: "k3d-weave-test.team-a", boundary: "team-a" }),
      ];
      backend.runners = [clusterRunner(true)];
      await page.getByRole("button", { name: "Refresh" }).click();
      await expect(status(page).getByRole("status")).toContainText(
        "Cluster connected",
      );
      await expectLayout(page);
      await expectCommand(page, command(page, "Connect"));
      await expectSound(page);
      await shot(page, size.tag, "05-connected-k3d-team-a");
    });

    test("an invalid cluster name", async ({ page }) => {
      await openClusters(page);
      await openWizard(page);
      await page
        .getByRole("textbox", { name: "Cluster name" })
        .fill("Weave_Test");
      // The refusal spans the row of fields, under them.
      const refusal = await box(page.locator(".connect-problem"));
      const fields = await box(page.locator(".connect-fields"));
      near(refusal.width, fields.width, "refusal width");
      expect(refusal.y).toBeGreaterThan(
        (await box(page.getByRole("textbox", { name: "Namespace" }))).bottom,
      );
      await expectLayout(page);
      await expectControls(page);
      await expectSound(page);
      await shot(page, size.tag, "06-invalid-cluster-name");
    });

    test("a namespace Kubernetes uses", async ({ page }) => {
      await openClusters(page);
      await openWizard(page);
      await page.getByRole("textbox", { name: "Namespace" }).fill("default");
      await expect(status(page)).toContainText("Not looking yet");
      await expectSound(page);
      await shot(page, size.tag, "07-reserved-namespace");
    });

    test("registered, its runner not online yet", async ({ page }) => {
      await openClusters(page, {
        targets: [clusterTarget()],
        runners: [clusterRunner(false)],
      });
      await openWizard(page);
      await expect(status(page).getByRole("status")).toContainText(
        "Registered",
      );
      await expectWholeNames(page);
      await expectSound(page);
      await shot(page, size.tag, "08-registered");
    });

    test("already connected", async ({ page }) => {
      await openClusters(page, {
        targets: [clusterTarget()],
        runners: [clusterRunner(true)],
      });
      await openWizard(page);
      await expect(status(page).getByRole("status")).toContainText(
        "Already connected",
      );
      await expectControls(page);
      await expectSound(page);
      await shot(page, size.tag, "09-already-connected");
    });

    test("the name is in use by another target", async ({ page }) => {
      await openClusters(page, {
        targets: [clusterTarget({ adapter: "docker-compose" })],
      });
      await openWizard(page);
      await expect(status(page).getByRole("status")).toContainText(
        "Name in use",
      );
      await expectWholeNames(page);
      await expectSound(page);
      await shot(page, size.tag, "10-name-in-use");
    });

    test("patience over", async ({ page }) => {
      await page.clock.install();
      await openClusters(page);
      await openWizardAndLook(page);
      await page.clock.fastForward(15 * 60_000);
      await expect(status(page).getByRole("status")).toContainText(
        "Not seen yet",
      );
      await expectControls(page);
      await expectSound(page);
      await shot(page, size.tag, "11-patience-over");
    });

    test("the clipboard refused", async ({ page }) => {
      await page.addInitScript(() => {
        Object.defineProperty(navigator, "clipboard", {
          value: { writeText: () => Promise.reject(new Error("denied")) },
        });
        document.execCommand = () => false;
      });
      await openClusters(page);
      await openWizard(page);
      await settled(page);
      await copyButton(page, "connect").click();
      await expect(
        page.getByRole("status").filter({ hasText: "Select the command" }),
      ).toBeVisible();
      await expectSound(page);
      await shot(page, size.tag, "12-clipboard-refused");
    });

    test("the platform unreachable while waiting", async ({ page }) => {
      const backend = await openClusters(page);
      await openWizard(page);
      await settled(page);
      backend.unreachable = true;
      await page.getByRole("button", { name: "Refresh" }).click();
      const problem = page
        .getByRole("alert")
        .filter({ hasText: "could not be loaded" });
      await expect(problem).toBeVisible();
      expect(
        (await box(page.locator(".connect-wizard"))).y -
          (await box(problem)).bottom,
        "space between the error and the wizard",
      ).toBeGreaterThanOrEqual(15.5);
      await expectSound(page);
      await shot(page, size.tag, "13-platform-unreachable");
    });

    test("the longest names", async ({ page }) => {
      await openClusters(page);
      await openWizard(page);
      await page
        .getByRole("textbox", { name: "Cluster name" })
        .fill("weave-acc-20261119t120000-a1b2c3");
      await page
        .getByRole("textbox", { name: "Namespace" })
        .fill(`n${"a".repeat(61)}z`);
      await expect(status(page)).toContainText("Waiting for kind-weave-acc");
      for (const which of ["Create", "Connect"] as const)
        await expectCommand(page, command(page, which));
      if (size.use.viewport.width >= 768) {
        const lines = (await box(command(page, "Connect").locator("pre")))
          .height;
        expect(lines, "connect command lines").toBeLessThanOrEqual(4 * 20 + 12);
      }
      await expectLayout(page);
      await expectPunctuationKept(page);
      await expectSound(page);
      await shot(page, size.tag, "14-longest-names");
    });
  });
