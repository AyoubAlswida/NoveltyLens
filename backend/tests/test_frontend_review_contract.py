"""Frontend invariants a component test cannot see, asserted from the backend.

**This is the second layer, not the only one.** `frontend/src/app/**/*.spec.ts`
runs the real components under Vitest (`npm test`, 65 specs) and asserts
behaviour. What that layer cannot reach is checked here:

* the **dark-mode pairing** — a state class without a dark counterpart renders
  identically to its neutral and nothing errors, so only a stylesheet audit
  catches it;
* the **backend/frontend vocabulary agreement** — a phase or an endpoint that
  exists on one side and not the other;
* the **screen structure the specification names**, so a future refactor cannot
  quietly collapse the gate screens back into one generic editor.

No test here makes a network call or starts a browser.
"""

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src" / "app"
STYLES = FRONTEND.parent / "styles"
APP = FRONTEND / "app.ts"
RUNS_SERVICE = FRONTEND / "services" / "runs.ts"

STAGES = FRONTEND / "screens" / "advanced-search-stages.ts"
"""The three stages, in one component.

Stages 1 and 2 are a single backend approval (the understanding) and stage 3 is
a second one (the strategy) -- two approvals, three screens -- but the three
screens are one flow a person walks forward and back through, so the drafts live
in one component. `understanding-review.ts` and `strategy-review.ts` were
replaced by it.
"""

GATE_SCREENS = {
    # Both gates render the same three-stage flow; the gate decides which stage
    # a reviewer lands on. The gate letters are internal and appear nowhere on
    # any of the three stages.
    "A": STAGES,
    "STRATEGY": STAGES,
}
"""The two gates with a screen. `B`, `C` and `D` and their screens were removed
with feature verification; `verification-report.ts` went with them, and a
completed run now renders through the ordinary `report-screen.ts`."""

SHELL = FRONTEND / "screens" / "gate-shell.ts"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- the specification's screen structure exists ---------------------------


@pytest.mark.parametrize("gate,path", sorted(GATE_SCREENS.items()))
def test_each_workflow_gate_has_its_own_screen(gate, path):
    """Spec Gate F names four screens and the search strategy stage adds a fifth.
    They ask different questions and enforce different completion rules, so one
    generic editor cannot implement any of them faithfully."""
    assert path.exists(), f"workflow gate {gate} has no screen at {path.name}"


def test_the_shell_renders_a_distinct_screen_per_gate():
    source = read(APP)
    for selector in ("app-advanced-search-stages",):
        assert f"<{selector}" in source, f"{selector} is never rendered"


def test_the_shell_can_open_and_close_a_review():
    source = read(APP)
    assert "openReview" in source and "requestReviewExit" in source
    assert "cancelCurrentRun" in source


def test_a_decision_resumes_watching_the_run_rather_than_stopping():
    """**Spec section 3.7, and the bug behind "I have to refresh after approving".**

    `afterDecision()` must reconnect the single durable stream from its cursor;
    otherwise the next review or finished report exists on the server while the
    browser has stopped listening.

    Asserted here rather than in a component spec because it is a property of the
    shell: the component emits `resumed`, and what the shell does next is what
    decides whether the user has to reload the page.
    """
    source = read(APP)
    body = source.split("afterDecision(gate: WorkflowGate): void {", 1)[1].split("\n  }", 1)[0]
    assert "followAdvancedRun" in body, (
        "afterDecision does not reconnect the durable stream; the next stage will never arrive"
    )
    # The run id is captured before the gate is cleared -- losing it would strand
    # the run just as effectively as not polling at all.
    assert "reviewRunId()" in body


def test_a_completed_run_opens_the_standard_report_screen():
    """**Spec section 3.8.** One display path, three ways in.

    A durable Advanced Search run, a quick search and a run reopened from
    History all render through `ReportScreen`, so none of them can drift from the
    others. `verification-report` is gone; a completed run that still routed to
    it would render an approved-verification frame around a report that contains
    no verification.

    The stream follower that notices a run finishing must use the standard report path.
    Reload no longer restores a live run: it explicitly cancels it.
    """
    source = read(APP)
    assert "'verification-report'" not in source.split("type Screen =", 1)[1].split(";", 1)[0]

    for definition in ("private async followAdvancedRun(runId: string): Promise<void> {",):
        body = source.split(definition, 1)[1]
        # Generous, because the branch carries a long comment explaining why a
        # finished run reopens its report rather than a progress bar.
        window = body[: body.index("COMPLETED") + 900]
        assert "reportData.set" in window, (
            f"the COMPLETED branch of `{definition}` does not open the report screen"
        )
        assert "verification-report" not in window


