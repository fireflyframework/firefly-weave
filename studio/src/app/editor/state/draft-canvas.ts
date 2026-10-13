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

export class DraftCanvases {
  constructor(private readonly backend: StoreBackend = browserBackend()) {}

  /** The stored canvas, as JSON, or null when none is kept for this account. */
  async read(scope: AccountScope, draftId: string): Promise<unknown | null> {
    const key = await draftCanvasKey(scope, draftId);
    if (!key) return null;
    const raw = await this.backend.get(key);
    if (raw === null) return null;
    try {
      return JSON.parse(raw) as unknown;
    } catch {
      return null;
    }
  }

  /** Keeps the canvas (an empty one removes the key); "memory" when it can't. */
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
    return (await this.backend.set(key, JSON.stringify(canvas)))
      ? "computer"
      : "memory";
  }

  /** Deletes every key of a saved platform; returns how many went. */
  forgetPlatform(profileName: string): Promise<number> {
    return purgeProfile(this.backend, profileName);
  }
}
