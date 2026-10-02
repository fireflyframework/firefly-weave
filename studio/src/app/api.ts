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
export interface Profile {
  name: string;
  baseUrl: string;
  tenantId: string | null;
  projectId: string | null;
  environmentId: string | null;
}
export interface Session {
  paired: boolean;
  csrfToken?: string;
  version: string;
  mode: "offline" | "connected";
  profile: Profile | null;
}
export interface Diagnostic {
  message: string;
  code?: string;
  severity?: string;
  [key: string]: unknown;
}
export interface Validation {
  validationOk: boolean;
  errorCount: number;
  diagnostics: Diagnostic[];
  partial?: boolean;
  artifact?: unknown;
}
export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === "object" ? JSON.stringify(detail) : String(detail));
  }
}
export class StudioApi {
  session: Session = {
    paired: false,
    version: "",
    mode: "offline",
    profile: null,
  };
  async request<T>(
    path: string,
    method = "GET",
    body?: unknown,
    headers: Record<string, string> = {},
  ): Promise<T> {
    if (!path.startsWith("/studio/") || path.startsWith("//"))
      throw Error("Studio requests must use the same-origin host boundary.");
    const h: Record<string, string> = { ...headers };
    if (body !== undefined) h["Content-Type"] = "application/json";
    if (method !== "GET" && this.session.csrfToken)
      h["X-Weave-CSRF"] = this.session.csrfToken;
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: h,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(15000),
    });
    const value: unknown = await response
      .json()
      .catch(() => ({ message: `HTTP ${response.status}` }));
    if (!response.ok) throw new ApiError(response.status, value);
    return value as T;
  }
  async pair(code?: string) {
    return (this.session = await this.request<Session>(
      "/studio/session",
      code === undefined ? "GET" : "POST",
      code === undefined ? undefined : { code },
    ));
  }
  get project() {
    const p = this.session.profile;
    if (!p?.tenantId || !p.projectId)
      throw Error("Choose an authorized workspace before this action.");
    return `/studio/api/api/v1/tenants/${encodeURIComponent(p.tenantId)}/projects/${encodeURIComponent(p.projectId)}`;
  }
  get environment() {
    const p = this.session.profile;
    if (!p) throw Error("Choose an environment.");
    if (!p.environmentId)
      throw Error("Choose an authorized environment before this action.");
    return `${this.project}/environments/${encodeURIComponent(p.environmentId)}`;
  }
  validate(source: string, format: string) {
    return this.request<Validation>("/studio/local/validate", "POST", {
      source,
      format,
    });
  }
  async page(
    collection: string,
    environment = false,
    cursor?: string,
    filters: Record<string, string> = {},
  ) {
    return this.request<{
      items: Record<string, unknown>[];
      next_cursor: string | null;
    }>(
      `${environment ? this.environment : this.project}/${collection}?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}${Object.entries(
        filters,
      )
        .filter(([, value]) => value)
        .map(
          ([key, value]) =>
            `&${encodeURIComponent(key)}=${encodeURIComponent(value)}`,
        )
        .join("")}`,
    );
  }
  mutate<T>(
    path: string,
    method: string,
    body: unknown,
    revision?: number,
    key: string = crypto.randomUUID(),
  ) {
    const headers: Record<string, string> = { "Idempotency-Key": key };
    if (revision !== undefined) headers["If-Match"] = `"${revision}"`;
    return this.request<T>(path, method, body, headers);
  }
}
