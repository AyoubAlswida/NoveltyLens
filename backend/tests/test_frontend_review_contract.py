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

GATE_SCREENS = {
    # The stage an inventor sees is "Invention understanding"; the gate letter
    # is internal and appears nowhere on the screen.
    "A": FRONTEND / "screens" / "understanding-review.ts",
    # The stage an inventor sees is "Search strategy"; STRATEGY is the internal
    # gate value and, like the letters, appears nowhere on the screen.
    "STRATEGY": FRONTEND / "screens" / "strategy-review.ts",
    "B": FRONTEND / "screens" / "candidate-review.ts",
    "C": FRONTEND / "screens" / "evidence-review.ts",
    "D": FRONTEND / "screens" / "draft-review.ts",
}
SHELL = FRONTEND / "screens" / "gate-shell.ts"
REPORT = FRONTEND / "screens" / "verification-report.ts"


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
    for selector in (
        "app-understanding-review",
        "app-strategy-review",
        "app-candidate-review",
        "app-evidence-review",
        "app-draft-review",
    ):
        assert f"<{selector}" in source, f"{selector} is never rendered"


def test_the_shell_can_open_and_close_a_review():
    source = read(APP)
    assert "openReview" in source and "closeReview" in source


def test_leaving_a_review_does_nothing_to_the_run():
    """A client disconnect never cancels, fails or pauses a run (constraint 10)."""
    assert "has never heard of this browser" in read(APP)


def test_the_final_report_has_its_own_renderer():
    assert REPORT.exists()
    assert "app-verification-report" in read(APP)


# --- every state is declared and distinct ----------------------------------