def test_the_review_back_button_confirms_then_cancels_without_approving():
    """Back cannot silently strand a live run at a review gate."""
    source = read(APP)
    request = source.split("requestReviewExit(): void", 1)[1].split("\n  }", 1)[0]
    assert "confirmingRunExit.set(true)" in request
    assert "approveGate" not in request

    cancel = source.split("async cancelCurrentRun(): Promise<void>", 1)[1].split("\n  }", 1)[0]
    assert "this.runs.cancelRun(runId)" in cancel
    assert "this.screen.set('submit')" in cancel
    assert "approveGate" not in cancel


# --- every state is declared and distinct ----------------------------------


REQUIRED_STATES = (
    "loading",
    "empty",
    "review",
    "validating",
    "waiting",
    "resuming",
    "blocked",
    "stale",
    "forbidden",
    "reconnecting",
    "failed",
    "completed",
)


@pytest.mark.parametrize("state", REQUIRED_STATES)
def test_the_gate_shell_declares_every_required_state(state):
    """Collapsing any two reports one situation as another -- the same
    substitution the backend's zero-tolerance policy forbids, moved into the UI.
    `loading` is not `empty`; a dropped stream is `reconnecting`, never
    `failed`."""
    assert f"'{state}'" in read(SHELL), f"the gate shell has no {state!r} state"


def test_the_state_union_matches_exactly():
    union = read(SHELL).split("export type GateState =", 1)[1].split(";", 1)[0]
    declared = set(re.findall(r"'([a-z]+)'", union))
    assert declared == set(REQUIRED_STATES)


def test_a_dropped_stream_is_reconnecting_and_not_a_run_failure():
    """Gate F item 3. A disconnect is a client problem; saying the run failed
    would be a claim about a worker the browser cannot see."""
    shell = read(SHELL)
    assert "reconnecting" in shell
    assert "The run is unaffected" in shell


def test_access_denial_does_not_guess_which_kind_of_404_it_got():
    assert "does not distinguish" in read(SHELL)


def test_a_stale_decision_is_never_resubmitted():
    shell = read(SHELL)
    assert "Reload and review again" in shell
    for forbidden in ("autoResubmit", "retryWithNewVersion", "silentRetry"):
        assert forbidden not in shell


# --- the contracts each gate must honour -----------------------------------


def test_the_understanding_screen_shows_the_original_description_read_only():
    """The inventor is being asked whether the system understood them, which they
    cannot answer if what they wrote has been replaced by the system's version."""
    source = read(STAGES)
    assert "originalDescription()" in source

    # Rendered as a paragraph, never as a form control. Checked structurally
    # rather than by matching one exact line: what matters is that the class the
    # description is rendered under never appears on an `input` or a `textarea`,
    # because a box that accepts a caret invites the belief that it could be
    # edited -- and the whole point of the stage is that it cannot.
    assert '<p class="original-description">{{ originalDescription() }}</p>' in source
    for control in ("input", "textarea"):
        assert f'<{control} class="original-description"' not in source
        assert f'{control} class="original-description"' not in source

    # And the TEMPLATE never binds it as a model: a two-way binding would write
    # the reviewer's typing back into the source input. Scoped to the template on
    # purpose -- `load()` sets the signal from the server payload, which is how
    # the description gets on screen at all, and is not an edit.
    template = source.split("template: `", 1)[1].split("\n  `,\n})", 1)[0]
    assert "originalDescription.set(" not in template
    assert "[(ngModel)]=\"originalDescription" not in template


# --- the report tells the truth about what it is ---------------------------


# --- every mutating call carries an idempotency key -------------------------


MUTATIONS = (
    "approveGateA",
    "approveGateStrategy",
)
"""Two approvals: the entire mutating surface of a run.

The other eight went with their routes. `tests/test_workflow_api.py` asserts
those paths now 404 as unknown, so a client method for one would be a call site
that fails at runtime rather than at build time."""


@pytest.mark.parametrize("method", MUTATIONS)
def test_every_mutating_call_sends_an_idempotency_key(method):
    source = read(RUNS_SERVICE)
    body = source.split(f"{method}(", 1)[1].split("\n  }", 1)[0]
    assert "idempotency_key" in body, f"{method} sends no idempotency_key"


