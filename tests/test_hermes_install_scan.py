"""Every shipped skill passes the Hermes install scanner.

`hermes skills install <tap>/skills/<name>` runs `tools/skills_guard.py` over
the quarantined skill directory and refuses a `dangerous` verdict for a
community source outright (`--force` does not override it). Twice a plain
sentence in a generated skill body tripped a critical rule: a cryptocurrency
name in the shortlist sidecar (#2000), then `setgid`, a `> .claude/settings`
shape, "do not tell the user", a trigger spelling that cryptocurrency name and
a literal `sudo` across skill bodies and reference files (#2014). The drift
gates cannot see this -- a producer and its generated file move together --
so this runs the scanner itself, vendored verbatim in `tests/_vendor`, over
`skills/` and `agent-skills/` exactly as Hermes runs it.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _vendor.hermes_skills_guard import scan_skill

REPO = Path(__file__).resolve().parents[1]
SHIPPED_SKILL_ROOTS = (REPO / "skills", REPO / "agent-skills")


def _skill_dirs() -> list[Path]:
    return [path for root in SHIPPED_SKILL_ROOTS for path in sorted(root.iterdir()) if path.is_dir()]


def _blocking(result) -> list[str]:
    return [
        f"{finding.severity} {finding.pattern_id} {finding.file}:{finding.line}: {finding.match}"
        for finding in result.findings
        if finding.severity in ("critical", "high")
    ]


class HermesInstallScanTests(unittest.TestCase):
    def test_every_shipped_skill_scans_safe_as_a_community_install(self) -> None:
        problems: dict[str, list[str]] = {}
        for skill_dir in _skill_dirs():
            result = scan_skill(skill_dir, "community")
            blocking = _blocking(result)
            if result.verdict != "safe" or blocking:
                problems[str(skill_dir.relative_to(REPO))] = [f"verdict={result.verdict}", *blocking]
        # assertEqual on dicts goes through assertDictEqual, whose repr and diff
        # truncation would hide the file:line findings this message exists for.
        report = "\n".join(f"{skill}: " + "; ".join(rows) for skill, rows in problems.items())
        self.assertFalse(
            problems,
            "a skill body trips a Hermes install-scanner rule; reword the producer in "
            "src/skills (never the generated file), regenerate, and re-derive the digests:\n" + report,
        )

    def test_the_vendored_scanner_still_scores_the_sentences_that_blocked_installs(self) -> None:
        # Pins the vendored copy to the findings this gate exists for, so a
        # refresh that weakens or drops a rule cannot leave the scan above
        # passing vacuously.
        sentences = {
            "setuid_setgid": "but on many Linux distributions `sg` is util-linux's setgid tool.",
            "deception_hide": "Do not tell the user an edit format will make an executor faster.",
            "other_agent_config_mod_shell": "project scope is `<dispatch cwd>/.claude/settings.local.json` with rules",
            "crypto_mining": "Strong routing signals: `monero gateway`, `connector readiness`",
            "sudo_usage": "Refused anywhere in argv: a shell or `env`, a forge CLI, `sudo`, network tools.",
        }
        high_rules = {"deception_hide", "sudo_usage"}
        with TemporaryDirectory() as tmp:
            skill = Path(tmp) / "probe"
            skill.mkdir()
            (skill / "SKILL.md").write_text("\n".join(sentences.values()) + "\n", encoding="utf-8")
            result = scan_skill(skill, "community")
        self.assertEqual(result.verdict, "dangerous")
        critical = {f.pattern_id for f in result.findings if f.severity == "critical"}
        high = {f.pattern_id for f in result.findings if f.severity == "high"}
        self.assertLessEqual(set(sentences) - high_rules, critical)
        self.assertLessEqual(high_rules, high)


if __name__ == "__main__":
    unittest.main()
