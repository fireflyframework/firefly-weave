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
// Measures the woven w of the weave wordmark in a rendered lockup. In each
// pixel row through the crossing, it finds how much ink fills the darkest
// pixel between the amber thread and the paper strand the thread passes over.
// The lockup is drawn in paths only, so installed fonts never change the result.
import type { Locator } from "@playwright/test";

/** What one render of a lockup shows at the woven w's crossing. */
export interface Crossing {
  /** Pixel rows checked: the top three quarters of the thread. */
  rows: number;
  /** Row sides where a paper strand sits within 8 CSS pixels of the thread. */
  measured: number;
  /** The fullest darkest pixel over the measured sides: 0 is bare charcoal, 1 is solid ink. */
  depth: number;
  /** Rows with no amber pixel: the thread vanished. */
  missing: number[];
}

/** The thread's box and the lockup's viewBox origin and width, in drawing units. */
function threadBox(svg: string) {
  const viewBox = svg
    .match(/viewBox="([^"]+)"/)?.[1]
    .split(/\s+/)
    .map(Number);
  const d = svg.match(/<path class="weave-thread" d="([^"]+)"/)?.[1];
  if (!viewBox || !d)
    throw Error("The lockup has no viewBox or no weave-thread path.");
  const values = (d.match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number);
  const xs = values.filter((_, i) => i % 2 === 0);
  const ys = values.filter((_, i) => i % 2 === 1);
  return {
    x: viewBox[0],
    y: viewBox[1],
    width: viewBox[2],
    left: Math.min(...xs),
    right: Math.max(...xs),
    top: Math.min(...ys),
    bottom: Math.max(...ys),
  };
}

/**
 * Screenshots a reversed lockup drawn on the charcoal ground (#10110f) and
 * measures its crossing. The pixels are read in a blank page of the same
 * browser context, which has no content security policy.
 */
export async function crossingOf(image: Locator): Promise<Crossing> {
  const page = image.page();
  const source = await image.evaluate((e: HTMLImageElement) => e.currentSrc);
  const box = threadBox(await (await page.request.get(source)).text());
  const bounds = await image.boundingBox();
  if (!bounds) throw Error("The lockup is not rendered.");
  const dpr = await page.evaluate(() => devicePixelRatio);
  const clip = {
    x: Math.floor(bounds.x),
    y: Math.floor(bounds.y),
    width: Math.ceil(bounds.width) + 2,
    height: Math.ceil(bounds.height) + 2,
  };
  const png = await page.screenshot({ clip });
  const k = (bounds.width / box.width) * dpr; // device pixels per drawing unit
  const [dx, dy] = [(bounds.x - clip.x) * dpr, (bounds.y - clip.y) * dpr];
  const thread = {
    left: dx + (box.left - box.x) * k,
    right: dx + (box.right - box.x) * k,
    top: dy + (box.top - box.y) * k,
    bottom: dy + (box.bottom - box.y) * k,
  };
  const blank = await page.context().newPage();
  try {
    return await blank.evaluate(
      async ({ data, thread, dpr }) => {
        const image = new Image();
        image.src = `data:image/png;base64,${data}`;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        const context = canvas.getContext("2d")!;
        context.drawImage(image, 0, 0);
        const { data: rgba, width } = context.getImageData(
          0,
          0,
          canvas.width,
          canvas.height,
        );
        const pixel = (x: number, y: number) => {
          const at = (y * width + Math.min(Math.max(x, 0), width - 1)) * 4;
          return [rgba[at], rgba[at + 1], rgba[at + 2]];
        };
        // Ink over charcoal (16, 17, 15), read from red, which paper (243)
        // and amber (255) share.
        const coverage = (p: number[]) => Math.max(0, (p[0] - 16) / 224);
        // Amber keeps little of its blue over charcoal (59 of 239); paper
        // keeps nearly all of it (220 of 227).
        const amber = (p: number[]) =>
          p[0] > 16 && (p[2] - 15) / (p[0] - 16) < 0.5;
        const missing: number[] = [];
        let rows = 0;
        let measured = 0;
        let depth = 0;
        // Both gaps lie in the top three quarters of the thread; lower down,
        // the thread meets its own V at the apex. The upper gap sits on the
        // thread's right in its top three tenths, the lower gap on its left.
        const end = thread.top + 0.75 * (thread.bottom - thread.top);
        const upper = thread.top + 0.3 * (thread.bottom - thread.top);
        for (let y = Math.ceil(thread.top); y + 1 <= end; y++) {
          rows++;
          let core = -1;
          for (
            let x = Math.floor(thread.left) - 1;
            x <= Math.ceil(thread.right);
            x++
          )
            if (
              amber(pixel(x, y)) &&
              (core < 0 || coverage(pixel(x, y)) > coverage(pixel(core, y)))
            )
              core = x;
          if (core < 0) {
            missing.push(y);
            continue;
          }
          for (const step of y + 1 <= upper ? [-1, 1] : [-1]) {
            let low = 1;
            for (let n = 1; n <= 8 * dpr; n++) {
              const p = pixel(core + step * n, y);
              if (!amber(p) && coverage(p) > 0.5) {
                measured++;
                depth = Math.max(depth, low);
                break;
              }
              low = Math.min(low, coverage(p));
            }
          }
        }
        return {
          rows,
          measured,
          depth: Math.round(depth * 100) / 100,
          missing,
        };
      },
      { data: png.toString("base64"), thread, dpr },
    );
  } finally {
    await blank.close();
  }
}
