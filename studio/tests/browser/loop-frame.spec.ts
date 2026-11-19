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
// A loop on the canvas: a frame around the steps it repeats, whose header
// names the loop and its list. The header is the loop's own target: a click
// selects it, a double-click or Enter opens its step details.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { parse } from "yaml";
import { openWorkflow, type CanvasPage } from "./canvas-po";
import { yamlFile } from "./step-details-po";
import { sourceText } from "./support";

const workflow = (name: string, steps: string) =>
  yamlFile(
    `${name}.yaml`,
    `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: ${name}, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      orders: {type: array, title: Orders, items: {type: object}}
  outputSchema: {}
  steps:
    - id: fetch
      kind: transform
      value: {object: {orders: {ref: /input/orders}}}
${steps}  output: {literal: null}
`,
  );
const loop = (items: string) => `    - id: orders
      kind: forEach
      items: ${items}
      concurrency: 4
      maxItems: 1000
      collect: all
      body:
        steps:
          - id: check
            kind: transform
            value: {object: {ok: {literal: true}}}
          - id: send
            kind: transform
            value: {object: {sent: {ref: /item}}}
        output: {ref: /item}
    - id: summarize
      kind: transform
      value: {object: {done: {literal: true}}}
`;
/** fetch, the loop orders over input.orders (four at a time) with check and send, then summarize. */
const ORDERS = workflow("order-notify", loop("{ref: /input/orders}"));
/** The same loop over a list whose path is too long for a phone's header. */
const LONG = workflow(
  "long-items",
  loop("{ref: /steps/fetch/output/customer/openOrders}"),
);
/** A loop just added after fetch: no list yet, nothing in its body. */
const EMPTY = workflow(
  "empty-loop",
  `    - id: loop-1
      kind: forEach
      concurrency: 1
      maxItems: 1000
      collect: all
      body:
        steps: []
        output: {ref: /item}
`,
);

const frame = (canvas: CanvasPage, id: string) =>
  canvas.root.locator(`.loop-frame[data-frame="${id}"]`);
/** The loop's header: its own target on the canvas. */
const header = (canvas: CanvasPage, id: string) => canvas.tileBody(id);

/** A token's color as the browser computes it, to compare with a style. */
const tokenColor = (page: Page, token: string) =>
  page.evaluate((name) => {
    const probe = document.createElement("span");
    probe.style.color = `var(${name})`;
    document.body.append(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }, token);

/** The steps of a sequence, read from Source: "root" or a loop's ID. */
async function stepsOf(page: Page, owner: string): Promise<string[]> {
  type Steps = { id: string; body?: { steps: Steps } }[];
  const source = parse(await sourceText(page)) as { spec: { steps: Steps } };
  const walk = (steps: Steps): Steps | null => {
    for (const step of steps) {
      if (step.id === owner) return step.body!.steps;
      const inner = step.body ? walk(step.body.steps) : null;
      if (inner) return inner;
    }
    return null;
  };
  const steps = owner === "root" ? source.spec.steps : walk(source.spec.steps);
  return (steps ?? []).map((step) => step.id);
}

/** Drags a step by its card onto an insert target, as a person does. */
async function dragTo(
  page: Page,
  canvas: CanvasPage,
  id: string,
  target: string,
) {
  const from = (await canvas.tileBody(id).boundingBox())!;
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    from.x + from.width / 2 + 12,
    from.y + from.height / 2 + 12,
    {
      steps: 4,
    },
  );
  await expect(canvas.root).toHaveClass(/\brevealing\b/);
  const box = (await canvas.insertTarget(target).boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, {
    steps: 10,
  });
  await page.mouse.up();
}

const contains = (outer: DOMRect, inner: DOMRect) =>
  inner.left >= outer.left &&
  inner.right <= outer.right &&
  inner.top >= outer.top &&
  inner.bottom <= outer.bottom;
const box = (locator: Locator) =>
  locator.evaluate((element) => element.getBoundingClientRect().toJSON());

