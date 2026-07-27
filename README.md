# graveyard-shift

Every mature repository has a Dependabot ignore list. It is a graveyard of
pinned dependencies, each with a comment explaining why the upgrade was
blocked at the time somebody gave up on it.

Apache Superset has ten:

```yaml
# `just-handlerbars-helpers` library in plugin-chart-handlebars requires `currencyformatter` to be < 2
- dependency-name: "currencyformatter.js"
  update-types: ["version-update:semver-major"]
# TODO: remove below clause once https://github.com/pmmmwh/react-refresh-webpack-plugin/pull/940 lands onto a future release
- dependency-name: "react-checkbox-tree"
  update-types: ["version-update:semver-major"]
```

Nobody re-reads these. The bot that wrote them cannot re-evaluate them, because
deciding whether a blocker still holds means reading upstream release notes,
checking whether a merged PR ever shipped, and understanding what the
application does with the dependency. So the pins outlive their reasons.

This project turns that list into an event-driven work queue. A scheduled audit
parses the ignore list, and for each pin Devin investigates whether the
documented blocker still holds, classifies it with cited evidence, and where
the answer is actionable, upgrades the dependency, adapts the code, runs the
affected tests, and opens a pull request. When CI fails, the failure logs go
back into the same session and Devin repairs its own work. Everything runs
under explicit concurrency, retry, and repository limits, and nothing is
merged automatically.

## What it produced on Apache Superset

