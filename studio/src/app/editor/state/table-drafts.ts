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
// Decision tables kept on this computer: drafts of the next version of a
// table (one per table name), and read-only copies of the published versions
// Studio has read, so an offline check or execution can still send them.
// Everything lives under the account's storage prefix (`local:` offline), so
// signing out of a platform purges it with the rest of that account's keys; a
// null prefix keeps entries in memory only. A published version never
// changes: the first change to one starts the next free patch version.
import { nextPatchVersion } from "../../integrations/http-action-model";
import type { TableSpec } from "../decision-grid/rules-model";
import { ownedName } from "../ndv/owned/owned-actions";
import { storeKey, type StoreBackend } from "./browser-store";

export interface TableDocument {
  apiVersion: "weave/v1alpha1";
  kind: "DecisionTable";
  metadata: { name: string; version: string };
  spec: TableSpec;
}
export interface DraftEntry {
  kind: "draft";
  name: string;
  version: string;
  /** The published version this draft started from; null for a new table. */
  base: string | null;
  document: TableDocument;
  savedAt: string;
}
export interface CopyEntry {
  kind: "copy";
  name: string;
  version: string;
  document: TableDocument;
  readAt: string;
}
export type TableEntry = DraftEntry | CopyEntry;

export const TABLE_DRAFT_MAX_BYTES = 1_048_576;
export const MAX_TABLE_DRAFTS = 50;
export const MAX_TABLE_COPIES = 200;
const TOO_LARGE =
  "This table is too large to keep on this computer (1 MB at most).";
const TOO_MANY =
  "Studio keeps 50 table drafts on this computer; publish or delete one first.";

const DRAFT = "weave.tableDraft.";
const COPY = "weave.tableCopy.";
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
// Semantic versions as the platform takes them: prerelease and build parts too.
const VERSION =
  /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$/;
const POLICIES: ReadonlySet<unknown> = new Set(["first", "unique", "collect"]);

/** Where a table's draft is kept. */
export const draftKey = (prefix: string, name: string): string =>
  storeKey(prefix, DRAFT + name);
/**
 * Where a copy of a published version is kept. "@" and "+" aren't key
 * characters, so "_v" stands for "@" and "_b" for a build's "+".
 */
export const copyKey = (
  prefix: string,
  name: string,
  version: string,
): string => storeKey(prefix, `${COPY}${name}_v${version.replace("+", "_b_")}`);

const parts = (version: string) => {
  const found = VERSION.exec(version);
  return {
    core: found ? [1, 2, 3].map((i) => Number(found[i])) : [0, 0, 0],
    pre: found?.[4] ?? null,
  };
};
/** Semantic version order: the core, then a prerelease before its release. */
function compareVersions(a: string, b: string): number {
  const [x, y] = [parts(a), parts(b)];
  const core =
    x.core[0] - y.core[0] || x.core[1] - y.core[1] || x.core[2] - y.core[2];
  if (core) return core;
  if (x.pre === y.pre) return 0;
  if (x.pre === null) return 1;
  if (y.pre === null) return -1;
  return x.pre.localeCompare(y.pre, "en", { numeric: true });
}

/**
 * The version a draft of `base` starts at: the next patch version above the
 * newest listed version (or above `base` when none is listed), never one the
 * platform lists.
 */
export function nextDraftVersion(
  base: string,
  listed: readonly string[],
): string {
  const versions = listed.filter((version) => VERSION.test(version));
  const newest = [base, ...versions]
    .filter((version) => VERSION.test(version))
    .sort(compareVersions)
    .at(-1);
  return nextPatchVersion(newest ?? base, versions);
}

/** A new table's name: `<workflow>.<step>`, `-2` when taken. */
export function newTableName(
  workflow: string,
  step: string,
  taken: ReadonlySet<string>,
): string {
  return ownedName(workflow, step, taken);
}