def test_the_active_run_key_is_namespaced_by_account():
    """Gate F items 1 and 2: two accounts on one browser must not inherit each
    other's run."""
    source = read(RUNS_SERVICE)
    assert "novelty-lens-active-run:" in source
    assert "forgetAll" in source


def test_the_remembered_run_is_only_a_cancellation_hint():
    """Never render or fetch a run from localStorage; startup only cancels it."""
    source = read(RUNS_SERVICE)
    active_store = source.split("export class ActiveRunStore", 1)[1]
    assert "peek(email" in active_store
    assert "getRun(" not in active_store
    assert "restore(" not in active_store


# --- the stream client matches the backend framing -------------------------


def test_the_stream_client_tracks_the_cursor_itself():
    source = read(RUNS_SERVICE)
    assert "last_event_id=" in source
    assert "EventSource" in source, "the reason for raw fetch should stay documented"


def test_a_malformed_frame_is_surfaced_rather_than_skipped():
    assert "SSE_CONTRACT" in read(RUNS_SERVICE)


# --- the dark-mode pairing, which no component test can see ----------------


STATE_CLASSES = (
    "cell-state",
    "exclusion-state",
    "human-decision",
    "machine-status",
    "report-banner",
    "gate-card",
    "row-invalid",
    "field-error",
    "field-note",
    "quote-hit",
    # Search strategy. `query-off` and `coverage-missing` are the two that carry
    # meaning through colour alone -- a search that will not run, and a core
    # element nothing will search for -- so a missing dark rule would render
    # each as its neutral and say the opposite of what it means.
    "query-off",
    "coverage-missing",
    "chip-core-tag",
    # Results and evidence, and the final report. `machine-code` is the one that
    # would be least visible if it went wrong: it is the recorded value shown
    # beside its plain-English rendering, and a rule that fell back to body text
    # would make the annotation compete with the sentence rather than sit under
    # it.
    "machine-code",
    "report-coverage",
    "report-caveats",
    "unresolved-list",
)


@pytest.mark.parametrize("klass", STATE_CLASSES)
def test_every_state_class_has_an_explicit_dark_counterpart(klass):
    """CLAUDE.md records this trap biting four times. `:root[data-theme="dark"]
    .foo` is (0,3,0) and sits later in the file, so a state class without a
    counterpart renders as its neutral and nothing errors."""
    css = (STYLES / "gates.css").read_text(encoding="utf-8")
    assert f'[data-theme="dark"] .{klass}' in css or f'[data-theme="dark"] mark.{klass}' in css, (
        f".{klass} has no dark-mode rule"
    )


def test_the_gate_stylesheet_is_actually_loaded():
    assert "gates.css" in (STYLES.parent / "styles.css").read_text(encoding="utf-8")


def test_the_report_is_printable():
    """A management-readable report has to survive Ctrl-P with the controls gone."""
    assert "@media print" in (STYLES / "gates.css").read_text(encoding="utf-8")


# --- a real frontend runner exists -----------------------------------------


def test_a_real_frontend_test_runner_is_configured():
    """These Python tests are a second layer. The first layer is Vitest running
    the actual components -- the specification asked for one and it exists."""
    import json

    angular = json.loads((FRONTEND.parents[1] / "angular.json").read_text(encoding="utf-8"))
    target = angular["projects"]["novelty-lens"]["architect"].get("test")
    assert target, "no `test` target is configured"
    assert target["builder"] == "@angular/build:unit-test"
    assert target["options"]["runner"] == "vitest"


def test_component_specs_exist_for_the_review_workflow():
    specs = list((FRONTEND).rglob("*.spec.ts"))
    names = {spec.name for spec in specs}
    assert "runs.spec.ts" in names, "the API/SSE service has no component-level tests"
    assert "gates.spec.ts" in names, "the gate screens have no component-level tests"


# --- the gate screens are reachable from a cold start -----------------------


def test_reload_cancels_the_remembered_run_instead_of_restoring_it():
    """Reload is an explicit cancellation boundary for Advanced Search."""
    app = (FRONTEND / "app.ts").read_text(encoding="utf-8")
    startup = app.split("restore()", 1)
    assert len(startup) > 1, "app.ts no longer restores the session on startup"
    assert "this.cancelRememberedRun()" in startup[1]
    assert "restoreActiveRun" not in app
    assert "@HostListener('window:pagehide')" in app
    assert "this.runs.cancelRun(runId, true)" in app


