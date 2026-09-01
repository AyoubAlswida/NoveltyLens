"""Every row of the specification's section 6 test matrix, and what covers it.

## Why this is a test and not a document

The specification says "every row must exist as a named test before the owning
gate is complete". A markdown table asserting that is a claim; this module is a
check. It parses section 6 out of the specification itself, so a row added there
fails here until somebody maps it, and it resolves every name in `COVERAGE`
against the actual test suite, so a renamed or deleted test fails here rather
than leaving the table quietly wrong.

`docs/acceptance/matrix-traceability.md` is **generated** from `COVERAGE`
(`python -m acceptance.matrix_traceability`) and this module asserts the file on
disk still matches, so the published table cannot drift from the mapping the
suite enforces.

## What an `acceptance:` entry means

A handful of rows are properties of running processes rather than of a function:
a backend restart mid-review, a rendered report surviving that restart, a
frontend build. Those name the acceptance script that exercises them, and they
are labelled differently in the generated table **on purpose** -- a script that
someone has to run is weaker evidence than a test that runs in CI, and writing
them into the same column would hide that difference.

Rows are mapped to the test that asserts the row's *stated* requirement. Where a
row has two halves ("new hash; dependents stale") both halves are named.
"""

import pathlib
import re

import pytest

SPEC = (
    pathlib.Path(__file__).resolve().parents[2]
    / "docs"
    / "prompts"
    / "integrated-feature-verification-workflow-implementation.md"
)
TESTS = pathlib.Path(__file__).resolve().parent
GENERATED = (
    pathlib.Path(__file__).resolve().parents[2]
    / "docs"
    / "acceptance"
    / "matrix-traceability.md"
)

