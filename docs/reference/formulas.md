<!--
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
-->

# Formulas in Studio

In step details, a field that takes data has a **Fixed | Mapped** switch. A
Mapped value is a formula: a short piece of text such as
`input.amount > 1000 and exists(input.email)`. Studio turns the text into the
workflow's expression and stores only that expression; the text is how Studio
shows it. Formulas compute values from data. They don't run code: there are no
variables, loops or JavaScript.

## Read data

| You write | It reads |
| --- | --- |
| `input.amount` | The run's input field `amount` |
| `input["first name"]` | A field whose name has spaces or other characters |
| `input.items[0]` | The first item of a list |
| `steps.check-customer.output.score` | A field of an earlier step's output |
| `steps["lookup.v2"].output` | A step whose ID contains a dot |
| `item.email`, `index` | Inside **Loop over items**: the current item and its position, from 0 |
| `loops.orders.item.id`, `loops.orders.index` | Inside nested loops: the item of the loop `orders` |
| `ref("/input/amount")` | Any JSON Pointer |

A formula can read only what its field can: the workflow input, and steps
that always run before the step on its path. Suggestions list exactly those.

## Write values

| You write | Value |
| --- | --- |
| `1000`, `2.5`, `-7` | A number |
| `"gold"` or `'gold'` | Text |
| `true`, `false`, `null` | Yes/No, or empty |
| `[1, 2]`, `{ "a": 1 }` | A fixed list or group |
| `[input.id, 1]` | A list built from data |
| `{ customerId: input.id, channel: "email" }` | A group built from data |

## Compare and combine

From the loosest to the tightest binding: `or`, `and`, `not`, comparisons.
`not a == b` means `not (a == b)`. Use parentheses to group.

| Operator | Means |
| --- | --- |
| `==`, `!=` | Equal, not equal. `true` is never equal to `1`. |
| `<`, `<=`, `>`, `>=` | Order two numbers, or two texts (by Unicode code point). |
| `and` (`&&`), `or` (<code>&#124;&#124;</code>), `not` (`!`) | Combine Yes/No values. |

Comparisons can't be chained: write `a < b and b < c`.

## Functions

| Function | Returns |
| --- | --- |
| `exists(input.email)` | Yes when the field is present, even when it's empty |
| `coalesce(a, b, …)` | The first value that is present and not empty |
| `contains(list or text, value)`, `notContains(…)` | Whether the list has the value, or the text has the text |
| `in(value, list)`, `notIn(…)` | Whether the value is in the list |
| `startsWith(text, start)`, `endsWith(text, end)` | Whether the text starts or ends so |
| `concat(a, b, …)` | Text joined from the values (needs a platform with `text.concat`) |
| `join(list, ", ")` | The list's items as text, with a separator (needs `text.join`) |

`concat` and `join` write numbers as JavaScript does (`2.0` is `2`, `0.1` is
`0.1`) and Yes/No values as `true` and `false`.

## Templates

Text fields such as a prompt, a title, a subject or a message are templates on
platforms with `text.concat`: text with formulas between `{{` and `}}`, such as
`Hello {{ input.name }}, you owe {{ input.amount }}`. Write `\{{` for two
braces in the text. A template without `{{ … }}` is plain text, so the field
shows Fixed again. Inside a formula, the same template is written between
backticks: `` `Hello {{ input.name }}` ``.

## Type, check and preview

- Type `=` in an empty Fixed field, or `{{` in a text field, to start a
  formula. A formula field accepts an outer `{{ … }}` pair, as other tools
  write formulas, and drops it.
- Suggestions follow what you type: fields with their types after `input.`,
  earlier steps after `steps.`, functions, and the values of a choice after
  `==`. ↑ and ↓ move, Enter or Tab accepts, Escape closes, Ctrl+Space opens
  them.
- References show as pills. Backspace removes a pill; double-click one to
  choose other data. Drag a field from **Input** into the formula to put its
  path where you drop it.
- Text that isn't a complete formula yet stays with **Not applied** and the
  reason, such as "The formula ends too early. Add a value after `>`." The
  workflow keeps the last formula that applied; Escape brings it back.
- When the field loses focus, the formula is shown in one standard form, for
  example `&&` as `and`.
- Under a Mapped field, `= value` shows what the formula gives with the test
  input of the trigger's **Test event**; while you type, **Result** shows the
  value, its type and where its data comes from. Studio computes previews in
  your browser with the same rules as the platform and sends nothing. A
  formula that reads a value marked secret is never previewed.
- ⤢ (**Edit formula**) opens the formula beside the input's fields and its
  result.

## Messages

| Code | When |
| --- | --- |
| `WV-FORMULA-EOF` | The formula ends before a value. |
| `WV-FORMULA-TOKEN` | A character or word doesn't belong where it is. |
| `WV-FORMULA-NAME` | A name isn't `input`, `steps` or, in a loop, `item`, `index` or `loops`. |
| `WV-FORMULA-FUNCTION` | The function doesn't exist. |
| `WV-FORMULA-LOOP` | `item`, `index` or `loops.<id>` outside that loop. |
| `WV-FORMULA-EXISTS` | `exists` takes one data reference. |
| `WV-FORMULA-ARITY` | A function gets too many or too few values. |
| `WV-FORMULA-CHAIN` | Comparisons are chained. |
| `WV-FORMULA-ASSIGN` | `=` where `==` was meant. |
| `WV-FORMULA-STRING` | Text or a template isn't closed. |
| `WV-FORMULA-NUMBER` | A whole number is beyond ±9,007,199,254,740,991, or a number is too large. |
| `WV-FORMULA-TEMPLATE` | Templates, `concat` or `join` on a platform without them. |
| `WV-FORMULA-SCOPE` (warning) | A step that runs later or on another path. |
| `WV-FORMULA-FIELD` (warning) | A field a closed schema doesn't have. |
| `WV-FORMULA-TYPE` (warning) | Values of the wrong type for an operator. |

Warnings never stop a formula from being saved; the compiler's own messages
show on the same field.
