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
// The project's catalog lock (`GET {project}/catalog`, catalog.read): every
// published definition with its digest. Studio reads it once per account and
// environment for the workflows a step may call and the workflows that call
// this one. Rendering asks for it, so a read that failed isn't asked again
// until its pause is over (5 s, doubling to a minute), and an answer that
// arrives after the account or environment changed is dropped.
import { signal } from "@angular/core";
import { describeError } from "../../errors";
import { isJsonObject, type JsonObject } from "../../forms/core/json";

export const LOCK_RETRY_MS = 5_000;
export const LOCK_RETRY_MAX_MS = 60_000;

/** A published workflow version of the lock. */
export interface LockedWorkflow {
  /** `name@version`. */
  uses: string;
  name: string;
  version: string;
  /** The definition as the platform published it. */
  document: JsonObject;
}
/** The workflow versions of a catalog lock, by `name@version`. */
export interface CatalogLock {
  workflows: ReadonlyMap<string, LockedWorkflow>;
}

export type CatalogLockState =
  /** Studio isn't connected to a platform. */
  | { kind: "offline" }
  /** This account can't read the project's catalog. */
  | { kind: "forbidden" }
  | { kind: "loading" }
  | { kind: "ready"; lock: CatalogLock }
  /** The read failed; it is asked again after a pause. */
  | { kind: "failed"; message: string };

/** What the reader needs from Studio. */
export interface CatalogLockHost {
  /** The platform, account, project and environment answers belong to; null when not connected. */
  scope(): string | null;
  /** Whether the account holds catalog.read here. */
  canRead(): boolean;
  /** `GET {project}/catalog` for the current scope. */
  fetch(): Promise<unknown>;
}

const OFFLINE: CatalogLockState = { kind: "offline" };
const FORBIDDEN: CatalogLockState = { kind: "forbidden" };
const LOADING: CatalogLockState = { kind: "loading" };
const UNREADABLE = "Studio couldn't read this project's published workflows.";

const text = (value: unknown) => (typeof value === "string" ? value : "");

/** The workflows of a catalog lock answer, or null when it isn't one. */
export function parseCatalogLock(value: unknown): CatalogLock | null {
  if (!isJsonObject(value) || !Array.isArray(value["definitions"])) return null;
  const workflows = new Map<string, LockedWorkflow>();
  for (const entry of value["definitions"]) {
    const document = isJsonObject(entry) ? entry["document"] : null;
    if (!isJsonObject(document) || document["kind"] !== "Workflow") continue;
    const metadata = isJsonObject(document["metadata"])
      ? document["metadata"]
      : {};
    const name = text(metadata["name"]),
      version = text(metadata["version"]);
    if (!name || !version) continue;
    const uses = `${name}@${version}`;
    workflows.set(uses, { uses, name, version, document });
  }
  return { workflows };
}

/**
 * Reads the catalog lock of the current scope once, keeps the answer (or the
 * refusal) for that scope, and waits before asking again after a failure.
 * State changes are announced through `version` only after an answer
 * arrives, never while a template or computed reads `current()`.
 */
export class CatalogLockReader {
  private readonly changed = signal(0);
  /** Changes whenever an answer is kept or dropped. */
  readonly version = this.changed.asReadonly();
  private kept: { scope: string; state: CatalogLockState } | null = null;
  private loading: {
    scope: string;
    token: object;
    answer: Promise<CatalogLockState>;
  } | null = null;
  private failed: {
    scope: string;
    at: number;
    pause: number;
    message: string;
  } | null = null;

  constructor(
    private readonly host: CatalogLockHost,
    private readonly now: () => number = () => Date.now(),
  ) {}

  /**
   * The lock as it stands for the current scope, starting a read when none
   * is kept, none is running and no pause is running. Safe to call while
   * rendering.
   */
  current(): CatalogLockState {
    this.version();
    const scope = this.host.scope();
    if (!scope) return OFFLINE;
    if (!this.host.canRead()) return FORBIDDEN;
    if (this.kept?.scope === scope) return this.kept.state;
    if (this.loading?.scope === scope) return LOADING;
    const waiting = this.waiting(scope);
    if (waiting) return waiting;
    void this.read();
    return LOADING;
  }

  /** The lock of the current scope: kept, being read, paused after a failure, or read now. */
  read(): Promise<CatalogLockState> {
    const scope = this.host.scope();
    if (!scope) return Promise.resolve(OFFLINE);
    if (!this.host.canRead()) return Promise.resolve(FORBIDDEN);
    if (this.kept?.scope === scope) return Promise.resolve(this.kept.state);
    if (this.loading?.scope === scope) return this.loading.answer;
    const waiting = this.waiting(scope);
    if (waiting) return Promise.resolve(waiting);
    // The request leaves now, for this scope; a synchronous throw becomes a
    // rejection so nothing below runs before the first await.
    let request: Promise<unknown>;
    try {
      request = this.host.fetch();
    } catch (error) {
      request = Promise.reject(error);
    }
    const token = {};
    const answer = this.settle(scope, token, request);
    this.loading = { scope, token, answer };
    return answer;
  }

  /** Refresh: forgets the kept answer and any pause of the current scope, then reads again. */
  refresh(): Promise<CatalogLockState> {
    const scope = this.host.scope();
    if (this.kept?.scope === scope) this.kept = null;
    if (this.failed?.scope === scope) this.failed = null;
    return this.read();
  }

  private waiting(scope: string): CatalogLockState | null {
    const failed = this.failed;
    return failed?.scope === scope && this.now() - failed.at < failed.pause
      ? { kind: "failed", message: failed.message }
      : null;
  }

  private async settle(
    scope: string,
    token: object,
    request: Promise<unknown>,
  ): Promise<CatalogLockState> {
    let state: CatalogLockState;
    try {
      const lock = parseCatalogLock(await request);
      state = lock
        ? { kind: "ready", lock }
        : { kind: "failed", message: UNREADABLE };
    } catch (error) {
      const plain = describeError(error);
      state =
        plain.status === 403
          ? FORBIDDEN
          : { kind: "failed", message: plain.message };
    }
    const latest = this.loading?.token === token;
    if (latest) this.loading = null;
    if (!latest || this.host.scope() !== scope) {
      // Studio moved to another account or environment while this read ran.
      this.changed.update((n) => n + 1);
      return this.current();
    }
    if (state.kind === "failed") {
      const previous = this.failed?.scope === scope ? this.failed.pause : 0;
      const pause = previous
        ? Math.min(previous * 2, LOCK_RETRY_MAX_MS)
        : LOCK_RETRY_MS;
      this.failed = { scope, at: this.now(), pause, message: state.message };
    } else {
      this.failed = null;
      this.kept = { scope, state };
    }
    this.changed.update((n) => n + 1);
    return state;
  }
}
