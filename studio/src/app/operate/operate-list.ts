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
// How the Workers and Incidents pages read their lists: the environment they
// read from, pages of 50 from the start or after a cursor, and what a failed
// read shows. Pure apart from the reader the page passes in.
import { describeError, type PlainError } from "../errors";
import type { Page } from "../operations/deployment-contracts";

/** What a list page reads from the shell. */
export interface ListHost {
  readonly profile: unknown;
  readonly api: { readonly environment: string };
  readonly identity: unknown;
  readonly signInEnded: boolean;
}

/** The environment a page reads; "" without a platform or an environment. */
export function environmentOf(host: ListHost): string {
  try {
    return host.profile ? host.api.environment : "";
  } catch {
    return "";
  }
}

/**
 * The environment, the person and whether their sign-in ended: when this
 * changes, a page forgets what it read and starts again.
 */
export function scopeKeyOf(host: ListHost): string {
  return JSON.stringify([environmentOf(host), host.identity, host.signInEnded]);
}

/**
 * Reads up to `pages` pages of 50, from the start or after `cursor` (Load
 * more), and returns their items with the cursor of the page after them.
 */
export async function readPages<T>(
  read: (query: string) => Promise<Page<T>>,
  pages: number,
  cursor: string | null = null,
): Promise<{ items: T[]; cursor: string | null }> {
  const items: T[] = [];
  let count = 0;
  do {
    const query = new URLSearchParams({ limit: "50" });
    if (cursor) query.set("cursor", cursor);
    const page = await read(query.toString());
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor && ++count < pages);
  return { items, cursor };
}

/**
 * What a failed read shows: a refusal (403) the access state, anything else
 * its message and support code.
 */
export function listFailure(error: unknown): {
  error: PlainError;
  forbidden: boolean;
} {
  const plain = describeError(error);
  return { error: plain, forbidden: plain.status === 403 };
}

/** Refresh and Load more share one sequence so neither overwrites the other. */
export class ListReads {
  private tail: Promise<unknown> = Promise.resolve();

  run<T>(read: () => Promise<T>): Promise<T> {
    const result = this.tail.then(read);
    this.tail = result.catch(() => undefined);
    return result;
  }
}