Working against [agrimsingh/superset](https://github.com/agrimsingh/superset),
a fork of `apache/superset`:

Ten pins in the ignore list, every one audited:

| Pin | Verdict | Result |
| --- | --- | --- |
| `currencyformatter.js` | `fixable_here` | [PR #3](https://github.com/agrimsingh/superset/pull/3), green |
| `react-checkbox-tree` | `fixable_here` | [PR #4](https://github.com/agrimsingh/superset/pull/4), green |
| `jest-environment-jsdom` | `stale_pin` | [PR #6](https://github.com/agrimsingh/superset/pull/6), green |
| `react-icons` | `stale_pin` | [PR #12](https://github.com/agrimsingh/superset/pull/12), green |
| `@swc/plugin-transform-imports` | `stale_pin` | [PR #15](https://github.com/agrimsingh/superset/pull/15), green |
| `react` | `fixable_here` | [PR #14](https://github.com/agrimsingh/superset/pull/14), a 6,812 line React 18→19 migration, escalated after its repair round |
| `@types/react` | `blocked_upstream` | parked, watching [apache/superset#42112](https://github.com/apache/superset/pull/42112) |
| `@types/react-dom` | `blocked_upstream` | parked behind the same migration |
| `react-dom` | escalated | duplicate of the React 19 work already in flight |
| `@babel/*` | `blocked_upstream` | parked behind the Babel 8 ecosystem |

Three verdicts, and the interesting thing is that they are interesting in
different ways.

**`stale_pin` is the cheapest win and the most common.** Three of the ten
entries were protecting nothing at all. The best of them is
`jest-environment-jsdom`, where the ignore comment says JSDOM v30 does not play
well with Jest v30. Devin established that the comment is simply wrong:
`jest-environment-jsdom` 30.x pins jsdom `^26` internally, so bumping it can
never pull jsdom 30, and the repo's separate direct `jsdom` dependency is not
covered by the ignore at all. The entry has been suppressing patch and security
updates to guard against something it cannot cause. That is a one-line diff
nobody would ever prioritise finding.

**`fixable_here` is where the judgment lives**, and both cases below were
originally pinned behind upstream projects that had not shipped a fix. Devin
got around both without waiting for either.

For `currencyformatter.js` the blocker was a stale peer range in an
unmaintained helper library. Devin verified the 2.x API was unchanged, routed
around the range with an npm override rather than waiting for an upstream that
has published nothing since 2022, removed the ignore entry, and wrote three
regression tests covering USD, a German-locale EUR case, and an unknown
currency code.

`react-checkbox-tree` is the harder one, and worth reading the
[diff](https://github.com/agrimsingh/superset/pull/4/files). Superset had
already tried this upgrade once: it broke the dashboard filter-scope UI with
`Cannot set properties of undefined (setting 'runtime')`, and they reverted it
in [apache/superset#39660](https://github.com/apache/superset/pull/39660) and
pinned it. The ignore comment blames an unreleased fix in
[react-refresh-webpack-plugin#940](https://github.com/pmmmwh/react-refresh-webpack-plugin/pull/940).

Devin confirmed that story, then went past it. It found the real mechanism: the
2.x ESM artifact is itself a webpack bundle that declares a top-level
`__webpack_require__`, which shadows the host compilation's runtime, so the
React Refresh runtime writes `$Refresh$` onto the wrong object. The package's
CommonJS artifact keeps its nested runtime inside a factory scope, so it does
not shadow. The fix is a three-line `resolve.alias` — no upstream release
required:

```js
// The CommonJS artifact keeps its nested runtime inside a factory function
// scope, so no shadowing occurs. Resolve to it until the Refresh plugin ships
// https://github.com/pmmmwh/react-refresh-webpack-plugin/pull/940.
'react-checkbox-tree$': 'react-checkbox-tree/lib/index.cjs',
```

It cited the webpack issue describing the shadowing class of bug, unpacked the
published tarballs to check which artifacts contained what, and adapted
`FilterScopeSelector.tsx` to the 2.x API. A dependency bot cannot produce that.

**`blocked_upstream` is a real answer, not a failure**, provided the pin comes
back on its own. `@types/react` is parked behind
[apache/superset#42112](https://github.com/apache/superset/pull/42112), the
coordinated React 19 upgrade, as a condition the controller re-checks every
tick for nothing:

```json
{"kind": "github_pr_merged", "repo": "apache/superset", "pr_number": 42112}
```

No timer, no re-asking Devin, no ACUs. The pin re-audits itself the hour that
migration lands.

### The prompt was the lever, not the model

Worth being direct about, because it is the most transferable finding here.

The first version of the audit asked Devin one question: is this pin still
blocked? Both pins came back `blocked_upstream` with good evidence — runs 1 and
2 are still in the run feed, and the reasoning was correct. The upstream fixes
genuinely have not shipped. It was a true answer and a useless one, because it
leaves the pins exactly where they were.

The second version asked a better question: is this still blocked, and if so,
is there a bounded route around it inside this repository? Same model, same
repository, same pins. Both flipped to `fixable_here` and both produced green
pull requests.

The agent's judgment is bounded by the question it is handed. The leverage came
from asking for the workaround.

### The agent argued for its own next feature

Three React pins came back `blocked_upstream` and all three asked to watch the
same thing: apache/superset#42112, the coordinated React 19 migration. None of
them could say so. The watch vocabulary only spoke npm versions, so each one
degraded to a prose note and a blind fourteen-day recheck for a condition that
is one API call to evaluate.

They were right and the schema was wrong. The commonest gate in a mature
repository is not a package release, it is a migration that has not landed.
Adding a `github_pr_merged` watch was about thirty lines, and it is worth
noticing where the requirement came from: three independent structured outputs
saying the same thing, which is a signal you only get if you make the agent
answer in a machine-readable shape and then read what it could not express.

### Five pins, one job

Superset's ignore list has five React entries under a single TODO. They are one
migration, and the controller originally audited them as five independent
pins — so `react` opened a 6,812 line upgrade while `react-dom` started a
second session on the same work.

Devin had already worked this out. Its own summary for `react-dom` said the
package "cannot be bumped independently of react/@types/react". The agent knew
the pins were coupled and the orchestrator had no way to represent it, which is
the more general failure: the model's understanding was better than the domain
model it was reporting into.

A Dependabot author writes one comment above the block of entries that move
together, so the comment is the grouping signal, and admission now runs at most
one member of a group at a time. Full group-level remediation — one branch that
clears all five entries — is the obvious next step and is not built.

## How it works

```
scheduled tick
      |
      v
parse .github/dependabot.yml ---> upsert pins (hash each entry)
      |
      v
admission: capacity, allowlist, due date, unblock watch
      |
      v
Devin session: investigate, then classify with required structured output
      |
      +-- fixable_here / stale_pin above confidence --> same session continues
      |         |                                        into remediation
      |         v
      |    pull request --> poll CI --> green
      |                        |
      |                        +-- failure --> logs back into the same session
      |                                          (one retry, then escalate)
      |
      +-- blocked_upstream --> park with evidence and a machine-checkable watch
      |
      +-- low confidence --> escalate to a human with full session context
```

Each pin has one run at a time, and each run is a state machine. Illegal
transitions raise rather than silently corrupting state.

```
classifying -> remediating -> awaiting_ci -> green
                                   |
     any state --------------------+--> escalated | blocked_upstream
```

### Design decisions worth knowing

**One session, not two.** Classification and remediation happen in the same
Devin session. Splitting them would double the cost and throw away the
environment Devin already built, including the cloned repo and installed
dependencies.

**Advance on artifacts, not on session mood.** The controller originally waited
for a session to go idle before reading its classification, on the assumption
that Devin pauses once it has decided. It does not. On the `react-checkbox-tree`
run it emitted its verdict and continued straight into remediation, opening a
pull request while the controller sat waiting for a pause that never came. Every
transition now keys off a durable artifact — structured output, a PR URL, a
check conclusion — and never off how busy the session looks.

**A CI verdict belongs to a commit, not to a pull request.** After feedback is
sent, GitHub keeps reporting the old failure until Devin actually pushes. Read
that as a fresh result and the run consumes its retry budget and escalates
before the repair can exist. Runs record the SHA they ruled on; remediating
waits for a newer commit and `awaiting_ci` ignores a conclusion it has already
acted on. This is the bug that a naive fake hides, which is why the simulation's
CI is keyed to a head SHA that only advances when something is pushed.

**The reconciler is idempotent.** Every tick reads the world and advances each
run by at most one step, so a crash mid-tick loses nothing. Admission consumes
a pin's due date at launch, which is what stops a due pin from starting a fresh
session on every pass. `scripts/verify_convergence.py` covers all six admission
paths.

**Parked pins carry a watch, not a timer.** A blind re-audit schedule burns
budget on pins where nothing changed. Instead Devin states the condition that
would unblock the pin, and the controller evaluates it for zero ACUs:

```json
{"kind": "npm_version",
 "package": "@pmmmwh/react-refresh-webpack-plugin",
 "min_version": "0.6.3"}
```

The controller polls the npm registry each tick and re-audits the moment that
condition flips. A fourteen-day timer remains the fallback for pins with no
machine-checkable condition. A fired watch clears itself, so a permanently true
condition cannot re-trigger forever.

**Guardrails.** A repository allowlist, a per-pin allowlist, a concurrency
ceiling, a per-session ACU cap, one CI retry before escalation, and no
automatic merging. A human always approves the diff.

## Running it

Requires a Devin service user key (`cog_`) and its organization ID, plus a
GitHub token with access to your fork.

```bash
cp .env.example .env      # then fill in the three values
docker compose up --build
```

The dashboard is at http://localhost:8090. It reconciles every 60 seconds, and
`POST /api/tick` forces one immediately. `GET /api/health` reports completed
reconciliation passes, which is the only way to see that the reconciler is alive
once the audit has converged and stopped writing anything.

Prepare a fork before the first run. This installs a focused CI workflow and
disables the roughly thirty-five inherited Superset workflows, so that "green"
means one relevant test suite passed rather than a full release matrix:

```bash
GS_FORK=you/superset ./scripts/setup_fork_ci.sh
```

### Running without any credentials

The full workflow replays against the real controller with the Devin and GitHub
calls faked, so it needs no API key and spends no ACUs:

```bash
make simulate     # or: .venv/bin/python scripts/simulate.py
# or, with nothing installed but Docker:
docker compose run --rm -e DEVIN_API_KEY=x -e DEVIN_ORG_ID=x \
  orchestrator python scripts/simulate.py
```

It exercises four outcomes and asserts each one: a fixable pin that needs a CI
repair round before going green, a pin parked behind an upstream release, a pin
escalated on low confidence, and a config-only fix that no workflow watches.

The fakes are deliberately unkind. CI results are keyed to a head commit that
only advances when Devin pushes, and Devin never idles after answering. Both
model failures that a friendlier fake hid for most of this project.

```bash
make verify       # or: .venv/bin/python scripts/verify_convergence.py
```

Proves admission converges: eight seeded situations, each asserting the exact
number of Devin sessions a repeated tick may start.

## Knowing whether it works

The dashboard answers the question an engineering leader actually asks.

| Metric | Why it matters |
| --- | --- |
| `audits_completed` | How much of the graveyard has been re-examined |
| `actionable_rate` | Share of pins that were not really blocked |
| `green_prs` | Review-ready output |
| `first_pass_ci` | How often Devin gets it right without a repair round |
| `median_trigger_to_pr_s` | Detection to proposed fix |
| `median_trigger_to_green_s` | Detection to passing CI |
| `ci_retries` | Repair rounds consumed |
| `human_escalations` | Where the system knew to stop |

A pin can be audited more than once, when a watch fires or the question
improves. Every metric reports the most recent run per pin, so the dashboard
answers "where does this pin stand now" rather than "how many attempts have
there been". Superseded runs stay in the feed. Every run links to its Devin
session, so any number can be traced to the work that produced it.

Final state of the Superset fork: 10 pins tracked and audited, 5 green PRs, 5/5
first-pass CI on those, median 10m 29s trigger to PR and 14m 49s trigger to
green, 1 CI repair round, 2 escalations, nothing left running.

The escalations are the interesting number. A system that never escalates is
not being careful, it is being lucky.

One is `react-dom`, stopped because the React 19 migration it wanted to perform
was already in flight on another pin's branch. The other is `react` itself, and
its event trail is the whole design in six lines:

```
17:29  run created
17:34  classified fixable_here
18:20  opened PR #14, 6,812 lines
18:42  CI failed, failure logs sent back into the same session
18:46  Devin pushed a repair commit
19:04  still failing, escalated to a human
```

It tried the largest thing in the graveyard, got one repair round, and then
stopped and handed a human the branch plus the full session history. That is
the correct outcome for a React 18 to 19 migration, and it is worth more than a
sixth green PR would have been.

### Honest caveats

**Cost per fix is not reported.** The account used here reports zero ACU
consumption at both the session and organization level, so any cost-per-fix
number would be fabricated. Consumption is still recorded per run and the
figure becomes meaningful on a metered account. Until then wall-clock time to
green is the defensible signal.

**The repair loop was broken until the fake stopped flattering it.** For most
of this project CI passed first time on every PR, so the feedback path never
ran, and the simulation covering it returned success on the second poll
regardless of whether anything had been pushed. Both together hid a controller
that could not repair anything: it re-read the pre-existing failure as a fresh
verdict and escalated within two ticks. Keying the fake's CI to a head SHA
exposed it, and deleting the per-commit guard still makes `simulate.py` fail
today. The loop has since fired for real, on PR #14.

Treat `scripts/simulate.py` as the specification of behaviour that live runs
have not all reached yet, and treat a green test suite as worth exactly as much
as its harshest fake.

**Green means the scoped suite.** Superset's full CI matrix is impractical on a
personal fork, so `scripts/setup_fork_ci.sh` installs a workflow that runs the
tests covering the directories a PR touches. PR #4 ran the 21 filterscope tests;
PR #3 ran the 20 handlebars plugin tests. Neither covers the dev-server HMR
behaviour that originally broke `react-checkbox-tree`, which is exactly why a
human still approves the merge.

**I pushed two empty commits to PR #4.** The first version of the CI workflow
resolved its diff from the wrong working directory, so its pathspec matched
nothing and every PR silently fell back to a smoke suite that did not cover the
changed code. PR #4 was reporting green on tests unrelated to its own diff. I
fixed the workflow and pushed empty commits to re-trigger CI against the real
scope. No source was changed, and `first_pass_ci` counts Devin repair rounds, of
which there were none.

**Two pins, deliberately.** The remaining eight are tracked and classifiable,
but the point was to prove the loop end to end rather than to maximise a funnel.

## Layout

| Path | Role |
| --- | --- |
| `graveyard_shift/controller.py` | The reconciler and its state machine |
| `graveyard_shift/store.py` | SQLite persistence, transitions, metrics |
| `graveyard_shift/devin.py` | Devin v3 organization API client |
| `graveyard_shift/watches.py` | Zero-ACU unblock conditions |
| `graveyard_shift/dependabot.py` | Ignore-list parser that keeps the comments |
| `graveyard_shift/prompts.py` | Session prompts and the structured-output contract |
| `graveyard_shift/web.py` | Dashboard and JSON API |
| `scripts/simulate.py` | Full workflow replay, no credentials needed |
| `scripts/verify_convergence.py` | Regression cover for admission |
| `scripts/setup_fork_ci.sh` | Fork preparation |
| `scripts/service.py` | Start/stop the orchestrator through a PID file |
| `spike/spike.py` | Environment check against a live Devin session |
