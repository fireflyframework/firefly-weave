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
import { canonicalJson } from "../forms/core/json";
import { describeError } from "../errors";
import type {
  Approval,
  Collection,
  Collections,
  Page,
} from "./deployment-contracts";

export interface OperationsTransport {
  request<T>(
    path: string,
    method?: string,
    body?: unknown,
    headers?: Record<string, string>,
  ): Promise<T>;
}
const collections: Record<Collection, string> = {
  targets: "deployment-targets",
  deployments: "deployments",
  observations: "deployment-observations",
  plans: "deployment-plans",
  jobs: "deployment-jobs",
  runners: "deployment-runners",
};
const emptyPages = (): { [K in Collection]: Collections[K][] } => ({
  targets: [],
  deployments: [],
  observations: [],
  plans: [],
  jobs: [],
  runners: [],
});

/** Owns only this view's scoped responses. Platform authorization remains server-side. */
export class OperationsStore {
  private scope = "";
  private readable = false;
  private authority = "";
  private filters: Partial<Record<Collection, string>> = {};
  private generation = 0;
  private sequence: Partial<Record<Collection, number>> = {};
  private pages = emptyPages();
  cursors: Partial<Record<Collection, string | null>> = {};
  loading = new Set<Collection>();
  errors: Partial<Record<Collection, string>> = {};
  /** The code and HTTP status behind each entry of `errors`. */
  errorInfo: Partial<Record<Collection, { code: string; status: number }>> = {};
  /** The filters each collection last loaded with, until it fails or changes. */
  private loadedFilters: Partial<Record<Collection, string>> = {};
  mutating = false;
  mutationError = "";
  mutationErrorCode = "";
  mutationUncertain = false;
  private pendingMutation: { identity: string; key: string } | null = null;
  constructor(
    private transport: OperationsTransport,
    private changed = () => {},
  ) {}
  get targets() {
    return this.pages.targets;
  }
  get deployments() {
    return this.pages.deployments;
  }
  get observations() {
    return this.pages.observations;
  }
  get plans() {
    return this.pages.plans;
  }
  get jobs() {
    return this.pages.jobs;
  }
  get runners() {
    return this.pages.runners;
  }
  /**
   * True once the collection has loaded with exactly these filters. False
   * before the first answer, after a failure and after the filters change
   * (the rows are emptied then), so a view never reads "none" as "unknown".
   */
  loaded(
    collection: Collection,
    filters: { target_id?: string; deployment_id?: string } = {},
  ): boolean {
    return this.loadedFilters[collection] === JSON.stringify(filters);
  }
  setScope(scope: string, readable: boolean, authority = "") {
    if (
      scope === this.scope &&
      readable === this.readable &&
      authority === this.authority
    )
      return;
    this.authority = authority;
    this.filters = {};
    this.generation++;
    this.scope = scope;
    this.readable = readable;
    this.pages = emptyPages();
    this.cursors = {};
    this.errors = {};
    this.errorInfo = {};
    this.loadedFilters = {};
    this.loading.clear();
    this.sequence = {};
    this.mutating = false;
    this.cancelMutation();
    this.changed();
  }
  async load<K extends Collection>(
    collection: K,
    append = false,
    filters: { target_id?: string; deployment_id?: string } = {},
  ): Promise<void> {
    if (!this.scope || !this.readable || (append && !this.cursors[collection]))
      return;
    const filterKey = JSON.stringify(filters);
    if (this.filters[collection] !== filterKey) {
      this.filters[collection] = filterKey;
      this.pages[collection] = [];
      this.cursors[collection] = null;
      delete this.loadedFilters[collection];
      append = false;
    }
    const generation = this.generation;
    const sequence = (this.sequence[collection] ?? 0) + 1;
    this.sequence[collection] = sequence;
    const params = new URLSearchParams({ limit: "50" });
    if (append && this.cursors[collection])
      params.set("cursor", this.cursors[collection]!);
    for (const [name, value] of Object.entries(filters))
      if (value) params.set(name, value);
    this.loading.add(collection);
    delete this.errors[collection];
    delete this.errorInfo[collection];
    this.changed();
    try {
      const result = await this.transport.request<Page<Collections[K]>>(
        `${this.scope}/${collections[collection]}?${params}`,
      );
      if (
        generation !== this.generation ||
        sequence !== this.sequence[collection]
      )
        return;
      // Do not retain stale rows when refreshing a target-filtered collection.
      this.pages[collection] = (
        append ? [...this.pages[collection], ...result.items] : result.items
      ) as (typeof this.pages)[K];
      this.cursors[collection] = result.next_cursor;
      this.loadedFilters[collection] = filterKey;
    } catch (error) {
      if (
        generation === this.generation &&
        sequence === this.sequence[collection]
      ) {
        const plain = describeError(error);
        this.errors[collection] = plain.message;
        this.errorInfo[collection] = { code: plain.code, status: plain.status };
        if (!append) {
          this.pages[collection] = [];
          this.cursors[collection] = null;
          delete this.loadedFilters[collection];
        }
      }
    } finally {
      if (
        generation === this.generation &&
        sequence === this.sequence[collection]
      ) {
        this.loading.delete(collection);
        this.changed();
      }
    }
  }
  async read<K extends Collection>(
    collection: K,
    id: string,
  ): Promise<Collections[K] | null> {
    if (!this.scope || !this.readable) return null;
    const generation = this.generation;
    const result = await this.transport.request<Collections[K]>(
      `${this.scope}/${collections[collection]}/${encodeURIComponent(id)}`,
    );
    return generation === this.generation ? result : null;
  }
  async approval(id: string): Promise<Approval | null> {
    if (!this.scope || !this.readable) return null;
    const generation = this.generation;
    const result = await this.transport.request<Approval | null>(
      `${this.scope}/deployment-plans/${encodeURIComponent(id)}/approval`,
    );
    return generation === this.generation ? result : null;
  }
  canRecover(path: string, body: unknown, method = "POST", revision?: number) {
    return (
      this.mutationUncertain &&
      this.pendingMutation?.identity ===
        canonicalJson([
          this.scope,
          this.authority,
          path,
          method,
          body,
          revision ?? null,
        ])
    );
  }
  cancelMutation() {
    if (this.mutating) return;
    this.pendingMutation = null;
    this.mutationError = "";
    this.mutationErrorCode = "";
    this.mutationUncertain = false;
  }
  async mutate<T>(
    path: string,
    body: unknown,
    method = "POST",
    revision?: number,
    key?: string,
  ): Promise<T | null> {
    if (!this.scope || !this.readable || this.mutating) return null;
    if (
      !/^(deployment-targets|deployments|deployment-observations|deployment-plans|deployment-jobs|deployment-runners)(\/[A-Za-z0-9-]+){0,2}$/.test(
        path,
      )
    )
      throw Error("Unsupported Operations command");
    const identity = canonicalJson([
      this.scope,
      this.authority,
      path,
      method,
      body,
      revision ?? null,
    ]);
    const intent = {
      identity,
      key:
        key ??
        (this.pendingMutation?.identity === identity
          ? this.pendingMutation.key
          : crypto.randomUUID()),
    };
    this.pendingMutation = intent;
    const generation = this.generation;
    this.mutating = true;
    this.mutationError = "";
    this.changed();
    this.mutationErrorCode = "";
    this.mutationUncertain = false;
    const headers: Record<string, string> = { "Idempotency-Key": intent.key };
    if (revision !== undefined) headers["If-Match"] = `"${revision}"`;
    try {
      const result = await this.transport.request<T>(
        `${this.scope}/${path}`,
        method,
        body,
        headers,
      );
      if (generation === this.generation && this.pendingMutation === intent)
        this.pendingMutation = null;
      return generation === this.generation ? result : null;
    } catch (error) {
      if (generation === this.generation) {
        const plain = describeError(error);
        this.mutationError = plain.message;
        this.mutationErrorCode = plain.code;
        this.mutationUncertain =
          plain.status === 0 || plain.status === 408 || plain.status >= 500;
        if (!this.mutationUncertain && this.pendingMutation === intent)
          this.pendingMutation = null;
      }
      return null;
    } finally {
      if (generation === this.generation) {
        this.mutating = false;
        this.changed();
      }
    }
  }
}
