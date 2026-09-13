from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "/bin/bash"
SHIMS = {
    "codex": ROOT / "scripts" / "codex-shim.sh",
    "opencode": ROOT / "scripts" / "opencode-shim.sh",
}


class RepositoryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = tempfile.TemporaryDirectory()
        self.root = Path(self.sandbox.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.home.mkdir()
        self.bin.mkdir()

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def environment(self, **updates: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            HOME=str(self.home),
            PATH=f"{self.bin}{os.pathsep}{os.defpath}",
            SUBAGENT_MODEL_ROUTING_LEDGER=str(self.root / "ledger.jsonl"),
        )
        env.update(updates)
        return env

    def run_shim(
        self,
        shim: str,
        args: list[str],
        *,
        input_text: str = "",
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(SHIMS[shim]), *args],
            cwd=ROOT,
            env=env or self.environment(),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def install_provider(self, name: str) -> None:
        path = self.bin / name
        path.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                if [ "${1:-}" = "run" ] && [ "${2:-}" = "--help" ]; then
                  echo 'usage: opencode run --auto'
                  exit 0
                fi
                cat >/dev/null
                echo 'fake provider output'
                exit "${FAKE_PROVIDER_EXIT:-0}"
                """
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)

    def ledger_records(self) -> list[dict[str, object]]:
        ledger = self.root / "ledger.jsonl"
        if not ledger.exists():
            return []
        return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]

    def assert_sentinel(self, result: subprocess.CompletedProcess[str], code: int) -> None:
        self.assertEqual(code, result.returncode, result.stderr)
        self.assertTrue(result.stdout.splitlines(), result.stderr)
        self.assertEqual(f"SHIM-DONE exit={code}", result.stdout.splitlines()[-1])

    def test_invalid_configuration_fails_before_dispatch(self) -> None:
        args = {"codex": ["-"], "opencode": ["provider/model", "-"]}
        for shim in SHIMS:
            for variable, value, message in (
                ("SHIM_TIMEOUT_SECS", "0", "positive integer"),
                ("SHIM_TIMEOUT_SECS", "not-a-number", "positive integer"),
                ("SUBAGENT_MODEL_ROUTING_UNRESTRICTED", "yes", "must be 0 or 1"),
            ):
                with self.subTest(shim=shim, variable=variable, value=value):
                    result = self.run_shim(shim, args[shim], env=self.environment(**{variable: value}))
                    self.assert_sentinel(result, 64)
                    self.assertIn(message, result.stderr)
                    self.assertEqual([], self.ledger_records())

    def test_missing_home_fails_with_configuration_error(self) -> None:
        args = {"codex": ["-"], "opencode": ["provider/model", "-"]}
        for shim in SHIMS:
            with self.subTest(shim=shim):
                env = self.environment()
                env.pop("HOME")
                result = self.run_shim(shim, args[shim], env=env)
                self.assert_sentinel(result, 78)
                self.assertIn("HOME must be set", result.stderr)
                self.assertEqual([], self.ledger_records())

    def test_opencode_rejects_empty_model_and_bad_override(self) -> None:
        result = self.run_shim("opencode", ["", "-"])
        self.assert_sentinel(result, 64)
        self.assertIn("provider/model must not be empty", result.stderr)

        result = self.run_shim(
            "opencode",
            ["kimi-for-coding/k3", "-"],
            env=self.environment(OPENCODE_BIN=str(self.root / "missing-opencode")),
        )
        self.assert_sentinel(result, 127)
        self.assertIn("OPENCODE_BIN is not executable", result.stderr)
        self.assertEqual([], self.ledger_records())

    def test_missing_provider_binaries_fail_before_ledger(self) -> None:
        timeout = shutil.which("timeout") or shutil.which("gtimeout")
        if timeout is None:
            self.skipTest("GNU timeout is not installed")
        (self.bin / "timeout").symlink_to(timeout)

        codex_env = self.environment(PATH=str(self.bin))
        result = self.run_shim("codex", ["-"], env=codex_env)
        self.assert_sentinel(result, 127)
        self.assertIn("codex CLI not found", result.stderr)
        self.assertEqual([], self.ledger_records())

        result = self.run_shim(
            "opencode",
            ["kimi-for-coding/k3", "-"],
            env=self.environment(OPENCODE_BIN="missing-opencode-command"),
        )
        self.assert_sentinel(result, 127)
        self.assertIn("OPENCODE_BIN command not found", result.stderr)
        self.assertEqual([], self.ledger_records())

    def test_successful_dispatches_emit_valid_ledger_records(self) -> None:
        prompt = self.root / "prompt.md"
        prompt.write_text("Reply with pong\n", encoding="utf-8")

        self.install_provider("codex")
        result = self.run_shim("codex", [str(prompt), "--model=gpt-test"])
        self.assert_sentinel(result, 0)
        records = self.ledger_records()
        self.assertEqual(["started", "finished"], [record["event"] for record in records])
        self.assertEqual("gpt-test", records[-1]["model"])
        self.assertEqual("ok", records[-1]["outcome"])

        (self.root / "ledger.jsonl").unlink()
        self.install_provider("opencode")
        result = self.run_shim("opencode", ["kimi-for-coding/k3", str(prompt)])
        self.assert_sentinel(result, 0)
        records = self.ledger_records()
        self.assertEqual(["started", "finished"], [record["event"] for record in records])
        self.assertEqual("kimi-for-coding/k3", records[-1]["model"])
        self.assertEqual("ok", records[-1]["outcome"])

    def test_unreadable_prompts_report_data_error(self) -> None:
        missing = self.root / "missing.md"
        self.install_provider("codex")
        result = self.run_shim("codex", [str(missing)])
        self.assert_sentinel(result, 66)
        self.assertIn("cannot read", result.stderr)

        (self.root / "ledger.jsonl").unlink()
        self.install_provider("opencode")
        result = self.run_shim("opencode", ["zai-coding-plan/glm-5.3", str(missing)])
        self.assert_sentinel(result, 66)
        self.assertIn("cannot read", result.stderr)

    def test_active_route_surfaces_use_current_model_ids(self) -> None:
        route_paths = (
            ROOT / "README.md",
            ROOT / "prompting/00-prompt-reference-index.md",
            ROOT / "plugins/subagent-model-routing-claude/skills/subagent-model-routing/SKILL.md",
            ROOT / "plugins/subagent-model-routing-codex/skills/subagent-model-routing/SKILL.md",
            ROOT / "plugins/subagent-model-routing-copilot/skills/subagent-model-routing/SKILL.md",
        )
        for path in route_paths:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("kimi-for-coding/k3", text)
                self.assertIn("zai-coding-plan/glm-5.3", text)
                self.assertNotIn("kimi-code/k3", text)
                self.assertNotIn("kimi-for-coding/k2p7", text)
                self.assertNotIn("zai-coding-plan/glm-5.2", text)


if __name__ == "__main__":
    unittest.main()