# Row number -> the names that cover it. A name starting with `acceptance:` is a
# script under `backend/acceptance/`, not a pytest function.
COVERAGE: dict[int, tuple[str, ...]] = {
    1: ("test_epo_claims_produce_claim_passages_from_epo_text",
        "test_provider_paragraph_ids_produce_epo_paragraph_locators"),
    2: ("test_a_no_text_enrichment_becomes_an_exclusion_and_never_a_cell",),
    3: ("test_abstract_only_is_excluded_no_full_text",
        "test_only_claims_and_description_ever_become_passages"),
    4: ("test_borrowed_family_text_is_excluded_ineligible",),
    5: ("test_duplicate_provider_claim_numbers_are_rejected",
        "test_duplicate_claim_numbers_are_rejected_at_the_parser_too",
        "test_missing_claim_numbers_are_never_invented",
        "test_a_successfully_parsed_claim_set_never_carries_a_duplicate_number"),
    6: ("test_a_passage_from_another_document_fails_the_provenance_check",
        "test_no_llm_call_is_made_when_provenance_fails"),
    7: ("test_provider_paragraph_ids_produce_epo_paragraph_locators",),
    8: ("test_a_source_range_passage_must_carry_its_offsets_and_version",),
    9: ("test_rerunning_over_the_same_text_yields_byte_identical_chunk_ids",),
    10: ("test_changed_source_text_changes_the_hash",
         "test_a_cell_citing_changed_passage_text_blocks_gate_c"),
    11: ("test_only_claims_and_description_ever_become_passages",),
    12: ("test_text_retrieved_but_no_locator_producible_is_excluded_ineligible",),
    13: ("test_the_boundary_date_is_excluded_under_the_approved_exclusive_policy",
         "test_a_publication_before_the_cutoff_is_eligible",
         "test_a_publication_after_the_cutoff_is_excluded"),
    14: ("test_an_unreadable_date_is_excluded_and_flagged_uncertain",),
    15: ("test_the_cutoff_date_reaches_the_graph_as_a_string",
         "test_the_approved_policies_are_frozen_onto_the_run",
         "test_a_cell_citing_changed_passage_text_blocks_gate_c"),
    16: ("test_a_missing_text_version_refuses_to_build_a_passage",),
    17: ("test_changed_source_text_changes_the_hash",
         "test_a_cell_citing_changed_passage_text_blocks_gate_c",
         "test_a_matching_citation_does_not_block"),
    18: ("test_malformed_json_is_a_validation_failure_with_the_raw_response_retained",
         "test_a_fenced_response_is_a_validation_failure_and_is_not_unwrapped"),
    19: ("test_two_malformed_responses_produce_one_failed_cell_and_exactly_two_calls",),
    20: ("test_the_validated_result_itself_refuses_collection_mutation",
         "test_a_machine_result_round_trips_byte_identical",
         "test_a_stored_machine_result_is_a_faithful_round_trip"),
    21: ("test_an_unapproved_run_produces_no_cells_and_no_partial_matrix",),
    22: ("test_a_missing_approved_candidate_set_refuses_and_calls_no_model",),
    23: ("test_a_candidate_decomposition_is_never_read_as_a_substitute",),
    24: ("test_only_approvals_py_writes_the_approved_columns",),
    25: ("test_case_v_an_override_leaves_the_machine_result_untouched",),
    26: ("test_check_15_refuses_a_review_from_another_cell_with_identical_requirement_ids",),
    27: ("test_check_16_refuses_a_stale_review_reattached_to_a_newer_result",),
    28: ("test_check_18_refuses_a_waiver_recorded_against_another_result_version",),
    29: ("test_every_mutating_endpoint_requires_an_idempotency_key",
         "test_the_list_above_is_actually_every_mutating_route",
         "test_the_same_key_with_a_different_payload_is_a_conflict"),
    30: ("test_a_duplicate_approval_produces_one_mutation_and_replays_the_response",
         "test_an_approval_appends_exactly_one_resumed_event"),
    31: ("test_an_approval_never_builds_or_calls_the_graph",
         "test_the_executor_never_calls_the_graph_from_the_approval_path"),
    32: ("test_a_run_pauses_at_its_gate_and_resumes_to_completion",),
    33: ("test_an_unresolved_unclear_requirement_blocks_gate_c",
         "test_case_v_an_override_leaves_the_machine_result_untouched"),
    34: ("test_an_outstanding_context_request_blocks_gate_c",),
    35: ("acceptance:real_process_acceptance.py", "acceptance:four_gate_acceptance.py"),
    36: ("test_two_executors_racing_one_run_yield_one_committed_set_of_events",),
    37: ("test_a_second_executor_reclaims_an_expired_lease_and_continues",
         "test_the_old_holders_writes_are_rejected_after_the_takeover"),
    38: ("test_a_run_recovered_mid_execution_does_not_repeat_a_committed_node",),
    39: ("test_a_run_marked_running_with_no_checkpoint_starts_rather_than_failing",),
    40: ("test_reconcile_repairs_a_checkpoint_committed_without_its_pause_transaction",),
    41: ("test_two_executors_racing_one_run_yield_one_committed_set_of_events",),
    42: ("test_every_unlisted_transition_is_rejected",),
    43: ("test_concurrent_identical_requests_produce_exactly_one_mutation",),
    44: ("test_a_rolled_back_transaction_leaves_no_record_and_frees_the_key",),
    45: ("test_the_workflow_schema_is_fully_applied",
         "test_the_rollbacks_reverse_the_workflow_schema_and_re_apply",
         "test_a_run_cannot_be_inserted_in_any_other_status"),
    46: ("test_a_run_cannot_be_inserted_in_any_other_status",),
    47: ("test_every_listed_transition_is_permitted",),
    48: ("test_every_unlisted_transition_is_rejected",),
    49: ("test_terminal_statuses_have_no_outgoing_transitions",),
    50: ("test_a_lease_lost_during_a_blocked_provider_call",),
    51: ("test_a_fenced_worker_cannot_commit_a_node_attempt_after_a_takeover",),
    52: ("test_a_fenced_attempts_response_never_becomes_evidence",
         "test_a_blocked_worker_returning_after_takeover_persists_nothing"),
    53: ("test_one_authoritative_event_is_committed_alongside_the_one_result",),
    54: ("test_telemetry_retains_every_physical_attempt",),
    55: ("test_is_terminal_must_match_the_event_type",),
    56: ("test_is_terminal_must_match_the_event_type",),
    57: ("test_a_second_terminal_of_any_type_is_refused",
         "test_the_one_terminal_index_refuses_a_hand_written_second_terminal"),
    58: ("test_nothing_of_any_type_can_be_appended_after_a_terminal",),
    59: ("test_nothing_of_any_type_can_be_appended_after_a_terminal",),
    60: ("test_concurrent_success_and_failure_finalizers_yield_one_terminal",),
    61: ("test_a_terminal_status_cannot_disagree_with_its_terminal_event",),
    62: ("test_an_active_worker_appends_a_progress_event",),
    63: ("test_a_worker_without_a_live_matching_lease_appends_nothing",),
    64: ("test_a_worker_without_a_live_matching_lease_appends_nothing",),
    65: ("test_a_worker_without_a_live_matching_lease_appends_nothing",),
    66: ("test_an_old_worker_after_a_takeover_cannot_append_anything_at_all",),
    67: ("test_append_worker_event_rejects_terminals",
         "test_append_worker_event_rejects_resumed"),
    68: ("test_the_api_writer_accepts_resumed_from_the_owner",
         "test_the_api_writer_rejects_every_other_event_type"),
    69: ("test_the_api_writer_rejects_the_wrong_owner",),
    70: ("test_an_approval_writes_an_audit_row_naming_the_authenticated_actor",
         "test_an_approval_moves_the_run_to_resuming_and_stops_there",
         "test_an_approval_appends_exactly_one_resumed_event"),
    71: ("test_finalize_success_writes_one_result_and_completes",),
    72: ("test_finalize_failure_writes_one_error_and_fails",),
    73: ("test_a_terminal_status_cannot_disagree_with_its_terminal_event",),
    74: ("test_a_transaction_failure_mid_finalization_leaves_neither_half",),
    75: ("test_concurrent_success_and_failure_finalizers_yield_one_terminal",),
    76: ("test_a_dedupe_collision_with_an_identical_payload_is_a_no_op",),
    77: ("test_a_dedupe_collision_with_a_different_payload_raises_fn003",),
    78: ("test_a_checkpoint_outage_stops_the_worker_and_fabricates_no_terminal",
         "test_a_transaction_failure_mid_finalization_leaves_neither_half"),
    79: ("test_two_attempts_at_one_execution_key_yield_one_committed",),
    80: ("test_two_attempts_with_different_execution_keys_may_both_commit",),
    81: ("test_a_fenced_attempt_can_never_become_committed",),
    82: ("test_a_recovery_reuses_the_execution_key_and_mints_a_new_attempt_id",),
    83: ("test_a_cell_rerun_at_result_version_two_commits_after_version_one",),
    84: ("test_changed_versions_each_produce_a_new_execution_key",
         "test_a_cell_key_carries_its_result_version"),
    85: ("test_one_authoritative_event_is_committed_alongside_the_one_result",
         "test_a_recovery_reuses_the_execution_key_and_mints_a_new_attempt_id"),
    86: ("test_telemetry_retains_every_physical_attempt",),
    87: ("test_an_execution_key_supplied_by_a_client_is_rejected",),
    88: ("test_a_takeover_blocks_until_the_authorized_transaction_commits",),
    89: ("test_after_a_takeover_the_old_token_can_write_nothing_at_all",),
    90: ("test_after_a_takeover_the_old_token_can_write_nothing_at_all",),
    91: ("test_a_fenced_worker_cannot_commit_a_node_attempt_after_a_takeover",),
    92: ("test_after_a_takeover_the_old_token_can_write_nothing_at_all",),
    93: ("test_a_heartbeat_racing_a_takeover_resolves_to_exactly_one_holder",),
    94: ("test_a_release_racing_a_finalization_serializes_and_the_terminal_commits",),
    95: ("test_every_lease_sensitive_function_locks_the_run_row_first",),
    96: ("test_no_deadlock_under_a_repeated_mixed_workload",),
    97: ("test_an_identical_retry_after_completed_returns_the_same_sequence",),
    98: ("test_an_identical_retry_after_failed_returns_the_same_sequence",),
    99: ("test_a_replay_needs_no_lease_at_all",),
    100: ("test_the_same_dedupe_key_with_a_different_terminal_type_raises_fn003",),
    101: ("test_the_same_dedupe_key_with_a_different_payload_raises_fn003",),
    102: ("test_the_same_failure_payload_with_a_different_failure_code_raises_fn003",),
    103: ("test_a_different_dedupe_key_when_a_terminal_exists_raises_fn003",),
    104: ("test_concurrent_identical_finalizers_return_the_same_sequence",),
    105: ("test_a_valid_replay_mutates_nothing",),
    106: ("test_worker_events_are_running_only",),
    107: ("test_worker_events_are_running_only",),
    108: ("test_worker_events_are_running_only",),
    109: ("test_nothing_of_any_type_can_be_appended_after_a_terminal",),
    110: ("test_the_pause_transaction_rolls_back_as_a_unit",),
    111: ("test_the_seq_one_queued_event_can_only_come_from_create_run",),
    112: ("test_reconcile_repairs_a_checkpoint_committed_without_its_pause_transaction",
          "test_reconcile_repairs_review_required_without_paused",
          "test_reconcile_repairs_both_events_without_the_transition"),
    113: ("test_an_unset_policy_refuses_admission",
          "test_admission_refuses_before_writing_anything_when_a_policy_is_unrecorded",
          "test_every_missing_item_is_reported_at_once"),
    114: ("test_a_policy_with_no_recorded_decision_refuses_admission",),
    115: ("test_fully_recorded_policies_admit_a_run",
          "test_create_run_writes_the_run_the_lease_and_the_queued_event"),
    116: ("test_admission_refuses_a_verified_run_with_no_capability_artifact",
          "test_a_node_that_raises_fails_the_run_with_one_sanitized_terminal"),
    117: ("test_dry_run_makes_zero_http_calls_and_lists_the_query_order",),
    118: ("test_duplicates_are_collapsed_before_querying",),
    119: ("test_the_tool_never_imports_a_discovery_provider",),
    120: ("test_a_missing_sample_stops_and_asks_rather_than_running_discovery",),
    121: ("test_failed_is_never_counted_as_no_text",
          "test_coverage_is_none_rather_than_zero_when_nothing_was_answered"),
    122: ("test_missed_event_replay_delivers_exactly_the_missed_events_in_order",),
    123: ("test_reconnecting_twice_with_the_same_id_yields_the_same_frames",
          "test_no_event_is_delivered_twice_within_one_connection",
          "test_after_a_snapshot_only_subsequent_events_arrive"),
    124: ("test_attaching_with_no_id_gets_a_snapshot_first",
          "test_the_snapshot_is_never_persisted"),
    125: ("test_an_event_written_by_another_connection_is_delivered_by_this_stream",),
    126: ("test_abandoning_the_stream_changes_nothing_about_the_run",
          "test_the_stream_module_never_consults_client_disconnection"),
    127: ("test_reconnecting_at_or_above_the_terminal_delivers_nothing_and_closes",),
    128: ("test_a_stalled_reader_is_disconnected_and_the_run_is_unaffected",
          "test_a_client_that_keeps_up_is_never_disconnected"),
    129: ("test_another_user_gets_404_on_every_run_scoped_endpoint",
          "test_no_endpoint_ever_answers_403",
          "test_every_run_scoped_route_on_the_router_is_covered_here"),
    130: ("test_a_provider_outage_mid_matrix_fails_the_run",
          "test_a_provider_failure_propagates_rather_than_becoming_an_exclusion"),
    131: ("test_a_checkpoint_outage_stops_the_worker_and_fabricates_no_terminal",
          "test_a_fenced_worker_is_not_reported_as_a_database_outage"),
    132: ("test_no_path_to_the_end_of_the_graph_skips_gate_d",
          "test_the_draft_must_exist_before_it_can_be_approved",
          "test_the_draft_report_node_sits_between_gate_c_and_gate_d"),
    133: ("acceptance:four_gate_acceptance.py",
          "test_a_harness_shaped_approved_run_produces_real_cells"),
    134: ("test_a_search_only_run_emits_exactly_the_phases_it_did_before",
          "test_a_search_only_run_returns_exactly_the_result_keys_it_did_before",
          "test_the_existing_endpoints_are_still_registered_and_unchanged"),
    135: ("acceptance:browser_acceptance.py",),
    136: ("test_create_run_makes_the_lease_row_unheld_at_token_zero",),
    137: ("test_first_acquisition_returns_token_one_not_zero",),
    138: ("test_acquire_release_acquire_returns_three",),
    139: ("test_release_clears_the_holder_and_keeps_the_row_and_the_token",),
    140: ("test_pause_releases_without_deleting_the_row",),
    141: ("test_no_module_deletes_a_lease_row",),
    142: ("test_an_update_lowering_a_fence_token_raises_fn006",
          "test_no_module_resets_or_lowers_a_fence_token"),
    143: ("test_a_superseded_token_cannot_write_even_with_a_reused_owner_id",),
    144: ("test_a_run_with_no_lease_row_raises_rather_than_inserting_one",),
    145: ("test_deleting_the_parent_run_removes_its_lease_row",
          "test_run_leases_has_no_delete_trigger"),
    146: ("test_the_expiry_equality_instant",),
    147: ("test_a_heartbeat_before_expiry_succeeds_and_extends",),
    148: ("test_the_expiry_equality_instant",),
    149: ("test_a_heartbeat_cannot_revive_a_lease",),
    150: ("test_a_heartbeat_cannot_revive_a_lease",),
    151: ("test_a_failed_beat_returns_false_rather_than_raising",),
    152: ("test_a_heartbeat_racing_a_takeover_resolves_to_exactly_one_holder",),
    153: ("test_only_acquisition_reactivates_and_it_increments",),
    154: ("test_release_clears_the_holder_and_keeps_the_row_and_the_token",),
    155: ("test_release_raises_fn001_when_the_caller_is_not_the_active_holder",),
    156: ("test_release_raises_fn001_when_the_caller_is_not_the_active_holder",),
    157: ("test_release_raises_fn001_when_the_caller_is_not_the_active_holder",),
    158: ("test_a_repeated_release_raises_rather_than_silently_succeeding",),
    159: ("test_acquisition_after_release_updates_the_same_row_and_increments",),
    160: ("test_deleting_the_parent_run_removes_its_lease_row",),
    161: ("test_lease_privileges_where_the_deployment_has_roles",),
    162: ("test_a_non_positive_ttl_is_rejected_and_the_row_is_unchanged",),
    163: ("test_a_positive_ttl_is_accepted",),
    164: ("test_an_invalid_lease_configuration_refuses_to_build_an_executor",
          "test_no_graph_adapter_configured_refuses_to_start"),
}

ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")


