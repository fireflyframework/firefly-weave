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

"""Exercise the gate runner at its command boundary without installing test dependencies."""

import importlib.util
import json
import os
import re
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner(monkeypatch):
    spec = importlib.util.spec_from_file_location("fixture_gate_runner", ROOT / "scripts/check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "release_prerequisites", lambda *args: None)
    return module


def capture(runner, monkeypatch, evidence, *, fail=None):
    calls = []

    def command(argv, **kwargs):
        name = kwargs["log_path"].stem
        calls.append((name, argv, kwargs))
        if name == fail:
            raise RuntimeError("controlled command failure")
        if name == "unit-contracts":
            kwargs["log_path"].write_text("3 passed in 0.01s\n")
        if name == "images":
            for directory, filename, value in (
                (
                    "images",
                    "images.json",
                    {"images": dict.fromkeys(("server", "worker", "teams", "kafka"), "sha256:test")},
                ),
                ("release", "release.json", {"wheel": "test.whl", "wheel_sha256": "test"}),
            ):
                path = evidence / directory
                path.mkdir()
                (path / filename).write_text(json.dumps(value))

    monkeypatch.setattr(runner, "run_command", command)
    # Keep the release runner's owned environment changes private to each test.
    monkeypatch.setattr(runner.os, "environ", dict(runner.os.environ))
    return calls


@pytest.mark.parametrize("release", [False, True])
def test_fixture_prepared_in_exact_interpreter_before_integration(runner, monkeypatch, tmp_path, release):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence)
    options = {} if release else {"integration": True}
    assert runner.run(ROOT, evidence, release=release, context=None, **options) == 0
    names = [name for name, _, _ in calls]
    index = names.index("test-fixtures")
    assert names[index + 1] == "integration"
    assert calls[index][1] == [
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--no-deps",
        str(ROOT / "tests/fixtures/e2-provider"),
    ]
    assert calls[index + 1][1][:3] == [sys.executable, "-m", "pytest"]
    assert calls[index][2]["timeout"] > 0 and calls[index][2]["limit"] > 0
    if release:
        assert names.index("installed-artifacts") < names.index("images") < index
    else:
        assert names == ["test-fixtures", "integration"]


@pytest.mark.parametrize("failed_stage", ["test-fixtures", "integration"])
def test_integration_failure_is_retained_and_stops_following_stages(runner, monkeypatch, tmp_path, failed_stage):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence, fail=failed_stage)
    assert runner.run(ROOT, evidence, release=True, context=None) == 1
    assert calls[-1][0] == failed_stage
    result = json.loads((evidence / "checks.json").read_text())
    assert result["complete"] is False
    assert result["stages"][failed_stage]["status"] == "failed"
    assert result["stages"]["process-e2e"]["status"] == "not_run"
    if failed_stage == "test-fixtures":
        assert result["stages"]["integration"]["status"] == "not_run"


def test_offline_checks_do_not_install_provider_fixture(runner, monkeypatch, tmp_path):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence)
    assert runner.run(ROOT, evidence, release=False, context=None) == 0
    assert all(name not in {"test-fixtures", "integration"} for name, _, _ in calls)


def test_fixture_metadata_matches_product_framework_pin():
    fixture = tomllib.loads((ROOT / "tests/fixtures/e2-provider/pyproject.toml").read_text())
    product = tomllib.loads((ROOT / "pyproject.toml").read_text())
    fixture_pin = next(d for d in fixture["project"]["dependencies"] if d.startswith("pyfly"))
    product_pin = next(d for d in product["project"]["optional-dependencies"]["server"] if d.startswith("pyfly"))
    assert fixture_pin.split(" @ ")[1] == product_pin.split(" @ ")[1]
    assert (
        fixture["project"]["entry-points"]["firefly_weave.connectors"]["e2-inbox-fixture"] == "e2_inbox_fixture:package"
    )


