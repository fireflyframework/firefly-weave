# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Bounded YAML and JSON parsing with deterministic, source-mapped diagnostics."""

import hashlib
import importlib
import json
import math

import pytest
import yaml

from firefly_weave.contracts.limits import Limits


def parser():
    return importlib.import_module("firefly_weave.compiler.parser")


def failure(source, *, format="yaml", limits=None):
    module = parser()
    with pytest.raises(module.ParseFailure) as caught:
        module.parse_source(source, format=format, filename="bad.yaml", limits=limits or Limits())
    assert isinstance(caught.value.diagnostics, tuple)
    assert all(issue.stage == "parse" and issue.severity == "error" for issue in caught.value.diagnostics)
    return caught.value.diagnostics


def test_duplicate_yaml_key_points_to_second_key():
    issue = failure("name: one\nname: two\n")[0]
    assert issue.code == "WV-PARSE-DUPLICATE_KEY"
    assert issue.path == "/name"
    assert (issue.source.line, issue.source.column) == (2, 1)
    assert issue.related[0].source.line == 1


def test_duplicate_json_key_points_to_second_key():
    issue = failure('{"name": 1,\n "name": 2}', format="json")[0]
    assert issue.code == "WV-PARSE-DUPLICATE_KEY"
    assert issue.path == "/name"
    assert (issue.source.line, issue.source.column, issue.source.end_column) == (2, 2, 8)
    assert issue.related[0].source.column == 2


@pytest.mark.parametrize(
    "format,source", [("yaml", 'é: 1\r\nx: ["😀", null]\r\n'), ("json", '{"é": 1,\r\n"x": ["😀", null]}')]
)
def test_source_maps_use_unicode_columns_and_crlf(format, source):
    result = parser().parse_source(source, format=format, filename="input")
    assert result.value == {"é": 1, "x": ["😀", None]}
    assert result.locations["/x/0"].line == 2
    assert result.locations["/x/0"].column == (5 if format == "yaml" else 7)
    assert result.locations["/x/0"].end_column == (8 if format == "yaml" else 10)
    assert result.locations["/x/1"].column == (10 if format == "yaml" else 12)
    assert result.locations[""].file == "input"
    assert result.source_hash == hashlib.sha256(source.encode()).hexdigest()


@pytest.mark.parametrize("format,source", [("yaml", '"a/b": {"~key": [3]}'), ("json", '{"a/b": {"~key": [3]}}')])
def test_pointer_keys_escape_slashes_and_tildes(format, source):
    result = parser().parse_source(source, format=format)
    assert result.locations["/a~1b/~0key/0"].column == (18 if format == "yaml" else 19)
    assert result.pointers == frozenset({"", "/a~1b", "/a~1b/~0key", "/a~1b/~0key/0"})


def test_json_string_escapes_decode_before_pointer_escaping():
    result = parser().parse_source(r'{"a\u002fb": "\ud83d\ude00"}', format="json")
    assert result.value == {"a/b": "😀"}
    assert "/a~1b" in result.locations


@pytest.mark.parametrize("source", ["a: &a [1]\nb: *a", "a: &a [*a]", "a: &a [1]\nb: &b [*a, *a]\nc: [*b, *b]"])
def test_yaml_aliases_and_alias_bombs_are_rejected(source):
    assert failure(source)[0].code == "WV-PARSE-ALIAS"


@pytest.mark.parametrize(
    "source,code",
    [
        ("x: !custom hi", "WV-PARSE-TAG"),
        ("x: !!python/object:builtins.str {}", "WV-PARSE-TAG"),
        ("x: {<<: {a: 1}}", "WV-PARSE-MERGE_KEY"),
        ("1: value", "WV-PARSE-NON_STRING_KEY"),
        ("? [a, b]\n: value", "WV-PARSE-NON_STRING_KEY"),
        ("a: 1\n---\nb: 2", "WV-PARSE-EXTRA_DOCUMENT"),
        ("a: [", "WV-PARSE-SYNTAX"),
    ],
)
def test_yaml_restricted_constructs(source, code):
    assert failure(source)[0].code == code


