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
// Calls the API action builder makes. Local analysis goes to the paired
// Studio host (/studio/local/*), which never fetches anything: OpenAPI text
// is pasted or uploaded, never referenced by URL. Platform calls go through
// the same-origin bridge and are authorized by the platform.
import type { StudioApi } from "../api";
import {
  HTTP_ADAPTER,
  type DiagnosticLike,
  type Relaxations,
} from "./http-action-model";

export type JsonObject = Record<string, unknown>;

export interface HostDiagnostic extends DiagnosticLike {
  code: string;
  severity: "error" | "warning" | "info";
  message: string;
  path: string;
  hint?: string | null;
}
/** POST /studio/local/http-action */
export interface HttpActionBuildResult {
  ok: boolean;
  action: JsonObject | null;
  diagnostics: HostDiagnostic[];
  compiled: boolean;
}
export interface InventoryOperation {
  key: string;
  operationId?: string | null;
  method: string;
  path: string;
  summary?: string | null;
  tags: string[];
  supported: boolean;
  sideEffect: "read_only" | "non_idempotent";
  statuses: number[];
  reasons: HostDiagnostic[];
}
/** POST /studio/local/openapi/inventory */
export interface OpenApiInventory {
  ok: boolean;
  openapi?: string | null;
  title?: string | null;
  servers: string[];
  operations: InventoryOperation[];
  diagnostics: HostDiagnostic[];
  truncated: boolean;
}
/** POST /studio/local/openapi/import (built-in target only) */
export interface OpenApiImportResult {
  ok: boolean;
  actions: JsonObject[];
  connectionExample?: JsonObject | null;
  provenance?: JsonObject | null;
  sourceMap?: Record<string, string>;
  truncated?: boolean;
  policy: JsonObject | null;
  diagnostics: HostDiagnostic[];
}
/** compiler.compile response (the fields the builder reads). */
export interface PlatformCompileResult {
  ok: boolean;
  validationOk?: boolean;
  diagnostics: HostDiagnostic[];
}
/** connector_descriptors.read response (the fields the builder reads). */
export interface ConnectorDescriptorView {
  adapter: string;
  reference: string;
  digest: string;
  source: string;
  published_version_id: string | null;
}
export interface PublishedVersion {
  id: string;
  kind?: string;
  name: string;
  version: string;
}
export interface OpenApiSource {
  source: string;
  format: "json" | "yaml";
  relaxations?: Relaxations;
}

/** Local analysis may take up to the host's 30-second budget. */
const LOCAL_TIMEOUT = 35_000;

export class HttpActionClient {
  constructor(private readonly api: StudioApi) {}

  build(request: JsonObject) {
    return this.api.request<HttpActionBuildResult>(
      "/studio/local/http-action",
      "POST",
      { request },
      {},
      LOCAL_TIMEOUT,
    );
  }
  inventory(source: OpenApiSource) {
    return this.api.request<OpenApiInventory>(
      "/studio/local/openapi/inventory",
      "POST",
      withoutUndefined({ ...source }),
      {},
      LOCAL_TIMEOUT,
    );
  }
  importOpenApi(
    source: OpenApiSource & { selection: string[]; name?: string },
  ) {
    return this.api.request<OpenApiImportResult>(
      "/studio/local/openapi/import",
      "POST",
      withoutUndefined({ ...source, target: "builtin" }),
      {},
      LOCAL_TIMEOUT,
    );
  }
  /** Full platform compile against the project catalog (not partial validation). */
  compile(document: JsonObject) {
    return this.api.request<PlatformCompileResult>(
      `${this.api.project}/compiler/compile`,
      "POST",
      { source: document, format: "object" },
      {},
      30_000,
    );
  }
  /** definitions.publish for one Action; the caller keeps the key across unknown outcomes. */
  publishAction(yaml: string, key: string) {
    return this.api.mutate<PublishedVersion>(
      `${this.api.project}/actions`,
      "POST",
      { source: yaml, format: "yaml" },
      undefined,
      key,
    );
  }
  descriptor() {
    return this.api.request<ConnectorDescriptorView>(
      `${this.api.project}/connector-descriptors/${HTTP_ADAPTER}`,
    );
  }
  /**
   * Publishes the installed built-in connector exactly as the platform
   * reports it; the key is derived from its digest, so repeats replay.
   */
  publishConnector(descriptor: ConnectorDescriptorView) {
    return this.api.mutate<PublishedVersion>(
      `${this.api.project}/connectors`,
      "POST",
      { source: descriptor.source, format: "json" },
      undefined,
      `enable-${descriptor.adapter}-${descriptor.digest}`.slice(0, 200),
    );
  }
}

function withoutUndefined(value: JsonObject): JsonObject {
  return Object.fromEntries(
    Object.entries(value).filter(([, v]) => v !== undefined),
  );
}
