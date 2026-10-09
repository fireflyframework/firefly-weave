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
// What every Clusters section reads and calls, forwarded to the page that
// owns the state and the commands. Sections render; the page decides.
import { Directive, Input } from "@angular/core";
import type { ClustersPage } from "./clusters-page";
import type { ReconcileRequest } from "../../operations/deployment-reconciliation";
import type { PlanRequest } from "../../operations/deployment-plan-builder";
import {
  adapterLabels,
  observationState,
  terminalJobs,
  type Collection,
  type Deployment,
  type Job,
  type Plan,
  type Target,
} from "../../operations/deployment-contracts";

@Directive()
export abstract class ClusterSection {
  @Input({ required: true }) page!: ClustersPage;
  readonly adapterLabels = adapterLabels;
  readonly terminalJobs = terminalJobs;
  readonly observationState = observationState;
  get host() {
    return this.page.host;
  }
  get store() {
    return this.page.store;
  }
  get target() {
    return this.page.target;
  }
  get deployment() {
    return this.page.deployment;
  }
  get plan() {
    return this.page.plan;
  }
  get job() {
    return this.page.job;
  }
  get approval() {
    return this.page.approval;
  }
  get relatedTarget() {
    return this.page.relatedTarget;
  }
  get planning() {
    return this.page.planning;
  }
  set planning(value: boolean) {
    this.page.planning = value;
  }
  get runnerPresent() {
    return this.page.runnerPresent;
  }
  get latestObservation() {
    return this.page.latestObservation;
  }
  get recoverApply() {
    return this.page.recoverApply;
  }
  can(capability: string) {
    return this.page.can(capability);
  }
  fresh(expires: string) {
    return this.page.fresh(expires);
  }
  riskLabel(risk: string) {
    return this.page.riskLabel(risk);
  }
  loadMore(collection: Collection) {
    return this.page.loadMore(collection);
  }
  startRegistration() {
    return this.page.startRegistration();
  }
  editTargetAuthority() {
    return this.page.editTargetAuthority();
  }
  editDeployment() {
    return this.page.editDeployment();
  }
  observe() {
    return this.page.observe();
  }
  readTarget(id: string) {
    return this.page.readTarget(id);
  }
  openTarget(value: Target) {
    return this.page.openTarget(value);
  }
  openDeployment(value: Deployment) {
    return this.page.openDeployment(value);
  }
  openPlan(value: Plan) {
    return this.page.openPlan(value);
  }
  openJob(value: Job) {
    return this.page.openJob(value);
  }
  createPlan(request: PlanRequest) {
    return this.page.createPlan(request);
  }
  cancelDraft() {
    this.page.cancelDraft();
  }
  approve() {
    return this.page.approve();
  }
  apply() {
    return this.page.apply();
  }
  cancelJob() {
    return this.page.cancelJob();
  }
  reconcile(request: ReconcileRequest) {
    return this.page.reconcile(request);
  }
}