def test_yaml_plain_scalars_have_json_compatible_resolution():
    result = parser().parse_source(
        'yes: yes\nno: no\non: on\noff: off\ndate: 2026-09-29\n"true": true\n"null": null\n'
        "values: [false, 12, -2.5, 1e2, 01, 0x10, True, NULL]\n",
        format="yaml",
    )
    # Plain JSON-type keys must be quoted in a JSON object.
    assert result.value["values"] == [False, 12, -2.5, 100.0, "01", "0x10", "True", "NULL"]


def test_yaml_boolean_traps_dates_and_quoted_scalars_remain_strings():
    result = parser().parse_source('yes: yes\nno: no\ndate: 2026-09-29\nx: ["true", "12", "null"]', format="yaml")
    assert result.value == {"yes": "yes", "no": "no", "date": "2026-09-29", "x": ["true", "12", "null"]}


@pytest.mark.parametrize("format,source", [("yaml", b"\xef\xbb\xbfx: 1"), ("json", "\ufeff{}")])
def test_utf8_bom_is_rejected(format, source):
    issue = failure(source, format=format)[0]
    assert issue.code == "WV-PARSE-BOM"
    assert (issue.source.line, issue.source.column) == (1, 1)


@pytest.mark.parametrize("format", ["yaml", "json"])
def test_invalid_utf8_is_a_typed_failure(format):
    assert failure(b"\xff", format=format)[0].code == "WV-PARSE-ENCODING"


@pytest.mark.parametrize("source", ["{} {}", '{"x":1,}', '{"x":01}', '{"x":NaN}', '{"x":Infinity}'])
def test_json_invalid_or_trailing_values(source):
    assert failure(source, format="json")[0].code == "WV-PARSE-SYNTAX"


@pytest.mark.parametrize(
    "format,source",
    [
        ("json", r'{"x":"\ud800"}'),
        ("json", '{"x":9007199254740992}'),
        ("json", '{"x":1e400}'),
        ("yaml", "x: 9007199254740992"),
        ("yaml", "x: .nan"),
        ("yaml", "x: -.inf"),
        ("yaml", 'x: "\\uD800"'),
    ],
)
def test_text_json_domain_is_enforced(format, source):
    assert failure(source, format=format)[0].code == "WV-PARSE-VALUE"


@pytest.mark.parametrize("value", [b"bytes", (1,), {1}, object(), math.nan, math.inf, 2**53, "\ud800"])
def test_object_rejects_non_json_values_without_fake_source(value):
    issue = failure({"x": value}, format="object")[0]
    assert issue.code == "WV-PARSE-VALUE"
    assert issue.path == "/x"
    assert issue.source is None


def test_object_detects_cycles_and_accepts_shared_acyclic_values():
    value = {}
    value["self"] = value
    assert failure(value, format="object")[0].code == "WV-PARSE-CYCLE"
    shared = [1]
    result = parser().parse_source({"a": shared, "b": shared}, format="object")
    assert result.value == {"a": [1], "b": [1]}
    assert result.locations == {}
    assert result.pointers == frozenset({"", "/a", "/a/0", "/b", "/b/0"})
    assert len(result.source_hash) == 64


@pytest.mark.parametrize("format,source", [("object", {1: "a"}), ("yaml", "[1]"), ("json", "null")])
def test_root_requires_object_and_object_keys_require_strings(format, source):
    assert failure(source, format=format)[0].code in {"WV-PARSE-ROOT", "WV-PARSE-NON_STRING_KEY"}


@pytest.mark.parametrize(
    "format,source", [("json", '{"x":{"y":{}}}'), ("yaml", "x: {y: {}}"), ("object", {"x": {"y": {}}})]
)
def test_container_depth_boundary(format, source):
    assert failure(source, format=format, limits=Limits(max_depth=2))[0].code == "WV-PARSE-DEPTH_LIMIT"
    assert parser().parse_source(source, format=format, limits=Limits(max_depth=3)).value == {"x": {"y": {}}}