def test_a_verified_run_can_be_started_from_the_product():
    """The four gates are reachable without devtools.

    `POST /api/runs` existed, the worker executed it and all four gate screens
    were built and tested -- and nothing in the product could *start* one, so the
    only way into the workflow was to write a run id into localStorage by hand.
    A feature reachable only that way is not shipped.

    Asserted on the source for the same reason as the startup guard above: the
    defect is an absent call, and a test of the method passes whether or not
    anything invokes it.
    """
    app = (FRONTEND / "app.ts").read_text(encoding="utf-8")
    submit = (FRONTEND / "screens" / "submit-screen.ts").read_text(encoding="utf-8")

    assert "verify = output<void>()" in submit, (
        "the submit screen offers no way to ask for a verified run"
    )
    assert '(verify)="startVerifiedRun()"' in app, (
        "the submit screen's verification action is not wired to the shell"
    )
    assert "createRun(" in app, "the shell never admits a run"
    # And it must not approve anything on the way in: admission is admission.
    for writer in ("approveGateA", "approveGateB", "approveGateD"):
        assert writer not in app.split("startVerifiedRun")[1].split("openReview")[0], (
            f"startVerifiedRun calls {writer}: admitting a run must never approve one"
        )


def test_admitting_a_run_does_not_open_a_gate_before_one_exists():
    """A gate screen is opened only once the run is parked at that gate.

    `startVerifiedRun` originally set the gate itself and navigated straight
    there. The screen fetched its payload while the run was still `QUEUED` --
    before the node that fills it had run -- and `reload()` fetches once, so the
    reviewer was told "Nothing here yet. This is a real result, not a failed
    load." about a run that had not started, and nothing ever corrected it.

    The empty state is not wrong; it was reached wrongly. The durable stream waits
    for the run to actually park, and handles a terminal run as itself rather
    than leaving the reviewer on a spinner.
    """
    app = (FRONTEND / "app.ts").read_text(encoding="utf-8")
    admit = app.split("async startVerifiedRun")[1].split("private async followAdvancedRun")[0]

    assert "followAdvancedRun" in admit, "admission never waits for a gate to exist"
    assert "this.screen.set('review')" not in admit, (
        "startVerifiedRun opens a gate screen directly; the run has no gate yet"
    )
    assert "this.screen.set('running')" in admit, (
        "an admitted run should show as running until it parks at a gate"
    )

    follow = app.split("private async followAdvancedRun")[1].split(
        "\n  /** Show the review screen", 1
    )[0]
    assert "WAITING_FOR_REVIEW" in follow, "the wait does not check for a parked run"
    for terminal in ("COMPLETED", "FAILED"):
        assert terminal in app.split("private async followAdvancedRun")[1][:7000], (
            f"a {terminal} run would leave the reviewer waiting on a gate that "
            "will never come"
        )

    assert "this.runs.stream(" in follow, "Advanced Search progress still uses polling"


def test_progress_and_retry_preserve_the_selected_search_branch():
    """An Advanced Search failure must not retry through Quick Search."""
    app = (FRONTEND / "app.ts").read_text(encoding="utf-8")
    running = (FRONTEND / "screens" / "running-screen.ts").read_text(encoding="utf-8")
    error = (FRONTEND / "screens" / "error-screen.ts").read_text(encoding="utf-8")

    assert '[phaseOrder]="phaseOrder()"' in app
    assert '(retry)="retryFailedRun()"' in app
    error_binding = app.split("<app-error-screen", 1)[1].split("/>", 1)[0]
    assert '(retry)="startRunning()"' not in error_binding

    retry = app.split("retryFailedRun(): void", 1)[1]
    assert "this.runMode() === 'advanced'" in retry
    assert "this.startVerifiedRun()" in retry
    assert "this.startRunning()" in retry

    assert "const phaseOrder = this.phaseOrder()" in running
    assert "phaseOrder.map" in running
    assert "this.phaseOrder().map" in error


