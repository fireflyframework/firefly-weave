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
// Action steps. A step that calls an action the workflow owns shows that
// action's own form: Call an API (method, URL, query, headers, body), Send
// email, Reply to an email, or a file operation. Any other action shows the
// action to call, its connection and one row per input. Retry and timeout
// are editable for owned actions and read-only for published ones.
import type { Step } from "../../../../model";
import { describeField, fieldsOf } from "../../../../forms/core/resolve";
import {
  EMAIL_CONNECTOR,
  HTTP_CONNECTOR,
  isWriteMethod,
  recipeOf,
  sendsBody,
  sideEffectOf,
  type ConnectorRecipe,
  type HttpMethod,
} from "../../owned/owned-actions";
import {
  isShownField,
  paramFromField,
  paramsFromSchema,
} from "../../params/schema-params";
import type {
  FormSpec,
  KindContext,
  NdvContext,
  ParamSpec,
} from "../../registry";
import { isRecord, schemaOf, specOf } from "../shared";
import { durationParam, wholeMapping } from "./shared";

export const HTTP_METHODS: HttpMethod[] = [
  "GET",
  "POST",
  "PUT",
  "PATCH",
  "DELETE",
  "HEAD",
];
export const DRIVE_CONNECTORS: ReadonlySet<string> = new Set([
  "weave-microsoft-drive@1.0.0",
  "weave-google-drive@1.0.0",
]);

export const connectionParam = (required = true): ParamSpec => ({
  id: "connection",
  path: ["connection"],
  type: "connection",
  label: "Connection",
  required,
});

const httpMethod = (step: Step, ctx: KindContext): string => {
  const recipe = recipeOf(step, ctx);
  return recipe?.kind === "http" ? recipe.method : "GET";
};

export const callApiForm = (): FormSpec => ({
  fields: [
    connectionParam(),
    {
      id: "method",
      path: ["method"],
      scope: "action",
      type: "select",
      label: "Method",
      required: true,
      default: "GET",
      choices: HTTP_METHODS.map((method) => ({
        value: method,
        label: `${method} · ${isWriteMethod(method) ? "Changes data" : "Reads data"}`,
      })),
    },
    {
      id: "url",
      path: ["pathTemplate"],
      scope: "action",
      type: "urlTemplate",
      label: "URL",
      required: true,
      placeholder: "/orders/{{ id }}",
      description:
        "The connection supplies the address and sign-in. Type the path; drag fields to fill parts of it.",
    },
  ],
  options: [
    {
      id: "query",
      path: ["with", "query"],
      type: "keyValue",
      label: "Query parameters",
      mapping: "fixed",
      addLabel: "Add query parameter",
      hint: "Add parameters to the address.",
    },
    {
      id: "headers",
      path: ["with", "headers"],
      type: "keyValue",
      label: "Headers",
      mapping: "fixed",
      addLabel: "Add header",
      hint: "Send extra request headers.",
    },
    {
      id: "body",
      path: ["with", "body"],
      type: "keyValue",
      label: "Body",
      required: true,
      mapping: "fixed",
      addLabel: "Add body field",
      hint: "The JSON fields the request sends.",
      showWhen: (step, ctx) => sendsBody(httpMethod(step, ctx)),
      hiddenReason: () => "Only for POST, PUT and PATCH",
    },
    {
      id: "responseSample",
      path: ["responseSample"],
      scope: "action",
      type: "json",
      label: "Response sample",
      hint: "Shape the output so later steps can map it.",
      description:
        "Paste a sample response. Later steps can map its fields before the first call.",
    },
    {
      id: "statuses",
      path: ["statuses"],
      scope: "action",
      type: "list",
      label: "Accepted status codes",
      default: [200],
      whenRemoved: "default",
      minItems: 1,
      maxItems: 20,
      addLabel: "Add status code",
      hint: "Answers with these codes count as success.",
      item: {
        id: "status",
        path: [],
        type: "number",
        label: "Status code",
        min: 200,
        max: 299,
      },
    },
    {
      id: "description",
      path: ["description"],
      scope: "action",
      type: "text",
      label: "Description",
      maxLength: 1024,
      hint: "Say what the request is for.",
    },
  ],
});

