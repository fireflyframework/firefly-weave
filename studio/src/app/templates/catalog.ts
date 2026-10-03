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
// Starter workflows for the template gallery. The Studio host serves only the
// built application, so the documents live in this lazily imported chunk
// instead of static .yaml assets. Every document must pass authoring
// validation (studio/tests/templates.test.ts checks it through Python).
import type { Kind } from "../model";

export interface WorkflowTemplate {
  /** Stable identifier, also used in test selectors. */
  id: string;
  title: string;
  description: string;
  /** Step kinds the template uses, in document order, for the card summary. */
  kinds: Kind[];
  /** True when an action step still uses the your-action@1.0.0 placeholder. */
  placeholder: boolean;
  /** Plain note for the card, for example a sample action the template needs. */
  note?: string;
  yaml: string;
}

/** Placeholder action every template uses until the author picks a real one. */
export const PLACEHOLDER_ACTION = "your-action@1.0.0";

const apiCall = `apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: call-an-api
  version: 1.0.0
spec:
  # The "api" slot is bound to a connection when the workflow is activated.
  connections:
    api:
      connector: weave-http@2.0.0
  inputSchema:
    type: object
    additionalProperties: false
    required: [recordId]
    properties:
      recordId:
        type: string
        title: Record ID
        minLength: 1
  outputSchema:
    type: object
  steps:
    # Replace your-action@1.0.0 with a published API action.
    - id: call-api
      kind: action
      uses: ${PLACEHOLDER_ACTION}
      connection: api
      with:
        object:
          path:
            object:
              id: {ref: /input/recordId}
  output: {ref: /steps/call-api/output}
`;

const approvalBranch = `apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: approval-then-branch
  version: 1.0.0
spec:
  inputSchema:
    type: object
    additionalProperties: false
    required: [requestId, amount]
    properties:
      requestId:
        type: string
        title: Request ID
        minLength: 1
      amount:
        type: number
        title: Amount
        minimum: 0
  outputSchema:
    type: object
    additionalProperties: false
    required: [approved, note]
    properties:
      approved: {type: boolean}
      note: {type: string}
  steps:
    - id: review
      kind: humanTask
      assignment: approvers
      title: {literal: Review the request}
      context: {ref: /input}
      formSchema:
        type: object
        additionalProperties: false
        required: [note]
        properties:
          note:
            type: string
            title: Note for the requester
      decisions: [approve, reject]
      dueSeconds: 86400
    - id: route
      kind: switch
      cases:
        - when:
            op:
              name: eq
              args:
                - {ref: /steps/review/output/decision}
                - {literal: approve}
          steps:
            - id: approved
              kind: transform
              value:
                object:
                  approved: {literal: true}
                  note: {ref: /steps/review/output/data/note}
          output: {ref: /steps/approved/output}
      default:
        steps:
          - id: rejected
            kind: transform
            value:
              object:
                approved: {literal: false}
                note: {ref: /steps/review/output/data/note}
        output: {ref: /steps/rejected/output}
  output: {ref: /steps/route/output}
`;

const signalTimeout = `apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: wait-for-confirmation
  version: 1.0.0
spec:
  inputSchema:
    type: object
    additionalProperties: false
    required: [orderId]
    properties:
      orderId:
        type: string
        title: Order ID
        minLength: 1
  outputSchema:
    type: object
    additionalProperties: false
    required: [orderId, confirmed]
    properties:
      orderId: {type: string}
      confirmed: {type: boolean}
  steps:
    # If no "order-confirmed" signal arrives within a day, the run ends as timed out.
    - id: confirmation
      kind: signal
      name: order-confirmed
      timeoutSeconds: 86400
      payloadSchema:
        type: object
        additionalProperties: false
        required: [confirmed]
        properties:
          confirmed:
            type: boolean
            title: Confirmed
    - id: result
      kind: transform
      value:
        object:
          orderId: {ref: /input/orderId}
          confirmed: {ref: /steps/confirmation/output/confirmed}
  output: {ref: /steps/result/output}
`;

const parallelMerge = `apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: parallel-calls
  version: 1.0.0
spec:
  inputSchema:
    type: object
    additionalProperties: false
    required: [customerId]
    properties:
      customerId:
        type: string
        title: Customer ID
        minLength: 1
  outputSchema:
    type: object
  steps:
    # Each branch output becomes one key of the parallel step's output.
    - id: lookups
      kind: parallel
      concurrency: 2
      branches:
        profile:
          steps:
            - id: get-profile
              kind: action
              uses: ${PLACEHOLDER_ACTION}
              with:
                object:
                  customerId: {ref: /input/customerId}
          output: {ref: /steps/get-profile/output}
        orders:
          steps:
            - id: get-orders
              kind: action
              uses: ${PLACEHOLDER_ACTION}
              with:
                object:
                  customerId: {ref: /input/customerId}
          output: {ref: /steps/get-orders/output}
    - id: merge
      kind: transform
      value:
        object:
          customerId: {ref: /input/customerId}
          profile: {ref: /steps/lookups/output/profile}
          orders: {ref: /steps/lookups/output/orders}
  output: {ref: /steps/merge/output}
`;

const customerOnboarding = `apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: customer-onboarding
  version: 1.0.0
spec:
  timeoutSeconds: 172800
  inputSchema:
    type: object
    additionalProperties: false
    required: [customerId]
    properties:
      customerId: {type: string, minLength: 1}
  outputSchema:
    type: object
    additionalProperties: false
    required: [accepted]
    properties:
      accepted: {type: boolean}
  steps:
    # Uses the sample check-customer action; pick your own action to replace it.
    - id: check
      kind: action
      uses: onboarding.check-customer@1.0.0
      with:
        object:
          customerId: {ref: /input/customerId}
    - id: approval
      kind: signal
      name: customer-approved
      timeoutSeconds: 86400
      payloadSchema:
        type: object
        additionalProperties: false
        required: [approved]
        properties:
          approved: {type: boolean}
    - id: decision
      kind: transform
      value:
        object:
          accepted:
            op:
              name: and
              args:
                - {ref: /steps/check/output/eligible}
                - {ref: /steps/approval/output/approved}
  output: {ref: /steps/decision/output}
`;

export const workflowTemplates: readonly WorkflowTemplate[] = [
  {
    id: "api-call",
    title: "Call an API",
    description:
      "Send one request to a REST API and return its response. Pick or create an API action to finish it.",
    kinds: ["action"],
    placeholder: true,
    yaml: apiCall,
  },
  {
    id: "approval-branch",
    title: "Approval, then branch",
    description:
      "Ask a person to approve or reject a request, then follow a different path for each answer.",
    kinds: ["humanTask", "switch", "transform", "transform"],
    placeholder: false,
    yaml: approvalBranch,
  },
  {
    id: "signal-timeout",
    title: "Wait for a signal with a time limit",
    description:
      "Pause until another system sends a confirmation. If nothing arrives within a day, the run ends as timed out.",
    kinds: ["signal", "transform"],
    placeholder: false,
    yaml: signalTimeout,
  },
  {
    id: "parallel-merge",
    title: "Two calls at once, merged",
    description:
      "Run two actions side by side and combine both results into one output.",
    kinds: ["parallel", "action", "action", "transform"],
    placeholder: true,
    yaml: parallelMerge,
  },
  {
    id: "customer-onboarding",
    title: "Customer onboarding",
    description:
      "Check a customer, wait for an approval message, then record whether they were accepted.",
    kinds: ["action", "signal", "transform"],
    placeholder: false,
    note: "Uses the sample check-customer action. Publish it first, or swap in your own action.",
    yaml: customerOnboarding,
  },
];