/** An empty table: no columns, First match, one rule that always matches. */
export function freshTable(name: string, version: string): TableDocument {
  return {
    apiVersion: "weave/v1alpha1",
    kind: "DecisionTable",
    metadata: { name, version },
    spec: {
      inputSchema: { type: "object", properties: {} },
      outputSchema: { type: "object", properties: {} },
      hitPolicy: "first",
      rules: [
        { id: "rule-1", when: { literal: true }, output: { literal: {} } },
      ],
    },
  };
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const hasExactly = (value: Record<string, unknown>, keys: string[]) => {
  const own = Object.keys(value);
  return own.length === keys.length && keys.every((key) => own.includes(key));
};
const isExpression = (value: unknown): boolean =>
  isRecord(value) && Object.keys(value).length === 1;
const isTime = (value: unknown): value is string =>
  typeof value === "string" && !Number.isNaN(Date.parse(value));

/** A table document with the fields the grid reads, named `name@version`; null for anything else. */
function readDocument(
  value: unknown,
  name: string,
  version: string,
): TableDocument | null {
  if (
    !isRecord(value) ||
    !hasExactly(value, ["apiVersion", "kind", "metadata", "spec"])
  )
    return null;
  if (
    value["apiVersion"] !== "weave/v1alpha1" ||
    value["kind"] !== "DecisionTable"
  )
    return null;
  const metadata = value["metadata"];
  if (
    !isRecord(metadata) ||
    !hasExactly(metadata, ["name", "version"]) ||
    metadata["name"] !== name ||
    metadata["version"] !== version
  )
    return null;
  const spec = value["spec"];
  if (!isRecord(spec)) return null;
  const keys = Object.keys(spec);
  const allowed = [
    "inputSchema",
    "outputSchema",
    "hitPolicy",
    "rules",
    "defaultOutput",
  ];
  if (keys.some((key) => !allowed.includes(key))) return null;
  if (!isRecord(spec["inputSchema"]) || !isRecord(spec["outputSchema"]))
    return null;
  if (!POLICIES.has(spec["hitPolicy"])) return null;
  const rules = spec["rules"];
  if (
    !Array.isArray(rules) ||
    !rules.length ||
    !rules.every(
      (rule) =>
        isRecord(rule) &&
        hasExactly(rule, ["id", "when", "output"]) &&
        typeof rule["id"] === "string" &&
        isExpression(rule["when"]) &&
        isExpression(rule["output"]),
    )
  )
    return null;
  if ("defaultOutput" in spec && !isExpression(spec["defaultOutput"]))
    return null;
  return value as unknown as TableDocument;
}

/** A stored entry, from its typed fields only; null for anything else. */
export function readEntry(raw: string): TableEntry | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(value)) return null;
  const { kind, name, version } = value;
  if (typeof name !== "string" || !NAME.test(name)) return null;
  if (typeof version !== "string" || !VERSION.test(version)) return null;
  if (kind === "draft") {
    if (
      !hasExactly(value, [
        "kind",
        "name",
        "version",
        "base",
        "document",
        "savedAt",
      ])
    )
      return null;
    const base = value["base"];
    if (base !== null && (typeof base !== "string" || !VERSION.test(base)))
      return null;
    if (!isTime(value["savedAt"])) return null;
    return readDocument(value["document"], name, version)
      ? (value as unknown as DraftEntry)
      : null;
  }
  if (kind === "copy") {
    if (!hasExactly(value, ["kind", "name", "version", "document", "readAt"]))
      return null;
    if (!isTime(value["readAt"])) return null;
    return readDocument(value["document"], name, version)
      ? (value as unknown as CopyEntry)
      : null;
  }
  return null;
}

const usesOf = (entry: { name: string; version: string }) =>
  `${entry.name}@${entry.version}`;
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/** The drafts and copies of one account on this computer. */
export class TableDrafts {
  private drafts = new Map<string, DraftEntry>();
  private copies = new Map<string, CopyEntry>();
  private loaded: Promise<void> | null = null;

  constructor(
    private readonly backend: StoreBackend,
    /** The account's storage prefix; null keeps everything in memory only. */
    readonly prefix: string | null,
  ) {}

  /** Reads what this account keeps; entries that don't read are deleted. */
  ready(): Promise<void> {
    this.loaded ??= this.load();
    return this.loaded;
  }

  private async load(): Promise<void> {
    if (this.prefix === null) return;
    for (const key of await this.backend.keys(this.prefix + "weave.table")) {
      const raw = await this.backend.get(key);
      const entry = raw === null ? null : readEntry(raw);
      const expected =
        entry?.kind === "draft"
          ? draftKey(this.prefix, entry.name)
          : entry
            ? copyKey(this.prefix, entry.name, entry.version)
            : null;
      if (!entry || key !== expected) {
        await this.backend.remove(key);
        continue;
      }
      if (entry.kind === "draft") this.drafts.set(entry.name, entry);
      else this.copies.set(usesOf(entry), entry);
    }
  }

  /** The draft of this table, if one is kept. */
  draft(name: string): DraftEntry | null {
    return this.drafts.get(name) ?? null;
  }

  /** The kept copy of a published `name@version`. */
  copyOf(uses: string): TableDocument | null {
    return this.copies.get(uses)?.document ?? null;
  }

  /** Drafts, then copies, newest first within each. */
  entries(): TableEntry[] {
    const newest = <T extends TableEntry>(
      list: T[],
      at: (entry: T) => string,
    ) => list.sort((a, b) => at(b).localeCompare(at(a)));
    return [
      ...newest([...this.drafts.values()], (entry) => entry.savedAt),
      ...newest([...this.copies.values()], (entry) => entry.readAt),
    ];
  }