REQUIRED_STATES = (
    "loading",
    "empty",
    "review",
    "validating",
    "waiting",
    "paused",
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


def test_the_understanding_screen_cannot_produce_a_colliding_requirement_id():
    """The §12.1 rule people get wrong: elements and relationships share one id
    namespace.

    The screen no longer lets anybody type an id -- elements are `E*`, minted
    above the highest already present so deleting one and adding another cannot
    reproduce it, and relationship ids are never authored here at all. That is a
    stronger guarantee than the editable-id screen it replaced, which could only
    warn. `workflow/review.py` still enforces the rule, because a UI that cannot
    produce a collision is not the same as a backend that would refuse one.
    """
    source = read(GATE_SCREENS["A"])
    assert "Math.max(0, ...used) + 1" in source, "element ids are minted from the count again"


def test_the_understanding_screen_never_approves_a_stale_relationship():
    """A relationship describes specific elements. One that survives an element
    edit can name an element that is no longer there -- a requirement the
    verifier would be asked to find among elements that do not exist."""
    source = read(GATE_SCREENS["A"])
    assert "elementsChanged" in source
    # Dropped on edit rather than sent and rejected: the reviewer cannot act on
    # a server-side dangling-reference refusal they did not cause.
    assert "? []" in source


def test_the_understanding_screen_shows_the_original_description_read_only():
    """The inventor is being asked whether the system understood them, which they
    cannot answer if what they wrote has been replaced by the system's version."""
    source = read(GATE_SCREENS["A"])
    assert "originalDescription()" in source
    assert "This is never changed." in source
    # Rendered as a blockquote, never as a form control.
    assert "<blockquote class=\"original-text\">{{ originalDescription() }}</blockquote>" in source


def test_the_strategy_screen_shows_coverage_and_names_it_that_way():
    """The stage exists to make coverage reviewable, so the screen has to show it.

    The label is the owner's wording. A screen that listed queries without saying
    what each one is looking for would make the coverage rule unmeetable by
    anyone who is not reading the backend.
    """
    source = read(GATE_SCREENS["STRATEGY"])
    assert "AI-proposed query coverage" in source
    assert "covered_element_ids" in source
    assert "coverage-summary" in source


def test_the_strategy_screen_blocks_on_an_uncovered_core_element():
    """The one rule this stage cannot be approved around. Enforced in
    `workflow/review.py` too -- that is the binding check and this is the one that
    tells the reviewer before they submit."""
    source = read(GATE_SCREENS["STRATEGY"])
    assert "core elements have no search that will run" in source
    assert "not searched for" in source


def test_the_strategy_screen_sends_disabled_queries_rather_than_dropping_them():
    """A switched-off search is a decision about the search. Filtering it out in
    the client would leave the record unable to tell "deliberately not searched
    for" from "never proposed"."""
    source = read(GATE_SCREENS["STRATEGY"])
    assert "enabled: query.enabled" in source
    assert "deliberately not searched for" in source


def test_the_strategy_screen_shows_the_approved_analysis_read_only():
    """This stage approves queries. Editing the analysis here would change an
    artifact that was already approved at its own version."""
    source = read(GATE_SCREENS["STRATEGY"])
    assert "approvedSummary()" in source
    assert "Not editable here." in source
    assert '<blockquote class="original-text">{{ approvedSummary() }}</blockquote>' in source


def test_the_strategy_screen_computes_coverage_with_a_method_not_a_computed():
    """`covered()` reads a signal AND plain nested objects. A `computed()` tracks
    only signals, so toggling a query off would never invalidate it -- the same
    defect `violations()` had on the understanding screen, where the approve
    control stayed enabled after the summary was cleared."""
    source = read(GATE_SCREENS["STRATEGY"])
    assert "covered(): Set<string>" in source
    assert "readonly covered = computed" not in source


def test_gate_b_keeps_exclusions_in_their_own_table():
    """Case AH: excluded publications are never matrix cells and never greyed
    candidate rows."""
    source = read(GATE_SCREENS["B"])
    assert "exclusion-table" in source
    assert "not evaluated" in source


def test_gate_b_refuses_to_override_a_date_or_full_text_exclusion():
    """Cases AK and AL. Date eligibility is arithmetic, not a judgement, and
    there is no manual eligibility flag."""
    source = read(GATE_SCREENS["B"])
    assert "EXCLUDED_INELIGIBLE" in source
    assert "overridable" in source


def test_gate_c_derives_the_reviewed_outcome_and_offers_no_control_to_type_one():
    """Gate F item 9. §13.4 check 4 rejects a reviewed outcome that contradicts
    its own statuses, so the screen must not let one be typed."""
    source = read(GATE_SCREENS["C"])
    assert "derivedOutcome" in source
    assert "read-only" in source


def test_gate_c_shows_the_locator_and_its_kind():
    """Gate F item 8: a reviewer needs to know whether `paragraph [0031]` is a
    provider id or a reproducible source range."""
    source = read(GATE_SCREENS["C"])
    assert "locator-kind" in source
    assert "locator_source" in source


def test_gate_c_requires_a_reason_and_a_limitation_before_waiving():
    """Gate F item 10, cases AJ/AZ."""
    source = read(GATE_SCREENS["C"])
    assert "waiverReason" in source and "waiverLimitation" in source
    assert "never becomes a verdict" in source


def test_gate_d_will_not_approve_a_report_into_existence():
    """Case AC."""
    assert "cannot be approved into existence" in read(GATE_SCREENS["D"])


# --- the report tells the truth about what it is ---------------------------


def test_the_report_carries_the_non_legal_disclaimer():
    source = read(REPORT)
    assert "not a legal opinion" in source
    assert "not an exhaustive prior-art search" in source
    assert "not determine that anything is novel" in source


def test_the_report_keeps_exclusions_out_of_the_matrix():
    source = read(REPORT)
    assert "report-exclusions" in source
    assert "not evaluated" in source


def test_the_report_shows_each_waiver_with_its_limitation():
    assert "unresolved_limitation" in read(REPORT)


# --- every mutating call carries an idempotency key -------------------------


MUTATIONS = (
    "approveGateA",
    "approveGateStrategy",
    "regenerateStrategy",
    "approveGateB",
    "addCandidate",
    "reviewCell",
    "waiveCell",
    "rerunCell",
    "completeGateC",
    "approveGateD",
)


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


def test_the_remembered_run_is_verified_before_it_is_rendered():
    """Never render a run from localStorage alone: a 404 clears the key."""
    source = read(RUNS_SERVICE)
    assert "getRun" in source
    assert "forget(email)" in source


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


def test_the_shell_reopens_a_parked_run_on_startup():
    """`restoreActiveRun` is called, not merely defined.

    It was fully implemented and **never invoked**, so the app signed in, read a
    remembered run id out of localStorage, and rendered the submit screen anyway.
    The effect was that all four gate screens -- built, styled, and covered by
    component tests -- were unreachable from the running product, and a reviewer
    who closed the tab mid-review had no way back to the gate.

    Asserted by reading the source because the defect was an absent call: a test
    of the method itself passes whether or not anything calls it. The browser
    acceptance script catches this too, by failing to find a gate to click, but
    that costs a browser and three processes to learn.
    """
    app = (FRONTEND / "app.ts").read_text(encoding="utf-8")
    assert "restoreActiveRun()" in app, "app.ts never defines the restore path"
    startup = app.split("restore()", 1)
    assert len(startup) > 1, "app.ts no longer restores the session on startup"
    assert "this.restoreActiveRun()" in startup[1], (
        "app.ts defines restoreActiveRun but never calls it from the session "
        "restore path, so a run parked at a gate cannot be reopened"
    )


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

    The empty state is not wrong; it was reached wrongly. `followToGate` waits
    for the run to actually park, and handles a terminal run as itself rather
    than leaving the reviewer on a spinner.
    """
    app = (FRONTEND / "app.ts").read_text(encoding="utf-8")
    admit = app.split("async startVerifiedRun")[1].split("private async followToGate")[0]

    assert "followToGate" in admit, "admission never waits for a gate to exist"
    assert "this.screen.set('review')" not in admit, (
        "startVerifiedRun opens a gate screen directly; the run has no gate yet"
    )
    assert "this.screen.set('running')" in admit, (
        "an admitted run should show as running until it parks at a gate"
    )

    follow = app.split("private async followToGate")[1].split("openReview(runId")[0]
    assert "WAITING_FOR_REVIEW" in follow, "the wait does not check for a parked run"
    for terminal in ("COMPLETED", "FAILED"):
        assert terminal in app.split("private async followToGate")[1][:2000], (
            f"a {terminal} run would leave the reviewer waiting on a gate that "
            "will never come"
        )


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
    "B": GATE_SCREENS["B"],
    "C": GATE_SCREENS["C"],
    "D": GATE_SCREENS["D"],
    "report": REPORT,
}

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


def test_the_scope_qualifier_survives_every_translation():
    """The rule CLAUDE.md states outright, pinned.

    `NOT_FOUND_IN_SUPPLIED_PASSAGES` and
    `NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES` **never** mean "not disclosed",
    and shortening either in a UI is the same class of defect the zero-tolerance
    policy forbids. Translating a code into words is exactly where that
    shortening would happen and look like an improvement, so every label for a
    scoped code has to still carry its scope.
    """
    source = read(VOCABULARY)
    scoped = (
        "NOT_FOUND_IN_SUPPLIED_PASSAGES",
        "NO_DISCLOSURE_FOUND_IN_SUPPLIED_PASSAGES",
        "PARTIALLY_DISCLOSED_IN_SUPPLIED_PASSAGES",
        "FULLY_DISCLOSED_IN_SUPPLIED_PASSAGES",
    )
    for code in scoped:
        block = source.split(f"{code}: {{", 1)
        assert len(block) > 1, f"{code} has no entry in the vocabulary"
        label = block[1].split("label:", 1)[1].split("\n", 1)[0]
        assert "passages examined" in label, (
            f"the label for {code} drops the scope qualifier: {label.strip()}"
        )


def test_a_negative_result_is_never_rendered_as_a_statement_about_the_patent():
    """The same rule, at the two places the words are put into a sentence."""
    for path in (GATE_SCREENS["C"], REPORT):
        rendered = rendered_text(path)
        assert "passages examined" in rendered, (
            f"{path.name} states a result without saying what it is scoped to"
        )


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
    from verification.schema import Outcome, Status
    from verification.states import CellState, PublicationExclusion

    source = read(VOCABULARY)
    expected = {
        "STATUS_TERMS": [member.value for member in Status],
        "OUTCOME_TERMS": [member.value for member in Outcome],
        "CELL_STATE_TERMS": [member.value for member in CellState],
        "EXCLUSION_TERMS": [member.value for member in PublicationExclusion],
    }
    for table, codes in expected.items():
        block = source.split(f"export const {table}", 1)
        assert len(block) > 1, f"the vocabulary has no {table}"
        body = block[1].split("\n};", 1)[0]
        for code in codes:
            assert f"{code}:" in body, f"{table} has no entry for {code}"


@pytest.mark.parametrize("table", ["STATUS_TERMS", "OUTCOME_TERMS", "CELL_STATE_TERMS"])
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


def test_the_report_finds_waivers_where_the_draft_builder_puts_them():
    """`build_draft` attaches each waiver to its own matrix entry.

    It writes no top-level `waivers` key at all, and the report's waiver list was
    read from one -- so a report with waivers rendered a count of zero and the
    word "None". The waiver section exists to disclose a gap a reviewer chose not
    to close; an empty one is indistinguishable from having nothing to disclose,
    which makes this the worst possible shape for the defect.
    """
    builder = read(Path(__file__).resolve().parents[1] / "graph" / "report.py")
    assert '"waivers": cell_waivers' in builder, (
        "the draft builder no longer attaches waivers per cell; the report reader "
        "below was written against that shape"
    )
    source = read(REPORT)
    collector = source.split("readonly waivers = computed", 1)[1].split("});", 1)[0]
    assert "this.matrix()" in collector, (
        "the report does not collect waivers from the matrix entries that carry them"
    )
    assert "cell['waivers']" in collector


def test_the_report_reads_unresolved_as_the_list_the_draft_holds():
    """`build_draft`'s `unresolved` is a list of entries, not a count per state.

    It was read as a dictionary and rendered as three numbers, so every report
    showed zero unresolved however many there were. The list also carries the
    failing check, which a count cannot, and which `graph/report.py`'s own
    contract requires be printed beside the comparison.
    """
    builder = read(Path(__file__).resolve().parents[1] / "graph" / "report.py")
    assert "unresolved: list[dict[str, Any]] = []" in builder
    source = read(REPORT)
    reader = source.split("readonly unresolved = computed", 1)[1].split("});", 1)[0]
    assert "Array.isArray" in reader, "the report still treats `unresolved` as a dict"


def test_the_report_prints_the_failing_check_beside_an_unvalidated_comparison():
    """§9.1 and case AJ: a `VALIDATION_FAILURE` is recorded *with* its failing
    check so the report can print it rather than blanking the row."""
    rendered = rendered_text(REPORT)
    assert "failed check" in rendered


def test_the_report_finds_an_excluded_publication_under_either_key():
    """`build_draft` emits `publication_number`; a stored exclusion row calls it
    `requested_number`. The reader used one, so the column was blank in every
    report."""
    source = read(REPORT)
    # The method, not the call site in the template above it.
    reader = source.split("excludedNumber(item: Record", 1)[1].split("\n  }", 1)[0]
    assert "publication_number" in reader and "requested_number" in reader


def test_the_report_shows_the_caveats_the_reviewer_recorded():
    """A caveat typed at the review and dropped from the rendered report is a
    caveat nobody reading the report ever sees. `approve_report` stores both
    columns; the report has to render them."""
    source = read(REPORT)
    assert "value('caveats')" in source
    assert "value('comments')" in source


def test_the_report_prints_the_coverage_statement_it_was_built_with():
    """`COVERAGE_STATEMENT` is stored with the draft, so an approved report
    carries the sentence that was approved rather than whatever the component
    would say today."""
    assert "value('coverage_statement')" in read(REPORT)


@pytest.mark.parametrize(
    "gate,server_rules",
    [("B", "GATE_B_RULES"), ("C", "GATE_C_RULES")],
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

    screen = read(GATE_SCREENS[gate])
    body = screen.split("readonly RULES = [", 1)[1].split("\n  ];", 1)[0]
    screen_count = len([line for line in body.splitlines() if line.strip().startswith("'")])

    assert server_count == screen_count, (
        f"{server_rules} has {server_count} rules and the {gate} screen glosses "
        f"{screen_count}"
    )
