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
// Readiness for calling REST APIs through the built-in weave-http@2.0.0
// connector: a checklist that names who must act for each missing piece. Pure:
// the component gathers the facts (identity grants, capabilities.read, the
// connector descriptor, published connectors and environment releases) and
// this module only judges them. Platform settings such as native executors
// and secret grants are not observable, so the verdict is "looks ready".
import type { Identity } from "../connection";
import { grantCommand } from "./grant-command";
import {
  connectorReleaseCandidates,
  type ConnectorVersionView,
  type DescriptorView,
  type ReleaseView,
} from "./activation-requirements";

export const httpConnector = "weave-http@2.0.0";
export const httpAdapter = "weave-http-v2";

/** The selected workspace, as Studio's session profile carries it. */
export interface ScopeProfile {
  baseUrl?: string | null;
  tenantId: string | null;
  projectId: string | null;
  environmentId: string | null;
}

/** What a platform read returned: a value, a refusal, a failure, or nothing yet. */
export type Observation<T> =
  | { state: "ok"; value: T }
  | { state: "forbidden" }
  | { state: "unavailable"; code: string }
  | { state: "pending" };

export interface ReadinessFacts {
  /** The signed-in identity with its grants; null when it is not known. */
  identity: Identity | null;
  profile: ScopeProfile | null;
  /** Installed adapters from capabilities.read (`connectors`). */
  adapters: Observation<string[]>;
  /** The weave-http-v2 descriptor; a null value means the platform does not have it. */
  descriptor: Observation<DescriptorView | null>;
  /** Published Connector versions of this project. */
  connectorVersions: Observation<ConnectorVersionView[]>;
  /** Worker releases of the selected environment. */
  releases: Observation<ReleaseView[]>;
}

export type StepStatus = "done" | "action" | "unknown" | "info";
export interface ReadinessStep {
  id:
    | "publish"
    | "connections"
    | "activate"
    | "run"
    | "installed"
    | "connector"
    | "release"
    | "secrets"
    | "access";
  status: StepStatus;
  title: string;
  /** What happens next, in plain language. */
  detail: string;
  /** Who must act, for example "Workspace administrator"; empty when nobody needs to. */
  who: string;
  /** A command the person who acts can run. */
  command?: string;
}
export interface Readiness {
  steps: ReadinessStep[];
  /** The platform side looks ready: connector installed, published and runnable here. */
  looksReady: boolean;
  /** Some facts are still loading. */
  checking: boolean;
  summary: string;
  /** One line for the API action builder: "Ready to publish", "2 things to set up". */
  short: string;
  /** The loopback (local or desktop) platform. */
  local: boolean;
  /** The published weave-http@2.0.0 Connector version ID, or "". */
  connectorVersionId: string;
  /** The installed or published weave-http@2.0.0 definition digest, or "". */
  connectorDigest: string;
  /** Environment releases that bind that digest. */
  releaseIds: string[];
}

/** Same scope rules as the shell's `can()`: a grant applies to this workspace and to any resource. */
export function grantedIn(
  identity: Identity | null,
  profile: ScopeProfile | null,
  capability: string,
): boolean {
  if (!identity || !profile) return false;
  return identity.grants.some(
    (grant) =>
      grant.capabilities.includes(capability) &&
      (!grant.scope ||
        (grant.scope.tenant_id === profile.tenantId &&
          (!grant.scope.project_id ||
            grant.scope.project_id === profile.projectId) &&
          (!grant.scope.environment_id ||
            grant.scope.environment_id === profile.environmentId))) &&
      !grant.resources.length,
  );
}

/** True for a platform on this computer (127.0.0.1, localhost or ::1). */
export function isLoopbackPlatform(baseUrl: string | null | undefined) {
  if (!baseUrl) return false;
  try {
    const host = new URL(baseUrl).hostname.toLowerCase();
    return (
      host === "localhost" ||
      host.endsWith(".localhost") ||
      host === "[::1]" ||
      /^127(\.\d{1,3}){3}$/.test(host)
    );
  } catch {
    return false;
  }
}

const enableLocal = "weave platform integrations enable";
const localUser =
  "weave platform user --username NAME --role tenant_admin --role developer --role deployer --role operator --role viewer";

