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
// What an import holds: one workflow file, and its canvas file when the
// person chose both. Canvas files end in .canvas.json, or .layout.json when
// Studio saved them before notes and trigger choices existed.
import { CANVAS_MAX_BYTES } from "./editor/state/test-data";

export interface ImportFile {
  name: string;
  size: number;
}
export type ImportChoice =
  | { workflow: number; canvas: number | null }
  | { error: string };

const WORKFLOW = /\.(ya?ml|json)$/i;
const CANVAS = /\.(canvas|layout)\.json$/i;
const PAIR = "Choose one workflow file, and its canvas file if you have one.";

function checked(
  files: readonly ImportFile[],
  workflow: number,
  canvas: number | null,
): ImportChoice {
  if (!WORKFLOW.test(files[workflow].name))
    return { error: "Choose a .yaml, .yml or .json workflow file." };
  if (files[workflow].size > 1024 * 1024)
    return {
      error: "This file is larger than 1 MiB. Choose a smaller workflow file.",
    };
  if (canvas !== null && files[canvas].size > CANVAS_MAX_BYTES)
    return {
      error:
        "This canvas file is larger than 256 KB. Choose the workflow file alone.",
    };
  return { workflow, canvas };
}

export function chooseImport(files: readonly ImportFile[]): ImportChoice {
  if (files.length === 1) {
    if (CANVAS.test(files[0].name))
      return {
        error:
          "Choose the workflow file too: a canvas file opens only with its workflow.",
      };
    return checked(files, 0, null);
  }
  if (files.length !== 2) return { error: PAIR };
  const canvas = files.findIndex((file) => CANVAS.test(file.name));
  const workflow = canvas === 0 ? 1 : 0;
  if (canvas < 0 || CANVAS.test(files[workflow].name)) return { error: PAIR };
  return checked(files, workflow, canvas);
}

/** What an import read: the workflow's text, and its canvas file's when that could be read. */
export interface ImportText {
  source: string;
  canvas: string | null;
  /** A canvas file was chosen but couldn't be read: the workflow opens without it. */
  canvasUnreadable: boolean;
}
/** Reads the chosen files; null when the workflow file itself can't be read. */
export async function readImport(
  workflow: Pick<Blob, "text">,
  canvas: Pick<Blob, "text"> | null,
): Promise<ImportText | null> {
  let source: string;
  try {
    source = await workflow.text();
  } catch {
    return null;
  }
  if (!canvas) return { source, canvas: null, canvasUnreadable: false };
  try {
    return { source, canvas: await canvas.text(), canvasUnreadable: false };
  } catch {
    return { source, canvas: null, canvasUnreadable: true };
  }
}