const recipients = (
  id: string,
  label: string,
  extra: Partial<ParamSpec> = {},
): ParamSpec => ({
  id,
  path: ["with", id],
  type: "list",
  label,
  maxItems: 50,
  addLabel: "Add recipient",
  item: {
    id: `${id}.item`,
    path: [],
    type: "text",
    label: "Recipient",
    mapping: "both",
  },
  ...extra,
});
const message: ParamSpec = {
  id: "message",
  path: ["with", "text"],
  type: "multiline",
  label: "Message",
  required: true,
  mapping: "both",
  templateCapable: true,
};
const html: ParamSpec = {
  id: "html",
  path: ["with", "html"],
  type: "multiline",
  label: "HTML version",
  mapping: "both",
  templateCapable: true,
  hint: "Mail apps that show HTML use this version.",
};
const attachments: ParamSpec = {
  id: "attachments",
  path: ["with", "attachments"],
  type: "list",
  label: "Attachments",
  maxItems: 10,
  addLabel: "Add attachment",
  hint: "Attachments carry base64 content, up to about 1 MB each.",
  item: {
    id: "attachment",
    path: [],
    type: "fields",
    label: "Attachment",
    children: () => [
      {
        id: "attachment.filename",
        path: ["filename"],
        type: "text",
        label: "File name",
        required: true,
        mapping: "both",
      },
      {
        id: "attachment.content_type",
        path: ["content_type"],
        type: "text",
        label: "Content type",
        mapping: "both",
        placeholder: "application/pdf",
      },
      {
        id: "attachment.content_base64",
        path: ["content_base64"],
        type: "text",
        label: "Content (base64)",
        required: true,
        mapping: "mapped",
      },
    ],
  },
};

export function emailForm(action: string): FormSpec {
  if (action === "reply")
    return {
      fields: [
        connectionParam(),
        {
          id: "parent_message_id",
          path: ["with", "parent_message_id"],
          type: "text",
          label: "Reply to message",
          required: true,
          mapping: "both",
        },
        {
          id: "conversation_id",
          path: ["with", "conversation_id"],
          type: "text",
          label: "Conversation",
          required: true,
          mapping: "both",
        },
        message,
      ],
      options: [
        {
          id: "reply_all",
          path: ["with", "reply_all"],
          type: "boolean",
          label: "Reply to all",
          mapping: "both",
        },
        html,
        attachments,
      ],
    };
  return {
    fields: [
      connectionParam(),
      recipients("to", "To", { required: true, minItems: 1 }),
      {
        id: "subject",
        path: ["with", "subject"],
        type: "text",
        label: "Subject",
        required: true,
        mapping: "both",
        templateCapable: true,
        maxLength: 998,
      },
      message,
    ],
    options: [
      recipients("cc", "Cc"),
      recipients("bcc", "Bcc"),
      html,
      attachments,
    ],
  };
}

export function fileForm(recipe: ConnectorRecipe): FormSpec {
  const drive = DRIVE_CONNECTORS.has(recipe.connector);
  const where = (label: string, required = true): ParamSpec =>
    drive
      ? {
          id: "itemId",
          path: ["with", "itemId"],
          type: "resource",
          label: label === "Path" ? "Item ID" : label,
          required,
          mapping: "both",
          hint: "Paste the item's ID or its link.",
        }
      : {
          id: "path",
          path: ["with", "path"],
          type: "text",
          label,
          required,
          mapping: "both",
        };
  const destination: ParamSpec = {
    id: "destination",
    path: ["with", "destination"],
    type: "text",
    label: "Destination",
    required: true,
    mapping: "both",
    hint: "Where to put the file.",
  };
  const name: ParamSpec = {
    id: "name",
    path: ["with", "name"],
    type: "text",
    label: "Name",
    required: true,
    mapping: "both",
    maxLength: 255,
  };
  const file: ParamSpec = {
    id: "file",
    path: ["with", "file"],
    type: "fileRef",
    label: "File",
    required: true,
    mapping: "mapped",
    hint: "Drag a file from Input, such as a download's output.",
  };
  const connection = connectionParam();
  switch (recipe.action) {
    case "list":
      return {
        fields: [connection, where("Folder", false)],
        options: [
          {
            id: "limit",
            path: ["with", "limit"],
            type: "number",
            label: "Page size",
            min: 1,
            max: 100,
            default: 100,
            mapping: "both",
          },
          {
            id: "cursor",
            path: ["with", "cursor"],
            type: "text",
            label: "Continue from",
            mapping: "both",
            hint: "The next cursor of an earlier page.",
          },
        ],
      };
    case "write":
      return {
        fields: [connection, file, destination, ...(drive ? [name] : [])],
      };
    case "move":
      return {
        fields: [
          connection,
          where("Path"),
          destination,
          ...(drive ? [name] : []),
        ],
      };
    default:
      return { fields: [connection, where("Path")] };
  }
}

const actionChoices = (ndv: NdvContext) =>
  ndv.catalog
    .actions()
    .then((rows) => rows.map((row) => ({ value: row.uses, label: row.title })));

