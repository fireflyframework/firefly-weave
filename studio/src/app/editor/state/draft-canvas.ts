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
// A copy on this computer, for recovery: the platform keeps a platform
// draft's canvas (notes, trigger choices, step notes and the recipes of the
// actions it owns) as a studio document. The copy is kept for the signed-in
// account under the connected key scheme, and goes up to the platform when
// the platform has none for the draft. Signing out of a platform, or removing it,
// deletes them with every other key of that platform. Canvas data is never
// a secret (the sidecar holds none).
import {
  connectedPrefix,
  purgeProfile,
  storeKey,
  type AccountScope,
  type StoreBackend,
} from "./browser-store";
import { studioBackend } from "./studio-storage";
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

/**
 * How many canvases an account keeps on this computer, across its
 * workspaces, and their total size in characters.
 */
export const KEPT_CANVASES = 50;
export const KEPT_CHARACTERS = 2 * 1024 * 1024;
const KEPT_AT = /^\{"keptAt":(\d+),/;
/** After the account's prefix (profile and subject): tenant, project, then the canvas name. */
const CANVAS_NAME = /^[^:]+:[^:]+:canvas\./;
/** The account part of a key: its profile and subject. */
const accountOf = (key: string) => key.split(":", 2).join(":") + ":";

interface Kept {
  at: number;
  size: number;
}

export class DraftCanvases {
  /**
   * Each account's kept canvases by key, read from storage once, then kept
   * up to date by this object's own writes, so an edit reads no other canvas.
   */
  private readonly index = new Map<string, Map<string, Kept>>();

  constructor(
    private readonly backend: StoreBackend = studioBackend(),
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
   * within KEPT_CHARACTERS, counted across its workspaces: older ones go first.
   */
  async write(
    scope: AccountScope,
    draftId: string,
    canvas: CanvasSidecar,
  ): Promise<KeptWhere> {
    const key = await draftCanvasKey(scope, draftId);
    if (!key) return "memory";
    const kept = await this.kept(accountOf(key));
    if (canvasBytes(canvas) === canvasBytes(emptyCanvas())) {
      await this.backend.remove(key);
      kept.delete(key);
      return "computer";
    }
    const at = this.now();
    const value = JSON.stringify({ keptAt: at, canvas });
    if (!(await this.backend.set(key, value))) {
      await this.backend.remove(key);
      kept.delete(key);
      return "memory";
    }
    kept.set(key, { at, size: value.length });
    await this.ageOut(kept, key);
    return "computer";
  }

  /** The account's kept canvases: read from storage on first use. */
  private async kept(account: string): Promise<Map<string, Kept>> {
    let kept = this.index.get(account);
    if (kept) return kept;
    kept = new Map();
    for (const key of await this.backend.keys(account)) {
      if (!CANVAS_NAME.test(key.slice(account.length))) continue;
      const raw = await this.backend.get(key);
      if (raw === null) continue;
      const at = KEPT_AT.exec(raw.slice(0, 40));
      kept.set(key, { at: at ? Number(at[1]) : 0, size: raw.length });
    }
    this.index.set(account, kept);
    return kept;
  }

  /** Removes the account's oldest canvases beyond the count and size it keeps. */
  private async ageOut(kept: Map<string, Kept>, written: string) {
    let total = 0;
    for (const item of kept.values()) total += item.size;
    let count = kept.size;
    if (count <= KEPT_CANVASES && total <= KEPT_CHARACTERS) return;
    const oldest = [...kept].sort(([, a], [, b]) => a.at - b.at);
    for (const [key, item] of oldest) {
      if (count <= KEPT_CANVASES && total <= KEPT_CHARACTERS) break;
      if (key === written) continue;
      if (await this.backend.remove(key)) {
        kept.delete(key);
        count--;
        total -= item.size;
      }
    }
  }

  /** Deletes every key of a saved platform; returns how many went. */
  forgetPlatform(profileName: string): Promise<number> {
    this.index.clear();
    return purgeProfile(this.backend, profileName);
  }
}
