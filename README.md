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
| `react` | `fixable_here` | [PR #14](https://github.com/agrimsingh/superset/pull/14), escalated after its repair round |
| `@types/react` | `blocked_upstream` | parked, watching [apache/superset#42112](https://github.com/apache/superset/pull/42112) |
| `@types/react-dom` | `blocked_upstream` | parked behind the same migration |
| `react-dom` | escalated locally | its remote session later opened follow-up [PR #17](https://github.com/agrimsingh/superset/pull/17) |
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
pins, so `react` and `react-dom` started separate sessions on the same work.
The current follow-up is
[PR #17](https://github.com/agrimsingh/superset/pull/17): a React 18→19
migration spanning 187 files, with 6,875 additions and 6,571 deletions,
roughly 13,400 changed lines.

Devin had already worked this out. Its own summary for `react-dom` said the
package "cannot be bumped independently of react/@types/react". The agent knew
the pins were coupled and the orchestrator had no way to represent it, which is
the more general failure: the model's understanding was better than the domain
model it was reporting into.

A Dependabot author writes one comment above the block of entries that move
together, so the comment is the grouping signal, and admission now runs at most
one member of a group at a time. Full group-level remediation, where one run
owns the whole comment block and reconciles every member into one branch, is
still not built.

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
run by at most one step. Admission writes a durable claim before launching a
session, which stops concurrent ticks from spending twice on the same pin or
group. Claims deliberately do not expire automatically after a crash; the
operator recovery tradeoff is covered below. `scripts/verify_convergence.py`
covers all six admission paths.

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

Create the local environment first:

```bash
make setup
cp .env.example .env
```

The live controller requires a Devin service user key (`cog_`), its
organization ID, a GitHub token with access to your fork, and a random bearer
token for manual control:

```bash
# Fill these in .env; do not commit it.
DEVIN_API_KEY=cog_...
DEVIN_ORG_ID=org-...
GITHUB_TOKEN=...
GS_CONTROL_TOKEN=...
GS_FORK=you/superset
GS_ALLOWED_FORKS=you/superset

docker compose up --build
```

`GS_FORK` must be present in the comma-separated `GS_ALLOWED_FORKS`; startup
fails before any GitHub or Devin work when it is not. Use `openssl rand -hex
32` to generate the control token.

The dashboard is at http://localhost:8090. It binds loopback by default and
Docker publishes it only on `127.0.0.1`, because the read routes are
unauthenticated and disclose the fork under audit, the pins in flight, and live
Devin session URLs. `GS_BIND=0.0.0.0` exposes it to the network, which is what
the container sets internally so the published loopback port can reach it. It
reconciles every 60 seconds. The manual trigger requires the bearer token:

```bash
make tick    # reads GS_CONTROL_TOKEN from .env

# or, if the token is exported in your shell:
curl -fsS -X POST \
  -H "Authorization: Bearer $GS_CONTROL_TOKEN" \
  http://localhost:8090/api/tick
```

Prefer `make tick`. `.env` is read by this project's Python, not by your shell,
so the `curl` form sends an empty token and gets a 401 unless you exported the
variable yourself.

`GITHUB_TOKEN` is optional locally, where it falls back to `gh auth token`, but
required under Docker, because the image does not ship the `gh` CLI.

`POST /api/tick` returns `503` when `GS_CONTROL_TOKEN` is unset, `401` for a
missing or incorrect bearer token, and `502` if that reconciliation pass
records an upstream failure. `GET /api/health` reports completed passes, the
current pass timestamps, and the most recent reconciliation error, which is
how to distinguish an idle converged controller from a dead one.

Prepare a fork before the first run. This limits Actions to full-SHA-pinned
GitHub-owned actions, installs the focused CI workflow, and disables the
roughly thirty-five inherited Superset workflows, so that "green" means one
relevant test suite passed rather than a full release matrix:

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

Proves admission converges: nine seeded situations, each asserting the exact
number of Devin sessions a repeated tick may start. Two of them guard spend
directly, since deleting either the concurrency cap or the shared-justification
guard is otherwise invisible: pins sharing one Dependabot comment must run one
at a time, and a graveyard that all comes due at once must not open a session
per pin.

```bash
make test         # unit and integration tests
make check        # test, verify and simulate together
```

Run `make check` before committing. The three layers catch different things, and
each one has caught a regression the others missed.

## Knowing whether it works

The dashboard answers the question an engineering leader actually asks.

| Metric | Why it matters |
| --- | --- |
| `audits_completed` | How much of the graveyard has been re-examined |
| `actionable_rate` | Share of pins that were not really blocked |
| `green_prs` | Review-ready output |
| `ci_retries` | Repair rounds consumed |
| `human_escalations` | Where the system knew to stop |
| `median_trigger_to_pr_s` | How long until there is something to review |
| `median_trigger_to_green_s` | How long until that something passes its tests |
| `green_without_repair_round` | How often it gets there without a CI failure fed back |
| `admission_claims_in_flight` | Claims currently blocking duplicate admission |
| `oldest_admission_claim_age_s` | How long the oldest claim has blocked work |

A pin can be audited more than once, when a watch fires or the question
improves. Every metric reports the most recent run per pin, so the dashboard
answers "where does this pin stand now" rather than "how many attempts have
there been". Superseded runs stay in the feed. Every run links to its Devin
session, so any number can be traced to the work that produced it.

The recorded controller snapshot has 10 pins audited, five scoped-suite-green
pull requests, three pins parked upstream, and two local escalations. Median
10m 29s from trigger to an open pull request, 24m 49s from trigger to green, and
5 of 5 of those greens with no CI failure ever fed back to Devin.

That last number is deliberately not called a first-pass CI rate. `attempts`
counts repair rounds this controller drove, and two of the five greens needed a
human before their tests were genuinely exercised — in both cases because the
CI harness here was wrong, not because Devin's diff was:

- **PR #4** passed on Devin's commit at 08:22:43 while the workflow's scope
  resolver silently fell back to an unrelated smoke suite, so the 21 tests that
  mattered had not run. Two empty commits re-triggered CI once the resolver was
  fixed, and the correctly scoped `filterscope` suite passed at 08:32:35 against
  Devin's unchanged code.
- **PR #6** produced no check run at all, because a `dependabot.yml`-only
  remediation fell outside the workflow's path filter. An empty commit
  re-triggered CI after the filter was widened.

Three of the five — PRs #3, #12 and #15 — went green unattended on Devin's own
commit with no human touch.

PR #4 is also why trigger-to-green measures a run's *last* recorded green rather
than its first: a run that recorded green twice had not finished the first time,
and reporting the earlier claim would have understated that run by ten minutes.

Both intervals start when the tick admitted the pin, not when the run row was
written. The run row only exists once the tracking issue and the Devin session
do, so stamping it on insert would have excluded that setup from every latency
figure — the metric would have been named for the trigger while measuring from
somewhere after it. It is timestamped from the claim instead. The runs in the
snapshot above predate that and are each short by the 1.2–2.3s their remote
setup took, which is under half a percent of either median.

They end honestly too: a terminal state requires a verified remote stop, so
`green` is written only after the Devin session is confirmed exited, and the
interval has a real end rather than the moment this controller stopped paying
attention. These are operational latencies for this fork's scoped suites, not a
benchmark of Devin.

The `react` escalation shows the feedback boundary:

```
17:29  run created
17:34  classified fixable_here
18:20  opened PR #14
18:42  CI failed, failure logs sent back into the same session
18:46  Devin pushed a repair commit
19:04  still failing, escalated to a human
```

Before remote termination was enforced, the controller stopped advancing that
run after one repair round and handed a human the branch plus the full session
history. A separate `react-dom` session was marked escalated locally, continued
remotely, and opened PR #17 later, while the historical dashboard row remained
escalated without its PR.

Terminal transitions now stop that failure mode. Before committing `green` or
`blocked_upstream`, the controller deletes the tracked Devin session and fetches
it again; only an exact remote `status == "exit"` permits the local transition.
A failed delete, failed verification, or non-exited session leaves the run
active so the next tick retries instead of orphaning remote work.

That retry is bounded by `GS_STOP_ATTEMPTS`, because the alternative to
orphaning remote work is stranding local work: a run that can never confirm a
stop would hold its concurrency slot and re-attempt the same failing call on
every tick forever. Exhausting the attempts is itself grounds for escalation.
Escalation is the one terminal state that never waits on a stop, since it is
what every other failure path falls back to; when it cannot confirm the session
exited it says so on the issue, so the human is told a session may still be
running rather than being told nothing.

### Honest caveats

**Admission claims fail closed.** If the process is killed after writing an
`admission_claims` row, that row continues to block the pin and its group; it
does not time out or recover automatically. This trades automatic availability
for protection against launching a duplicate Devin session and paying for the
same work twice. The cost is that a claim silently consumes a concurrency slot,
which at a limit of one looks exactly like a dead trigger, so it is surfaced
three ways: `admission_claims_in_flight` and `oldest_admission_claim_age_s` on
the dashboard, and `make demo-ready` refuses to hand over a machine that holds
any claim. Clearing one is deliberately manual, because the row means a Devin
session may exist with no run tracking it:

```bash
sqlite3 graveyard.sqlite3 'SELECT * FROM admission_claims'
# find the untracked session for that pin at https://app.devin.ai/sessions,
# stop it, then release that one claim by its token:
sqlite3 graveyard.sqlite3 "DELETE FROM admission_claims WHERE token = '<token>'"
```

Release claims one at a time, by token. Clearing the table would also release
claims whose sessions are still unaccounted for, and those pins would then be
launched a second time — which is the duplicate spend the claim exists to
prevent.

**Cost per fix is not reported.** The account used here reports zero ACU
consumption at both the session and organization level, so any cost-per-fix
number would be fabricated. Wall-clock latency is reported instead.

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
scope. No source was changed, which is why a green check without an inspected
scope is not evidence that the changed code was tested.

**Demo cleanup is local and remote.** `make demo-stop` stops only the recorded
local orchestrator process, then inspects any rehearsal Devin session before
removing its local rows. If a remote session is still active, cleanup
terminates it and verifies that it stopped; if inspection or termination
cannot be verified, cleanup refuses to delete the tracking data. This matters
because PR #17 proved that remote work can continue after a local run is
terminal.

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
| `tests/` | Unit and integration tests, run by `make test` |
| `scripts/setup_fork_ci.sh` | Fork preparation |
| `scripts/service.py` | Start/stop the orchestrator through a PID file |
| `scripts/tick.py` | Authenticated manual trigger, reading the token from `.env` |
| `spike/spike.py` | Environment check against a live Devin session |
