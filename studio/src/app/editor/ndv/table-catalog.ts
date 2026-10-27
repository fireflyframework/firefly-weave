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
// The decision tables step details can choose from and read: the published
// versions of the project (newest first), then the drafts and copies kept on
// this computer. A published version Studio reads is kept as a copy, so it
// can still be read and sent to the Studio host when the platform can't be
// reached. Every answer that arrives after an await applies only while the
// account, the project and the generation it was asked for still hold.
import type { Profile, StudioApi } from "../../api";
import type { Json } from "../../forms/core/json";
import { connectedPrefix, type AccountScope } from "../state/browser-store";
import { studioBackend } from "../state/studio-storage";
import { TableDrafts, type TableDocument } from "../state/table-drafts";

export interface TableChoice {
  uses: string;
  title: string;
  where: "published" | "draft" | "copy";
  /** The platform's ID of a published version. */
  id?: string;
}
export interface TableCatalogHost {
  readonly api: StudioApi;
  readonly profile: Profile | null;
  readonly stepDataScope?: string;
  readonly runAccount?: AccountScope | null;
  readonly decisionContracts: ReadonlyMap<string, Record<string, unknown>>;
  can(capability: string, resource?: string): boolean;
  cacheDecisionContract(uses: string, document: Record<string, unknown>): void;
  refreshView(): void;
}

export const ON_THIS_COMPUTER = "on this computer";
export const SIGN_IN_TO_LIST = "Sign in to list published tables.";

const versionParts = (uses: string): number[] =>
  (uses.slice(uses.lastIndexOf("@") + 1).match(/\d+/g) ?? []).map(Number);
const newerFirst = (a: string, b: string) => {
  const [x, y] = [versionParts(a), versionParts(b)];
  for (let i = 0; i < Math.max(x.length, y.length); i++)
    if ((x[i] ?? 0) !== (y[i] ?? 0)) return (y[i] ?? 0) - (x[i] ?? 0);
  return 0;
};
const nameOf = (uses: string) => uses.slice(0, uses.lastIndexOf("@"));

export class TableCatalog {
  private retries = 0;
  private listed: {
    key: string;
    rows: Promise<{ uses: string; id: string }[]>;
  } | null = null;
  private store: { prefix: string | null; drafts: TableDrafts } | null = null;
  private storeKey = "";
  private loading = new Map<string, Promise<Json | null>>();
  /** Tables that couldn't be read, with the generation they were asked in. */
  private missing = new Map<string, string>();

  constructor(private readonly host: TableCatalogHost) {}

  /** Changes with the account, the platform, the project and every Retry. */
  get generation(): string {
    const host = this.host;
    let project = "";
    try {
      project = host.profile ? host.api.project : "";
    } catch {
      project = "";
    }
    return JSON.stringify([
      host.profile?.name ?? null,
      project,
      host.stepDataScope ?? "",
      host.can("catalog.read"),
      this.retries,
    ]);
  }

  /** Lists the published versions again (the field's Retry). */
  retry(): void {
    this.retries++;
    this.listed = null;
  }

  /** True when the published list needs a signed-in account it doesn't have yet. */
  needsSignIn(): boolean {
    return !!this.host.profile && !this.host.can("catalog.read");
  }

  /** The drafts and copies of the account shown now (memory only when no account can own them). */
  async drafts(): Promise<TableDrafts> {
    const host = this.host;
    const scope = host.runAccount ?? null;
    const key = JSON.stringify([host.profile?.name ?? null, scope]);
    if (!this.store || this.storeKey !== key) {
      const prefix = !host.profile
        ? "local:"
        : scope?.subject
          ? await connectedPrefix(scope).catch(() => null)
          : null;
      if (
        !this.store ||
        this.storeKey !== key ||
        this.store.prefix !== prefix
      ) {
        this.storeKey = key;
        this.store = {
          prefix,
          drafts: new TableDrafts(studioBackend(), prefix),
        };
      }
    }
    const drafts = this.store.drafts;
    await drafts.ready();
    return drafts;
  }

  /** The drafts kept now, without waiting; null until they are read. */
  private kept(): TableDrafts | null {
    return this.store?.drafts ?? null;
  }

