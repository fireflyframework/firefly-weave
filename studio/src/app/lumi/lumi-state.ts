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
export interface DraftRevision {
  opened: number;
  revision: number;
  source: string;
  buffer: string;
}
export interface LumiOperationAttachment {
  kind:
    | "deployment-target"
    | "deployment"
    | "deployment-observation"
    | "deployment-plan"
    | "deployment-job";
  id: string;
  label: string;
}
export interface LumiProposal {
  title: string;
  kind: "workflow" | "decisionTable" | "action" | "connector";
  format: "yaml" | "json";
  source: string;
  /** Errors the platform found when it checked the proposal; 0 when it is valid. */
  diagnostics?: number;
}
/** The longest message Weave AI accepts. */
const MESSAGE_LIMIT = 20000;
/** "2 problems", or "" for a proposal the platform found valid. */
export function problemLabel(proposal: LumiProposal): string {
  const count = proposal.diagnostics ?? 0;
  return count ? `${count} problem${count === 1 ? "" : "s"}` : "";
}
/** A request that asks Weave AI to fix a proposal: the problems in the message, the proposal as the draft. */
export function fixRequest(
  proposal: LumiProposal,
  problems: readonly string[],
): { message: string; draft: { format: "yaml" | "json"; source: string } } {
  const count = problems.length;
  let message = `Fix the proposal "${proposal.title}". The platform found ${count} problem${count === 1 ? "" : "s"}:\n${problems
    .map((problem) => `- ${problem}`)
    .join("\n")}`;
  if (message.length > MESSAGE_LIMIT)
    message = message.slice(0, MESSAGE_LIMIT - 1) + "…";
  return {
    message,
    draft: { format: proposal.format, source: proposal.source },
  };
}
export interface ProposalReview extends LumiProposal {
  base: DraftRevision;
  validated: string | null;
  problems: string[];
}
export interface LumiTurn {
  role: "user" | "assistant";
  content: string;
  proposals?: ProposalReview[];
  followUps?: string[];
}
export class LumiConversation {
  key = "";
  generation = 0;
  turns: LumiTurn[] = [];
  sync(key: string) {
    if (key === this.key) return false;
    this.key = key;
    this.clear();
    return true;
  }
  clear() {
    this.generation++;
    this.turns = [];
  }
  history() {
    return this.turns
      .slice(-16)
      .map(({ role, content }) => ({ role, content }));
  }
}
export const unchangedDraft = (base: DraftRevision, current: DraftRevision) =>
  base.opened === current.opened &&
  base.revision === current.revision &&
  base.source === current.source &&
  base.buffer === current.buffer;