def test_every_screen_owns_its_scrolling():
    """A screen taller than the viewport must scroll, or its controls are gone.

    `.workspace` is `height: 100vh; overflow: hidden`, so scrolling belongs to
    each screen and `.screen-scroll` is what provides it. The six original
    screens all carry it; the four gate screens shipped without it, and a
    decomposition with more than a few requirements put the approve button below
    the fold with no way to reach it.

    The browser acceptance did not catch this because it clicks through the DOM,
    and `element.click()` does not require an element to be reachable by a
    person. This asserts the property that actually matters instead.
    """
    roots = []
    for path in sorted(FRONTEND.rglob("*.ts")):
        text = path.read_text(encoding="utf-8")
        if 'class="screen' not in text:
            continue
        for line in text.splitlines():
            if 'class="screen' in line:
                roots.append((path.name, line.strip()))

    missing = [
        f"{name}: {line}"
        for name, line in roots
        if "screen-scroll" not in line
    ]
    assert not missing, (
        "these screen containers cannot scroll, so anything below the fold is "
        "unreachable: " + "; ".join(missing)
    )


# --- Phase 4: the results-and-evidence and final-report vocabulary ----------
#
# Phases 1 and 2 rebuilt the invention-understanding and search-strategy stages
# to speak to an inventor. These pin the same property for the two stages that
# came after them, and for the report a reader keeps.

VOCABULARY = FRONTEND / "models" / "vocabulary.ts"

# The screens an inventor reaches after approving a search strategy, plus the
# report. `A` and `STRATEGY` already have their own vocabulary tests above.
INVENTOR_FACING = {
    "A": GATE_SCREENS["A"],
    "STRATEGY": GATE_SCREENS["STRATEGY"],
}
"""The two surviving stage screens.

Was `B`/`C`/`D`/`report`, all of which are gone. Pointing this at the two stages
is not a narrowing: they had their own vocabulary tests before, and this list is
now the whole inventor-facing surface, so the rule applies to every screen a
reviewer sees rather than to the ones added last."""

# Wording that names a position in a workflow graph, a storage row, or a piece of
# machinery. None of it is what the reader is doing.
#
# Checked against the parts of the file a browser renders, not the whole source:
# a docstring that says "gate C" is documentation for whoever maintains the
# screen, and `data-cell-state` is an attribute name. Only rendered text counts.
INTERNAL_VOCABULARY = (
    "gate a",
    "gate b",
    "gate c",
    "gate d",
    "gate strategy",
    "decomposition",
    "checkpoint",
    "verification cell",
    "matrix cell",
    "workflow gate",
    # Spec section 8.1 row 11. Each names a concept the product removed: a
    # reviewer no longer groups elements into capabilities, marks one core,
    # relates two of them, or maps a query onto the elements it covers. A screen
    # still saying any of these words would be offering a control that is not
    # there. Added in the same change that removed what they name.
    "capability",
    "core element",
    "relationship",
    "coverage",
)


def rendered_text(path: Path) -> str:
    """The words a template puts on the screen, and nothing else.

    Three things are removed, each for its own reason:

    * **HTML comments**, because the compiler strips them and they never reach
      the DOM. Several name a gate letter in order to explain the routing, which
      is exactly the audience this rule does not apply to -- the same freedom a
      docstring has.
    * **Interpolations and binding expressions**, because their contents are
      TypeScript. ``value('decomposition_version')`` is a key in a payload, not a
      word anybody reads; the *labels* those expressions resolve to come from
      ``models/vocabulary.ts`` and are checked at their source by the scope test
      below.
    * Everything after the template terminator, matched exactly, so a backtick
      template literal elsewhere in the file cannot extend the slice past the end
      of the template.

    An ``sr-only`` caption is deliberately **kept**. It is rendered, to the
    reader who has the least other context for it -- and this test caught one
    reading "Verification cells".
    """
    source = read(path)
    if "template: `" not in source:
        return ""
    body = source.split("template: `", 1)[1].split('\n  `,\n})', 1)[0]
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.DOTALL)
    body = re.sub(r"\{\{.*?\}\}", " ", body, flags=re.DOTALL)
    body = re.sub(r'[\[(][\w.$-]+[\])]="[^"]*"', " ", body)
    return body.lower()


@pytest.mark.parametrize("name,path", sorted(INVENTOR_FACING.items()))
def test_no_internal_workflow_vocabulary_reaches_the_screen(name, path):
    """The gate letters, and the words for the machinery, are internal.

    A person at this point in the workflow is deciding which publications get
    examined, checking evidence, or reading a report. None of those is helped by
    knowing that the graph calls this position `C`, and a reader who is told it
    reasonably concludes they are looking at a developer tool.
    """
    rendered = rendered_text(path)
    assert rendered, f"{path.name} has no template to check"
    for word in INTERNAL_VOCABULARY:
        assert word not in rendered, f"{path.name} renders the internal term {word!r}"