test.describe("Loop frames at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a loop draws as a frame whose header names the loop, its list and how many run at a time", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, ORDERS);
    await expect(frame(canvas, "orders")).toBeVisible();
    const head = header(canvas, "orders");
    await expect(head.locator(".frame-name")).toHaveText("orders");
    await expect(head.locator(".frame-lead")).toHaveText("For each item in");
    await expect(head.locator(".frame-items")).toHaveText("input.orders");
    await expect(head.locator(".frame-suffix")).toHaveText(
      "· up to 4 at a time",
    );
    await expect(head).toHaveAccessibleName(
      "orders, Loop over items, For each item in input.orders · up to 4 at a time, step 2 of 3 in Main sequence",
    );
    // The header is the frame's top band, as wide as the frame.
    const outer = await box(frame(canvas, "orders"));
    const band = await box(head);
    expect(Math.abs(band.left - outer.left)).toBeLessThan(1);
    expect(Math.abs(band.right - outer.right)).toBeLessThan(1);
    expect(Math.abs(band.top - outer.top)).toBeLessThan(1);
    // Its body's steps sit inside it and say which loop repeats them.
    for (const [index, id] of ["check", "send"].entries()) {
      expect(
        contains(outer, await box(canvas.tileBody(id))),
        `${id} inside the frame`,
      ).toBe(true);
      await expect(canvas.tileBody(id)).toHaveAccessibleName(
        new RegExp(`, step ${index + 1} of 2 in For each item of orders$`),
      );
    }
    // The frame and its header take their colors from the tokens.
    const style = await frame(canvas, "orders").evaluate((element) => ({
      border: getComputedStyle(element).borderTopColor,
      fill: getComputedStyle(element).backgroundColor,
    }));
    expect(style.border).toBe(await tokenColor(page, "--flow-line-dim"));
    expect(style.fill).toBe("rgba(0, 0, 0, 0)");
    expect(
      await head
        .locator(".tile-shape")
        .evaluate((element) => getComputedStyle(element).backgroundColor),
    ).toBe(await tokenColor(page, "--surface"));
  });

  test("the header keeps its whole sentence where it fits, and drops For each item in first where it doesn't", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, ORDERS);
    await canvas.closeInspector();
    await page.getByRole("button", { name: "Fit view", exact: true }).click();
    const head = header(canvas, "orders");
    const parts = () =>
      head.evaluate((element) => {
        const part = (name: string) =>
          element.querySelector<HTMLElement>(`.frame-${name}`)!;
        const cut = (name: string) =>
          part(name).scrollWidth > part(name).clientWidth;
        return {
          lead: part("lead").getBoundingClientRect().width > 1,
          items: cut("items"),
          suffix: cut("suffix"),
        };
      });
    // At Fit view a two-step loop has room for all of it.
    await expect
      .poll(parts)
      .toEqual({ lead: true, items: false, suffix: false });
    // Zoomed out, the words go before the list.
    const out = page.getByRole("button", { name: "Zoom out", exact: true });
    await out.click();
    await out.click();
    await expect.poll(parts).toMatchObject({ lead: false, items: false });
  });

  test("edges cross the frame's sides with arrowheads only at steps", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, ORDERS);
    const arrow = (key: string) =>
      canvas.edgeLine(key).getAttribute("marker-end");
    // Into the frame, then on to its first step.
    expect(await arrow("fetch>orders")).toBeNull();
    expect(await arrow("orders>check")).toContain("canvas-arrow-");
    // Out of the frame from its last step, then on to the next step.
    expect(await arrow("send>$exit:orders")).toBeNull();
    expect(await arrow("orders>summarize")).toContain("canvas-arrow-");
    // The loop's output dot sits outside the frame, clear of the body's last +.
    const side = (await box(frame(canvas, "orders"))).right;
    expect((await box(canvas.handle("orders>out"))).left).toBeGreaterThan(side);
    expect(
      (
        await box(
          canvas.insertTarget(
            "Add a step at the end of For each item of orders",
          ),
        )
      ).right,
    ).toBeLessThan(side);
  });

  test("a click on the header selects the loop and a double-click opens its step details", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, ORDERS);
    const head = header(canvas, "orders");
    await head.click();
    await expect(head).toHaveAttribute("aria-pressed", "true");
    await expect(frame(canvas, "orders")).toHaveClass(/\bselected\b/);
    // The frame says the body is in the selection: its steps draw as they are.
    await expect(canvas.tileBody("check")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    // Selected wins over the pointer still on the header, once its transition ends.
    await expect
      .poll(() =>
        frame(canvas, "orders").evaluate(
          (element) => getComputedStyle(element).borderTopColor,
        ),
      )
      .toBe(await tokenColor(page, "--accent"));
    await expect
      .poll(() =>
        head
          .locator(".tile-shape")
          .evaluate((element) => getComputedStyle(element).backgroundColor),
      )
      .toBe(await tokenColor(page, "--selected"));
    await head.dblclick();
    await expect(
      page.getByRole("dialog", { name: "Step details: orders" }),
    ).toBeVisible();
  });

  test("keyboard focus rings the header inside its band, and Enter opens the loop's step details", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, ORDERS);
    await canvas.tileBody("fetch").click();
    await page.keyboard.press("ArrowRight");
    const head = header(canvas, "orders");
    await expect(head).toBeFocused();
    const ring = await head.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        color: style.outlineColor,
        style: style.outlineStyle,
        offset: parseFloat(style.outlineOffset),
        width: parseFloat(style.outlineWidth),
      };
    });
    expect(ring.style).toBe("solid");
    expect(ring.color).toBe(await tokenColor(page, "--focus"));
    expect(ring.width).toBeGreaterThan(0);
    // Inside the band: the ring never draws over the frame's border.
    expect(ring.offset).toBeLessThanOrEqual(-ring.width);
    // The sentence keyboard focus shows lets the pointer through to the steps under it.
    const tip = canvas.root.locator('.frame-tip[data-frame-tip="orders"]');
    await expect(tip).toBeVisible();
    expect(
      await tip.evaluate((element) => {
        const { x, y, width, height } = element.getBoundingClientRect();
        return element.contains(
          document.elementFromPoint(x + width / 2, y + height / 2),
        );
      }),
    ).toBe(false);
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("dialog", { name: "Step details: orders" }),
    ).toBeVisible();
  });

  test("an empty loop offers Add a step inside its frame, which adds to its body", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, EMPTY);
    const head = header(canvas, "loop-1");
    await expect(head.locator(".frame-name")).toHaveText("loop-1");
    await expect(head.locator(".frame-items")).toHaveText("…");
    const slot = canvas.insertTarget("Add a step to For each item of loop-1");
    expect(
      contains(await box(frame(canvas, "loop-1")), await box(slot)),
      "the slot inside the frame",
    ).toBe(true);
    await slot.click();
    await canvas.pick("Transform");
    await canvas.closeInspector();
    expect(await stepsOf(page, "loop-1")).toEqual(["transform-1"]);
    expect(await stepsOf(page, "root")).toEqual(["fetch", "loop-1"]);
  });

  test("the + after the frame adds a step after the loop", async ({ page }) => {
    const canvas = await openWorkflow(page, EMPTY);
    const plus = canvas.insertTarget("Add a step after loop-1");
    expect((await box(plus)).left).toBeGreaterThan(
      (await box(frame(canvas, "loop-1"))).right,
    );
    await plus.click();
    await canvas.pick("Transform");
    await canvas.closeInspector();
    expect(await stepsOf(page, "root")).toEqual([
      "fetch",
      "loop-1",
      "transform-1",
    ]);
    expect(await stepsOf(page, "loop-1")).toEqual([]);
  });
});

