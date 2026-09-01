# Section 6 test matrix — traceability

**Generated.** Do not edit by hand: run
`python -m acceptance.matrix_traceability` from `backend/`.

Every row of section 6 of
`docs/prompts/integrated-feature-verification-workflow-implementation.md`,
and the named test that asserts it. The mapping lives in
`backend/tests/test_matrix_traceability.py`, which parses section 6 out of the
specification itself and resolves every name below against the real suite — so
a row added to the spec, a test renamed, or a test deleted fails the suite
rather than leaving this table quietly wrong.

A **script** entry is an acceptance script under `backend/acceptance/`, not a
pytest function. Those are properties of running processes that no in-process
test can exhibit, and they are labelled differently on purpose: evidence that
someone has to run by hand is weaker than evidence that runs with the suite.

Rows: **164**. Mapped: **164**. Covered only by a script: **2** (35, 135).

| # | Case | Gate | Must assert | Covered by |
|---|---|---|---|---|
| 1 | EPO full text available | B | `TEXT`; sections recorded; passages carry real locators | `test_epo_claims_produce_claim_passages_from_epo_text`<br>`test_provider_paragraph_ids_produce_epo_paragraph_locators` |
| 2 | EPO full text unavailable | B | `NO_TEXT` → `EXCLUDED_NO_FULL_TEXT`; **no cells** | `test_a_no_text_enrichment_becomes_an_exclusion_and_never_a_cell` |
| 3 | Abstract-only patent rejection | B | `EXCLUDED_NO_FULL_TEXT`; refused even with `evidence_eligible=True` | `test_abstract_only_is_excluded_no_full_text`<br>`test_only_claims_and_description_ever_become_passages` |
| 4 | Family-member text mismatch | B | `EXCLUDED_INELIGIBLE`; text never borrowed | `test_borrowed_family_text_is_excluded_ineligible` |
| 5 | Duplicate / ambiguous claim numbering | B | `ClaimNumberingError`; no duplicate `chunk_id`; no invented number | `test_duplicate_provider_claim_numbers_are_rejected`<br>`test_duplicate_claim_numbers_are_rejected_at_the_parser_too`<br>`test_missing_claim_numbers_are_never_invented`<br>`test_a_successfully_parsed_claim_set_never_carries_a_duplicate_number` |
| 6 | Invalid passage provenance | B | `PassageSetIneligible` with the spec §7.3 check named; **no LLM call** | `test_a_passage_from_another_document_fails_the_provenance_check`<br>`test_no_llm_call_is_made_when_provenance_fails` |
| 7 | EPO paragraph identifiers present | B | `EPO_PARAGRAPH` locators; `locator_source` recorded | `test_provider_paragraph_ids_produce_epo_paragraph_locators` |
| 8 | No paragraph identifiers | B | `SOURCE_RANGE`; offsets reproduce the text; **no invented `paragraph [...]`** | `test_a_source_range_passage_must_carry_its_offsets_and_version` |
| 9 | Stable passage IDs across reruns | B | same text version → byte-identical `chunk_id`s | `test_rerunning_over_the_same_text_yields_byte_identical_chunk_ids` |
| 10 | Changed source text | B | new `text_sha256`; dependents stale; old passages retained | `test_changed_source_text_changes_the_hash`<br>`test_a_cell_citing_changed_passage_text_blocks_gate_c` |
| 11 | Description-only verification | B | claims absent, description passages present, verifier gets a non-empty set | `test_only_claims_and_description_ever_become_passages` |
| 12 | No trustworthy locator producible | B | `EXCLUDED_INELIGIBLE`; **never eligible-with-no-passages** | `test_text_retrieved_but_no_locator_producible_is_excluded_ineligible` |
| 13 | Boundary date at the cutoff | B | matches the approved inclusive/exclusive policy | `test_the_boundary_date_is_excluded_under_the_approved_exclusive_policy`<br>`test_a_publication_before_the_cutoff_is_eligible`<br>`test_a_publication_after_the_cutoff_is_excluded` |
| 14 | Missing / malformed / conflicting date | B | `EXCLUDED_DATE`, `date_certainty=UNCERTAIN`; never silently passes | `test_an_unreadable_date_is_excluded_and_flagged_uncertain` |
| 15 | Changed cutoff date | B | new run version; eligibility re-derived; dependents stale | `test_the_cutoff_date_reaches_the_graph_as_a_string`<br>`test_the_approved_policies_are_frozen_onto_the_run`<br>`test_a_cell_citing_changed_passage_text_blocks_gate_c` |
| 16 | Missing text version | B | the approved outcome; `retrieved_at` never substituted | `test_a_missing_text_version_refuses_to_build_a_passage` |
| 17 | Changed text version | B | new hash; dependent results stale | `test_changed_source_text_changes_the_hash`<br>`test_a_cell_citing_changed_passage_text_blocks_gate_c`<br>`test_a_matching_citation_does_not_block` |
| 18 | Malformatted or fenced LLM JSON | C | `VALIDATION_FAILURE` check 1; not unwrapped; raw retained | `test_malformed_json_is_a_validation_failure_with_the_raw_response_retained`<br>`test_a_fenced_response_is_a_validation_failure_and_is_not_unwrapped` |
| 19 | Verifier retry exhaustion | C | exactly two calls; failed cell; no repair; no machine result | `test_two_malformed_responses_produce_one_failed_cell_and_exactly_two_calls` |
| 20 | Immutable machine result | C | collection mutation refused; stored JSONB re-validates unchanged | `test_the_validated_result_itself_refuses_collection_mutation`<br>`test_a_machine_result_round_trips_byte_identical`<br>`test_a_stored_machine_result_is_a_faithful_round_trip` |
| 21 | Unapproved decomposition cannot verify | C | `UnapprovedArtifactError`; **no LLM call** | `test_an_unapproved_run_produces_no_cells_and_no_partial_matrix` |
| 22 | Unapproved candidate set cannot verify | C | `UnapprovedArtifactError`; **no LLM call** | `test_a_missing_approved_candidate_set_refuses_and_calls_no_model` |
| 23 | Candidate decomposition is never a substitute | C | candidate field never read by `verify_features` | `test_a_candidate_decomposition_is_never_read_as_a_substitute` |
| 24 | Only `approvals.py` writes approved artifacts | C | grep test over the backend | `test_only_approvals_py_writes_the_approved_columns` |
| 25 | Valid human override | D | machine result unchanged; overlay records both; outcome recomputed; audit row | `test_case_v_an_override_leaves_the_machine_result_untouched` |
| 26 | Wrong-cell review | D | rejected, spec §13.4 check 15 | `test_check_15_refuses_a_review_from_another_cell_with_identical_requirement_ids` |
| 27 | Stale-result review | D | rejected, check 16 | `test_check_16_refuses_a_stale_review_reattached_to_a_newer_result` |
| 28 | Invalid waiver | D | rejected, check 18; composite FK rejects a bad result version | `test_check_18_refuses_a_waiver_recorded_against_another_result_version` |
| 29 | Duplicate review submission | D | idempotent for same payload; `409` for different | `test_every_mutating_endpoint_requires_an_idempotency_key`<br>`test_the_list_above_is_actually_every_mutating_route`<br>`test_the_same_key_with_a_different_payload_is_a_conflict` |
| 30 | Duplicate resume requests | D | one mutation, one `resumed` event, one transition | `test_a_duplicate_approval_produces_one_mutation_and_replays_the_response`<br>`test_an_approval_appends_exactly_one_resumed_event` |
| 31 | Approval does not resume the graph | D | the graph is provably not invoked by the endpoint | `test_an_approval_never_builds_or_calls_the_graph`<br>`test_the_executor_never_calls_the_graph_from_the_approval_path` |
| 32 | Resume after review approval | D | advances to the next node exactly once | `test_a_run_pauses_at_its_gate_and_resumes_to_completion` |
| 33 | Resume after override | D | reviewed statuses drive the assessment | `test_an_unresolved_unclear_requirement_blocks_gate_c`<br>`test_case_v_an_override_leaves_the_machine_result_untouched` |
| 34 | Resume with unresolved context request | D | blocked until fulfilled or waived | `test_an_outstanding_context_request_blocks_gate_c` |
| 35 | Backend restart while paused | D | resumes from Postgres; nothing lost | script `real_process_acceptance.py`<br>script `four_gate_acceptance.py` |
| 36 | Duplicate executor attempts | A | exactly one acquires; the other does not execute | `test_two_executors_racing_one_run_yield_one_committed_set_of_events` |
| 37 | Lease expiration and takeover | A | reclaimed; `fence_token` increments; old holder's write fails | `test_a_second_executor_reclaims_an_expired_lease_and_continues`<br>`test_the_old_holders_writes_are_rejected_after_the_takeover` |
| 38 | Backend termination during execution | A | reclaimed and continued from the checkpoint | `test_a_run_recovered_mid_execution_does_not_repeat_a_committed_node` |
| 39 | Backend restart while running | A | recovers to the correct status | `test_a_run_marked_running_with_no_checkpoint_starts_rather_than_failing` |
| 40 | Checkpoint written, event/status write fails | A | reconcile repairs forward-only; no duplicates | `test_reconcile_repairs_a_checkpoint_committed_without_its_pause_transaction` |
| 41 | One committed result under concurrency | A | one committed node result; one terminal; **not** an exactly-once external claim | `test_two_executors_racing_one_run_yield_one_committed_set_of_events` |
| 42 | Invalid state transition rejected | A | every pair outside §3.3's table | `test_every_unlisted_transition_is_rejected` |
| 43 | Concurrent identical idempotent requests | A | exactly one mutation; both get the same response | `test_concurrent_identical_requests_produce_exactly_one_mutation` |
| 44 | Rolled-back transaction leaves no idempotency record | A | the key is reusable afterwards | `test_a_rolled_back_transaction_leaves_no_record_and_frees_the_key` |
| 45 | Migration applies on PostgreSQL 17 | A | `run_status_transitions` has no NULL creation row; trigger installs | `test_the_workflow_schema_is_fully_applied`<br>`test_the_rollbacks_reverse_the_workflow_schema_and_re_apply`<br>`test_a_run_cannot_be_inserted_in_any_other_status` |
| 46 | A run can only be inserted as `QUEUED` | A | any other insert status raises | `test_a_run_cannot_be_inserted_in_any_other_status` |
| 47 | Every permitted update transition | A | all nine listed pairs succeed | `test_every_listed_transition_is_permitted` |
| 48 | Every unlisted transition | A | rejected by the trigger | `test_every_unlisted_transition_is_rejected` |
| 49 | `COMPLETED` / `FAILED` have no outgoing transitions | A | any update away from either raises | `test_terminal_statuses_have_no_outgoing_transitions` |
| 50 | Lease expires during a blocked provider call | A | replacement acquires; original is fenced | `test_a_lease_lost_during_a_blocked_provider_call` |
| 51 | Replacement worker starts the same node | A | second attempt recorded; first cannot commit | `test_a_fenced_worker_cannot_commit_a_node_attempt_after_a_takeover` |
| 52 | Fenced worker returns and tries to write | A | every write rejected; response never becomes evidence | `test_a_fenced_attempts_response_never_becomes_evidence`<br>`test_a_blocked_worker_returning_after_takeover_persists_nothing` |
| 53 | One committed node result and one event | A | one `COMMITTED` attempt; one event; both attempts visible | `test_one_authoritative_event_is_committed_alongside_the_one_result` |
| 54 | Attempt telemetry distinguishes retries | A | `node_attempts` shows two; the matrix shows one result | `test_telemetry_retains_every_physical_attempt` |
| 55 | `progress` marked terminal | A | rejected by the CHECK | `test_is_terminal_must_match_the_event_type` |
| 56 | `result` marked non-terminal | A | rejected by the CHECK | `test_is_terminal_must_match_the_event_type` |
| 57 | Second terminal event | A | rejected by the partial unique index | `test_a_second_terminal_of_any_type_is_refused`<br>`test_the_one_terminal_index_refuses_a_hand_written_second_terminal` |
| 58 | `error` after `result` | A | rejected inside the append function | `test_nothing_of_any_type_can_be_appended_after_a_terminal` |
| 59 | `progress` after `result` | A | rejected: nothing follows a terminal | `test_nothing_of_any_type_can_be_appended_after_a_terminal` |
| 60 | Concurrent terminal writers | A | serialized on the run row; one commits, one raises | `test_concurrent_success_and_failure_finalizers_yield_one_terminal` |
| 61 | Run status / terminal-event mismatch | A | `COMPLETED`+`error` and `FAILED`+`result` both rejected | `test_a_terminal_status_cannot_disagree_with_its_terminal_event` |
| 62 | Active worker appends a progress event | A | accepted under a live lease | `test_an_active_worker_appends_a_progress_event` |
| 63 | Wrong `owner_id` | A | rejected `FN001`; nothing appended | `test_a_worker_without_a_live_matching_lease_appends_nothing` |
| 64 | Wrong fence token | A | rejected `FN001` | `test_a_worker_without_a_live_matching_lease_appends_nothing` |
| 65 | Expired lease | A | rejected `FN001` | `test_a_worker_without_a_live_matching_lease_appends_nothing` |
| 66 | Old worker returns after takeover | A | cannot append **any** event type | `test_an_old_worker_after_a_takeover_cannot_append_anything_at_all` |
| 67 | Worker function rejects terminals | A | `result` / `error` raise `FN005` | `test_append_worker_event_rejects_terminals`<br>`test_append_worker_event_rejects_resumed` |
| 68 | API function rejects worker events | A | only `resumed` accepted | `test_the_api_writer_accepts_resumed_from_the_owner`<br>`test_the_api_writer_rejects_every_other_event_type` |
| 69 | API event with wrong owner | A | rejected `FN004`, indistinguishable from unknown run | `test_the_api_writer_rejects_the_wrong_owner` |
| 70 | API event + mutation + audit + idempotency + `RESUMING` | D | all five commit together or none | `test_an_approval_writes_an_audit_row_naming_the_authenticated_actor`<br>`test_an_approval_moves_the_run_to_resuming_and_stops_there`<br>`test_an_approval_appends_exactly_one_resumed_event` |
| 71 | Successful finalization | A | one `result` **and** `COMPLETED`, one transaction | `test_finalize_success_writes_one_result_and_completes` |
| 72 | Failed finalization | A | one `error` **and** `FAILED`, one transaction | `test_finalize_failure_writes_one_error_and_fails` |
| 73 | Terminal event / status disagreement | A | `COMPLETED`+`error`, `FAILED`+`result` rejected | `test_a_terminal_status_cannot_disagree_with_its_terminal_event` |
| 74 | Transaction failure mid-finalization | A | neither terminal event nor terminal status survives | `test_a_transaction_failure_mid_finalization_leaves_neither_half` |
| 75 | Concurrent success and failure finalizers | A | exactly one terminal; loser raises `FN002` | `test_concurrent_success_and_failure_finalizers_yield_one_terminal` |
| 76 | Idempotent terminal replay | A | same dedupe key + identical payload returns the same terminal | `test_a_dedupe_collision_with_an_identical_payload_is_a_no_op` |
| 77 | Conflicting terminal replay | A | same key, different payload rejected `FN003` | `test_a_dedupe_collision_with_a_different_payload_raises_fn003` |
| 78 | Database outage during finalization | A | no fabricated success, no in-memory terminal | `test_a_checkpoint_outage_stops_the_worker_and_fabricates_no_terminal`<br>`test_a_transaction_failure_mid_finalization_leaves_neither_half` |
| 79 | Two attempts, one execution key | A | only one may commit | `test_two_attempts_at_one_execution_key_yield_one_committed` |
| 80 | Two attempts, different execution keys | A | both may commit | `test_two_attempts_with_different_execution_keys_may_both_commit` |
| 81 | Fenced attempt cannot commit | A | stays `FENCED`; never `COMMITTED` | `test_a_fenced_attempt_can_never_become_committed` |
| 82 | Recovery reuses the execution key | A | same `execution_key`, new `attempt_id` | `test_a_recovery_reuses_the_execution_key_and_mints_a_new_attempt_id` |
| 83 | Cell rerun at `result_version=2` | A/D | commits after version 1 committed | `test_a_cell_rerun_at_result_version_two_commits_after_version_one` |
| 84 | Changed passage text / decomposition / candidate-set version | A/B | each yields a new execution key | `test_changed_versions_each_produce_a_new_execution_key`<br>`test_a_cell_key_carries_its_result_version` |
| 85 | Duplicate recovery attempts | A | no duplicate machine result, no duplicate event | `test_one_authoritative_event_is_committed_alongside_the_one_result`<br>`test_a_recovery_reuses_the_execution_key_and_mints_a_new_attempt_id` |
| 86 | Attempt telemetry retains all attempts | A | every physical attempt kept; one committed per logical execution | `test_telemetry_retains_every_physical_attempt` |
| 87 | Client-supplied execution key | E | ignored or rejected; never trusted | `test_an_execution_key_supplied_by_a_client_is_rejected` |
| 88 | Validate-lease races takeover **(real PG)** | A | takeover blocks until the authorized txn commits | `test_a_takeover_blocks_until_the_authorized_transaction_commits` |
| 89 | Old token cannot append after takeover **(real PG)** | A | `FN001` | `test_after_a_takeover_the_old_token_can_write_nothing_at_all` |
| 90 | Old token cannot transition status after takeover **(real PG)** | A | `FN001` | `test_after_a_takeover_the_old_token_can_write_nothing_at_all` |
| 91 | Old token cannot commit a node attempt after takeover **(real PG)** | A | `FN001` | `test_a_fenced_worker_cannot_commit_a_node_attempt_after_a_takeover` |
| 92 | Old token cannot finalize after takeover **(real PG)** | A | `FN001`, success and failure | `test_after_a_takeover_the_old_token_can_write_nothing_at_all` |
| 93 | Heartbeat races takeover **(real PG)** | A | exactly one holder survives | `test_a_heartbeat_racing_a_takeover_resolves_to_exactly_one_holder` |
| 94 | Lease release races finalization **(real PG)** | A | serialized; terminal still commits | `test_a_release_racing_a_finalization_serializes_and_the_terminal_commits` |
| 95 | Uniform lock order **(real PG)** | A | run row then lease row, in every lease-sensitive function | `test_every_lease_sensitive_function_locks_the_run_row_first` |
| 96 | No deadlock under concurrent load **(real PG)** | A | no SQLSTATE `40P01` across a repeated mixed workload | `test_no_deadlock_under_a_repeated_mixed_workload` |
| 97 | Identical retry after `COMPLETED` | A | same sequence; nothing created; status unchanged | `test_an_identical_retry_after_completed_returns_the_same_sequence` |
| 98 | Identical retry after `FAILED` | A | same sequence; nothing created | `test_an_identical_retry_after_failed_returns_the_same_sequence` |
| 99 | Identical retry after lease release | A | replay needs no lease | `test_a_replay_needs_no_lease_at_all` |
| 100 | Same dedupe key, different terminal type | A | `FN003` | `test_the_same_dedupe_key_with_a_different_terminal_type_raises_fn003` |
| 101 | Same dedupe key, different payload hash | A | `FN003` | `test_the_same_dedupe_key_with_a_different_payload_raises_fn003` |
| 102 | Same failure payload, different failure code | A | `FN003` | `test_the_same_failure_payload_with_a_different_failure_code_raises_fn003` |
| 103 | Different dedupe key when a terminal exists | A | `FN003` | `test_a_different_dedupe_key_when_a_terminal_exists_raises_fn003` |
| 104 | Concurrent identical finalizers **(real PG)** | A | one event, one status, same sequence to both | `test_concurrent_identical_finalizers_return_the_same_sequence` |
| 105 | Valid replay mutates nothing | A | one sequence; no status change; no new row | `test_a_valid_replay_mutates_nothing` |
| 106 | Worker `progress` while `QUEUED` | A | `FN005` | `test_worker_events_are_running_only` |
| 107 | Worker `progress` while `RESUMING` | A | `FN005` | `test_worker_events_are_running_only` |
| 108 | Worker `progress` while `WAITING_FOR_REVIEW` | A | `FN005` | `test_worker_events_are_running_only` |
| 109 | Worker `progress` after terminal | A | `FN002` | `test_nothing_of_any_type_can_be_appended_after_a_terminal` |
| 110 | Pause transaction rollback | A/D | no partial pause state survives | `test_the_pause_transaction_rolls_back_as_a_unit` |
| 111 | Seq-1 `queued` event only from `create_run` | A | the worker path cannot produce it | `test_the_seq_one_queued_event_can_only_come_from_create_run` |
| 112 | Reconcile repairs each pause crash boundary | A/D | all three §3.3.2 rows, no duplicated event | `test_reconcile_repairs_a_checkpoint_committed_without_its_pause_transaction`<br>`test_reconcile_repairs_review_required_without_paused`<br>`test_reconcile_repairs_both_events_without_the_transition` |
| 113 | Missing policy at admission | B | `422`; **no** run, lease, checkpoint, event or idempotency success | `test_an_unset_policy_refuses_admission`<br>`test_admission_refuses_before_writing_anything_when_a_policy_is_unrecorded`<br>`test_every_missing_item_is_reported_at_once` |
| 114 | Unrecorded decision at admission | B | `422`; no run created | `test_a_policy_with_no_recorded_decision_refuses_admission` |
| 115 | Approved policies admit a run | B | exactly one `QUEUED` run and one queued event, one transaction | `test_fully_recorded_policies_admit_a_run`<br>`test_create_run_writes_the_run_the_lease_and_the_queued_event` |
| 116 | Post-admission capability failure | B | existing run `QUEUED → FAILED`; one sanitized terminal `error` | `test_admission_refuses_a_verified_run_with_no_capability_artifact`<br>`test_a_node_that_raises_fails_the_run_with_one_sanitized_terminal` |
| 117 | Coverage dry-run | B | zero HTTP calls; lists the numbers it would query | `test_dry_run_makes_zero_http_calls_and_lists_the_query_order` |
| 118 | Coverage deduplication | B | duplicates normalized and queried once; collapse count reported | `test_duplicates_are_collapsed_before_querying` |
| 119 | Coverage uses stored/supplied IDs | B | discovery, embeddings and LLM never invoked | `test_the_tool_never_imports_a_discovery_provider` |
| 120 | Coverage with no sample | B | fails clearly and requests approval; no discovery call | `test_a_missing_sample_stops_and_asks_rather_than_running_discovery` |
| 121 | Coverage keeps `FAILED` distinct from `NO_TEXT` | B | a provider outage never counts as absent text | `test_failed_is_never_counted_as_no_text`<br>`test_coverage_is_none_rather_than_zero_when_nothing_was_answered` |
| 122 | Missed-event replay | E | exactly the missed events, in order, once | `test_missed_event_replay_delivers_exactly_the_missed_events_in_order` |
| 123 | Reconnect with `Last-Event-ID` | E | no snapshot; nothing re-delivered | `test_reconnecting_twice_with_the_same_id_yields_the_same_frames`<br>`test_no_event_is_delivered_twice_within_one_connection`<br>`test_after_a_snapshot_only_subsequent_events_arrive` |
| 124 | Reconnect with no id | E | `snapshot` at current max, then live | `test_attaching_with_no_id_gets_a_snapshot_first`<br>`test_the_snapshot_is_never_persisted` |
| 125 | Multiple backend processes | E | an event written by one is delivered by another | `test_an_event_written_by_another_connection_is_delivered_by_this_stream` |
| 126 | SSE client disconnect during execution | E | run unaffected; terminal persisted exactly once | `test_abandoning_the_stream_changes_nothing_about_the_run`<br>`test_the_stream_module_never_consults_client_disconnection` |
| 127 | No second terminal after reconnection | E | closed without re-emitting | `test_reconnecting_at_or_above_the_terminal_delivers_nothing_and_closes` |
| 128 | Slow-client backpressure | E | disconnected within bound; run unaffected; catch-up works | `test_a_stalled_reader_is_disconnected_and_the_run_is_unaffected`<br>`test_a_client_that_keeps_up_is_never_disconnected` |
| 129 | Two-user authorization, every category | E | `404 RUN_NOT_FOUND`, never 403, never data | `test_another_user_gets_404_on_every_run_scoped_endpoint`<br>`test_no_endpoint_ever_answers_403`<br>`test_every_run_scoped_route_on_the_router_is_covered_here` |
| 130 | Provider outage | G | run fails; no partial result; no exclusion invented | `test_a_provider_outage_mid_matrix_fails_the_run`<br>`test_a_provider_failure_propagates_rather_than_becoming_an_exclusion` |
| 131 | Database / checkpoint outage | G | run fails cleanly; no in-memory continuation | `test_a_checkpoint_outage_stops_the_worker_and_fabricates_no_terminal`<br>`test_a_fenced_worker_is_not_reported_as_a_database_outage` |
| 132 | No final report before required gates complete | E/G | no `result` while any mandatory gate is outstanding | `test_no_path_to_the_end_of_the_graph_skips_gate_d`<br>`test_the_draft_must_exist_before_it_can_be_approved`<br>`test_the_draft_report_node_sits_between_gate_c_and_gate_d` |
| 133 | Successful final reviewed report | G | one `result`; verification block present; machine/human distinct | script `four_gate_acceptance.py`<br>`test_a_harness_shaped_approved_run_produces_real_cells` |
| 134 | Existing search and history regression | every gate | `test_pipeline_contract.py`, `test_sse_transport.py`, `test_history.py`, `test_auth.py` pass **unedited** | `test_a_search_only_run_emits_exactly_the_phases_it_did_before`<br>`test_a_search_only_run_returns_exactly_the_result_keys_it_did_before`<br>`test_the_existing_endpoints_are_still_registered_and_unchanged` |
| 135 | Frontend build and type checking | F | `npm run build` and `npm run typecheck` clean | script `browser_acceptance.py` |
| 136 | Lease row created unheld at token 0 | A | `create_run` makes it; `owner_id IS NULL` | `test_create_run_makes_the_lease_row_unheld_at_token_zero` |
| 137 | First acquisition returns token 1 | A | never 0 | `test_first_acquisition_returns_token_one_not_zero` |
| 138 | Acquire / release / acquire | A | returns 3; the counter never resets | `test_acquire_release_acquire_returns_three` |
| 139 | Release keeps the row | A | holder cleared, `released_at` stamped, token unchanged | `test_release_clears_the_holder_and_keeps_the_row_and_the_token` |
| 140 | Pause releases without deleting | A | row still present after `pause_run_for_review` | `test_pause_releases_without_deleting_the_row` |
| 141 | No application lease-delete path | A | source contract: no `DELETE FROM run_leases` anywhere in `backend/` | `test_no_module_deletes_a_lease_row` |
| 142 | Fence token lowered by update | A | `FN006` | `test_an_update_lowering_a_fence_token_raises_fn006`<br>`test_no_module_resets_or_lowers_a_fence_token` |
| 143 | Superseded token with a **reused `owner_id`** | A | still `FN001`; no coincidental match | `test_a_superseded_token_cannot_write_even_with_a_reused_owner_id` |
| 144 | Run with no lease row | A | `FN006` on acquisition; never an insert | `test_a_run_with_no_lease_row_raises_rather_than_inserting_one` |
| 145 | Lease row removed only by run cascade | A | deleting the run removes it; nothing else does | `test_deleting_the_parent_run_removes_its_lease_row`<br>`test_run_leases_has_no_delete_trigger` |
| 146 | Equality instant `expires_at = transaction_timestamp()` **(real PG)** | A | heartbeat fails **and** acquisition succeeds | `test_the_expiry_equality_instant` |
| 147 | Heartbeat before expiry | A | succeeds and extends | `test_a_heartbeat_before_expiry_succeeds_and_extends` |
| 148 | Heartbeat exactly at expiry | A | fails | `test_the_expiry_equality_instant` |
| 149 | Heartbeat after expiry | A | fails | `test_a_heartbeat_cannot_revive_a_lease` |
| 150 | Heartbeat after release | A | fails | `test_a_heartbeat_cannot_revive_a_lease` |
| 151 | Failed heartbeat changes nothing | A | row byte-identical before and after | `test_a_failed_beat_returns_false_rather_than_raising` |
| 152 | Late heartbeat races takeover **(real PG)** | A | cannot revive the old lease | `test_a_heartbeat_racing_a_takeover_resolves_to_exactly_one_holder` |
| 153 | Only acquisition reactivates | A | and it increments the token | `test_only_acquisition_reactivates_and_it_increments` |
| 154 | Active holder releases | A | holder cleared, `released_at` stamped, token unchanged | `test_release_clears_the_holder_and_keeps_the_row_and_the_token` |
| 155 | Release with wrong owner | A | `FN001` | `test_release_raises_fn001_when_the_caller_is_not_the_active_holder` |
| 156 | Release with wrong token | A | `FN001` | `test_release_raises_fn001_when_the_caller_is_not_the_active_holder` |
| 157 | Release of an expired lease | A | `FN001` | `test_release_raises_fn001_when_the_caller_is_not_the_active_holder` |
| 158 | Release of an already-released lease | A | `FN001`, **not** a silent no-op | `test_a_repeated_release_raises_rather_than_silently_succeeding` |
| 159 | Acquisition after release | A | same row updated; token incremented | `test_acquisition_after_release_updates_the_same_row_and_increments` |
| 160 | Parent-run delete cascades **(real PG)** | A | approved retention removes the run and its lease row | `test_deleting_the_parent_run_removes_its_lease_row` |
| 161 | Lease-delete privilege revoked | A | API and worker roles cannot `DELETE`; worker can `EXECUTE` the lease functions | `test_lease_privileges_where_the_deployment_has_roles` |
| 162 | NULL / zero / negative TTL | A | `invalid_parameter_value` from acquire and heartbeat; lease row unchanged | `test_a_non_positive_ttl_is_rejected_and_the_row_is_unchanged` |
| 163 | Positive TTL | A | accepted | `test_a_positive_ttl_is_accepted` |
| 164 | Invalid lease configuration | A | worker refuses to start; no default substituted | `test_an_invalid_lease_configuration_refuses_to_build_an_executor`<br>`test_no_graph_adapter_configured_refuses_to_start` |
