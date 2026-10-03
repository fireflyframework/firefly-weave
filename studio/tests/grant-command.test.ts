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
  grantCommand,
  httpReadCapability,
} from "../src/app/integrations/grant-command";

const id = "0f8fad5b-d9cb-469f-a165-70867728950e";

describe("the grant step after creating a connection", () => {
  it("fills in the real revision ID on this computer", () => {
    const step = grantCommand(id, true);
    expect(step.command).toBe(
      `weave platform integrations grant --connection ${id} --access read`,
    );
    expect(step.command).not.toContain("REVISION_ID");
    expect(step.request).toBe("");
  });

  it("gives an administrator the request with the revision ID", () => {
    const step = grantCommand(id, false);
    expect(step.command).toBe("weave workers grant --request grant.json");
    expect(step.file).toBe("grant.json");
    expect(JSON.parse(step.request)).toEqual({
      release_id: "RELEASE_ID",
      connection_revision_id: id,
      capability: httpReadCapability,
    });
  });
});