def spec_rows() -> dict[int, tuple[str, str, str]]:
    """Section 6's table, read from the specification rather than restated."""
    body = SPEC.read_text(encoding="utf-8")
    section = body.split("## 6. Required test matrix")[1].split("## 7.")[0]
    rows: dict[int, tuple[str, str, str]] = {}
    for line in section.splitlines():
        found = ROW.match(line)
        if found:
            rows[int(found.group(1))] = (
                found.group(2),
                found.group(3),
                found.group(4),
            )
    return rows


def suite_test_names() -> set[str]:
    names: set[str] = set()
    for path in TESTS.glob("test_*.py"):
        # Indentation optional: a test method inside a class is as much a named
        # test as a module-level function, and anchoring at column 0 silently
        # dropped every one of them.
        names.update(
            re.findall(
                r"^\s*def (test_[a-z0-9_]+)", path.read_text(encoding="utf-8"), re.M
            )
        )
    return names


def test_the_spec_table_parses_as_a_contiguous_run_of_rows():
    """If this fails the parser has drifted from the table, and every assertion
    below would be checking a subset while looking complete."""
    rows = spec_rows()
    assert rows, "section 6 produced no rows: the parser or the heading changed"
    assert set(rows) == set(range(1, max(rows) + 1)), (
        "the row numbers are not contiguous: " + str(sorted(set(range(1, max(rows) + 1)) - set(rows)))
    )