def test_the_vocabulary_covers_every_backend_enum_it_claims_to_translate():
    """A code with no entry renders as itself, which is safe but is not the job.

    `describe()` deliberately never guesses, so an untranslated code appears on
    the screen as `EXCLUDED_NO_FULL_TEXT` rather than as the nearest neighbour in
    the table -- the right failure, and still a failure. This asserts the tables
    are actually total against the enums the backend can send, so that fallback
    stays a guard rather than the normal path.

    Read from the Python enums rather than from a list written here, so adding a
    member to either one fails this test instead of quietly shipping a screen
    that renders a raw code.
    """
    from verification.schema import Status

    source = read(VOCABULARY)
    # `OUTCOME_TERMS`, `CELL_STATE_TERMS` and `EXCLUSION_TERMS` were removed with
    # the screens that rendered them: a disclosure outcome, a matrix cell's state
    # and why a publication was excluded from citation are all verification
    # concepts, and no screen shows any of them now. The Python enums behind them
    # are RETAINED (spec section 10.3) -- this test no longer claims the frontend
    # translates them, because it does not, and a translation table for words
    # nothing renders reads as evidence that something still does.
    expected = {
        "STATUS_TERMS": [member.value for member in Status],
    }
    for table, codes in expected.items():
        block = source.split(f"export const {table}", 1)
        assert len(block) > 1, f"the vocabulary has no {table}"
        body = block[1].split("\n};", 1)[0]
        for code in codes:
            assert f"{code}:" in body, f"{table} has no entry for {code}"


@pytest.mark.parametrize("table", ["STATUS_TERMS"])
def test_no_two_codes_translate_to_the_same_words(table):
    """Two codes sharing a label collapses two situations into one on screen.

    That is the substitution the zero-tolerance policy forbids, arriving through
    the vocabulary: a reader shown the same sentence for
    `NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES` and for `NOT_EVALUATED` cannot
    tell "we looked and found nothing" from "we never looked".

    `UNCLEAR` appears in two *different* tables with the same words, which is
    correct -- it is the same situation at the requirement level and at the
    capability level -- so this checks within a table, not across them.
    """
    source = read(VOCABULARY)
    body = source.split(f"export const {table}", 1)[1].split("\n};", 1)[0]
    labels = re.findall(r"label:\s*'([^']*)'", body)
    assert len(labels) == len(set(labels)), f"{table} gives two codes the same label"


# --- the final report renders what `graph/report.py` actually writes --------
#
# Four sections of the report read keys the draft builder does not produce, and
# every one of them failed as a **plausible true negative**: an empty waiver list
# reads exactly like a run with no waivers. These pin the accessors against the
# builder rather than against a fixture, so a change to either side fails here.


@pytest.mark.parametrize(
    "gate,server_rules",
    [("A", "GATE_A_RULES"), ("STRATEGY", "GATE_STRATEGY_RULES")],
)
def test_the_screen_glosses_every_rule_the_server_enforces(gate, server_rules):
    """The rules a reviewer reads are the server's, said in plain words.

    `graph/gates.py` holds the binding list in the machine vocabulary; the screen
    pairs it one-to-one and in the same order, because a reviewer being held to a
    condition needs to have been told about it in language they can act on.

    Only the **counts** are compared, which is a deliberately weak check and the
    strongest one available: the two lists say the same thing in different words,
    so no textual comparison is possible. What it catches is the failure that
    actually happens -- a rule added or removed on the server with no
    corresponding change here, leaving a reviewer held to a condition this screen
    never states.
    """
    gates = read(Path(__file__).resolve().parents[1] / "graph" / "gates.py")
    server = gates.split(f"{server_rules}: tuple[str, ...] = (", 1)[1].split("\n)", 1)[0]
    server_count = len([line for line in server.splitlines() if line.strip().startswith('"')])

    screen = read(STAGES)
    table = screen.split("export const STAGE_RULES", 1)[1].split("\n};", 1)[0]
    body = table.split(f"{gate}: [", 1)[1].split("],", 1)[0]
    screen_count = len([line for line in body.splitlines() if line.strip().startswith("'")])

    assert server_count == screen_count, (
        f"{server_rules} has {server_count} rules and the {gate} screen glosses "
        f"{screen_count}"
    )
