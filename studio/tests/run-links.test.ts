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
import {
  parseRunsLink,
  runsLink,
} from "../src/app/editor/runs-tab/run-links";

const RUN = "1c0d7f8a-4d66-4e7c-8b62-5a2c1d3e8f12";

describe("a link to the Runs tab", () => {
  it("reads only a Runs link, with a run ID only when it is one", () => {
    expect(parseRunsLink(`?tab=runs&run=${RUN}`)).toEqual({
      tab: "runs",
      run: RUN,
    });
    expect(parseRunsLink("?tab=runs")).toEqual({ tab: "runs", run: null });
    // Anything but a UUID opens the tab without a run.
    expect(parseRunsLink("?tab=runs&run=abc")).toEqual({
      tab: "runs",
      run: null,
    });
    expect(parseRunsLink("?tab=runs&run=../../settings")).toEqual({
      tab: "runs",
      run: null,
    });
    expect(parseRunsLink("?tab=source")).toBeNull();
    expect(parseRunsLink("")).toBeNull();
    // Other parameters change nothing.
    expect(parseRunsLink(`?x=1&run=${RUN}&tab=runs&env=prod`)).toEqual({
      tab: "runs",
      run: RUN,
    });
  });

  it("writes the link with the run's ID only", () => {
    expect(runsLink("d-1", RUN)).toBe(
      `/workflows/d-1/designer?tab=runs&run=${RUN}`,
    );
    expect(runsLink("d-1", null)).toBe("/workflows/d-1/designer?tab=runs");
    expect(runsLink("local:a b", null)).toBe(
      "/workflows/local%3Aa%20b/designer?tab=runs",
    );
  });
});
