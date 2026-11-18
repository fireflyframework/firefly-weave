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
// How step details show a file reference: one value with its name, size and
// type, never its fields or content. A file from a real run can be
// downloaded; a simulated or generated one is a sample that holds no file.
import { fileReference, fileSize } from "../../../forms/core/file-reference";

/** Where a value came from: a real run, or a simulation or generated sample. */
export type ValueOrigin = "real" | "sample";

export interface FileValue {
  /** "File" or "Sample file". */
  label: string;
  /** "invoice.pdf · 120 KiB · application/pdf". */
  detail: string;
  download: boolean;
}

export function fileValue(
  value: unknown,
  origin: ValueOrigin,
): FileValue | null {
  const file = fileReference(value);
  if (!file) return null;
  return {
    label: origin === "real" ? "File" : "Sample file",
    detail: [file.filename, fileSize(file.sizeBytes), file.contentType].join(
      " · ",
    ),
    download: origin === "real",
  };
}

/** The file on one line: "File · invoice.pdf · 120 KiB · application/pdf", or null for anything else. */
export function fileText(value: unknown, origin: ValueOrigin): string | null {
  const file = fileValue(value, origin);
  return file ? `${file.label} · ${file.detail}` : null;
}