def test_every_specification_row_is_mapped_to_something_named():
    """The specification's own rule: a row must exist as a named test."""
    unmapped = sorted(set(spec_rows()) - set(COVERAGE))
    assert not unmapped, (
        "section 6 rows with no entry in COVERAGE: " + str(unmapped)
    )


def test_no_mapping_points_at_a_row_the_specification_does_not_have():
    """The other direction, so a deleted row leaves no orphaned claim behind."""
    rows = spec_rows()
    orphans = sorted(set(COVERAGE) - set(rows))
    assert not orphans, "COVERAGE claims rows section 6 does not define: " + str(orphans)


def test_every_named_test_actually_exists():
    """A renamed or deleted test fails here rather than leaving the table wrong.

    This is the assertion that makes the generated document worth reading: it is
    the difference between "these names were plausible when written" and "these
    names resolve, today, in this suite".
    """
    names = suite_test_names()
    missing: list[str] = []
    for row, covers in sorted(COVERAGE.items()):
        for name in covers:
            if name.startswith("acceptance:"):
                script = TESTS.parent / "acceptance" / name.split(":", 1)[1]
                if not script.exists():
                    missing.append(f"row {row}: {name}")
            elif name not in names:
                missing.append(f"row {row}: {name}")
    assert not missing, "COVERAGE names that do not resolve:\n" + "\n".join(missing)


