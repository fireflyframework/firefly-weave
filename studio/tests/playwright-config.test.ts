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
import { afterEach, describe, expect, it, vi } from "vitest";

// Parallel browser-test lanes: STUDIO_TEST_PORT and STUDIO_TEST_OUTPUT.
async function load(env: { port?: string; output?: string }) {
  vi.resetModules();
  vi.stubEnv("STUDIO_TEST_PORT", env.port);
  vi.stubEnv("STUDIO_TEST_OUTPUT", env.output);
  return (await import("../playwright.config")).default;
}
afterEach(() => {
  vi.unstubAllEnvs();
});

describe("Playwright lanes", () => {
  it("keeps the default lane unchanged", async () => {
    const config = await load({});
    expect(config.use?.baseURL).toBe("http://127.0.0.1:4200");
    expect(config.outputDir).toBeUndefined();
    expect(config.webServer).toMatchObject({
      command: "npm start -- --port 4200",
      url: "http://127.0.0.1:4200",
      env: {},
    });
  });
  it("moves the server, URL and artifacts to the lane's port and folder", async () => {
    const config = await load({ port: "4311", output: "test-results/lib-a" });
    expect(config.use?.baseURL).toBe("http://127.0.0.1:4311");
    expect(config.outputDir).toBe("test-results/lib-a");
    expect(config.webServer).toMatchObject({
      command: "npm start -- --port 4311",
      url: "http://127.0.0.1:4311",
      env: { CI: "1" },
    });
  });
  it("gives a lane without an output folder its own, so it never clears the shared one", async () => {
    // Playwright empties outputDir before a run; the default is all of test-results/.
    const config = await load({ port: "4312" });
    expect(config.outputDir).toBe("test-results/port-4312");
  });
  it("rejects a port that is not a whole number from 1024 to 65535", async () => {
    for (const port of ["80", "4311.5", "70000", "abc"])
      await expect(load({ port }), port).rejects.toThrow(/STUDIO_TEST_PORT/);
  });
});
