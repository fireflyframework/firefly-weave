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
import {
  MAX_BACKOFF_MS,
  Poller,
  pollDelay,
  updatedText,
  type PollEnvironment,
} from "../src/app/operate/operate-store";

/** A clock, timers and a visibility switch the test drives by hand. */
class FakeBrowser implements PollEnvironment {
  time = 1_000_000;
  shown = true;
  private next = 1;
  timers = new Map<number, { at: number; run: () => void }>();
  private listeners = new Set<() => void>();
  now() {
    return this.time;
  }
  setTimeout(run: () => void, milliseconds: number) {
    const handle = this.next++;
    this.timers.set(handle, { at: this.time + milliseconds, run });
    return handle;
  }
  clearTimeout(handle: number) {
    this.timers.delete(handle);
  }
  visible() {
    return this.shown;
  }
  onVisibilityChange(listener: () => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  setVisible(shown: boolean) {
    this.shown = shown;
    for (const listener of this.listeners) listener();
  }
  /** The single pending wait, in milliseconds from now. */
  pending() {
    expect(this.timers.size).toBeLessThanOrEqual(1);
    const [timer] = this.timers.values();
    return timer ? timer.at - this.time : null;
  }
  /** Moves time forward, firing the timers that come due. */
  async advance(milliseconds: number) {
    const end = this.time + milliseconds;
    for (;;) {
      const due = [...this.timers.entries()]
        .filter(([, timer]) => timer.at <= end)
        .sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      this.timers.delete(due[0]);
      this.time = due[1].at;
      due[1].run();
      await settle();
    }
    this.time = end;
  }
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("pollDelay", () => {
  it("doubles after each failure and stops at 60 s", () => {
    expect(pollDelay(10_000, 0)).toBe(10_000);
    expect(pollDelay(10_000, 1)).toBe(20_000);
    expect(pollDelay(10_000, 2)).toBe(40_000);
    expect(pollDelay(10_000, 3)).toBe(MAX_BACKOFF_MS);
    expect(pollDelay(15_000, 9)).toBe(60_000);
  });
});

describe("Poller", () => {
  it("does not report freshness for a skipped or superseded load", async () => {
    const browser = new FakeBrowser();
    const poller = new Poller(async () => false, 10_000, browser);
    await poller.refresh();
    expect(poller.lastSuccessAt).toBeNull();
    expect(poller.failures).toBe(0);
  });

  it("loads at once, then on the interval", async () => {
    const browser = new FakeBrowser();
    let loads = 0;
    const poller = new Poller(async () => void loads++, 10_000, browser);
    poller.start();
    await settle();
    expect(loads).toBe(1);
    expect(poller.lastSuccessAt).toBe(browser.time);
    expect(browser.pending()).toBe(10_000);
    await browser.advance(10_000);
    expect(loads).toBe(2);
    poller.stop();
    expect(browser.pending()).toBeNull();
  });

  it("can wait first when the page has just loaded by itself", async () => {
    const browser = new FakeBrowser();
    let loads = 0;
    const poller = new Poller(async () => void loads++, 15_000, browser);
    poller.start(false);
    await settle();
    expect(loads).toBe(0);
    expect(browser.pending()).toBe(15_000);
    await browser.advance(15_000);
    expect(loads).toBe(1);
    poller.stop();
  });

  it("backs off after failures and resets after a success", async () => {
    const browser = new FakeBrowser();
    let fail = true;
    const poller = new Poller(
      async () => {
        if (fail) throw new Error("down");
      },
      10_000,
      browser,
    );
    poller.start();
    await settle();
    expect(poller.failures).toBe(1);
    expect(browser.pending()).toBe(20_000);
    await browser.advance(20_000);
    expect(browser.pending()).toBe(40_000);
    await browser.advance(40_000);
    expect(browser.pending()).toBe(60_000);
    await browser.advance(60_000);
    expect(browser.pending()).toBe(60_000);
    fail = false;
    await browser.advance(60_000);
    expect(poller.failures).toBe(0);
    expect(browser.pending()).toBe(10_000);
    poller.stop();
  });

  it("pauses while the page is hidden and catches up when it shows", async () => {
    const browser = new FakeBrowser();
    let loads = 0;
    const poller = new Poller(async () => void loads++, 15_000, browser);
    poller.start();
    await settle();
    browser.setVisible(false);
    expect(browser.pending()).toBeNull();
    await browser.advance(120_000);
    expect(loads).toBe(1);
    browser.setVisible(true);
    await settle();
    expect(loads).toBe(2);
    expect(browser.pending()).toBe(15_000);
    // Shown again before the interval passed: no extra load, just the wait.
    browser.setVisible(false);
    await browser.advance(5_000);
    browser.setVisible(true);
    await settle();
    expect(loads).toBe(2);
    expect(browser.pending()).toBe(15_000);
    poller.stop();
  });

  it("refreshes now and restarts the wait", async () => {
    const browser = new FakeBrowser();
    let loads = 0;
    const poller = new Poller(async () => void loads++, 10_000, browser);
    poller.start();
    await settle();
    await browser.advance(4_000);
    await poller.refresh();
    expect(loads).toBe(2);
    expect(browser.pending()).toBe(10_000);
    poller.setInterval(2_000);
    expect(browser.pending()).toBe(2_000);
    poller.stop();
  });

  it("never loads twice at once: a refresh during a load queues one follow-up that every caller shares", async () => {
    const browser = new FakeBrowser();
    const releases: (() => void)[] = [];
    let started = 0;
    const poller = new Poller(
      () =>
        new Promise<void>((resolve) => {
          started++;
          releases.push(resolve);
        }),
      10_000,
      browser,
    );
    poller.start();
    expect(poller.busy).toBe(true);
    expect(started).toBe(1);
    const settled: string[] = [];
    const first = poller.refresh().then(() => settled.push("first"));
    const second = poller.refresh().then(() => settled.push("second"));
    // Nothing overlaps: the follow-up waits for the load in flight.
    expect(started).toBe(1);
    releases[0]();
    await settle();
    expect(started).toBe(2);
    expect(settled).toEqual([]);
    // A call during the follow-up queues the next one, not a third at once.
    const third = poller.refresh().then(() => settled.push("third"));
    expect(started).toBe(2);
    releases[1]();
    await Promise.all([first, second]);
    expect(settled).toEqual(["first", "second"]);
    expect(started).toBe(3);
    releases[2]();
    await third;
    expect(settled).toEqual(["first", "second", "third"]);
    expect(started).toBe(3);
    expect(poller.busy).toBe(false);
    expect(browser.pending()).toBe(10_000);
    poller.stop();
  });

  it("does not run a refresh queued before it stopped", async () => {
    const browser = new FakeBrowser();
    const releases: (() => void)[] = [];
    let started = 0;
    const poller = new Poller(
      () =>
        new Promise<void>((resolve) => {
          started++;
          releases.push(resolve);
        }),
      10_000,
      browser,
    );
    poller.start();
    expect(started).toBe(1);
    const queued = poller.refresh();
    // The page goes away while its load is in flight.
    poller.stop();
    releases[0]();
    await settle();
    expect(started).toBe(1);
    for (const release of releases) release();
    await queued;
    expect(poller.busy).toBe(false);
    expect(browser.pending()).toBeNull();
  });

  it("does not load, stamp or fail while paused, and keeps its schedule", async () => {
    const browser = new FakeBrowser();
    let loads = 0;
    let paused = true;
    const poller = new Poller(
      async () => void loads++,
      15_000,
      browser,
      () => paused,
    );
    poller.start(false);
    await settle();
    await browser.advance(15_000);
    expect(loads).toBe(0);
    expect(poller.lastSuccessAt).toBeNull();
    expect(poller.failures).toBe(0);
    expect(browser.pending()).toBe(15_000);
    await browser.advance(45_000);
    expect(loads).toBe(0);
    expect(poller.lastSuccessAt).toBeNull();
    paused = false;
    await browser.advance(15_000);
    expect(loads).toBe(1);
    expect(poller.lastSuccessAt).toBe(browser.time);
    poller.stop();
  });

  it("does not catch up on showing the page while paused", async () => {
    const browser = new FakeBrowser();
    let loads = 0;
    let paused = false;
    const poller = new Poller(
      async () => void loads++,
      15_000,
      browser,
      () => paused,
    );
    poller.start();
    await settle();
    browser.setVisible(false);
    paused = true;
    await browser.advance(120_000);
    browser.setVisible(true);
    await settle();
    expect(loads).toBe(1);
    expect(browser.pending()).toBe(15_000);
    paused = false;
    await browser.advance(15_000);
    expect(loads).toBe(2);
    poller.stop();
  });

  it("still loads on an explicit refresh while paused", async () => {
    const browser = new FakeBrowser();
    let loads = 0;
    const poller = new Poller(
      async () => void loads++,
      15_000,
      browser,
      () => true,
    );
    poller.start(false);
    await poller.refresh();
    expect(loads).toBe(1);
    expect(poller.lastSuccessAt).toBe(browser.time);
    poller.stop();
  });
});

describe("updatedText", () => {
  it("says when the page last loaded", () => {
    const now = Date.parse("2026-10-08T12:00:00Z");
    expect(updatedText(null, now)).toBe("");
    expect(updatedText(now - 2_000, now)).toBe("Updated just now");
    expect(updatedText(now - 12_400, now)).toBe("Updated 12 s ago");
    expect(updatedText(now - 180_000, now)).toBe("Updated 3 min ago");
  });
});
