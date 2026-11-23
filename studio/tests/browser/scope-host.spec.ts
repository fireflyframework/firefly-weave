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
// The process that answers hostScope's questions, with a small Node program
// in place of Python: what a bad answer or an ended process does to the
// question that gets it and to the questions after it.
import { expect, test } from "@playwright/test";
import { ScopeHost } from "./support";

/**
 * Answers each line, in order, with `{"scope": <the question>}`. A question
 * with `say` prints that line first and its answer 100 ms later, as a stray
 * print would; one with `end` writes it to stderr and exits with code 3.
 */
const ANSWERS = `
let answered = Promise.resolve();
require("node:readline")
  .createInterface({ input: process.stdin })
  .on("line", (line) => {
    const question = JSON.parse(line);
    answered = answered.then(async () => {
      if (question.end !== undefined) {
        process.stderr.write(question.end);
        process.exit(3);
      }
      if (question.say !== undefined) {
        console.log(question.say);
        await new Promise((done) => setTimeout(done, 100));
      }
      console.log(JSON.stringify({ scope: question }));
    });
  });
`;

let host: ScopeHost;
test.beforeEach(() => {
  host = new ScopeHost([process.execPath, "-e", ANSWERS]);
});
test.afterEach(() => host.close());

test("an answer that isn't JSON fails its question, and the next gets its own answer", async () => {
  await expect(host.ask({ say: "Loading the compiler…" })).rejects.toThrow(
    "The scope process answered with a line that isn't JSON: Loading the compiler…",
  );
  await expect(host.ask({ path: "steps.charge" })).resolves.toEqual({
    path: "steps.charge",
  });
});

// JSON that isn't an object with "scope" or "error": a stray print, not an answer.
for (const line of ["42", "[1, 2]", '"loading"', "null"]) {
  test(`a JSON line that isn't an answer (${line}) fails its question, and the next gets its own answer`, async () => {
    await expect(host.ask({ say: line })).rejects.toThrow(
      `The scope process answered with a line that isn't an answer: ${line}`,
    );
    await expect(host.ask({ path: "steps.charge" })).resolves.toEqual({
      path: "steps.charge",
    });
  });
}

test("a process that ends says what it wrote to stderr", async () => {
  await expect(
    host.ask({ end: "Traceback: ModuleNotFoundError: weave" }),
  ).rejects.toThrow(
    "The scope process ended: 3.\nTraceback: ModuleNotFoundError: weave",
  );
  await expect(host.ask({ path: "steps.charge" })).resolves.toEqual({
    path: "steps.charge",
  });
});

test("only the end of a long stderr is kept", async () => {
  const error = await host
    .ask({ end: `${"x".repeat(20_000)}the last words` })
    .catch((reason: Error) => reason);
  expect(error).toBeInstanceOf(Error);
  expect((error as Error).message).toMatch(/xthe last words$/);
  expect((error as Error).message.length).toBeLessThan(5_000);
});
