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
import type { StudioApi } from "../../api";

export interface FileReference {
  kind: "weave/file";
  id: string;
  filename: string;
  contentType: string;
  sizeBytes: number;
  sha256: string;
}
export interface FileAccess {
  api: StudioApi;
  key: string;
  canRead: boolean;
  canManage: boolean;
  active: () => boolean;
  task?: { id: string; revision: number };
}
export interface FileUpload {
  file: FileReference;
  state: "uploading" | "ready" | "deleted";
  received_chunks: number[];
  chunk_bytes: number;
}
export const FILE_CHUNK_BYTES = 262144;
export const MAX_FILE_BYTES = 26214400;
const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
export function isFileSchema(schema: unknown): boolean {
  return (
    object(object(object(schema)["properties"])["kind"])["const"] ===
    "weave/file"
  );
}
export function fileReference(value: unknown): FileReference | null {
  const item = object(value);
  return item["kind"] === "weave/file" &&
    typeof item["id"] === "string" &&
    typeof item["filename"] === "string" &&
    typeof item["sizeBytes"] === "number" &&
    typeof item["contentType"] === "string" &&
    typeof item["sha256"] === "string"
    ? (value as FileReference)
    : null;
}
export function filesIn(
  value: unknown,
  label = "File",
  depth = 0,
): { label: string; file: FileReference }[] {
  const file = fileReference(value);
  if (file) return [{ label, file }];
  if (!value || typeof value !== "object" || depth > 16) return [];
  return Object.entries(value).flatMap(([key, child]) =>
    filesIn(child, label === "File" ? key : `${label} › ${key}`, depth + 1),
  );
}
