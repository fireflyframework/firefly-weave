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
// When a platform draft saves itself: 2 s after the last change, never
// within 10 s of the previous save, and at once when Studio flushes (closing
// step details, switching tabs, leaving the editor, closing the window).
// A failed save waits for the next change or flush; nothing is retried in a
// loop.
import { signal } from "@angular/core";

export interface AutosaveClock {
  now(): number;
  set(run: () => void, ms: number): unknown;
  clear(handle: unknown): void;
}
export type AutosaveState = "idle" | "waiting" | "saving" | "saved" | "failed";
const realClock: AutosaveClock = {
  now: () => Date.now(),
  set: (run, ms) => setTimeout(run, ms),
  clear: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export class Autosave {
  readonly state = signal<AutosaveState>("idle");
  readonly savedAt = signal<number | null>(null);
  private dirty = false;
  private timer: unknown = null;
  private lastSave = -Infinity;
  private running: Promise<boolean> | null = null;
  private disposed = false;

  constructor(
    private readonly run: () => Promise<boolean>,
    private readonly clock: AutosaveClock = realClock,
    private readonly idleMs = 2000,
    private readonly gapMs = 10000,
  ) {}

  touch(): void {
    if (this.disposed) return;
    this.dirty = true;
    if (this.state() !== "saving") this.state.set("waiting");
    this.schedule();
  }
  private schedule(): void {
    if (this.timer !== null) this.clock.clear(this.timer);
    const wait = Math.max(
      this.idleMs,
      this.lastSave + this.gapMs - this.clock.now(),
    );
    this.timer = this.clock.set(() => {
      this.timer = null;
      void this.flush();
    }, wait);
  }
  async flush(): Promise<boolean> {
    if (this.timer !== null) {
      this.clock.clear(this.timer);
      this.timer = null;
    }
    if (this.running) await this.running;
    if (!this.dirty || this.disposed) return this.state() !== "failed";
    this.dirty = false;
    this.state.set("saving");
    this.running = this.run().catch(() => false);
    const ok = await this.running;
    this.running = null;
    this.lastSave = this.clock.now();
    if (ok) {
      this.savedAt.set(this.clock.now());
      this.state.set(this.dirty ? "waiting" : "saved");
      if (this.dirty) this.schedule();
    } else {
      this.dirty = true;
      this.state.set("failed");
    }
    return ok;
  }
  /** Another draft opened: what waited belonged to the previous one, and nothing is saved for it. */
  cancel(): void {
    if (this.timer !== null) this.clock.clear(this.timer);
    this.timer = null;
    this.dirty = false;
    this.savedAt.set(null);
    this.state.set("idle");
  }
  dispose(): void {
    this.disposed = true;
    if (this.timer !== null) this.clock.clear(this.timer);
  }
}