@pytest.mark.parametrize("format,source", [("json", '{"x":[1,2]}'), ("yaml", "x: [1,2]"), ("object", {"x": [1, 2]})])
def test_node_count_boundary(format, source):
    assert failure(source, format=format, limits=Limits(max_document_nodes=3))[0].code == "WV-PARSE-NODE_LIMIT"
    assert parser().parse_source(source, format=format, limits=Limits(max_document_nodes=4)).value == {"x": [1, 2]}


@pytest.mark.parametrize("format,source", [("json", '{"é":1}'), ("yaml", "é: 1"), ("object", {"é": 1})])
def test_byte_limit_boundary(format, source):
    size = 8 if format in {"json", "object"} else 5
    assert parser().parse_source(source, format=format, limits=Limits(max_source_bytes=size)).value == {"é": 1}
    assert failure(source, format=format, limits=Limits(max_source_bytes=size - 1))[0].code == "WV-PARSE-SOURCE_LIMIT"


def test_byte_limit_checked_before_utf8_decode():
    assert failure(b"\xff\xff", format="json", limits=Limits(max_source_bytes=1))[0].code == "WV-PARSE-SOURCE_LIMIT"


@pytest.mark.parametrize(
    "format,source", [("yaml", "x: 1\nx: 2\ny: 1\ny: 2\nz: 1\nz: 2"), ("json", '{"x":1,"x":2,"y":1,"y":2,"z":1,"z":2}')]
)
def test_diagnostics_are_truncated_deterministically(format, source):
    issues = failure(source, format=format, limits=Limits(max_diagnostics=2))
    assert len(issues) == 2
    assert [issue.path for issue in issues] == ["/x", "/y"]


def test_invalid_fixture_duplicate_yaml(fixture_dir):
    source = (fixture_dir / "definitions/invalid/duplicate-key.yaml").read_bytes()
    assert failure(source)[0].source.line == 2


@pytest.mark.parametrize(
    "format,source",
    [("json", '{"x":' + "[" * 1500 + "0" + "]" * 1500 + "}"), ("yaml", "x: " + "[" * 1500 + "0" + "]" * 1500)],
)
def test_extreme_nesting_is_rejected_before_python_recursion(format, source):
    assert failure(source, format=format)[0].code == "WV-PARSE-DEPTH_LIMIT"


@pytest.mark.parametrize(
    "source", ["x: !!map scalar", "!!map key: value", "x: !!bool yes", "x: !!int 0x10", "x: !!seq scalar"]
)
def test_explicit_yaml_tags_cannot_override_json_shape(source):
    assert failure(source)[0].code in {"WV-PARSE-TAG", "WV-PARSE-VALUE"}


@pytest.mark.parametrize("source,size", [({"x": "\n"}, 10), ({"x": "😀"}, 12), ({'"': "\\"}, 11)])
def test_object_byte_budget_counts_json_escaping_and_utf8(source, size):
    assert parser().parse_source(source, format="object", limits=Limits(max_source_bytes=size)).value == source
    assert failure(source, format="object", limits=Limits(max_source_bytes=size - 1))[0].code == "WV-PARSE-SOURCE_LIMIT"


def test_object_size_is_bounded_during_prevalidation():
    source = {"large": "x" * 100}
    source["cycle"] = source
    assert failure(source, format="object", limits=Limits(max_source_bytes=20))[0].code == "WV-PARSE-SOURCE_LIMIT"


@pytest.mark.parametrize("format,source", [("json", '{"x":[1,2]}'), ("yaml", "x: [1,2]"), ("object", {"x": [1, 2]})])
def test_parser_does_not_consume_expression_budget(format, source):
    result = parser().parse_source(source, format=format, limits=Limits(max_expression_nodes=1))
    assert result.value == {"x": [1, 2]}


