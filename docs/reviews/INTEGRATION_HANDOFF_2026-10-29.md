# Local integration handoff — 2026-10-29

Source: feature/ryn-integrated-workspace at 9128434. This integration retains the model pool, Agent, app lifecycle, four office adapters, friend AI sharing, connection recovery and generic corporate-app bridge. It is an integration candidate, not a release or a claim that all backends/platforms are ready.

## Completed here

- Repaired seven focused regression cases without changing permission assertions: one-shot configuration race injection, valid diagnostic tool name/schema exposure, prompt semantic assertion, scheduler-aware subagent fixture, schema exposure for Node Apps approval, and floating-point timing tolerance.
- Fixed an actual missing math import in task budget continuation. Removed duplicate imports/unused bindings and corrected local variable naming. Verified imported pytest fixture arguments have targeted lint annotations; test assertions remain intact.
- Resolved cross-platform test collection importing a tests namespace shadowed by an installed package.
- Removed 54 extracted browser-adapter files. Package data now explicitly includes only the first-party bridge and documentation. Optional externally supplied adapters require RYN_CHROME_RUNTIME_DIR; absence is reported before launching a browser, and normal JS/tools still work. Built-in ryn_browser_* remains available. No automatic extraction/download or proprietary assets in the inspected wheel.
- Canonical personal/corporate node/service/API/MCP/plugin/app definitions and the invited-user productivity milestone are documented. Gshopper business code is in private yeogirlyun/gshopper-ryn-node, not the public runtime.

## Current validation

- Final focused backend: 294 passed, 2 skipped (explicit external-browser acceptance).
- Whole rynmesh/tests Ruff check passed.
- Earlier complete frontend at source 9128434: 1,326 passed; TypeScript/Vite and five native-drop JS tests passed. No frontend code changed in this patch.
- Built a wheel and inspected all entries: only README, Python resolver and first-party CDP bridge are in chrome_runtime; extracted client/service/WASM/EXE assets absent.
- A broad backend run was deliberately stopped after 6m26s to scope substantial contributor repairs: 1,465 passed, 85 failed, 89 skipped. It is incomplete and predates final lint/budget-import corrections; those counts are not final-head acceptance.
- No installed desktop release, live model provider, production account/DB or physical WAN acceptance is claimed here.

## Small follow-up: settings display efficiency

`codex/settings-projection-efficiency-20261029` moves model context-budget display
projection after the settings transaction commits and releases the encrypted
state lock. It uses the catalogue's display-only metadata path; explicit model
selection validation and task admission still resolve current model metadata.
The settings response shape, persisted limits and permission transitions remain
compatible. The two new regression tests failed before the fix: a concurrent
state reader was blocked by display work, and saving a limit refreshed discovery.
After the fix, eight focused Agent suites pass (82 tests); changed Python files
also pass Ruff. This does not supersede the broader failure inventory below.

## Larger revisions requested from Ziling

The stopped broad run identified failures across synthetic query/controller fixtures, native/image geometry and Windows fixture assumptions, compact-context/schema projection, streaming/retry timing and pinned JavaScript resource/runtime tests. Several are platform/fixture mismatches; they are not all demonstrated production bugs. Use the named failure inventory below to reproduce and classify them before changing behavior. Preserve useful compatibility, budget and no-duplicate-execution checks.

Additional design work: self-contained browser adapter; clear Agent saved-file versus open-editor unsaved-document handoff; GridCraft empty-string formula compatibility; a single Apps catalogue projection over the two existing lifecycle implementations; convenient installation/invite/first answer/artifact/repeat-use journey.

No main merge of the complete workspace should be inferred from this draft. Finish source compatibility in small independently verified PRs. Keep authentication lightweight; corporate sign-in corrections are tracked only in the private node repository. No production services, migration or recurring monitoring were started.

## Broad-run failure inventory