  private published(): Promise<{ uses: string; id: string }[]> {
    const host = this.host;
    if (!host.profile || !host.can("catalog.read")) return Promise.resolve([]);
    const key = this.generation;
    if (this.listed?.key === key) return this.listed.rows;
    const rows = host.api.page("decision-tables").then((page) =>
      page.items
        .map((row) => ({
          uses: `${String(row["name"])}@${String(row["version"])}`,
          id: String(row["id"] ?? ""),
        }))
        .sort(
          (a, b) =>
            nameOf(a.uses).localeCompare(nameOf(b.uses)) ||
            newerFirst(a.uses, b.uses),
        ),
    );
    const listed = { key, rows };
    this.listed = listed;
    // A failed list is asked for again next time.
    rows.catch(() => {
      if (this.listed === listed) this.listed = null;
    });
    return rows;
  }

  /** Published versions, newest first, then the drafts and copies on this computer. */
  async list(): Promise<TableChoice[]> {
    const key = this.generation;
    const [drafts, published] = await Promise.all([
      this.drafts(),
      this.published().catch((error: unknown) => {
        // Without the platform, what this computer keeps can still be chosen.
        if (this.kept()?.entries().length) return null;
        throw error;
      }),
    ]);
    if (key !== this.generation) return [];
    const choices: TableChoice[] = (published ?? []).map((row) => ({
      uses: row.uses,
      title: row.uses,
      where: "published" as const,
      id: row.id,
    }));
    const listed = new Set(choices.map((choice) => choice.uses));
    for (const entry of drafts.entries()) {
      const uses = `${entry.name}@${entry.version}`;
      if (listed.has(uses)) continue;
      listed.add(uses);
      choices.push({
        uses,
        title: `${uses} · ${ON_THIS_COMPUTER}`,
        where: entry.kind,
      });
    }
    return choices;
  }

  /** The table's document if Studio has it: a draft, a copy, then what it read this session. */
  contract(uses: string): Json | null {
    const drafts = this.kept();
    const draft = drafts?.draft(nameOf(uses));
    if (draft && `${draft.name}@${draft.version}` === uses)
      return draft.document as unknown as Json;
    const copy = drafts?.copyOf(uses);
    if (copy) return copy as unknown as Json;
    return (this.host.decisionContracts.get(uses) as Json | undefined) ?? null;
  }

  /** Where the step's table comes from, for its label: on this computer, or published. */
  where(uses: string): "draft" | "copy" | "published" | null {
    const drafts = this.kept();
    const draft = drafts?.draft(nameOf(uses));
    if (draft && `${draft.name}@${draft.version}` === uses) return "draft";
    if (this.host.decisionContracts.has(uses)) return "published";
    if (drafts?.copyOf(uses)) return "copy";
    return null;
  }

  /**
   * Reads a table's document: what this computer has, or the published
   * version's export, which is then kept as a copy. Null when it can't be read.
   */
  load(uses: string): Promise<Json | null> {
    const pending = this.loading.get(uses);
    if (pending) return pending;
    const known = this.contract(uses);
    if (known) return Promise.resolve(known);
    const key = this.generation;
    // Asked again only once the account, the project or the list changes.
    if (this.missing.get(uses) === key) return Promise.resolve(null);
    const work = this.read(uses)
      .catch(() => null)
      .then((document) => {
        if (!document) this.missing.set(uses, key);
        return document;
      })
      .finally(() => {
        if (this.loading.get(uses) === work) this.loading.delete(uses);
      });
    this.loading.set(uses, work);
    return work;
  }

  private async read(uses: string): Promise<Json | null> {
    const host = this.host;
    const key = this.generation;
    const drafts = await this.drafts();
    if (key !== this.generation) return null;
    const known = this.contract(uses);
    if (known) return known;
    if (!host.profile || !host.can("catalog.read")) return null;
    const api = host.api;
    const row = (await this.published().catch(() => [])).find(
      (item) => item.uses === uses,
    );
    if (!row?.id || key !== this.generation || host.api !== api) return null;
    const detail = await api
      .request<
        Record<string, unknown>
      >(`${api.project}/decision-tables/${encodeURIComponent(row.id)}/export`)
      .catch(() => null);
    if (!detail || key !== this.generation || host.api !== api) return null;
    const document = detail["definition"] ?? detail["document"];
    if (!document || typeof document !== "object" || Array.isArray(document))
      return null;
    const record = document as Record<string, unknown>;
    host.cacheDecisionContract(uses, record);
    await drafts.copy(record as unknown as TableDocument);
    if (key === this.generation) host.refreshView();
    return record as Json;
  }
}
