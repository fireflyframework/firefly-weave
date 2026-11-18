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
import { describe, expect, it } from "vitest";
import { fileValue } from "../src/app/editor/ndv/panes/file-value";
import { cellText, tableOf } from "../src/app/editor/ndv/panes/table-view";
import { schemaRows, withSamples } from "../src/app/editor/ndv/panes/views";
import { fileSize } from "../src/app/forms/core/file-reference";

const invoice = {
  kind: "weave/file",
  id: "f-1",
  filename: "invoice.pdf",
  contentType: "application/pdf",
  sizeBytes: 122_880,
  sha256: "ab".repeat(32),
};
const fileSchema = {
  type: "object",
  properties: {
    kind: { const: "weave/file" },
    id: { type: "string" },
    filename: { type: "string" },
    contentType: { type: "string" },
    sizeBytes: { type: "integer" },
    sha256: { type: "string" },
  },
};

describe("file values in step details", () => {
  it("a file reference reads as a file with its name, size and type", () => {
    expect(fileValue(invoice, "real")).toEqual({
      label: "File",
      detail: "invoice.pdf · 120 KiB · application/pdf",
      download: true,
    });
    expect(fileSize(900)).toBe("900 B");
    expect(fileSize(2_621_440)).toBe("2.5 MiB");
  });

  it("a simulated or generated one reads as a sample file and can't be downloaded", () => {
    expect(fileValue(invoice, "sample")).toEqual({
      label: "Sample file",
      detail: "invoice.pdf · 120 KiB · application/pdf",
      download: false,
    });
  });

  it("anything else isn't a file", () => {
    const { sha256: _missing, ...noDigest } = invoice;
    expect(fileValue(noDigest, "real")).toBeNull();
    expect(fileValue("invoice.pdf", "real")).toBeNull();
    expect(fileValue(null, "sample")).toBeNull();
  });

  it("the tree shows a file as one value, never its fields", () => {
    const schema = {
      type: "object",
      properties: { attachment: fileSchema, note: { type: "string" } },
    };
    const rows = schemaRows(schema);
    expect(rows.map((row) => [row.key, row.typeLabel])).toEqual([
      ["/attachment", "File"],
      ["/note", "Text"],
    ]);
    const sampled = withSamples(rows, { attachment: invoice, note: "x" }, "");
    expect(sampled[0].sample).toBe("invoice.pdf · 120 KiB · application/pdf");
  });

  it("the table shows a file as one cell", () => {
    expect(cellText(invoice)).toBe(
      "Sample file · invoice.pdf · 120 KiB · application/pdf",
    );
    expect(tableOf({ attachment: invoice }, "real").rows).toEqual([
      ["attachment", "File · invoice.pdf · 120 KiB · application/pdf"],
    ]);
    expect(tableOf([{ attachment: invoice }]).rows).toEqual([
      ["Sample file · invoice.pdf · 120 KiB · application/pdf"],
    ]);
  });
});