- `tests/test_agent_access_query.py::test_q06_real_model_grants_and_signed_admission`
- `tests/test_agent_access_query.py::test_q06_write_rejected_at_real_boundaries[candidate]`
- `tests/test_agent_access_query.py::test_q06_write_rejected_at_real_boundaries[reference]`
- `tests/test_agent_access_query.py::test_q06_write_rejected_at_real_boundaries[direct]`
- `tests/test_agent_access_query.py::test_q06_detects_out_of_band_grant_mutation`
- `tests/test_agent_access_query.py::test_q06_controller_cli_both_cohorts_unchanged_facts[ryn]`
- `tests/test_agent_access_query.py::test_q06_actual_gate_and_pinned_worker_share_generation_policy[ryn]`
- `tests/test_agent_access_query.py::test_q06_only_protocol_nonces_excluded_from_relationship_comparison[nonce]`
- `tests/test_agent_access_query.py::test_q06_only_protocol_nonces_excluded_from_relationship_comparison[relationship]`
- `tests/test_agent_access_query.py::test_q06_only_protocol_nonces_excluded_from_relationship_comparison[key]`
- `tests/test_agent_access_query.py::test_q06_refuses_provider_tools_and_unrelated_consumer_runtime`
- `tests/test_agent_access_query.py::test_q06_actual_consumer_provider_gate_rejects_changes_without_upstream[revoke]`
- `tests/test_agent_access_query.py::test_q06_actual_consumer_provider_gate_rejects_changes_without_upstream[model_revision]`
- `tests/test_agent_access_query.py::test_q06_actual_consumer_provider_gate_rejects_changes_without_upstream[quota]`
- `tests/test_agent_checkpoint_refs.py::test_schema_cache_yields_to_exact_references_without_dropping_evidence`
- `tests/test_agent_computer_automation.py::test_overlay_renders_feathered_halo_and_three_phase_hud_capsules`
- `tests/test_agent_computer_image_fidelity.py::test_full_original_image_fidelity_and_small_chinese_ui_pixels_preserved_across_inspect_mcp_and_codex_rpc`
- `tests/test_agent_computer_image_fidelity.py::test_independent_budget_context_and_transport_gates_before_model_call`
- `tests/test_agent_computer_image_fidelity.py::test_nested_inspect_post_entry_exception_records_attempted_link_while_pre_entry_refusal_records_none`
- `tests/test_agent_computer_image_fidelity.py::test_native_operation_pixels_use_owner_registry_and_keep_mcp_scope_strict`
- `tests/test_agent_computer_image_fidelity.py::test_native_8mib_vs_mcp_4mib_provenance_and_computer_image_geometry_mapping`
- `tests/test_agent_computer_status.py::test_native_bridge_frozen_hook_and_unbound_cache_rejection`
- `tests/test_agent_consumer_process_proof.py::test_controller_rejects_unowned_boundary_before_replacement`
- `tests/test_agent_consumer_process_proof.py::test_controller_preserves_cleanup_failure_and_closes_every_handle`
- `tests/test_agent_consumer_process_proof.py::test_wrapper_permission_failure_remains_failed_and_identifies_substep[wait]`
- `tests/test_agent_consumer_process_proof.py::test_wrapper_permission_failure_remains_failed_and_identifies_substep[read_marker]`
- `tests/test_agent_consumer_process_proof.py::test_wrapper_permission_failure_remains_failed_and_identifies_substep[rewrite_marker]`
- `tests/test_agent_conversation_reliability.py::test_large_window_does_not_send_entire_tool_registry`
- `tests/test_agent_desktop_recovery.py::test_computer_read_window_auto_restores_minimized_window_and_refreshes_tray_hwnd`
- `tests/test_agent_dispatch_nested_read.py::test_actual_runtime_diagnostics_and_readback_survive_collection`
- `tests/test_agent_evidence_semantics.py::test_effective_model_context_budget_omits_optional_metadata_before_raw_evidence`
- `tests/test_agent_execution_efficiency.py::test_small_stream_chunks_are_coalesced_without_losing_first_or_final_text[100-0.3-35]`
- `tests/test_agent_execution_efficiency.py::test_small_stream_chunks_are_coalesced_without_losing_first_or_final_text[100-0.001-3]`
- `tests/test_agent_execution_efficiency.py::test_model_timeout_retry_records_request_id_monotonic_timing_and_safe_diagnostics`
- `tests/test_agent_extensions_oauth.py::test_dcr_fixed_port_and_preregistered_priority`
- `tests/test_agent_image_geometry_trust.py::test_forged_seal_post_seal_mutations_untrusted_tags_and_mcp_paths_rejected`
- `tests/test_agent_image_geometry_trust.py::test_trusted_private_issuer_rust_window_ref_identity_bindings_and_missing_wildcard_refusals`
- `tests/test_agent_image_geometry_trust.py::test_non_original_presentation_payload_mismatch_refused_and_lossless_crop_tile_verified_with_pixel_markers`
- `tests/test_agent_image_geometry_trust.py::test_generic_mcp_4mib_vs_native_8mib_and_bounded_fd_containment_protections`
- `tests/test_agent_image_input.py::test_image_tool_sends_actual_bounded_pixels_without_putting_base64_in_receipts`
- `tests/test_agent_image_input.py::test_image_tool_cannot_read_outside_workspace_or_invent_vision_support`
- `tests/test_agent_image_routes.py::test_codex_cli_image_inspect_preserves_turn_start_image_items_and_keeps_store_base64_free`
- `tests/test_agent_image_routes.py::test_inspect_image_uses_model_configured_output_up_to_16384_tokens`
- `tests/test_agent_image_routes.py::test_inspect_image_never_reinvokes_model_on_type_error`
- `tests/test_agent_image_routes.py::test_antigravity_cli_and_explicit_negative_models_refuse_both_tool_and_existing_canonical_images`
- `tests/test_agent_image_routes.py::test_image_inspect_retains_destination_budget_and_cancellation_guards`
- `tests/test_agent_image_routes.py::test_mcp_screenshot_materializes_into_main_loop_with_preparation_vs_submission_evidence`
- `tests/test_agent_image_routes.py::test_mcp_screenshot_rejected_by_context_budget_never_claims_submitted`
- `tests/test_agent_image_routes.py::test_mcp_artifact_verification_rejects_tamper_crosstask_outside_symlink_and_bounded_read_race`
- `tests/test_agent_image_routes.py::test_inspect_image_auto_downscales_oversized_pixel_image_and_retries_transient_invalid_response`
- `tests/test_agent_js_resource_mount.py::test_production_default_manager_fails_closed_with_zero_spawn_and_no_dev_or_env_fallback`
- `tests/test_agent_js_resource_mount.py::test_whole_closure_handle_pinning_and_windows_sharing_barriers`
- `tests/test_agent_js_resource_mount.py::test_windows_pinned_directory_handles_deny_rename_when_executed`
- `tests/test_agent_js_resource_mount.py::test_pre_resume_verification_rejects_mismatched_pid_creation_time_image_and_hook_errors`
- `tests/test_agent_js_resource_mount.py::test_ready_handshake_strictly_validates_session_generation_engine_variant_and_pid`
- `tests/test_agent_js_resource_mount.py::test_unconfirmed_child_exit_retains_lease_pins_in_quarantine_until_exit_confirmed`
- `tests/test_agent_js_resource_mount.py::test_manager_live_status_tracks_active_sessions_and_rejects_relabelled_dev_lease`
- `tests/test_agent_js_resource_mount.py::test_manager_session_lock_order_concurrent_startup_quarantine_and_single_flight`
- `tests/test_agent_js_resources.py::test_prepare_and_resolve_deterministic_profile_with_unicode_and_spaces`
- `tests/test_agent_js_resources.py::test_cli_entry_point_and_refusal_to_overwrite_existing_target`
- `tests/test_agent_js_resources.py::test_strict_json_and_manifest_schema_rejections`
- `tests/test_agent_js_resources.py::test_tampered_node_wasm_missing_variant_extra_package_and_export_map_failures`
- `tests/test_agent_js_resources.py::test_reparse_point_and_symlink_rejection`
- `tests/test_agent_js_resources.py::test_output_leaf_validation_dangling_link_existing_file_dir_race_and_permission_errors`
- `tests/test_agent_long_resource_context.py::test_public_long_notes_all_checkpoints_to_approved_anonymous_report`
- `tests/test_agent_native_bridge.py::test_cache_status_is_strictly_read_only`
- `tests/test_agent_native_computer_integration.py::test_computer_read_uses_one_control_payload_and_preserves_trusted_bindings`
- `tests/test_agent_native_computer_integration.py::test_focus_and_following_actions_return_current_state_without_model_read_roundtrip`
- `tests/test_agent_native_computer_integration.py::test_committed_action_with_failed_post_capture_remains_committed`
- `tests/test_agent_native_computer_integration.py::test_shared_facade_builtin_mcp_read_write_dispatch_order_and_artifact_retirement`
- `tests/test_agent_native_computer_integration.py::test_confirmed_dedup_and_post_commit_unknown_no_replay`
- `tests/test_agent_native_computer_integration.py::test_ready_time_authority_and_approval_guards_yield_zero_commit`
- `tests/test_agent_native_computer_integration.py::test_negative_screen_coordinates_and_local_approval_preview_guard`
- `tests/test_agent_native_computer_integration.py::test_screenshot_bounds_rejection_exclusive_create_and_verified_cleanup`
- `tests/test_agent_native_scope.py::test_small_native_window_projects_tools_before_actual_dispatch`
- `tests/test_agent_p03_controller.py::test_approved_write_is_rejected_after_actual_owner_api_revocation`
- `tests/test_agent_p03_controller.py::test_fixture_cli_does_not_read_owner_and_never_claims_formal_or_model_success`
- `tests/test_agent_p03_controller.py::test_settings_failure_cancels_before_releasing_approved_write[False]`
- `tests/test_agent_p03_controller.py::test_settings_failure_cancels_before_releasing_approved_write[True]`
- `tests/test_agent_planner_policy.py::test_mapping_precedes_snapshot_and_real_outer_gate_enforces_it[ryn]`
- `tests/test_agent_planner_policy.py::test_mapping_precedes_snapshot_and_real_outer_gate_enforces_it[reference]`
- `tests/test_agent_planner_policy.py::test_reference_documents_dispatch_honors_same_policy_and_lower_request[None]`
- `tests/test_agent_planner_policy.py::test_reference_documents_dispatch_honors_same_policy_and_lower_request[6000]`
- `tests/test_agent_planner_policy.py::test_reference_documents_dispatch_honors_same_policy_and_lower_request[256000]`
- `tests/test_agent_query_cases.py::test_real_encrypted_world_and_actual_presence_classifier[Q01]`
