# Banking Discoverable Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Banking's fixed-success discoverable-tool simulation with the
real Tau2 Banking hidden-tool execution model while keeping all changes inside
`application/banking_golden_agent/`.

**Architecture:** Keep the existing 16 Runtime-visible tools. Add an immutable
registry of hidden Agent/User tool methods and adapt the official Tau2 Banking
business behavior to `BankingDBService`. The generic unlock/give/call entry
points validate registry membership and Python signatures, then execute real
methods against the in-memory database.

**Tech Stack:** Python 3.13, Pydantic-backed Runtime Tool schemas, pytest, Ruff,
Mypy, JSON Banking fixtures.

**Spec:**
`application/banking_golden_agent/docs/2026-08-27-discoverable-tools-design.md`

## Global Constraints

- Modify or create files only under `application/banking_golden_agent/`.
- Keep `BankingDBService` process-global; session-scoped capability state is out
  of scope.
- Do not register the 44 Agent discoverable tools individually with Runtime.
- Adapt behavior from Tau2 commit
  `a2c024725189473d2d7cea3a5cfdbcc67478e41f`.
- Do not add dependencies or modify `pyproject.toml`/`uv.lock`.
- Every production behavior must be preceded by a failing test.

---

### Task 1: Hidden-tool registry and database primitives

**Files:**
- Create: `application/banking_golden_agent/services/discoverable_registry.py`
- Modify: `application/banking_golden_agent/services/banking_db_service.py`
- Create: `application/banking_golden_agent/tests/test_discoverable_registry.py`
- Create: `application/banking_golden_agent/tests/conftest.py`

**Interfaces:**
- Produces `DiscoverableToolKind`, `DiscoverableToolDefinition`,
  `discoverable_tool(...)`, `build_discoverable_registry(owner_type)`, and
  `invoke_discoverable_tool(definition, owner, arguments)`.
- Produces locked BankingDBService primitives for reading a record, querying a
  table, adding a record, updating a record, deleting a record, and resetting
  the service from its initial JSON snapshot.

- [ ] Write registry tests asserting duplicate names fail, unknown annotations
  fail, JSON arguments must be an object, missing/extra arguments fail, and a
  valid definition invokes its bound method.
- [ ] Run the registry tests and verify they fail because the registry module
  does not exist.
- [ ] Implement the frozen registry definitions and signature-based invocation.
- [ ] Run the registry tests and verify they pass.
- [ ] Write database tests proving mutations affect only memory, reset restores
  the original snapshot, and reset clears unlocked/given/called state.
- [ ] Run them and verify reset/primitives fail before implementation.
- [ ] Implement minimal locked database primitives and reset support.
- [ ] Run Task 1 tests, Ruff, and Mypy for the changed files.

### Task 2: Generic Agent discoverable execution and current trace regression

**Files:**
- Create: `application/banking_golden_agent/services/discoverable_agent_tools.py`
- Modify: `application/banking_golden_agent/services/banking_db_service.py`
- Modify: `application/banking_golden_agent/plugins/banking_plugin.py`
- Create: `application/banking_golden_agent/tests/test_agent_discoverable_execution.py`

**Interfaces:**
- Produces `AgentDiscoverableTools` with hidden methods
  `example_agent_tool_0000`,
  `get_all_user_accounts_by_user_id_3847` and
  `transfer_funds_between_bank_accounts_7291`.
- Changes `unlock_discoverable_agent_tool` to reject unknown names and export
  metadata from real method definitions.
- Changes `call_discoverable_agent_tool` to require unlock, parse a JSON object,
  bind arguments, invoke the hidden method, and return its actual result.

- [ ] Write a failing regression test that queries user `125` and asserts the
  result contains both account records and their IDs instead of the fixed
  success sentence.
- [ ] Write a failing transfer test asserting a positive amount atomically
  decreases the source balance and increases the destination balance.
- [ ] Add failing tests for unknown tools, calls before unlock, malformed JSON,
  non-object JSON, missing arguments, same source/destination, non-positive
  amount, missing/closed accounts, and insufficient funds.
- [ ] Run the tests and confirm the current fixed-success implementation causes
  the expected failures.
- [ ] Implement the two hidden methods and generic invocation path.
- [ ] Convert expected gateway failures to the existing `ToolError` boundary
  without catching unexpected exceptions.
- [ ] Run Task 2 tests and the existing Banking tool-error tests.

### Task 3: Bank-account hidden tools

**Files:**
- Modify: `application/banking_golden_agent/services/discoverable_agent_tools.py`
- Create: `application/banking_golden_agent/tests/test_bank_account_discoverable_tools.py`

**Interfaces:**
- Adds `open_bank_account_4821`, `close_bank_account_7392`,
  `apply_checking_account_credit_5829`, `apply_savings_account_credit_6831`,
  `submit_interest_discrepancy_report_7294`, and
  `get_bank_account_transactions_9173`.

- [ ] Add failing read tests for reverse-chronological transaction history and
  missing accounts.
- [ ] Add failing write tests for account opening, eligible/ineligible early
  closure, checking/savings credit class validation, balance changes, and
  interest-report creation.
- [ ] Verify every failing test is caused by a missing hidden method.
- [ ] Adapt the six official methods, including deterministic IDs, date parsing,
  full precondition validation before mutation, and human-readable results.