test.describe("Editing inside a loop at 1440x900", () => {
  test.use({
    viewport: { width: 1440, height: 900 },
    permissions: ["clipboard-read", "clipboard-write"],
  });

  test("the right arrow enters an empty loop's slot, Enter opens Add a step there, and the left arrow goes back", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, EMPTY);
    await canvas.tileBody("fetch").click();
    await page.keyboard.press("ArrowRight");
    await expect(header(canvas, "loop-1")).toBeFocused();
    await page.keyboard.press("ArrowRight");
    const slot = canvas.insertTarget("Add a step to For each item of loop-1");
    await expect(slot).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(header(canvas, "loop-1")).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(slot).toBeFocused();
    await page.keyboard.press("Enter");
    await canvas.pick("Transform");
    await canvas.closeInspector();
    expect(await stepsOf(page, "loop-1")).toEqual(["transform-1"]);
  });

  test("arrow keys pressed in quick succession go into an empty loop's slot and on past the loop", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, EMPTY);
    await canvas.tileBody("fetch").focus();
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await expect(
      canvas.insertTarget("Add a step to For each item of loop-1"),
    ).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await expect(canvas.tileBody("$end")).toBeFocused();
  });

  test("a step dragged into a loop's body joins it, and dragged out again leaves it", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, ORDERS);
    await canvas.closeInspector();
    await dragTo(
      page,
      canvas,
      "summarize",
      "Move summarize between check and send",
    );
    await canvas.closeInspector();
    expect(await stepsOf(page, "orders")).toEqual([
      "check",
      "summarize",
      "send",
    ]);
    await canvas.ready();
    await dragTo(page, canvas, "summarize", "Move summarize after orders");
    await canvas.closeInspector();
    expect(await stepsOf(page, "root")).toEqual([
      "fetch",
      "orders",
      "summarize",
    ]);
    expect(await stepsOf(page, "orders")).toEqual(["check", "send"]);
  });

  test("an empty body's Add a step takes a dropped step", async ({ page }) => {
    const canvas = await openWorkflow(page, EMPTY);
    await canvas.closeInspector();
    await dragTo(
      page,
      canvas,
      "fetch",
      "Move fetch to For each item of loop-1",
    );
    await canvas.closeInspector();
    expect(await stepsOf(page, "loop-1")).toEqual(["fetch"]);
  });

  test("Move to… offers every place but the loop's own body", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, ORDERS);
    await canvas.closeInspector();
    await canvas.tileBody("orders").hover();
    await canvas.root
      .getByRole("button", { name: "More commands for orders", exact: true })
      .click();
    await page.getByRole("menuitem", { name: "Move to…" }).click();
    await expect(
      canvas.insertTarget("Move orders after summarize"),
    ).toBeVisible();
    await expect(
      canvas.insertTarget("Move orders between check and send"),
    ).toHaveCount(0);
  });

  test("a step deleted from a loop's body comes back with Undo and goes again with Redo", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, ORDERS);
    await canvas.closeInspector();
    await canvas.tileBody("send").click();
    await page.keyboard.press("Delete");
    await expect(canvas.tile("send")).toHaveCount(0);
    await page.keyboard.press("ControlOrMeta+z");
    await expect(canvas.tile("send")).toHaveCount(1);
    await page.keyboard.press("ControlOrMeta+Shift+z");
    await expect(canvas.tile("send")).toHaveCount(0);
    await canvas.closeInspector();
    expect(await stepsOf(page, "orders")).toEqual(["check"]);
  });

  test("a copied loop pastes after the focused step with new IDs for it and its steps", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, ORDERS);
    await canvas.tileBody("orders").click();
    await page.keyboard.press("ControlOrMeta+c");
    await canvas.tileBody("summarize").click();
    await page.keyboard.press("ControlOrMeta+v");
    await expect(frame(canvas, "orders-1")).toBeVisible();
    await canvas.closeInspector();
    expect(await stepsOf(page, "root")).toEqual([
      "fetch",
      "orders",
      "summarize",
      "orders-1",
    ]);
    expect(await stepsOf(page, "orders-1")).toEqual(["check-1", "send-1"]);
  });
});

