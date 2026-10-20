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
// What an import holds: one workflow file, with its canvas file and its
// test data file when the person chose them. Canvas files end in
// .canvas.json, or .layout.json when Studio saved them before notes and
// trigger choices existed; test data files end in .testdata.json.
import {
  CANVAS_MAX_BYTES,
  TEST_DATA_MAX_BYTES,
} from "./editor/state/test-data";

export interface ImportFile {
  name: string;
  size: number;
}
export type ImportChoice =
  | { workflow: number; canvas: number | null; testData: number | null }
  | { error: string };

const WORKFLOW = /\.(ya?ml|json)$/i;
const CANVAS = /\.(canvas|layout)\.json$/i;
export const TEST_DATA = /\.testdata\.json$/i;
const SET =
  "Choose one workflow file, and its canvas and test data files if you have them.";

function checked(
  files: readonly ImportFile[],
  workflow: number,
  canvas: number | null,
  testData: number | null,
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
  if (testData !== null && files[testData].size > TEST_DATA_MAX_BYTES)
    return {
      error:
        "This test data file is larger than 1 MB. Choose the workflow file without it.",
    };
  return { workflow, canvas, testData };
}

export function chooseImport(files: readonly ImportFile[]): ImportChoice {
  const canvases: number[] = [];
  const tests: number[] = [];
  const workflows: number[] = [];
  files.forEach((file, index) =>
    (CANVAS.test(file.name)
      ? canvases
      : TEST_DATA.test(file.name)
        ? tests
        : workflows
    ).push(index),
  );
  if (files.length === 1 && canvases.length)
    return {
      error:
        "Choose the workflow file too: a canvas file opens only with its workflow.",
    };
  if (files.length === 1 && tests.length)
    return {
      error:
        "Choose the workflow file too: a test data file opens only with its workflow.",
    };
  if (workflows.length !== 1 || canvases.length > 1 || tests.length > 1)
    return { error: SET };
  return checked(files, workflows[0], canvases[0] ?? null, tests[0] ?? null);
}

/** What an import read: the workflow's text, and its other files' when they could be read. */
export interface ImportText {
  source: string;
  canvas: string | null;
  /** A canvas file was chosen but couldn't be read: the workflow opens without it. */
  canvasUnreadable: boolean;
  testData: string | null;
  /** A test data file was chosen but couldn't be read. */
  testDataUnreadable: boolean;
}
async function readOptional(
  file: Pick<Blob, "text"> | null,
): Promise<{ text: string | null; unreadable: boolean }> {
  if (!file) return { text: null, unreadable: false };
  try {
    return { text: await file.text(), unreadable: false };
  } catch {
    return { text: null, unreadable: true };
  }
}
/** Reads the chosen files; null when the workflow file itself can't be read. */
export async function readImport(
  workflow: Pick<Blob, "text">,
  canvas: Pick<Blob, "text"> | null,
  testData: Pick<Blob, "text"> | null = null,
): Promise<ImportText | null> {
  let source: string;
  try {
    source = await workflow.text();
  } catch {
    return null;
  }
  const layout = await readOptional(canvas);
  const tests = await readOptional(testData);
  return {
    source,
    canvas: layout.text,
    canvasUnreadable: layout.unreadable,
    testData: tests.text,
    testDataUnreadable: tests.unreadable,
  };
}
