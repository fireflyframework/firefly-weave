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
// How the Operate pages stay current: each page loads on an interval while it
// is visible, waits twice as long after each failure (at most 60 s), and says
// when it last loaded. Pure apart from the browser environment, which tests
// replace.
import { relativeTime } from "../format";

/** The parts of the browser a poller uses; tests pass a fake. */
export interface PollEnvironment {
  now(): number;
  setTimeout(run: () => void, milliseconds: number): number;
  clearTimeout(handle: number): void;
  visible(): boolean;
  onVisibilityChange(listener: () => void): () => void;
}

export const browserEnvironment: PollEnvironment = {
  now: () => Date.now(),
  setTimeout: (run, milliseconds) => window.setTimeout(run, milliseconds),
  clearTimeout: (handle) => window.clearTimeout(handle),
  visible: () => document.visibilityState === "visible",
  onVisibilityChange: (listener) => {
    document.addEventListener("visibilitychange", listener);
    return () => document.removeEventListener("visibilitychange", listener);
  },
};

/** The longest wait after repeated failures. */
export const MAX_BACKOFF_MS = 60_000;

/** The wait before the next load: the interval, doubled per failure, at most 60 s. */
export function pollDelay(intervalMs: number, failures: number): number {
  return failures <= 0
    ? intervalMs
    : Math.min(intervalMs * 2 ** failures, MAX_BACKOFF_MS);
}

/**
 * Loads a page now and then on an interval while the page is visible. The
 * load throws to report a failure; the page shows the failure itself.
 *
 * While `paused` says so (a form is open), a scheduled load is skipped: it
 * does not load, does not count as a success and does not count as a failure.
 * A person's own Refresh still loads.
 */
export class Poller {
  /** When the last load succeeded (epoch milliseconds). */
  lastSuccessAt: number | null = null;
  /** Failures since the last success. */
  failures = 0;
  private timer: number | null = null;
  private loading = false;
  private current: Promise<void> | null = null;
  private followUp: Promise<void> | null = null;
  private stopped = true;
  private unsubscribe: (() => void) | null = null;

  constructor(
    private readonly load: () => Promise<void>,
    private intervalMs: number,
    private readonly env: PollEnvironment = browserEnvironment,
    private readonly paused: () => boolean = () => false,
  ) {}

  get interval() {
    return this.intervalMs;
  }
  get busy() {
    return this.loading;
  }

  /**
   * Keeps loading on the interval: at once, or (`loadNow` false) after the
   * first wait when the page has just loaded by itself.
   */
  start(loadNow = true) {
    if (!this.stopped) return;
    this.stopped = false;
    this.unsubscribe = this.env.onVisibilityChange(() => this.visibility());
    if (loadNow) void this.run();
    else this.schedule();
  }

  /** Stops loading; a load in flight finishes but schedules nothing. */
  stop() {
    this.stopped = true;
    this.clear();
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  /** A new interval, for example 2 s while a job runs; the wait restarts. */
  setInterval(milliseconds: number) {
    if (milliseconds === this.intervalMs) return;
    this.intervalMs = milliseconds;
    if (!this.stopped && !this.loading) this.schedule();
  }

  /**
   * Loads now (Refresh, Try again, a changed tab) and restarts the wait. A
   * load never overlaps another: asked during one, it runs once more right
   * after it, so what the person just changed is what loads. Every caller
   * during the same load shares that one follow-up, which never runs once
   * the poller has stopped (the page is gone).
   */
  refresh(): Promise<void> {
    if (this.current === null) return this.run();
    this.followUp ??= this.current.then(() => {
      this.followUp = null;
      if (this.stopped) return;
      return this.current ?? this.run();
    });
    return this.followUp;
  }

  private run(): Promise<void> {
    this.clear();
    this.loading = true;
    this.current = this.execute().finally(() => {
      this.current = null;
    });
    return this.current;
  }

  private async execute() {
    try {
      await this.load();
      this.failures = 0;
      this.lastSuccessAt = this.env.now();
    } catch {
      this.failures++;
    } finally {
      this.loading = false;
      if (!this.stopped) this.schedule();
    }
  }

  /** A scheduled load, or the catch-up on showing the page. */
  private tick() {
    if (this.loading) return;
    if (this.paused()) return this.schedule();
    void this.run();
  }

  private schedule() {
    this.clear();
    if (!this.env.visible()) return;
    this.timer = this.env.setTimeout(
      () => {
        this.timer = null;
        this.tick();
      },
      pollDelay(this.intervalMs, this.failures),
    );
  }

  private clear() {
    if (this.timer !== null) this.env.clearTimeout(this.timer);
    this.timer = null;
  }

  private visibility() {
    if (this.stopped) return;
    if (!this.env.visible()) return this.clear();
    const age =
      this.lastSuccessAt === null
        ? Infinity
        : this.env.now() - this.lastSuccessAt;
    if (age >= this.intervalMs) this.tick();
    else if (!this.loading) this.schedule();
  }
}

/** "Updated 12 s ago" from the last successful load; "" before the first. */
export function updatedText(lastSuccessAt: number | null, now: number): string {
  if (lastSuccessAt === null) return "";
  const seconds = Math.max(0, Math.floor((now - lastSuccessAt) / 1000));
  if (seconds < 5) return "Updated just now";
  if (seconds < 60) return `Updated ${seconds} s ago`;
  return `Updated ${relativeTime(new Date(lastSuccessAt), new Date(now))}`;
}