test.describe("Loop frames on a phone", () => {
  test.use({ viewport: { width: 360, height: 640 } });

  test("the header shortens on one line and shows its whole sentence on hover and focus", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, LONG);
    // The pointer away from the canvas: only keyboard focus shows the sentence.
    await page.mouse.move(0, 0);
    // Arrow keys reach the loop, which comes into view.
    await canvas.tileBody("fetch").focus();
    await page.keyboard.press("ArrowRight");
    const head = header(canvas, "orders");
    await expect(head).toBeFocused();
    await expect(head).toBeInViewport();
    const sentence =
      "orders: For each item in steps.fetch.output.customer.openOrders · up to 4 at a time";
    const shape = await head.evaluate((element) => {
      const line = element.querySelector<HTMLElement>(".frame-title")!;
      const items = element.querySelector<HTMLElement>(".frame-items")!;
      const lead = element.querySelector<HTMLElement>(".frame-lead")!;
      return {
        lines: Math.round(
          line.getBoundingClientRect().height /
            items.getBoundingClientRect().height,
        ),
        itemsCut: items.scrollWidth > items.clientWidth,
        leadWidth: lead.getBoundingClientRect().width,
        leadText: lead.textContent,
      };
    });
    expect(shape.lines).toBe(1);
    expect(shape.itemsCut).toBe(true);
    // "For each item in" goes first, from sight only.
    expect(shape.leadWidth).toBeLessThanOrEqual(1);
    expect(shape.leadText).toBe("For each item in");
    await expect(head).toHaveAccessibleName(
      /^orders, Loop over items, For each item in steps\.fetch\.output\.customer\.openOrders · up to 4 at a time, /,
    );
    // Keyboard focus shows the whole sentence, and so does the pointer.
    const tip = canvas.root.locator('.frame-tip[data-frame-tip="orders"]');
    await expect(tip).toHaveText(sentence);
    await expect(tip).toBeVisible();
    // On the canvas, even where the frame runs past its right edge.
    const area = await box(canvas.root);
    const shown = await box(tip);
    expect(shown.left).toBeGreaterThanOrEqual(area.left);
    expect(shown.right).toBeLessThanOrEqual(area.right);
    await page.keyboard.press("ArrowRight");
    await expect(tip).toBeHidden();
    await head.hover();
    await expect(tip).toBeVisible();
  });
});
