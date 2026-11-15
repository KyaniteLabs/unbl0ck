# Features and the tests that pin them

Each row is a user-facing behaviour the [README](../README.md) documents, and the test or tests
that fail if it changes. A row lists the tests that check the behaviour itself, not every test
that touches it. `tests/test_features_doc.py` fails when a test named here no longer exists, so a
rename or removal must update this table.

Test ids are pytest node ids: `tests/<file>::<Class>::<test>` or `tests/<file>::<test>`.

| Area | Behaviour | Pinned by |
|---|---|---|
| Queue | Answer + unblock posts one comment and releases the card | `tests/test_needs.py::FakeKanban::test_answer_and_release` |
| Queue | Answer, keep blocked posts the comment, keeps the card listed as answered, and offers Release | `tests/test_needs.py::FakeKanban::test_answer_only`<br>`tests/test_needs.py::FakeKanban::test_answered_and_kept_blocked_stays_listed_and_can_be_released` |
| Queue | An answer to an old block or a stale version is refused, never posted | `tests/test_needs.py::FakeKanban::test_answer_to_an_old_block_is_refused`<br>`tests/test_api_qa.py::ApiQA::test_answer_carries_the_version_and_stale_is_409` |
| Queue | A double submit never posts a second comment | `tests/test_needs.py::FakeKanban::test_double_submit_is_rejected_before_a_second_comment` |
| Queue | A failed release stays in the queue and retries without a second comment | `tests/test_needs.py::FakeKanban::test_failed_release_stays_in_queue_and_retries_without_a_second_comment` |
| Queue | Sources are ordered by urgency: live prompts, pairing, Kanban, outside, chats | `tests/test_qa.py::Ordering::test_urgent_sources_lead_whatever_the_board_priority`<br>`tests/test_outside.py::Outside::test_order_places_outside_between_kanban_and_chat` |
| Queue | The needs-you count and Pocket pack include only unanswered asks | `tests/test_needs.py::Chats::test_queue_count_and_pocket_pack_include_only_unanswered_asks` |
| Queue | Not for me hides one version; a changed source resurfaces it; Undo restores it | `tests/test_state_machines.py::VisibilityMachine::test_legal_transitions`<br>`tests/test_polish.py::Unhide::test_unhide_restores_only_that_item` |
| Slash command | On a messaging platform /unbl0ck shows only a count, never the queue | `tests/test_qa.py::Summary::test_slash_shows_the_queue_only_on_local_surfaces` |
| Pocket pack | The pack never carries pairing codes or prompt secrets | `tests/test_capability_matrix.py::ReadOnlyExport::test_pocket_pack_never_carries_pairing_or_prompt_secrets` |
| Install | role_words are configurable | `tests/test_wave2_judgment.py::test_u8_role_words_are_configurable` |
| Install | Summaries off sends nothing to the model | `tests/test_disclosure_matrix.py::WhatLeavesTheMachine::test_summaries_off_sends_nothing` |
| Install | ask_enabled turns TL;DR off | `tests/test_needs.py::Ask::test_ask_enabled_setting` |
| Kanban | A needs_input block that names you counts | `tests/test_needs.py::Classify::test_needs_input_naming_principal` |
| Kanban | An unblock option names the board and the block; triage cards get none | `tests/test_r4_stack01d_ask.py::PocketPackIsBoundAndHonest::test_the_unblock_option_names_the_board_and_the_block`<br>`tests/test_r4_stack01d_ask.py::PocketPackIsBoundAndHonest::test_a_triage_card_is_not_offered_an_unblock` |
| Chats | Human platforms are listed; agent-to-agent sessions are not | `tests/test_cover.py::AllPlatformChats::test_human_platforms_are_listed_agents_are_not` |
| Chats | In a group, channel or thread the ask must name you | `tests/test_cover.py::AllPlatformChats::test_group_chat_must_name_you` |
| Chats | Gateway chats say who they are with; owner_user_ids filters others out | `tests/test_qa.py::Chats::test_gateway_chat_says_who_it_is_with_and_owner_ids_filter` |
| Chats | A compressed chain is checked only at its newest link | `tests/test_r4_stack11.py::ReadBudget::test_a_continuation_chain_still_lists_only_its_newest_link` |
| Chats | The chat window defaults to 14 days and is clamped | `tests/test_ask_coverage.py::Window::test_default_is_14_days_and_the_setting_is_clamped` |
| Pairing | Requests list the admin-side id, never the code | `tests/test_pairing_stock.py::RealStore::test_lists_request_id_never_code` |
| Live prompts | A prompt whose process exited is dropped | `tests/test_waits.py::LivePrompts::test_dead_process_wait_is_dropped` |
| Outside | Filing with a dedupe_key updates the item in place | `tests/test_outside.py::Outside::test_dedupe_updates_in_place_and_unchanged_content_keeps_version` |
| Outside | Resolve by id needs the current version; a stale one keeps the record | `tests/test_r4_stack01c_outside.py::ResolveNeedsTheVersion::test_resolve_by_id_with_the_current_version_deletes`<br>`tests/test_r4_stack01c_outside.py::ResolveNeedsTheVersion::test_resolve_by_id_with_a_stale_version_keeps_the_record`<br>`tests/test_r4_stack01c_outside.py::ResolveNeedsTheVersion::test_resolve_by_id_without_a_version_is_refused` |
| Outside | An over-long dedupe_key is refused, not shortened | `tests/test_r4_stack01c_outside.py::FilingInputs::test_an_oversized_dedupe_key_is_rejected_not_truncated` |
| Outside | .eml drafts use subject, recipient and the first plain part | `tests/test_outside.py::Outside::test_eml_uses_subject_to_and_first_plain_part` |
| Outside | HTML-only mail shows inert text, never markup | `tests/test_outside.py::Outside::test_html_only_mail_shows_inert_text_never_markup` |
| Outside | Symlinks are not followed; dotfiles and unlisted types stay quiet | `tests/test_outside_paging.py::NestedFolders::test_symlinked_directory_and_file_are_not_followed`<br>`tests/test_export_formats.py::UnsupportedFormats::test_unlisted_extensions_and_dotfiles_stay_quiet` |
| Outside | Recursive intake reads nested files read-only | `tests/test_outside_paging.py::NestedFolders::test_recursive_intake_reads_nested_files_read_only` |
| Outside | Files are read at most 256 KiB; the newest 200 per folder are shown | `tests/test_outside.py::Outside::test_read_is_capped_at_256_kib`<br>`tests/test_outside.py::Outside::test_only_newest_200_files_per_folder_are_read` |
| Outside | Done keeps a filed record recoverable; Undo restores it | `tests/test_outside.py::OutsideAPI::test_done_hides_filed_record_without_deleting_it`<br>`tests/test_outside_done_undo.py::DoneUndo::test_undo_restores_the_same_record_with_profile_and_version` |
| Outside | Done, Not for me and Undo agree with a model under any sequence | `tests/test_state_properties.py::TestOutsideDismissalProperties` |
| Ask | A restart never offers a resend for a request that may have reached the model | `tests/test_ask_job_recovery.py::RestartStates::test_restart_after_send_is_unknown_not_resent`<br>`tests/test_state_properties.py::TestAskJobsProperties` |
| Full text | The queue never carries full text; full text is bounded | `tests/test_full_text.py::SourceFullText::test_queue_never_carries_full_text`<br>`tests/test_full_text.py::SourceFullText::test_chat_full_text_is_bounded` |
| Disclosure | The only subprocess is ps -o lstart=, without a shell | `tests/test_subprocess_sites.py::SubprocessSites::test_subprocess_is_used_at_exactly_the_disclosed_site`<br>`tests/test_subprocess_sites.py::SubprocessSites::test_the_one_call_is_ps_lstart_without_a_shell` |
| Disclosure | No route can send a reply, approve pairing or answer a prompt | `tests/test_capability_matrix.py::ApiBoundaries::test_no_route_can_send_a_reply_approve_pairing_or_answer_a_prompt` |
