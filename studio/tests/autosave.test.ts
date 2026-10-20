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
import { describe, expect, it } from "vitest";
import { Autosave, type AutosaveClock } from "../src/app/editor/state/autosave";

/** A clock the test moves by hand. */
function clock() {
  let now = 0;
  const timers = new Map<number, { at: number; run: () => void }>();
  let next = 1;
  const value: AutosaveClock = {
    now: () => now,
    set: (run, ms) => {
      timers.set(next, { at: now + ms, run });
      return next++;
    },
    clear: (handle) => void timers.delete(handle as number),
  };
  const advance = async (ms: number) => {
    now += ms;
    for (const [id, timer] of [...timers].sort((a, b) => a[1].at - b[1].at))
      if (timer.at <= now) {
        timers.delete(id);
        timer.run();
      }
    await Promise.resolve();
    await Promise.resolve();
  };
  return { value, advance };
}

describe("autosave", () => {
  it("saves 2 s after the last change", async () => {
    const time = clock();
    let saves = 0;
    const autosave = new Autosave(async () => (saves++, true), time.value);
    autosave.touch();
    await time.advance(1500);
    autosave.touch();
    await time.advance(1500);
    expect(saves).toBe(0);
    await time.advance(600);
    expect(saves).toBe(1);
    expect(autosave.state()).toBe("saved");
  });
  it("never saves twice within 10 s", async () => {
    const time = clock();
    let saves = 0;
    const autosave = new Autosave(async () => (saves++, true), time.value);
    autosave.touch();
    await time.advance(2000);
    expect(saves).toBe(1);
    autosave.touch();
    await time.advance(9999);
    expect(saves).toBe(1);
    await time.advance(1);
    expect(saves).toBe(2);
  });
  it("flushes at once, and reports a failed save until the next one", async () => {
    const time = clock();
    let fail = true;
    const autosave = new Autosave(async () => !fail, time.value);
    autosave.touch();
    expect(await autosave.flush()).toBe(false);
    expect(autosave.state()).toBe("failed");
    fail = false;
    expect(await autosave.flush()).toBe(true);
    expect(autosave.state()).toBe("saved");
    expect(await autosave.flush()).toBe(true); // nothing waits: no save
  });
  it("forgets a waiting change when another draft opens", async () => {
    const time = clock();
    let saves = 0;
    const autosave = new Autosave(async () => (saves++, true), time.value);
    autosave.touch();
    autosave.cancel();
    expect(autosave.state()).toBe("idle");
    await time.advance(15_000);
    expect(saves).toBe(0);
    expect(await autosave.flush()).toBe(true);
    expect(saves).toBe(0);
  });
});
