"""Session prompts and the structured-output contract."""

from . import config

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["fixable_here", "blocked_upstream", "stale_pin"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["summary"],
            },
        },
        "proposed_validation": {"type": "string"},
        "estimated_scope": {"type": "string", "enum": ["small", "medium", "large"]},
        "remediation_summary": {"type": "string"},
    },
    "required": ["classification", "confidence", "evidence"],
}


def classification_prompt(dependency: str, reason: str, issue_number: int) -> str:
    return f"""\
You are auditing a pinned dependency in https://github.com/{config.FORK} \
(tracking issue #{issue_number}).

The Dependabot config ignores upgrades for `{dependency}`. The comment \
justifying the pin says:

> {reason}

Phase 1 (investigate only, change nothing yet):
1. Clone the repo and confirm how `{dependency}` is used.
2. Investigate whether the documented blocker still holds. Check the linked \
issues/PRs, upstream release notes, and the current code.
3. Classify the pin via structured output:
   - `fixable_here`: the blocker is in this repo's code and you can remediate \
it now (upgrade the dependency, adapt the code, tests pass).
   - `blocked_upstream`: an external project must ship first. Cite the \
blocking issue/PR/release in evidence.
   - `stale_pin`: the documented blocker no longer holds; the pin can be \
removed and the upgrade applied.
4. Include confidence (0-1), evidence with URLs, proposed_validation (the \
exact test command that would prove a fix), and estimated_scope.

Stop after providing structured output. You will receive a follow-up message \
if remediation should proceed. Do not commit, push, or open a PR in phase 1.
"""


def remediation_message(dependency: str, issue_number: int, validation: str) -> str:
    return f"""\
Your classification is approved. Proceed with remediation of `{dependency}`:

1. Create a branch, apply the upgrade and all required code adaptations.
2. Remove the corresponding ignore entry from .github/dependabot.yml.
3. Validate with: {validation or "the focused test suite for the affected package"}.
4. Open a PR against {config.DEFAULT_BRANCH} in {config.FORK}. Reference \
issue #{issue_number}. In the PR body, explain every breaking change you \
adapted to and paste the test output.
5. Update structured output with remediation_summary once the PR is open.
"""


def ci_feedback_message(failures: list[dict]) -> str:
    blocks = "\n\n".join(
        f"### {f['name']}\n{f['url']}\n```\n{f['summary']}\n```" for f in failures
    )
    return f"""\
CI failed on your PR. Investigate and push a fix to the same branch. \
Failing checks:

{blocks}

If the failure is unrelated to your change (flaky or pre-existing), say so in \
a PR comment with evidence instead of forcing a fix.
"""
