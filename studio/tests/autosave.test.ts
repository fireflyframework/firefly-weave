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
  it("a save running when another draft opens says nothing about the new one", async () => {
    const time = clock();
    let answer!: (ok: boolean) => void;
    const seen: boolean[] = [];
    const autosave = new Autosave(
      (generation) =>
        new Promise<boolean>((done) => {
          answer = (ok) => {
            seen.push(autosave.current(generation));
            done(ok);
          };
        }),
      time.value,
    );
    autosave.touch();
    const first = autosave.flush();
    expect(autosave.state()).toBe("saving");
    autosave.cancel();
    answer(false);
    await first;
    // The run learns that its draft is no longer open, and its failure
    // neither marks the new draft failed nor leaves it dirty.
    expect(seen).toEqual([false]);
    expect(autosave.state()).toBe("idle");
    expect(autosave.savedAt()).toBeNull();
    expect(await autosave.flush()).toBe(true);
  });
  it("a successful save of the previous draft shows no Saved time", async () => {
    const time = clock();
    let answer!: (ok: boolean) => void;
    const autosave = new Autosave(
      () => new Promise<boolean>((done) => (answer = done)),
      time.value,
    );
    autosave.touch();
    const first = autosave.flush();
    autosave.cancel();
    answer(true);
    await first;
    expect(autosave.savedAt()).toBeNull();
    expect(autosave.state()).toBe("idle");
  });
  it("never runs two saves at once: a flush waits for the one running", async () => {
    const time = clock();
    let running = 0;
    let most = 0;
    const answers: ((ok: boolean) => void)[] = [];
    const autosave = new Autosave(
      () =>
        new Promise<boolean>((done) => {
          most = Math.max(most, ++running);
          answers.push((ok) => {
            running--;
            done(ok);
          });
        }),
      time.value,
    );
    autosave.touch();
    const first = autosave.flush();
    autosave.touch();
    const second = autosave.flush();
    await Promise.resolve();
    expect(answers).toHaveLength(1);
    answers[0](true);
    await first;
    await Promise.resolve();
    await Promise.resolve();
    expect(answers).toHaveLength(2);
    answers[1](true);
    expect(await second).toBe(true);
    expect(most).toBe(1);
  });
});