@pytest.mark.parametrize("failed_stage", ["installed-artifacts", "images"])
def test_release_artifact_failures_prevent_fixture_install(runner, monkeypatch, tmp_path, failed_stage):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence, fail=failed_stage)
    assert runner.run(ROOT, evidence, release=True, context=None) == 1
    assert all(name != "test-fixtures" for name, _, _ in calls)


def test_release_prerequisite_failure_prevents_fixture_install(runner, monkeypatch, tmp_path):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence)

    def unavailable(*args):
        raise ValueError("owned services are not configured")

    monkeypatch.setattr(runner, "release_prerequisites", unavailable)
    assert runner.run(ROOT, evidence, release=True, context=None) == 1
    assert not calls
    result = json.loads((evidence / "checks.json").read_text())
    assert result["stages"]["prerequisites"]["status"] == "failed"


def test_conflicting_modes_reject_before_commands_or_evidence(runner, monkeypatch, tmp_path):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence)
    with pytest.raises(ValueError, match="either integration or release"):
        runner.run(ROOT, evidence, release=True, context=None, integration=True)
    assert not calls and not evidence.exists()


@pytest.mark.parametrize("fail_images", [False, True])
def test_native_sql_and_kafka_images_are_exported_only_after_verified_build(runner, monkeypatch, tmp_path, fail_images):
    evidence = tmp_path / "checks"
    capture(runner, monkeypatch, evidence, fail="images" if fail_images else None)
    original = runner.run_command
    observed = []
    for key in ("WEAVE_D1_IMAGE_ID", "WEAVE_D2_IMAGE_ID"):
        runner.os.environ.pop(key, None)

    def command(argv, **kwargs):
        name = kwargs["log_path"].stem
        observed.append((name, runner.os.environ.get("WEAVE_D1_IMAGE_ID"), runner.os.environ.get("WEAVE_D2_IMAGE_ID")))
        original(argv, **kwargs)
        if name == "images":
            (evidence / "images/images.json").write_text(
                json.dumps({"images": {key: "sha256:" + key for key in ("server", "worker", "teams", "kafka")}})
            )

    monkeypatch.setattr(runner, "run_command", command)
    assert runner.run(ROOT, evidence, release=True, context=None) == int(fail_images)
    assert all(
        sql is kafka is None for name, sql, kafka in observed if name in {"prepare", "installed-artifacts", "images"}
    )
    if fail_images:
        assert observed[-1][0] == "images"
    else:
        assert next(row for row in observed if row[0] == "integration") == (
            "integration",
            "sha256:server",
            "sha256:kafka",
        )


@pytest.mark.parametrize("release", [False, True])
def test_worker_gates_keep_independent_locked_python_runtimes(runner, monkeypatch, tmp_path, release):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence)
    assert runner.run(ROOT, evidence, release=release, context=None) == 0
    commands = {name: argv for name, argv, _ in calls}
    for worker, version in (("agentic", "3.13"), ("files", "3.12")):
        for gate in ("tests", "lint", "format", "types"):
            command = commands[f"worker-{worker}-{gate}"]
            assert command[:7] == ["uv", "run", "--locked", "--project", f"workers/{worker}", "--python", version]
            assert f"workers/{worker}/{'tests' if gate == 'tests' else 'src'}" in command
            assert list(commands).index(f"worker-{worker}-{gate}") < list(commands).index("prepare")


def test_every_worker_stage_rebuilds_the_core_it_checks_against(runner):
    # The workers install firefly-weave from its path as a copy, so a worker environment made before a merge keeps
    # the old core until uv rebuilds it; the option belongs to uv run, before the tool and its own arguments.
    stages = runner.worker_stages()
    assert stages
    for name, command, _ in stages:
        assert command[7:9] == ["--reinstall-package", "firefly-weave"], name


def test_worker_failure_stops_packaging_with_private_evidence(runner, monkeypatch, tmp_path):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence, fail="worker-agentic-tests")
    assert runner.run(ROOT, evidence, release=False, context=None) == 1
    result = json.loads((evidence / "checks.json").read_text())
    assert result["stages"]["worker-agentic-tests"]["status"] == "failed"
    assert result["stages"]["prepare"]["status"] == "not_run"
    assert calls[-1][0] == "worker-agentic-tests"