@pytest.mark.parametrize("format", ["object", "json", "yaml"])
def test_valid_thousand_step_workflow_is_within_default_document_budget(format):
    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "bounded-workflow", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "steps": [
                {
                    "id": f"transform-{index}",
                    "kind": "transform",
                    "value": {"literal": {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}},
                }
                for index in range(1000)
            ],
            "output": {"literal": {}},
        },
    }
    definitions = importlib.import_module("firefly_weave.contracts.definitions")
    assert len(definitions.load_definition(workflow).spec.steps) == 1000
    source = workflow if format == "object" else json.dumps(workflow) if format == "json" else yaml.safe_dump(workflow)
    result = parser().parse_source(source, format=format)
    assert result.value == workflow
    assert "/spec/steps/999/value/literal/e" in result.pointers


@pytest.mark.parametrize("line_break", ["\x85", "\u2028", "\u2029"])
def test_yaml_unicode_line_break_value_and_container_spans(line_break):
    result = parser().parse_source(f"x: 1{line_break}y: 2", format="yaml", filename="unicode.yaml")
    assert result.value == {"x": 1, "y": 2}
    span = result.locations["/y"]
    assert (span.line, span.column, span.end_line, span.end_column) == (2, 4, 2, 5)
    assert (result.locations[""].end_line, result.locations[""].end_column) == (2, 5)


@pytest.mark.parametrize("line_break", ["\x85", "\u2028", "\u2029"])
def test_yaml_unicode_line_break_duplicate_key_spans(line_break):
    issue = failure(f"x: 1{line_break}x: 2")[0]
    assert (issue.source.line, issue.source.column, issue.source.end_line, issue.source.end_column) == (2, 1, 2, 2)
    first = issue.related[0].source
    assert (first.line, first.column, first.end_line, first.end_column) == (1, 1, 1, 2)


@pytest.mark.parametrize("line_break", ["\x85", "\u2028", "\u2029"])
def test_json_unicode_separators_in_strings_do_not_change_line_spans(line_break):
    result = parser().parse_source('{"x":"' + line_break + '","y":2}', format="json")
    assert result.value == {"x": line_break, "y": 2}
    span = result.locations["/y"]
    assert (span.line, span.column, span.end_line, span.end_column) == (1, 14, 1, 15)


@pytest.mark.parametrize("line_break", ["\x85", "\u2028", "\u2029"])
def test_json_unicode_separators_are_not_whitespace(line_break):
    issue = failure('{"x":1,' + line_break + '"y":2}', format="json")[0]
    assert issue.code == "WV-PARSE-SYNTAX"
    assert (issue.source.line, issue.source.column) == (1, 8)


@pytest.mark.parametrize("format", ["yaml", "json"])
@pytest.mark.parametrize("duplicates,omitted_count", [(1, 0), (2, 1), (3, 2)])
def test_parse_failure_distinguishes_exact_diagnostic_cap_from_truncation(format, duplicates, omitted_count):
    pairs = [(key, value) for key in "abc"[:duplicates] for value in (1, 2)]
    source = (
        "\n".join(f"{key}: {value}" for key, value in pairs)
        if format == "yaml"
        else ("{" + ",".join(f'"{key}":{value}' for key, value in pairs) + "}")
    )
    module = parser()
    with pytest.raises(module.ParseFailure) as caught:
        module.parse_source(source, format=format, limits=Limits(max_diagnostics=1))
    error = caught.value
    assert len(error.diagnostics) == 1
    assert error.diagnostics[0].path == "/a"
    assert error.omitted_count == omitted_count
    assert error.truncated is (omitted_count > 0)


def test_truncation_counts_terminal_failure_after_diagnostic_cap():
    module = parser()
    with pytest.raises(module.ParseFailure) as caught:
        module.parse_source('{"a":1,"a":2,"b":', format="json", limits=Limits(max_diagnostics=1))
    assert len(caught.value.diagnostics) == 1
    assert caught.value.omitted_count == 1
    assert caught.value.truncated is True
