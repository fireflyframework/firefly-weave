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
// Connecting a local kind or k3d cluster from Clusters: the names and the two
// commands Studio shows, and how it recognizes the target that
// `weave platform cluster connect` registers. Pure: the wizard renders it,
// tests check it. The name rules are the command line's
// (`platform_clusters.target_name`, `cluster_name_problem`,
// `namespace_problem`); both suites read tests/fixtures/kubernetes/target-names.json.
import { relativeTime } from "../../format";
import type {
  Observation,
  Runner,
  Target,
} from "../../operations/deployment-contracts";
import { runnerState, targetRunner } from "./cluster-model";

export type ClusterProvider = "kind" | "k3d";
export const PROVIDERS: readonly ClusterProvider[] = ["kind", "k3d"];
export interface ConnectInput {
  provider: ClusterProvider;
  cluster: string;
  namespace: string;
}
/** The namespace whose target is named without a suffix. */
export const DEFAULT_NAMESPACE = "weave-workers";
export const DEFAULT_INPUT: ConnectInput = {
  provider: "kind",
  cluster: "weave-test",
  namespace: DEFAULT_NAMESPACE,
};
/** How often Clusters looks for the new target while the wizard is open. */
export const CONNECT_POLL_MS = 5000;
/** How long Studio waits for a target it has not seen before it pauses. */
export const CONNECT_PATIENCE_MS = 900_000;

const CLUSTER = /^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/;
const LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

/** Why a cluster name can't be used, or null. */
export function clusterNameProblem(value: string): string | null {
  return CLUSTER.test(value)
    ? null
    : "Use a cluster name of 1 to 32 lowercase letters, digits and hyphens that starts and ends with a letter or digit.";
}

/** Why a workers namespace can't be used, or null; checked in the command line's order. */
export function namespaceProblem(value: string): string | null {
  if (!LABEL.test(value))
    return "Use a namespace of 1 to 63 lowercase letters, digits and hyphens.";
  if (value === "weave-system")
    return "weave-system is used by Weave's runners. Choose another namespace.";
  if (value === "default" || value.startsWith("kube-"))
    return `${value} is used by Kubernetes. Choose another namespace.`;
  return null;
}

/** "kind-weave-test", the kubeconfig context the create command adds. */
export function contextName(input: ConnectInput): string {
  return `${input.provider}-${input.cluster}`;
}

/** "kind-weave-test", or "kind-weave-test.team-a" outside weave-workers. */
export function targetName(input: ConnectInput): string {
  return input.namespace === DEFAULT_NAMESPACE
    ? contextName(input)
    : `${contextName(input)}.${input.namespace}`;
}

export function connectCommand(input: ConnectInput): string {
  return `weave platform cluster connect --provider ${input.provider} --name ${input.cluster} --namespace ${input.namespace}`;
}

export function createCommand(input: ConnectInput): string {
  return `weave platform cluster create --provider ${input.provider} --name ${input.cluster}`;
}

/** A command's words as it may wrap: each flag stays with its value. */
export function commandArguments(command: string): string[] {
  const words = command.split(" ");
  const parts: string[] = [];
  for (let index = 0; index < words.length; index++) {
    const word = words[index];
    const value = words[index + 1];
    if (word.startsWith("--") && value !== undefined && !value.startsWith("--"))
      parts.push(`${word} ${words[++index]}`);
    else parts.push(word);
  }
  return parts;
}

/** What the targets and runners say about the cluster these inputs name. */
export type Detection =
  | { state: "waiting" }
  | { state: "registered"; target: Target }
  | { state: "connected"; target: Target; runner: Runner }
  | { state: "clash"; target: Target };

/**
 * The target `connect` registers for these inputs: Kubernetes, named
 * `targetName(input)`, bounded by the namespace. Connected once its runner is
 * online. A disabled target is one `disconnect` left behind, so Studio waits
 * for `connect` to enable it again.
 */
export function detect(
  targets: readonly Target[],
  runners: readonly Runner[],
  input: ConnectInput,
  now: number,
): Detection {
  const name = targetName(input);
  const named = targets.filter((item) => item.name === name && !item.disabled);
  const match = named.find(
    (item) =>
      item.adapter === "kubernetes" && item.boundary === input.namespace,
  );
  if (!match)
    return named.length
      ? { state: "clash", target: named[0] }
      : { state: "waiting" };
  const runner = targetRunner(runners, match.id);
  return runner && runnerState(runner, now) === "online"
    ? { state: "connected", target: match, runner }
    : { state: "registered", target: match };
}

/** Studio's look for one set of inputs, from the wizard opening or the inputs changing. */
export interface Watch {
  /** The last answer applied for these inputs. */
  detection: Detection;
  /** Whether any answer has been applied yet. */
  answered: boolean;
  /** Whether an answer said the cluster was not connected yet. */
  sawUnconnected: boolean;
  /** When Studio started, or went on, waiting (epoch milliseconds). */
  since: number;
  /** Patience is over: polling waits for Keep waiting. */
  paused: boolean;
}

export function startWatch(now: number): Watch {
  return {
    detection: { state: "waiting" },
    answered: false,
    sawUnconnected: false,
    since: now,
    paused: false,
  };
}

/** The watch after one poll's answer for its inputs. */
export function observe(
  watch: Watch,
  detection: Detection,
  now: number,
): Watch {
  return {
    detection,
    answered: true,
    sawUnconnected: watch.sawUnconnected || detection.state !== "connected",
    since: watch.since,
    paused:
      detection.state === "waiting" && now - watch.since >= CONNECT_PATIENCE_MS,
  };
}

/** Keep waiting: patience starts again from now. */
export function keepWaiting(watch: Watch, now: number): Watch {
  return { ...watch, since: now, paused: false };
}

export type ConnectView =
  | "waiting"
  | "registered"
  | "connected"
  | "already"
  | "clash"
  | "patience";

/** Which status the wizard shows for a watch. */
export function connectView(watch: Watch): ConnectView {
  if (watch.paused) return "patience";
  const state = watch.detection.state;
  if (state === "connected")
    return watch.sawUnconnected ? "connected" : "already";
  return state;
}

/** "Observed 12 s ago", from the target's newest observation. */
export function observedLine(
  observations: readonly Observation[],
  targetId: string,
  now: number,
): string {
  const newest = observations
    .filter((item) => item.target_id === targetId)
    .map((item) => Date.parse(item.observed_at))
    .filter((time) => Number.isFinite(time))
    .sort((a, b) => b - a)[0];
  if (newest === undefined) return "Not observed yet";
  const seconds = Math.max(0, Math.floor((now - newest) / 1000));
  if (seconds < 5) return "Observed just now";
  if (seconds < 60) return `Observed ${seconds} s ago`;
  return `Observed ${relativeTime(new Date(newest), new Date(now))}`;
}