def test_integration_allocates_its_own_private_native_image_proof(runner, monkeypatch, tmp_path):
    evidence = tmp_path / "checks"
    capture(runner, monkeypatch, evidence)
    runner.os.environ.pop("WEAVE_IMAGE_PROOF_PATH", None)
    assert runner.run(ROOT, evidence, release=False, integration=True, context=None) == 0
    proof = runner.os.environ.get("WEAVE_IMAGE_PROOF_PATH")
    assert proof == str(evidence / "native-image.json")


@pytest.mark.parametrize("release", [False, True])
def test_integration_has_bounded_full_suite_budget_and_actionable_failure_output(
    runner, monkeypatch, tmp_path, release
):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence)
    assert runner.run(ROOT, evidence, release=release, integration=not release, context=None) == 0
    _, command, options = next(call for call in calls if call[0] == "integration")
    assert options["timeout"] == 4800
    assert "-v" in command and "--maxfail=1" in command
    assert "--tb=short" in command and "--show-capture=no" in command
    assert "--showlocals" not in command


def test_failed_pytest_stage_names_failing_tests_without_output(runner, monkeypatch, tmp_path, capsys):
    for path in ("tests/unit/test_a.py", "tests/unit/sub-dir/test_b.py", "workers/files/tests/test_c.py", "secret.py"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("")

    def command(argv, **kwargs):
        name = kwargs["log_path"].stem
        kwargs["log_path"].write_bytes(
            b"....F.E\n"
            b"FAILED tests/unit/test_a.py::printed_by_a_test_before_the_summary\n"
            b"E   AssertionError: token=never-print-this-secret\n"
            b"=========================== short test summary info ============================\n"
            b"FAILED tests/unit/test_a.py::test_one[case-1] - AssertionError: token=never-print-this-secret\n"
            b"ERROR tests/unit/sub-dir/test_b.py::TestTwo::test_two - RuntimeError: never-print-this-body\n"
            b"FAILED workers/files/tests/test_c.py::test_three\x1b[31m\n"
            b"FAILED tests/unit/test_a.py::test_four[above 2 - 1] - AssertionError: first line\n"
            # On CI pytest prints a message whole, so each of its lines starts at the margin, like a summary line.
            b"FAILED token=never-print-this-secret\n"
            b"ERROR token=never-print-this-secret::test\n"
            b"FAILED tests/unit/test_a.py token=never-print-this-secret\n"
            b"FAILED tests/unit/test_a.py::token=never-print-this-secret\n"
            b"FAILED tests/unit/never_print_this_secret.py::test_one\n"
            b"FAILED tests/unit::never_print_this_secret\n"
            b"FAILED tests/../secret.py::never_print_this_secret\n"
            b"FAILED secret.py::never_print_this_secret\n"
            b"FAILED tests/" + b"x" * 256 + b".py::never_print_this_secret\n"
            b"ERROR tests/unit/test_a.py - ModuleNotFoundError: never-print-this-body\n"
            b"===================== 2 failed, 1 error in 3.00s ======================\n"
            b"FAILED tests/unit/test_a.py::printed_after_the_summary\n"
            if name == "unit-contracts"
            else b""
        )
        if name == "unit-contracts":
            raise RuntimeError("controlled command failure")

    monkeypatch.setattr(runner, "run_command", command)
    assert runner.run(tmp_path, tmp_path / "checks", release=False, context=None) == 1
    printed = capsys.readouterr().out
    assert [line for line in printed.splitlines() if line.startswith(("FAILED", "ERROR"))] == [
        "FAILED tests/unit/test_a.py::test_one[case-1]",
        "ERROR tests/unit/sub-dir/test_b.py::TestTwo::test_two",
        "FAILED workers/files/tests/test_c.py::test_three",
        # The id ends at the first " - ", which starts the message in every other line.
        "FAILED tests/unit/test_a.py::test_four[above 2",
        # A test file that can't be imported has no test to name.
        "ERROR tests/unit/test_a.py",
    ]
    assert "Stage unit-contracts failed" in printed
    assert "never" not in printed and "AssertionError" not in printed and "\x1b" not in printed


def test_failed_stage_without_pytest_summary_names_only_the_stage(runner, monkeypatch, tmp_path, capsys):
    evidence = tmp_path / "checks"

    def command(argv, **kwargs):
        name = kwargs["log_path"].stem
        kwargs["log_path"].write_bytes(b"src/x.py:1:1: F401 secret-looking-output\n")
        if name == "lint":
            raise RuntimeError("controlled command failure")

    monkeypatch.setattr(runner, "run_command", command)
    assert runner.run(ROOT, evidence, release=False, context=None) == 1
    printed = capsys.readouterr().out
    assert "Stage lint failed" in printed
    assert "secret-looking-output" not in printed and "F401" not in printed


@pytest.fixture
def stage(runner, monkeypatch, tmp_path, capsys):
    """Run the unit and contract stage's own pytest command on a small tree; return its status and what it prints."""
    environment = {name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")}
    run_command = runner.run_command

    def run(files, *, addopts="", timeout=60):
        root = tmp_path / "root"
        (root / "tests/contracts").mkdir(parents=True)
        (root / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
        for name, source in files.items():
            (root / "tests/unit").mkdir(exist_ok=True)
            (root / "tests/unit" / name).write_text(textwrap.dedent(source))

        def command(argv, **kwargs):
            if kwargs["log_path"].stem == "unit-contracts":
                # pytest prints whole assertion messages in its summary only when CI is set, as it is on the runners.
                options = {"cwd": root, "env": {**environment, "CI": "true", "PYTEST_ADDOPTS": addopts}}
                run_command(argv, **{**kwargs, "timeout": 60}, **options)

        monkeypatch.setattr(runner, "TEST_TIMEOUT_SECONDS", timeout)
        monkeypatch.setattr(runner, "run_command", command)
        status = runner.run(root, tmp_path / "checks", release=False, context=None)
        return status, capsys.readouterr().out

    return run


def test_a_test_that_never_ends_stops_the_stage_with_its_name_and_stack(stage):
    status, printed = stage(
        {
            "test_hang.py": """
            import asyncio
            import pytest

            @pytest.mark.parametrize("n", range(3))
            async def test_waits_forever(n):
                await asyncio.Event().wait()
            """
        },
        timeout=1,
    )
    assert status == 1
    # Only the first: it ends the run.
    assert "Stage unit-contracts failed.\nFAILED tests/unit/test_hang.py::test_waits_forever[0]\nThread" in printed
    # The coroutine is suspended, so its thread is in the event loop, waiting for input or output.
    assert re.search(
        r'^Thread \(most recent call first\):\n  File ".+selectors\.py", line \d+ in select$', printed, re.M
    )
    assert "_pytest" not in printed


@pytest.mark.parametrize("addopts", ["", "-n 0"], ids=["worker", "serial"])
def test_a_crashed_test_stops_the_stage_with_its_stack(stage, addopts):
    status, printed = stage(
        {"test_crash.py": "import os, signal\n\ndef test_crashes():\n    os.kill(os.getpid(), signal.SIGSEGV)\n"},
        addopts=addopts,
    )
    assert status == 1
    assert re.search(
        r'^Thread \(most recent call first\):\n  File ".+/tests/unit/test_crash\.py", line 4 in test_crashes$',
        printed,
        re.M,
    )
    # A serial pytest dies with the test, so only its stack names it.
    assert ("FAILED tests/unit/test_crash.py::test_crashes\n" in printed) is (addopts == "")


def test_a_hung_serial_run_prints_its_stack_without_the_name_of_generated_code(stage):
    status, printed = stage(
        {
            "test_hang.py": """
            import time

            def test_hangs_in_generated_code():
                source = "def never_print(): time.sleep(60)\\nnever_print()"
                exec(compile(source, "token=never-print-this-secret", "exec"), {"time": time})
            """
        },
        addopts="-n 0",
        timeout=1,
    )
    assert status == 1
    assert re.search(r'^  File ".+/tests/unit/test_hang\.py", line 6 in test_hangs_in_generated_code$', printed, re.M)
    assert "never" not in printed


def test_a_message_that_looks_like_a_stack_or_a_summary_is_not_printed(stage):
    status, printed = stage(
        {
            "test_forged.py": """
            def test_fails():
                raise AssertionError(
                    "first line\\n"
                    "Thread 0x0000000000000abc (most recent call first):\\n"
                    '  File "token=never-print-this-secret", line 1 in never_print\\n'
                    f'  File "{__file__}", line 1 in never_print_this_secret\\n'
                    "FAILED token=never-print-this-secret\\n"
                )
            """
        }
    )
    assert status == 1
    assert "Stage unit-contracts failed.\nFAILED tests/unit/test_forged.py::test_fails\nChecks failed" in printed


def test_a_stage_whose_tests_all_pass_is_passed(stage):
    status, printed = stage(
        {
            "test_pass.py": """
            import warnings
            import pytest

            def test_passes():
                warnings.warn("[gw0] went well")

            @pytest.mark.skip(reason="not here")
            def test_skipped():
                pass

            @pytest.mark.xfail
            def test_expected_failure():
                assert 0
            """
        },
        addopts="-n 2",
    )
    assert status == 0, printed


PASSING_TESTS = "import pytest\n\n@pytest.mark.parametrize('n', range(8))\ndef test_passes(n):\n    pass\n"


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_a_worker_killed_after_collection_fails_the_stage(stage, workers):
    """With two workers or more, pytest-xdist then runs no test at all and exits 0."""
    status, printed = stage(
        {
            "test_pass.py": PASSING_TESTS,
            "conftest.py": """
            import os, signal, time

            def pytest_collection_finish():
                if os.environ.get("PYTEST_XDIST_WORKER") == "gw0":
                    os.kill(os.getpid(), signal.SIGKILL)
                time.sleep(1)
            """,
        },
        addopts=f"-n {workers}",
    )
    assert status == 1
    assert "Stage unit-contracts failed." in printed


def test_a_worker_killed_between_its_tests_and_its_end_fails_the_stage(stage):
    status, printed = stage(
        {
            "test_pass.py": PASSING_TESTS,
            "conftest.py": """
            import os, signal

            def pytest_sessionfinish():
                if os.environ.get("PYTEST_XDIST_WORKER") == "gw0":
                    os.kill(os.getpid(), signal.SIGKILL)
            """,
        }
    )
    assert status == 1
    assert "pytest exited 0, but a worker stopped early or no test passed.\nStage unit-contracts failed." in printed


@pytest.mark.parametrize(
    ("output", "status"),
    [
        ("..\n12 passed in 0.01s\n", 0),
        ("..\n\x1b[32m\x1b[1m12 passed\x1b[0m\x1b[32m in 0.01s\x1b[0m\n", 0),
        ("=== 1 deselected, 12 passed, 2 warnings in 90.00s (0:01:30) ===\n", 0),
        ("no tests ran in 0.01s\n", 1),
        ("2 skipped in 0.01s\n", 1),
        ("2 passed their warm-up\nwarm-up: 2 passed in 1.00s\n", 1),
        ("..[gw3] node down: Not properly terminated\n12 passed in 0.01s\n", 1),
        ("", 1),
    ],
    ids=["passed", "colored", "verbose", "none-ran", "skipped", "no-summary", "worker-stopped", "empty"],
)
def test_the_stage_passes_only_when_tests_passed_and_no_worker_stopped(
    runner, monkeypatch, tmp_path, capsys, output, status
):
    def command(argv, **kwargs):
        if kwargs["log_path"].stem == "unit-contracts":
            kwargs["log_path"].write_text(output)

    monkeypatch.setattr(runner, "run_command", command)
    assert runner.run(ROOT, tmp_path / "checks", release=False, context=None) == status
    result = json.loads((tmp_path / "checks/checks.json").read_text())
    assert result["stages"]["unit-contracts"]["status"] == ("failed" if status else "passed")


STACK_OF_A_HUNG_TEST = (
    "....Timeout (0:10:00)!\n"
    "Thread 0x000000016ccff000 (most recent call first):\n"
    '  File "{root}/execnet/gateway_base.py", line 534 in read\n'
    "\n"
    "Thread 0x000000016ccff001 [token=never-print-this-secret] (most recent call first):\n"
    '  File "{root}/src/helper.py", line 7 in drain\n'
    "\n"
    "Thread 0x0000000204e35300 (most recent call first):\n"
    '  File "<frozen importlib._bootstrap>", line 355 in _find_and_load\n'
    '  File "<frozen token_never_print_this_secret>", line 1 in <module>\n'
    '  File "token=never-print-this-secret", line 1 in <module>\n'
    '  File "{root}/src/never-print-this-secret.py", line 1 in <module>\n'
    '  File "{root}/src", line 1 in never_print_this_secret\n'
    '  File "{root}/src/helper.py", line 1 in token=never-print-this-secret\n'
    '  File "/' + "x" * 280 + '", line 1 in never_print_this_secret\n'
    '  File "{root}/tests/unit/test_a.py", line 18 in <lambda>\n'
    '  File "{root}/_pytest/python.py", line 167 in pytest_pyfunc_call\n'
    '  File "{root}/pluggy/_callers.py", line 121 in _multicall\n'
    "[gw0] node down: Not properly terminated\n"
    '  File "{root}/tests/unit/test_a.py", line 1 in printed_by_a_test\n'
)


@pytest.fixture
def stack_files(tmp_path):
    for name in ("execnet/gateway_base.py", "src/helper.py", "tests/unit/test_a.py", "_pytest/python.py"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("")
    (tmp_path / "pluggy").mkdir()
    (tmp_path / "pluggy/_callers.py").write_text("")
    return STACK_OF_A_HUNG_TEST.replace("{root}", str(tmp_path))


@pytest.mark.parametrize(
    "header",
    [
        "=================================== FAILURES ===================================",
        "==================================== ERRORS ====================================",
        "=============================== warnings summary ===============================",
        "\x1b[31m\x1b[1m= short test summary info =\x1b[0m",
    ],
    ids=["failures", "errors", "warnings", "colored-summary"],
)
def test_dumped_stacks_show_frames_of_existing_files_and_never_output(runner, tmp_path, stack_files, header):
    log = tmp_path / "stage.log"
    # What follows the first header of pytest's report is output: here 1 MB, so the stacks are far from the log's end.
    forged = f'Thread 0x1 (x):\n  File "{tmp_path}/src/helper.py", line 1 in printed_by_a_test\n'
    log.write_text(stack_files + header + "\n" + forged + "E   x\n" * 200_000)
    assert runner.dumped_stacks(log) == [
        "Thread (most recent call first):",
        f'  File "{tmp_path}/src/helper.py", line 7 in drain',
        "Thread (most recent call first):",
        '  File "<frozen importlib._bootstrap>", line 355 in _find_and_load',
        f'  File "{tmp_path}/tests/unit/test_a.py", line 18 in <lambda>',
    ]


@pytest.mark.parametrize("stage_name", ["unit-contracts", "worker-files-tests"])
def test_only_the_unit_and_contract_stage_prints_stacks(runner, monkeypatch, tmp_path, capsys, stack_files, stage_name):
    def command(argv, **kwargs):
        kwargs["log_path"].write_text("3 passed in 0.01s\n")
        if kwargs["log_path"].stem == stage_name:
            kwargs["log_path"].write_text(stack_files)
            raise RuntimeError("controlled command failure")

    monkeypatch.setattr(runner, "run_command", command)
    assert runner.run(tmp_path, tmp_path / "checks", release=False, context=None) == 1
    printed = capsys.readouterr().out
    assert f"Stage {stage_name} failed." in printed
    assert ("helper.py" in printed) is (stage_name == "unit-contracts")
