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

"""A stored artifact is verified once per process, and only identical content under the same rules reuses it."""

import json
from collections import OrderedDict
from uuid import uuid4

import pytest

from firefly_weave.compiler import api
from firefly_weave.compiler.api import CompiledArtifact, compile_source, import_artifact
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.ir import UnsupportedIR
from firefly_weave.compiler.lowering import ArtifactLimits
from firefly_weave.contracts import language_features


def stored(output=None):
    """A compiled artifact as a run row holds it; each one is new content, so no other test has imported it."""
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "greeting", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "steps": [],
            "output": output or {"literal": uuid4().hex},
        },
    }
    result = compile_source(document, format="object", catalog=CatalogSnapshot.empty())
    assert result.ok, result.diagnostics
    return json.loads(result.artifact.to_bytes())


def read_again(value):
    """The same row read again: equal content in a new object."""
    return json.loads(json.dumps(value))


def verifications(monkeypatch) -> list[str]:
    """From here on, the digest of every artifact verified."""
    digests: list[str] = []
    verify = CompiledArtifact.__post_init__

    def counted(artifact):
        verify(artifact)
        digests.append(artifact.digest)

    monkeypatch.setattr(CompiledArtifact, "__post_init__", counted)
    return digests


def test_an_artifact_read_again_is_verified_once(monkeypatch):
    row = stored()
    verified = verifications(monkeypatch)
    first = import_artifact(row)
    assert import_artifact(read_again(row)) is first
    assert verified == [row["digest"]]


@pytest.mark.parametrize("changed", ["sourceHash", "executable"])
def test_an_artifact_whose_bytes_changed_is_verified_on_its_own(monkeypatch, changed):
    row = stored()
    verified = verifications(monkeypatch)
    first = import_artifact(row)
    other = read_again(row)
    if changed == "sourceHash":
        # Outside the executable's identity: accepted, and the result carries the new value.
        other["sourceHash"] = "f" * len(row["sourceHash"])
        assert import_artifact(other).source_hash == other["sourceHash"] != first.source_hash
    else:
        # The executable no longer matches its digest: refused, never answered with the verified original.
        other["executable"]["timeoutSeconds"] = 30
        with pytest.raises(ValueError):
            import_artifact(other)
    assert import_artifact(read_again(row)) is first
    # Only an accepted artifact is verified to the end; the refused one never reaches it.
    assert verified == ([row["digest"], other["digest"]] if changed == "sourceHash" else [row["digest"]])


def test_an_artifact_under_other_limits_is_verified_again(monkeypatch):
    row = stored()
    verified = verifications(monkeypatch)
    first = import_artifact(row)
    with pytest.raises(ValueError, match="budget exceeded"):
        import_artifact(read_again(row), limits=ArtifactLimits(max_bytes=64))
    assert import_artifact(read_again(row)) is first
    assert verified == [row["digest"]]


@pytest.mark.parametrize("given", ["argument", "advertised"])
def test_an_artifact_under_other_language_features_is_verified_again(monkeypatch, given):
    row = stored({"op": {"name": "concat", "args": [{"literal": uuid4().hex}, {"ref": "/input"}]}})
    verified = verifications(monkeypatch)
    first = import_artifact(row)
    with monkeypatch.context() as patch, pytest.raises(UnsupportedIR):
        if given == "argument":
            import_artifact(read_again(row), features=())
        else:
            # A platform rolled back to a release that lists no language features.
            patch.setattr(language_features, "ADVERTISED_FEATURES", ())
            import_artifact(read_again(row))
    assert import_artifact(read_again(row)) is first
    assert verified == [row["digest"]]


def test_the_most_recently_used_artifacts_are_kept(monkeypatch):
    a, b, c = stored(), stored(), stored()
    monkeypatch.setattr(api, "IMPORTED_ARTIFACTS", 2)
    verified = verifications(monkeypatch)
    import_artifact(a)
    import_artifact(b)
    import_artifact(read_again(a))
    import_artifact(c)
    import_artifact(read_again(a))
    import_artifact(read_again(b))
    assert verified == [a["digest"], b["digest"], c["digest"], b["digest"]]


def test_the_kept_artifacts_stay_within_their_byte_budget(monkeypatch):
    a, b = stored(), stored()
    monkeypatch.setattr(api, "IMPORTED_BYTES", len(canonical_bytes(a)) + len(canonical_bytes(b)) - 1)
    verified = verifications(monkeypatch)
    import_artifact(a)
    kept = import_artifact(b)
    assert import_artifact(read_again(b)) is kept
    import_artifact(read_again(a))
    assert verified == [a["digest"], b["digest"], a["digest"]]


def test_an_artifact_larger_than_the_byte_budget_leaves_the_kept_ones(monkeypatch):
    small, large = stored(), stored({"literal": "x" * 4096 + uuid4().hex})
    monkeypatch.setattr(api, "IMPORTED_BYTES", len(canonical_bytes(small)) + 1)
    verified = verifications(monkeypatch)
    kept = import_artifact(small)
    import_artifact(large)
    assert import_artifact(read_again(small)) is kept
    import_artifact(read_again(large))
    assert verified == [small["digest"], large["digest"], large["digest"]]


class Text(str):
    """A string that is not plain JSON, as a caller might build one."""


def test_a_mapping_subclass_is_refused_even_after_equal_content_was_kept():
    row = stored()
    import_artifact(row)
    with pytest.raises(ValueError, match="root must be an object"):
        import_artifact(OrderedDict(read_again(row)))


def test_a_nested_string_subclass_is_refused_even_after_equal_content_was_kept():
    row = stored()
    import_artifact(row)
    other = read_again(row)
    other["sourceHash"] = Text(other["sourceHash"])
    with pytest.raises(ValueError, match="interoperable JSON domain"):
        import_artifact(other)


def test_the_kept_artifact_matches_the_content_its_key_came_from(monkeypatch):
    x, y = stored(), stored()
    key = api._import_key

    def changed_after_the_key(source, limits, features):
        """Another holder of the same dict changes it once its key is taken."""
        found = key(source, limits, features)
        source.clear()
        source.update(read_again(y))
        return found

    with monkeypatch.context() as patch:
        patch.setattr(api, "_import_key", changed_after_the_key)
        import_artifact(read_again(x))
    assert import_artifact(read_again(x)).digest == x["digest"]


def test_what_one_caller_changes_in_an_artifact_it_read_is_not_seen_by_the_next(monkeypatch):
    row = stored()
    verified = verifications(monkeypatch)
    import_artifact(row).executable["graph"] = None
    assert import_artifact(read_again(row)).executable == row["executable"]
    assert verified == [row["digest"]]