export function publishedActionForm(step: Step, ctx: KindContext): FormSpec {
  const uses = String(step["uses"] ?? "");
  const spec = specOf(uses ? ctx.actionContract(uses) : null);
  const action: ParamSpec = {
    id: "action",
    path: ["uses"],
    type: "resource",
    label: "Action",
    required: true,
    placeholder: "orders.get@1.0.0",
    hint: "The workflow uses this exact published version.",
    choices: actionChoices,
  };
  const fields: ParamSpec[] = [action];
  const requirement = isRecord(spec["connection"]) ? spec["connection"] : null;
  if (requirement || step["connection"] !== undefined)
    fields.push(connectionParam(requirement?.["required"] !== false));
  const input = schemaOf(spec["inputSchema"]);
  if (!input) return { fields };
  const inputInfo = describeField("input", input, { required: true });
  if (!isShownField(inputInfo)) return { fields };
  const rows = paramsFromSchema(input, ["with"], { idPrefix: "input." });
  if (!fieldsOf(input).length)
    return {
      fields: [
        ...fields,
        {
          ...paramFromField(inputInfo, ["with"], "", 0),
          label: "Input",
        },
      ],
    };
  if (wholeMapping(step["with"]))
    return {
      fields: [
        ...fields,
        {
          id: "input",
          path: ["with"],
          type: "fields",
          label: "Input",
          mapping: "both",
          hint: "The whole input is mapped. Switch to Fixed to fill it field by field.",
          children: () => rows.fields,
        },
      ],
    };
  return { fields: [...fields, ...rows.fields], options: rows.options };
}

export function actionForm(step: Step, ctx: KindContext): FormSpec {
  const recipe = recipeOf(step, ctx);
  if (recipe?.kind === "http") return callApiForm();
  if (recipe?.kind === "connector")
    return recipe.connector === EMAIL_CONNECTOR
      ? emailForm(recipe.action)
      : fileForm(recipe);
  return publishedActionForm(step, ctx);
}

export function actionSettings(step: Step, ctx: KindContext): FormSpec {
  const uses = String(step["uses"] ?? "");
  if (!uses) return { fields: [] };
  const recipe = recipeOf(step, ctx);
  const sideEffect =
    recipe?.kind === "http"
      ? isWriteMethod(recipe.method)
        ? "non_idempotent"
        : "read_only"
      : sideEffectOf(ctx.actionContract(uses));
  const setBy = recipe ? null : `Set by ${uses}.`;
  const retryReason =
    sideEffect === "non_idempotent"
      ? "Changes data, so Weave never retries it automatically."
      : recipe &&
          recipe.kind !== "http" &&
          !["read_only", "idempotent", "idempotency_key"].includes(sideEffect)
        ? "Load the action to check whether it can retry."
        : setBy;
  const seconds = {
    units: ["seconds"] as ("seconds" | "minutes" | "hours" | "days")[],
  };
  return {
    fields: [
      {
        id: "retry",
        path: ["retry"],
        scope: "action",
        type: "fields",
        label: "Retry on fail",
        description:
          "Tries again after temporary failures, such as timeouts or a dropped connection.",
        hint: "Waits double after each try, up to the longest wait.",
        readOnly: () => retryReason,
        children: () => [
          {
            id: "retry.maxAttempts",
            path: ["retry", "maxAttempts"],
            scope: "action",
            type: "number",
            label: "Max tries",
            min: 2,
            max: 10,
            default: 3,
          },
          {
            id: "retry.initialDelaySeconds",
            path: ["retry", "initialDelaySeconds"],
            scope: "action",
            type: "number",
            label: "Wait between tries",
            min: 1,
            default: 1,
            ...seconds,
          },
          {
            id: "retry.maxDelaySeconds",
            path: ["retry", "maxDelaySeconds"],
            scope: "action",
            type: "number",
            label: "Longest wait",
            min: 1,
            default: 30,
            ...seconds,
          },
        ],
      },
      durationParam("timeout", ["timeoutSeconds"], "Timeout", {
        scope: "action",
        required: true,
        ...(recipe?.kind === "http"
          ? { max: 30, default: 30, units: ["seconds" as const] }
          : {}),
        hint:
          recipe?.kind === "http"
            ? "The step fails if the API hasn't answered by then."
            : "The step fails if the action hasn't finished by then.",
        readOnly: () => setBy,
      }),
    ],
  };
}

export const connectorOf = (step: Step, ctx: KindContext): string | null => {
  const recipe = recipeOf(step, ctx);
  if (recipe?.kind === "http") return HTTP_CONNECTOR;
  if (recipe?.kind === "connector") return recipe.connector;
  const requirement = specOf(ctx.actionContract(String(step["uses"] ?? "")))[
    "connection"
  ];
  return isRecord(requirement) && typeof requirement["connector"] === "string"
    ? requirement["connector"]
    : null;
};