function grantStep(
  facts: ReadinessFacts,
  local: boolean,
  id: "publish" | "connections" | "activate" | "run",
  capability: string,
  done: string,
  missing: string,
  who: string,
): ReadinessStep {
  if (!facts.identity)
    return {
      id,
      status: "unknown",
      title: done,
      detail: "Sign in to this platform to check your access.",
      who: "",
    };
  if (grantedIn(facts.identity, facts.profile, capability))
    return { id, status: "done", title: done, detail: "", who: "" };
  return {
    id,
    status: "action",
    title: missing,
    detail: local
      ? `On this computer's platform, create a person with all the roles you need (${capability}).`
      : `Ask for access that includes ${capability}, or ask someone who has it to do this step.`,
    who: local ? "You, on this computer" : who,
    command: local ? localUser : undefined,
  };
}

function unknownStep(
  id: ReadinessStep["id"],
  title: string,
  observation: Observation<unknown>,
  forbidden: string,
): ReadinessStep {
  return {
    id,
    status: "unknown",
    title,
    detail:
      observation.state === "pending"
        ? "Checking…"
        : observation.state === "forbidden"
          ? forbidden
          : "Studio couldn't check this right now. Try again in a moment.",
    who: "",
  };
}

/** Judges the gathered facts and names who acts for each missing piece. */
export function integrationReadiness(facts: ReadinessFacts): Readiness {
  const local = isLoopbackPlatform(facts.profile?.baseUrl);
  const steps: ReadinessStep[] = [
    grantStep(
      facts,
      local,
      "publish",
      "definition.publish",
      "You can publish API actions",
      "You can't publish API actions yet",
      "Workspace administrator",
    ),
    grantStep(
      facts,
      local,
      "connections",
      "connection.manage",
      "You can create connections",
      "An administrator creates the connection",
      "Workspace administrator",
    ),
    grantStep(
      facts,
      local,
      "activate",
      "release.activate",
      "You can activate workflow versions",
      "A deployer activates the workflow",
      "Deployer",
    ),
    grantStep(
      facts,
      local,
      "run",
      "run.start",
      "You can start runs",
      "An operator starts runs",
      "Operator",
    ),
  ];
  // Is the built-in connector installed on the platform?
  const descriptor =
    facts.descriptor.state === "ok" ? facts.descriptor.value : undefined;
  // A platform without the descriptor endpoint answers 404 too, so the
  // installed adapters decide when they are known.
  let installed: boolean | undefined;
  if (descriptor) installed = true;
  else if (facts.adapters.state === "ok")
    installed = facts.adapters.value.includes(httpAdapter);
  else if (facts.descriptor.state === "ok") installed = false;
  if (installed === true)
    steps.push({
      id: "installed",
      status: "done",
      title: "The platform has the built-in HTTP connector",
      detail: "",
      who: "",
    });
  else if (installed === false)
    steps.push({
      id: "installed",
      status: "action",
      title: "The platform doesn't have the built-in HTTP connector",
      detail: `Upgrade the platform to a version that includes ${httpConnector}.`,
      who: local ? "You, on this computer" : "Platform operator",
    });
  else
    steps.push(
      unknownStep(
        "installed",
        "The platform has the built-in HTTP connector",
        facts.descriptor.state === "pending"
          ? facts.descriptor
          : facts.adapters,
        "Your account can't read the platform's capabilities (catalog access).",
      ),
    );
  // Is it published in this project?
  const versions =
    facts.connectorVersions.state === "ok" ? facts.connectorVersions.value : [];
  const [name, version] = httpConnector.split("@");
  const published = versions.find(
    (item) =>
      !item.retired &&
      item.name === name &&
      item.version === version &&
      (!descriptor || item.definition_digest === descriptor.digest),
  );
  const connectorVersionId =
    descriptor?.published_version_id ?? published?.id ?? "";
  const connectorDigest =
    descriptor?.digest ?? published?.definition_digest ?? "";
  if (connectorVersionId)
    steps.push({
      id: "connector",
      status: "done",
      title: `${httpConnector} is published in this project`,
      detail: "",
      who: "",
    });
  else if (
    installed === false ||
    (descriptor && descriptor.published_version_id === null) ||
    (!descriptor && facts.connectorVersions.state === "ok")
  )
    steps.push({
      id: "connector",
      status: "action",
      title: `${httpConnector} isn't published in this project yet`,
      detail: local
        ? "Enabling integrations publishes the installed connector, registers this computer as its release and grants it access."
        : "Publish the installed connector exactly as the platform describes it: publish its source with weave definitions publish --collection connectors.",
      who: local ? "You, on this computer" : "Developer or platform operator",
      command: local
        ? enableLocal
        : `weave connector descriptor ${httpAdapter} --output json`,
    });
  else
    steps.push(
      unknownStep(
        "connector",
        `${httpConnector} is published in this project`,
        facts.descriptor.state === "ok"
          ? facts.connectorVersions
          : facts.descriptor,
        "Your account can't read this project's connectors (catalog access).",
      ),
    );
  // Can a release in this environment run it?
  let releaseIds: string[] = [];
  if (facts.releases.state === "ok" && connectorDigest) {
    releaseIds = connectorReleaseCandidates(
      {
        reference: httpConnector,
        digest: connectorDigest,
        adapter: httpAdapter,
        actions: [],
        usedBy: [],
      },
      facts.releases.value,
    ).map((release) => release.id);
    steps.push(
      releaseIds.length
        ? {
            id: "release",
            status: "done",
            title: local
              ? "Connector actions are enabled on this computer"
              : "A release in this environment can run API actions",
            detail: local
              ? "If you enabled them just now, restart the platform with weave platform start."
              : "",
            who: "",
          }
        : {
            id: "release",
            status: "action",
            title: local
              ? "Connector actions aren't enabled on this computer"
              : "No release in this environment runs API actions yet",
            detail: local
              ? "Enable integrations, then restart the platform. Only public HTTPS addresses can be called from this computer."
              : `Register a release with the ${httpConnector} bindings in this environment and turn on native connector execution.`,
            who: local ? "You, on this computer" : "Platform operator",
            command: local
              ? enableLocal
              : "weave workers releases create --request release.json",
          },
    );
  } else if (facts.releases.state === "ok" && installed === false)
    steps.push({
      id: "release",
      status: "action",
      title: "No release in this environment runs API actions yet",
      detail: "This follows once the platform has the connector.",
      who: local ? "You, on this computer" : "Platform operator",
    });
  else if (facts.releases.state === "ok")
    steps.push({
      id: "release",
      status: "unknown",
      title: "A release in this environment can run API actions",
      detail: `Studio checks this once ${httpConnector} is published in this project.`,
      who: "",
    });
  else
    steps.push(
      unknownStep(
        "release",
        "A release in this environment can run API actions",
        facts.releases,
        "Your account can't list this environment's releases (catalog access).",
      ),
    );
  steps.push(
    local
      ? {
          id: "secrets",
          status: "info",
          title: "Secret values stay on this computer",
          detail:
            "Store each value under a handle name, then restart the platform after adding a new handle. Studio only uses the handle name.",
          who: "You, on this computer",
          command: "weave platform secret set --handle NAME",
        }
      : {
          id: "secrets",
          status: "info",
          title: "Secret values stay with the platform",
          detail:
            "The platform operator stores each value and tells you its handle name. Studio never asks for secret values.",
          who: "Platform operator",
        },
    local
      ? {
          id: "access",
          status: "info",
          title:
            "After creating a connection with secrets, let the release use it",
          detail: "Grant read, and write only when an action changes data.",
          who: "You, on this computer",
          // The same command every screen shows; the real revision ID
          // replaces REVISION_ID once the connection exists.
          command: grantCommand("REVISION_ID", true).command,
        }
      : {
          id: "access",
          status: "info",
          title:
            "After creating a connection with secrets, let the release use it",
          detail:
            "An administrator grants the connector release access to the connection revision.",
          who: "Workspace administrator",
          command: grantCommand("REVISION_ID", false).command,
        },
  );
  const platform = steps.filter((step) =>
    ["installed", "connector", "release"].includes(step.id),
  );
  const looksReady = platform.every((step) => step.status === "done");
  const checking = [
    facts.adapters,
    facts.descriptor,
    facts.connectorVersions,
    facts.releases,
  ].some((observation) => observation.state === "pending");
  const waiting = steps.filter((step) => step.status === "action").length;
  const summary = checking
    ? "Checking what this platform needs…"
    : looksReady
      ? "Looks ready. Studio can't see platform settings, so the first run confirms it."
      : waiting
        ? `${waiting} ${waiting === 1 ? "step needs" : "steps need"} someone to act before API actions can run here.`
        : "Studio couldn't check everything. The steps below explain what it could see.";
  return {
    steps,
    looksReady,
    checking,
    summary,
    short: checking
      ? "Checking what this platform needs…"
      : waiting
        ? `${waiting} ${waiting === 1 ? "thing" : "things"} to set up`
        : looksReady
          ? "Ready to publish"
          : "Studio couldn't check everything",
    local,
    connectorVersionId,
    connectorDigest,
    releaseIds,
  };
}