def test_rows_covered_only_by_a_script_are_the_ones_we_expect():
    """Script-only evidence is weaker than a test, so the set of rows relying on
    it is pinned rather than allowed to grow quietly.

    Each of these three is a property of running processes -- a restart mid
    review, an approved report surviving one, a frontend build -- which no
    in-process test can exhibit. Anything else appearing here means a row lost
    its test.
    """
    script_only = {
        row
        for row, covers in COVERAGE.items()
        if all(name.startswith("acceptance:") for name in covers)
    }
    assert script_only == {35, 135}, sorted(script_only)


@pytest.mark.parametrize("row", sorted(COVERAGE))
def test_each_row_names_at_least_one_cover(row: int):
    assert COVERAGE[row], f"row {row} is mapped to an empty tuple"


def test_the_generated_table_on_disk_matches_this_mapping():
    """`docs/acceptance/matrix-traceability.md` is generated, and stale
    generated documents are worse than none: this one is evidence."""
    from acceptance.matrix_traceability import render

    if not GENERATED.exists():
        pytest.fail(
            "docs/acceptance/matrix-traceability.md is missing; "
            "regenerate it with `python -m acceptance.matrix_traceability`"
        )
    assert GENERATED.read_text(encoding="utf-8") == render(), (
        "the generated traceability table is out of date; "
        "regenerate it with `python -m acceptance.matrix_traceability`"
    )
