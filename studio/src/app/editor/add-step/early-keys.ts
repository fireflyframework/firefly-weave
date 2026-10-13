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
// Keys pressed between opening the Add a step panel and its search taking
// focus. The panel draws on the next frame (and loads with its first
// opening), so a person who presses N and types at once would lose their
// first letters, or move the canvas's focus with an arrow. These keys are
// held here, in order, and the panel replays them into its search.

/** A held key: what the panel needs to replay it. */
export interface EarlyKey {
  readonly key: string;
  readonly code: string;
  readonly shiftKey: boolean;
}

const MOVES = new Set([
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Enter",
  "Escape",
  "Backspace",
]);
const FIELDS =
  "input, textarea, select, [contenteditable]:not([contenteditable='false'])";

/** Typed characters and the panel's own keys; shortcuts with a modifier pass. */
export function heldKey(
  event: Pick<
    KeyboardEvent,
    "key" | "ctrlKey" | "metaKey" | "altKey" | "isComposing"
  >,
): boolean {
  if (event.ctrlKey || event.metaKey || event.altKey || event.isComposing)
    return false;
  return event.key.length === 1 || MOVES.has(event.key);
}

export class EarlyKeys {
  private keys: EarlyKey[] = [];
  private listening = false;
  /** `panel` matches the panel's element: its search is not another field. */
  constructor(private readonly panel: string) {}

  private readonly hold = (event: KeyboardEvent) => {
    if (!heldKey(event)) return;
    const target = event.target as Partial<Pick<Element, "closest">> | null;
    // A field outside the panel keeps what is typed into it.
    if (target?.closest?.(FIELDS) && !target.closest(this.panel)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    this.keys.push({
      key: event.key,
      code: event.code,
      shiftKey: event.shiftKey,
    });
  };

  /** Starts holding keys, forgetting any held before. */
  start() {
    this.keys = [];
    if (this.listening || typeof document === "undefined") return;
    document.addEventListener("keydown", this.hold, true);
    this.listening = true;
  }
  /** Stops holding keys and hands over those held, oldest first. */
  take(): EarlyKey[] {
    const keys = this.keys;
    this.stop();
    return keys;
  }
  stop() {
    this.keys = [];
    if (!this.listening) return;
    document.removeEventListener("keydown", this.hold, true);
    this.listening = false;
  }
}
