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
// The repository's Python interpreter for vitest and Playwright, per
// operating system: .venv/bin/python on macOS and Linux, and
// .venv\Scripts\python.exe on Windows. Locally a missing environment skips
// the Python parity suites; on CI it is an error, so no suite skips silently.
import { existsSync } from "node:fs";
import { dirname, join, posix, resolve, win32 } from "node:path";

/** The checkout root: the nearest folder holding pyproject.toml and studio/package.json. */
export function repositoryRoot(start: string = process.cwd()): string {
  let current = resolve(start);
  for (;;) {
    if (
      existsSync(join(current, "pyproject.toml")) &&
      existsSync(join(current, "studio", "package.json"))
    )
      return current;
    const parent = dirname(current);
    if (parent === current)
      throw new Error(`No Firefly Weave checkout contains ${start}.`);
    current = parent;
  }
}

/** The virtual environment's interpreter inside a checkout. */
export function pythonPath(
  root: string = repositoryRoot(),
  platform: NodeJS.Platform = process.platform,
): string {
  return platform === "win32"
    ? win32.join(root, ".venv", "Scripts", "python.exe")
    : posix.join(root, ".venv", "bin", "python");
}

export const repository = repositoryRoot();
export const python = pythonPath(repository);

/**
 * True when the interpreter exists. On CI a missing interpreter throws, so a
 * Python-backed suite fails instead of skipping.
 */
export function pythonAvailable(
  options: {
    path?: string;
    env?: NodeJS.ProcessEnv;
    exists?: (path: string) => boolean;
  } = {},
): boolean {
  const path = options.path ?? python;
  if ((options.exists ?? existsSync)(path)) return true;
  const ci = (options.env ?? process.env)["CI"];
  if (ci && ci !== "false" && ci !== "0")
    throw new Error(
      `The repository's Python environment is missing at ${path}. Run "uv sync --locked --all-extras --group docs" in the repository first.`,
    );
  return false;
}
