# Blind evaluation version binding

The public implementation is `tests/fixtures/agent_acceptance/blind_binding.py`.
It is a binding and export interface, not a replacement grader or a completed
private executor. The release remains pending until that executor is independently
reviewed and connected to actual dispatch collectors. Its three synthetic tests
do not open the private suite and do not count as any acceptance trial.

## Independent evaluator procedure

1. After candidate, reference, policy, controllers and public grading are frozen,
   assign an evaluator that did not author the product or these controllers.
   It alone opens the six private variants. The product author must not inspect
   private prompts, expectations, raw runs or case-level failures to tune code.
   Earlier filename/metadata exposure is recorded; complete prior non-exposure
   is not claimed. Process separation alone is not an independent review.
2. That evaluator contributes two manifest artifacts: `blind_evaluator`, its
   reviewed executor implementation, and `blind_suite_commitment`, a JSON object
   containing only `suite_sha256`, `cases: 6`, and `repetitions: 3`. The evaluator
   computes and verifies the actual private-suite digest. The manifest's normal
   snapshot and independent review rules bind both artifacts. An interface
   descriptor alone cannot start the blind run; `begin()` rejects missing ones.
3. Call `begin()` with the activated release and an absent private run directory
   before creating tasks or dispatching. Keep the returned file binding. Use its
   nonce to bind newly collected tasks/evidence; never import development runs.
   Immediately before every actual model/tool dispatch, call `check()` and the
   same release's role/permission/budget admission checks. The private executor
   must capture actual operation identities, source/consumer identities and
   all model phases, including probes. The binding helper does not install those
   hooks and does not itself prove that dispatch coverage is complete.
4. Preserve the original six variants, three repetitions each, original scope,
   interventions, dependencies and expected results. Apply each variant's
   original base-case role contract. Do not map a hidden variant into a public
   trial slot or count its parent case instead. Store immutable raw evidence and
   private independent semantic review under the fresh private run directory.
5. Inside the independent evaluator, use unchanged `validate.grade()` and
   `validate.aggregate(..., "held_out")` on the actual private evidence, plus
   the frozen release's actual role/dispatch audits. All 18 candidate slots must
   pass; missing trials, role audit, dependencies or coverage remain pending.
   Do not call the CLI merely to validate public inputs: its `check_inputs()`
   also opens the private suite.
6. Call `public_result()` inside that evaluator, passing the original aggregate,
   role audit status and hash binding of its private evidence index. Export only
   the returned whitelist: release/suite/evidence hashes, total counts and
   overall status. The helper rechecks frozen bindings and retains the fixed
   denominator. It does not authenticate arbitrary caller-supplied grades;
   correctness still depends on the reviewed evaluator and original evidence.

The implementation deliberately returns no question text, case IDs, detailed
assertions, prompts, raw response, private paths or repair hints. Evidence remains
available to the independent reviewer. A failed blind result is retained; it
must not be used for repeated private-case tuning or be replaced by a new sample
under the old frozen release. Any changed product requires a new snapshot and
an explicit evaluation decision, not silent promotion of prior trials.

Current status: public binding interface tested with invented data; private
executor, actual private dispatch and all 18 blind trials remain pending.
