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
// The built-in connector actions the Studio host serves
// (GET /studio/contracts/connector-actions), loaded once per session.
import type { Json } from "../registry";

export interface ConnectorActionTemplate {
  connector: string;
  action: string;
  document: Json;
}

const PATH = "/studio/contracts/connector-actions";

function readActions(answer: unknown): ConnectorActionTemplate[] {
  const actions = (answer as { actions?: unknown } | null)?.actions;
  if (
    !Array.isArray(actions) ||
    !actions.every(
      (entry: unknown) =>
        typeof entry === "object" &&
        entry !== null &&
        typeof (entry as { connector?: unknown }).connector === "string" &&
        typeof (entry as { action?: unknown }).action === "string" &&
        typeof (entry as { document?: unknown }).document === "object" &&
        (entry as { document?: unknown }).document !== null,
    )
  )
    throw Error("Studio sent a list of built-in actions it can't read.");
  return actions as ConnectorActionTemplate[];
}

export class ConnectorActions {
  private loading: Promise<ConnectorActionTemplate[]> | null = null;
  constructor(private readonly get: (path: string) => Promise<unknown>) {}

  /** The list, loaded on first use; a failed load is not kept, so the next call asks again. */
  list(): Promise<ConnectorActionTemplate[]> {
    this.loading ??= this.get(PATH)
      .then(readActions)
      .catch((error: unknown) => {
        this.loading = null;
        throw error;
      });
    return this.loading;
  }
  async find(
    connector: string,
    action: string,
  ): Promise<ConnectorActionTemplate | null> {
    return (
      (await this.list()).find(
        (entry) => entry.connector === connector && entry.action === action,
      ) ?? null
    );
  }
}
