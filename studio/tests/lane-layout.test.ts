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
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { StructuredCanvasAdapter } from "../src/app/model";

const source = readFileSync(
  new URL("./fixtures/vendor-payment-approval.yaml", import.meta.url),
  "utf8",
);
const fixture = () => {
  const model = new StructuredCanvasAdapter();
  model.setSource(source);
  return model;
};

describe("bounded sibling lanes", () => {
  it("reserves a separate answer-branch prompt and a normal insertion slot", () => {
    const model = new StructuredCanvasAdapter();
    model.insert("humanTask");
    model.insert("wait");
    const [human, next] = model.nodes();
    expect(human.answerPrompt).toBe("Branch on the answer (approve / reject)");
    const after = model
      .targets()
      .find((target) => target.owner === "root" && target.index === 1)!;
    expect(after.point.y).toBeGreaterThan(human.point.y + 144);
    expect(next.point.y).toBeGreaterThan(after.point.y);
    expect(
      fixture()
        .nodes()
        .find((node) => node.step.id === "approval")!.answerPrompt,
    ).toBeUndefined();
  });
  it("names answer lanes from their rules and keeps tidy a no-op when automatic", () => {
    const model = fixture();
    expect(
      model
        .laneGroups()
        .find((group) => group.id === "route")!
        .lanes.map((lane) => lane.label),
    ).toEqual(["Approve", "Reject", "Otherwise"]);
    expect(
      model
        .targets()
        .find(
          (target) => target.owner === "route/case 1" && target.index === 0,
        )!.label,
    ).toContain("Approve of route");
    const revision = model.revision;
    model.autoLayout();
    expect(model.revision).toBe(revision);
  });
  it("restarts Decision paths and Parallel branches at their shared split", () => {
    const model = fixture();
    const nodes = new Map(model.nodes().map((node) => [node.step.id, node]));
    const approve = nodes.get("pay-vendor")!.point;
    const reject = nodes.get("rejected")!.point;
    expect(approve.y).toBe(reject.y);
    expect(Math.abs(approve.x - reject.x)).toBeGreaterThanOrEqual(288);
    const ledger = nodes.get("post-ledger-entry")!.point;
    const email = nodes.get("send-confirmation")!.point;
    expect(ledger.y).toBe(email.y);
    expect(email.x - ledger.x).toBe(288);
    expect(
      nodes.get("approval")!.point.y - nodes.get("prepare-request")!.point.y,
    ).toBe(112);
    expect(model.source).toBe(source);
  });

  it("ends Fail paths without an outgoing edge or an insertion target", () => {
    const model = fixture();
    expect(
      model.visualConnections(true).filter((edge) => edge.from === "rejected"),
    ).toEqual([]);
    expect(
      model
        .targets()
        .filter(
          (target) => target.owner === "route/case 2" && target.index === 1,
        ),
    ).toEqual([]);
  });

  it("tidy restores automatic lane positions without changing workflow source", () => {
    const model = fixture();
    const automatic = model.nodes().map((node) => node.point);
    model.position("post-ledger-entry", { x: 5, y: 9 });
    model.autoLayout();
    expect(model.layout.positions).toEqual({});
    expect(model.nodes().map((node) => node.point)).toEqual(automatic);
    expect(model.source).toBe(source);
  });
});

describe("lane groups and routes", () => {
  it("bounds each group's sibling lanes and routes them through a shared split and join", () => {
    const model = fixture();
    const group = model
      .laneGroups()
      .find((group) => group.id === "pay-and-notify")!;
    expect(group.lanes.map((lane) => lane.label)).toEqual(["ledger", "email"]);
    expect(group.lanes[0].point.y).toBe(group.lanes[1].point.y);
    expect(group.lanes[0].point.x + group.lanes[0].width + 48).toBe(
      group.lanes[1].point.x,
    );
    for (const lane of group.lanes) {
      expect(lane.point.x).toBeGreaterThanOrEqual(group.point.x);
      expect(lane.point.x + lane.width).toBeLessThanOrEqual(
        group.point.x + group.width,
      );
      expect(lane.point.y + lane.height).toBeLessThanOrEqual(
        group.point.y + group.height,
      );
    }
    expect(model.junctions()["$split:pay-and-notify"]).toEqual(group.split);
    expect(model.junctions()["$join:pay-and-notify"]).toEqual(group.join);
    const links = model.visualConnections(true);
    expect(links).toContainEqual({
      from: "$split:pay-and-notify",
      to: "post-ledger-entry",
    });
    expect(links).toContainEqual({
      from: "$split:pay-and-notify",
      to: "send-confirmation",
    });
    expect(links).toContainEqual({
      from: "post-ledger-entry",
      to: "$join:pay-and-notify",
    });
    expect(links).toContainEqual({
      from: "send-confirmation",
      to: "$join:pay-and-notify",
    });
    expect(links).not.toContainEqual({
      from: "post-ledger-entry",
      to: "send-confirmation",
    });
    expect(model.terminals().map((terminal) => terminal.step)).toEqual([
      "rejected",
    ]);
  });
});