  /** Starts the next version of a published table as a draft. */
  async startDraft(
    published: TableDocument,
    listed: readonly string[],
  ): Promise<DraftEntry> {
    const { name, version } = published.metadata;
    const next = nextDraftVersion(version, listed);
    const document = clone(published);
    document.metadata = { name, version: next };
    return this.keep({
      kind: "draft",
      name,
      version: next,
      base: version,
      document,
      savedAt: "",
    });
  }

  /** Starts a new table named after the workflow and the step, at 1.0.0. */
  async newDraft(
    workflow: string,
    step: string,
    taken: ReadonlySet<string>,
  ): Promise<DraftEntry> {
    const name = newTableName(
      workflow,
      step,
      new Set([...taken, ...this.drafts.keys()]),
    );
    return this.keep({
      kind: "draft",
      name,
      version: "1.0.0",
      base: null,
      document: freshTable(name, "1.0.0"),
      savedAt: "",
    });
  }

  /** Writes the grid's table into the draft; its name and version stay. */
  async save(entry: DraftEntry, spec: TableSpec): Promise<DraftEntry> {
    const document: TableDocument = {
      ...entry.document,
      metadata: { name: entry.name, version: entry.version },
      spec,
    };
    return this.keep({ ...entry, document, savedAt: "" });
  }

  /** Moves a draft to another version (its version was published meanwhile). */
  async moveDraft(entry: DraftEntry, version: string): Promise<DraftEntry> {
    const document = clone(entry.document);
    document.metadata = { name: entry.name, version };
    return this.keep({ ...entry, version, document, savedAt: "" });
  }
  /** Forgets a table's draft. */
  async remove(name: string): Promise<void> {
    this.drafts.delete(name);
    if (this.prefix !== null)
      await this.backend.remove(draftKey(this.prefix, name));
  }

  /** Keeps a read-only copy of a published version; the 200 most recently read stay. */
  async copy(document: TableDocument): Promise<void> {
    const { name, version } = document.metadata;
    const entry: CopyEntry = {
      kind: "copy",
      name,
      version,
      document: clone(document),
      readAt: this.now(),
    };
    const text = JSON.stringify(entry);
    if (
      new TextEncoder().encode(text).length > TABLE_DRAFT_MAX_BYTES ||
      !readEntry(text)
    )
      return;
    this.copies.delete(usesOf(entry));
    this.copies.set(usesOf(entry), entry);
    if (this.prefix !== null)
      await this.backend.set(copyKey(this.prefix, name, version), text);
    const oldest = [...this.copies.values()]
      .sort((a, b) => a.readAt.localeCompare(b.readAt))
      .slice(0, Math.max(0, this.copies.size - MAX_TABLE_COPIES));
    for (const old of oldest) {
      this.copies.delete(usesOf(old));
      if (this.prefix !== null)
        await this.backend.remove(copyKey(this.prefix, old.name, old.version));
    }
  }

  /** The documents of the drafts and copies these `uses` name: drafts first, then copies, once each. */
  definitionsFor(uses: readonly string[]): TableDocument[] {
    const wanted = [...new Set(uses)];
    const drafts = wanted
      .map((ref) => {
        const at = ref.lastIndexOf("@");
        const draft = this.drafts.get(ref.slice(0, at));
        return draft && usesOf(draft) === ref ? draft.document : null;
      })
      .filter((document): document is TableDocument => document !== null);
    const taken = new Set(drafts.map((document) => usesOf(document.metadata)));
    const copies = wanted
      .filter((ref) => !taken.has(ref))
      .map((ref) => this.copyOf(ref))
      .filter((document): document is TableDocument => document !== null);
    return [...drafts, ...copies];
  }

  private async keep(entry: DraftEntry): Promise<DraftEntry> {
    const saved: DraftEntry = { ...entry, savedAt: this.now() };
    const text = JSON.stringify(saved);
    if (!readEntry(text))
      throw new Error("Studio can't keep this table on this computer.");
    if (new TextEncoder().encode(text).length > TABLE_DRAFT_MAX_BYTES)
      throw new Error(TOO_LARGE);
    if (!this.drafts.has(saved.name) && this.drafts.size >= MAX_TABLE_DRAFTS)
      throw new Error(TOO_MANY);
    this.drafts.set(saved.name, saved);
    if (this.prefix !== null)
      await this.backend.set(draftKey(this.prefix, saved.name), text);
    return saved;
  }

  // Strictly increasing, so two entries kept in the same millisecond keep their order.
  private last = 0;
  private now(): string {
    this.last = Math.max(Date.now(), this.last + 1);
    return new Date(this.last).toISOString();
  }
}
