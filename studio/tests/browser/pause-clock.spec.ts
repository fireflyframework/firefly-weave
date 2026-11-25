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
// pauseClock on a runner that stalls: the page's fake clock follows real
// time until the pause reaches it, so a stall can carry it past the moment
// pauseClock asked for.
import { expect, test } from "@playwright/test";
import { pauseClock } from "./support";

test("pauseClock stops the clock when a stall of more than 1 s delays the pause", async ({
  page,
}) => {
  await page.clock.install();
  await page.goto("data:text/html,<p>clock</p>");
  // The stall a busy runner can put between reading the page's time and the
  // pause reaching the page, once: the running clock moves on 1.5 s.
  const pauseAt = page.clock.pauseAt.bind(page.clock);
  let stalls = 1;
  page.clock.pauseAt = async (time) => {
    if (stalls-- > 0) await page.clock.fastForward(1_500);
    return pauseAt(time);
  };
  await pauseClock(page);
  // Paused, only the test moves the clock: exactly the time it runs for.
  const paused = await page.evaluate(() => Date.now());
  await page.clock.runFor(1_000);
  expect(await page.evaluate(() => Date.now())).toBe(paused + 1_000);
});