- [ ] Run the Task 3 tests and Task 2 regression tests.

### Task 4: Credit-card and dispute hidden tools

**Files:**
- Modify: `application/banking_golden_agent/services/discoverable_agent_tools.py`
- Create: `application/banking_golden_agent/tests/test_credit_card_discoverable_tools.py`

**Interfaces:**
- Adds `update_transaction_rewards_3847`, three initial/escalation transfer
  tools, `file_credit_card_transaction_dispute_4829`,
  `order_replacement_credit_card_7291`, `get_user_dispute_history_7291`,
  `get_pending_replacement_orders_5765`,
  `log_credit_card_closure_reason_4521`, `get_closure_reason_history_8293`,
  `apply_statement_credit_8472`, `apply_credit_card_account_flag_6147`,
  `close_credit_card_account_7834`, `pay_credit_card_from_checking_9182`,
  `submit_credit_limit_increase_request_7392`,
  `get_credit_limit_increase_history_4829`, `get_payment_history_6183`,
  `approve_credit_limit_increase_5847`, and
  `deny_credit_limit_increase_5848`.

- [ ] Write failing tests for each read tool using existing fixture records.
- [ ] Write one successful state-transition test and one critical precondition
  failure test for every mutating method: missing record, invalid status,
  duplicate/open dispute, balance/limit violation, or invalid enum as applicable.
- [ ] Verify all tests fail because the methods are absent.
- [ ] Adapt the official methods and deterministic record-ID helpers without
  weakening their business validation.
- [ ] Run the Task 4 tests and all earlier Banking tests.

### Task 5: Debit-card hidden tools

**Files:**
- Modify: `application/banking_golden_agent/services/discoverable_agent_tools.py`
- Create: `application/banking_golden_agent/tests/test_debit_card_discoverable_tools.py`

**Interfaces:**
- Adds `file_debit_card_transaction_dispute_6281`,
  `set_debit_card_recurring_block_7382`, `get_debit_dispute_status_7483`,
  `get_atm_deposit_images_8473`, `order_debit_card_5739`, three activation
  variants, `close_debit_card_4721`, `freeze_debit_card_3892`,
  `unfreeze_debit_card_3893`, `clear_debit_card_fraud_alert_4892`,
  `reset_debit_card_pin_6284`, `change_debit_card_pin_6285`,
  `get_debit_cards_by_account_id_7823`, and
  `request_temporary_debit_card_limit_increase_8374`.

- [ ] Write failing tests for debit-card reads, status transitions, replacement
  ordering, dispute creation, PIN validation, fraud-alert clearing, recurring
  blocks, and temporary limits.
- [ ] Include failure tests for missing cards/accounts, invalid status, invalid
  PIN, incompatible account class, insufficient balance, invalid delivery/design,
  and limits outside account-tier policy.
- [ ] Verify the tests fail because the methods are absent.
- [ ] Adapt the official methods and shared activation/PIN helpers.
- [ ] Run Task 5 and all earlier Banking tests.

### Task 6: User discoverable tools

**Files:**
- Create: `application/banking_golden_agent/services/discoverable_user_tools.py`
- Modify: `application/banking_golden_agent/services/banking_db_service.py`
- Create: `application/banking_golden_agent/tests/test_user_discoverable_tools.py`

**Interfaces:**
- Produces `UserDiscoverableTools` with `submit_cash_back_dispute_0589`,
  `get_referral_link`, `get_card_last_4_digits`, and `deposit_check_3847`.
- Produces BankingDBService `call_discoverable_user_tool(name, arguments)`;
  this service method is not registered in the Agent ToolRegistry.

- [ ] Write failing tests proving unknown user tools cannot be given, unexpected
  prefilled parameters are rejected, and calls require a prior give.
- [ ] Write failing success/failure tests for all four methods, including the
  resulting database changes and user-tool call bookkeeping.
- [ ] Implement give validation, argument merging, invocation, and the four real
  methods.
- [ ] Run Task 6 and all earlier Banking tests.

### Task 7: Metadata completeness and final verification

**Files:**
- Modify: `application/banking_golden_agent/services/discoverable_agent_tools.py`
- Modify: `application/banking_golden_agent/services/discoverable_user_tools.py`
- Modify: `application/banking_golden_agent/services/banking_db_service.py`
- Modify: `application/banking_golden_agent/services/__init__.py`
- Create: `application/banking_golden_agent/tests/test_discoverable_tool_completeness.py`

**Interfaces:**
- Guarantees the Agent registry contains exactly the official 44 names and the
  User registry exactly the official 4 names.
- Guarantees real signatures agree with `discoverable_tools.json`.

- [ ] Write a failing exact-set test for all 44+4 tools and a metadata/signature
  consistency test.
- [ ] Add any missing registrations and correct signature discrepancies until
  the exact-set tests pass.
- [ ] Run `rtk uv run pytest application/banking_golden_agent/tests`.
- [ ] Run existing Banking-related tests without modifying them.
- [ ] Run Ruff format check, Ruff lint, and Mypy for
  `application/banking_golden_agent/`.
- [ ] Run `rtk git diff --check` and inspect the final diff to confirm no path
  outside `application/banking_golden_agent/` changed during this implementation.
