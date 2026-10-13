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
// What a platform draft keeps beside its workflow until the platform keeps
// it too: its canvas (notes, trigger choices, step notes and the recipes of
// the actions it owns), for the signed-in account, on this computer, under
// the connected key scheme. Signing out of a platform, or removing it,
// deletes them with every other key of that platform. Canvas data is never
// a secret (the sidecar holds none).
import {
  browserBackend,
  connectedPrefix,
  purgeProfile,
  storeKey,
  type AccountScope,
  type StoreBackend,
} from "./browser-store";
import { canvasBytes, emptyCanvas, type CanvasSidecar } from "./canvas-sidecar";

/** Where a draft's canvas is kept: on this computer, or only while it is open. */
export type KeptWhere = "computer" | "memory";

/** The key of a draft's canvas, or null when it can't be attributed to an account. */
export async function draftCanvasKey(
  scope: AccountScope,
  draftId: string,
): Promise<string | null> {
  try {
    const prefix = await connectedPrefix(scope);
    return prefix ? storeKey(prefix, `canvas.${draftId}`) : null;
  } catch {
    return null;
  }
}

/** How many canvases an account keeps on this computer, and their total size in characters. */
export const KEPT_CANVASES = 50;
export const KEPT_CHARACTERS = 2 * 1024 * 1024;
const KEPT_AT = /^\{"keptAt":(\d+),/;

export class DraftCanvases {
  constructor(
    private readonly backend: StoreBackend = browserBackend(),
    private readonly now: () => number = Date.now,
  ) {}

  /** The stored canvas, as JSON, or null when none is kept for this account. */
  async read(scope: AccountScope, draftId: string): Promise<unknown | null> {
    const key = await draftCanvasKey(scope, draftId);
    if (!key) return null;
    const raw = await this.backend.get(key);
    if (raw === null) return null;
    try {
      const kept = JSON.parse(raw) as unknown;
      return kept && typeof kept === "object" && "canvas" in kept
        ? (kept.canvas as unknown)
        : null;
    } catch {
      return null;
    }
  }

  /**
   * Keeps the canvas (an empty one removes the key); "memory" when it can't,
   * and then the older canvas goes too, so it never comes back in its place.
   * Each account keeps its KEPT_CANVASES most recently changed canvases,
   * within KEPT_CHARACTERS: older ones go first.
   */
  async write(
    scope: AccountScope,
    draftId: string,
    canvas: CanvasSidecar,
  ): Promise<KeptWhere> {
    const key = await draftCanvasKey(scope, draftId);
    if (!key) return "memory";
    if (canvasBytes(canvas) === canvasBytes(emptyCanvas())) {
      await this.backend.remove(key);
      return "computer";
    }
    const value = JSON.stringify({ keptAt: this.now(), canvas });
    const before = (await this.backend.get(key))?.length ?? -1;
    if (!(await this.backend.set(key, value))) {
      await this.backend.remove(key);
      return "memory";
    }
    // Only a new or larger canvas can take the account over what it keeps.
    if (value.length > before)
      await this.ageOut(
        key.slice(0, key.length - `canvas.${draftId}`.length),
        key,
      );
    return "computer";
  }

  /** Removes the account's oldest canvases beyond the count and size it keeps. */
  private async ageOut(prefix: string, written: string) {
    const kept: { key: string; at: number; size: number }[] = [];
    for (const key of await this.backend.keys(`${prefix}canvas.`)) {
      const raw = await this.backend.get(key);
      if (raw === null) continue;
      const at = KEPT_AT.exec(raw.slice(0, 40));
      kept.push({ key, at: at ? Number(at[1]) : 0, size: raw.length });
    }
    kept.sort((a, b) => a.at - b.at);
    let total = kept.reduce((sum, item) => sum + item.size, 0);
    let count = kept.length;
    for (const item of kept) {
      if (count <= KEPT_CANVASES && total <= KEPT_CHARACTERS) break;
      if (item.key === written) continue;
      if (await this.backend.remove(item.key)) {
        count--;
        total -= item.size;
      }
    }
  }

  /** Deletes every key of a saved platform; returns how many went. */
  forgetPlatform(profileName: string): Promise<number> {
    return purgeProfile(this.backend, profileName);
  }
}
