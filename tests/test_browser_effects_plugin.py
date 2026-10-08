"""Registered browser consumer contracts; real stores and held-byte adapter."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import current_thread, Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from _browser_adapter_support import HostContext, host_session, request
from test_browser_effect_attempts import EffectAdapter, run_on_second_thread
from omh.plugin_bundle.omh import register
from omh.workflows import browser_lease_store
from omh.workflows.approval_receipts import build_approval_receipt
from omh.workflows.browser_adapter import digest
from omh.workflows.browser_effect_attempts_contract import approval_scope, OPERATIONS
from omh.plugin_bundle.omh.egress_attempt_receipts import AttemptStore
from omh.workflows.browser_effect_attempts_store import EffectBindingStore
from _module_patch import patch_modules


class PluginAdapter(EffectAdapter):
    def __init__(self, home):
        super().__init__()
        self.cap['unsupported'] = []
        self.cap['channels'].append('upload')
        self.home = home

    def resume(self, lease_id, preview_ref, attempt_id):
        self.engine = SimpleNamespace(attempts=AttemptStore(self.home), store=EffectBindingStore(self.home))
        return super().resume(lease_id, preview_ref, attempt_id)

    def release(self, lease_id, deadline):
        self.calls['release'] += 1
        self.pending = None
        return {'reaped': True}


class BrowserEffectsPluginTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(TemporaryDirectory())) / 'omh'
        self.host = self.stack.enter_context(host_session())
        self.event = SimpleNamespace(_approval_session_id=ContextVar('session', default='owner'),
            _approval_tool_call_id=ContextVar('event', default='event-1'))
        self.stack.enter_context(patch_modules({'tools.approval_context': self.event}))
        self.adapter = PluginAdapter(self.home)
        self.ctx = HostContext(self.home, self.adapter)
        self.approvals = {}
        self.ctx.browser_effect_approval = lambda identity, task, key: self.approvals.get((identity, task, key))
        self.ctx.get_config = lambda key, default=None: ({'enabled': True, 'effects_enabled': True,
            'omh_home': str(self.home)} if key == 'browser_adapter' else default)
        self.schemas = {}
        original = self.ctx.register_tool

        def registered(name, toolset, schema, handler, **kwargs):
            self.schemas[name] = schema
            return original(name, toolset, schema, handler, **kwargs)

        self.ctx.register_tool = registered
        register(self.ctx)

    def call(self, args, **kwargs):
        return json.loads(self.ctx.tools['omh_browser'](args, **kwargs))

    def acquire(self, actions=None):
        self.stack.enter_context(self.ctx.browser_task('task'))
        lease = self.call(request(actions=['read', 'click', 'upload'] if actions is None else actions))
        self.assertEqual(lease['status'], 'active', lease)
        page = lease['page']
        self.request = dict(operation='effect_preview', action='submit', lease_id=lease['lease_id'],
            tab_id='tab-1', revision=page['revision'], handle=page['elements'][0]['handle'],
            trace_revision=None, expected_postcondition='confirmation')
        return lease

    def preview(self, **updates):
        result = self.call({**self.request, **updates})
        self.assertEqual(result['status'], 'awaiting_approval', result)
        self.adapter.key = result['intent_digest']
        return result

    def approve(self, preview, **updates):
        receipt = build_approval_receipt(**approval_scope(preview['intent_digest'], preview['intent']),
            confirmation_ladder='operator_confirmation',
            decided_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
        receipt.update(updates)
        self.approvals[(('owner', 'principal', 'local-api', 'request'), 'task', preview['intent_digest'])] = receipt

    def execute(self, preview, **updates):
        return self.call(dict(operation='effect_execute', intent_digest=preview['intent_digest'], **updates))

    def test_registered_effect_schema_is_opt_in_and_closed(self):
        operations = self.schemas['omh_browser']['parameters']['properties']['operation']['enum']
        self.assertTrue({'effect_preview', 'effect_execute', 'effect_abort'} <= set(operations))
        self.assertFalse(self.ctx.checks['omh_browser']())
        self.assertFalse(self.home.exists())

    def test_all_operations_hold_before_approval(self):
        self.acquire()
        for operation in sorted(OPERATIONS):
            with self.subTest(operation=operation):
                preview = self.preview(action=operation)
                self.assertEqual(self.execute(preview)['status'], 'blocked')
                self.assertEqual(self.adapter.send_count, 0)
                self.assertFalse(AttemptStore(self.home).database_path.exists())

    def test_approved_registered_execution_replays_once_with_receipt(self):
        self.acquire()
        preview = self.preview()
        self.approve(preview)
        result = self.execute(preview)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.execute(preview), result)
        self.assertEqual(self.adapter.send_count, 1)
        self.assertEqual(EffectBindingStore(self.home).receipt(result['attempt_id'])['receipt_id'], result['receipt_id'])

    def test_read_only_lease_cannot_be_widened(self):
        self.acquire(['read'])
        result = self.call(self.request)
        self.assertEqual(result.get('reason'), 'action_out_of_scope', result)
        self.assertEqual(self.adapter.calls['preview'], 0)
        self.assertEqual(self.adapter.send_count, 0)

    def test_shared_cap_and_replay_do_not_use_separate_budgets(self):
        self.adapter.cap['limits']['actions'] = 2
        lease = self.acquire()
        preview = self.preview()
        self.assertEqual(self.call(dict(operation='observe', lease_id=lease['lease_id'], tab_id='tab-1'))['status'], 'observed')
        self.approve(preview)
        result = self.execute(preview)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.execute(preview), result)
        row = json.loads((self.home / 'runtime/browser/leases.json').read_text())['leases'][lease['lease_id']]
        self.assertEqual(row['action_count'], 2)
        self.assertEqual(self.call(dict(operation='observe', lease_id=lease['lease_id'], tab_id='tab-1'))['reason'], 'action_capped')

    def test_inert_work_exhausts_effect_budget_before_mutation(self):
        self.adapter.cap['limits']['actions'] = 1
        lease = self.acquire()
        preview = self.preview()
        self.approve(preview)
        self.call(dict(operation='observe', lease_id=lease['lease_id'], tab_id='tab-1'))
        self.assertEqual(self.execute(preview).get('reason'), 'action_capped')
        self.assertEqual(self.adapter.send_count, 0)

    def test_missing_or_forged_event_fails_closed(self):
        self.acquire()
        preview = self.preview()
        self.approve(preview)
        self.event._approval_tool_call_id.set('')
        self.assertEqual(self.execute(preview).get('reason'), 'host_event_required')
        self.assertEqual(self.execute(preview, event_id='forged').get('status'), 'blocked')
        self.assertEqual(self.adapter.send_count, 0)
        self.assertFalse(AttemptStore(self.home).database_path.exists())

    def test_same_host_event_cannot_switch_intent(self):
        self.acquire()
        first = self.preview()
        second = self.preview(action='publish')
        self.approve(first)
        self.approve(second)
        self.adapter.key = first['intent_digest']
        self.assertEqual(self.execute(first)['status'], 'succeeded')
        self.assertEqual(self.execute(second).get('reason'), 'event_binding_changed')
        self.assertEqual(self.adapter.send_count, 1)

    def test_current_identity_and_task_rechecked_at_approval(self):
        self.acquire()
        preview = self.preview()
        self.approve(preview)
        source = self.ctx.browser_effect_approval

        def revoke(identity, task, key):
            self.host._BROWSER_CONTROL_PRINCIPAL.set('foreign')
            return source(identity, task, key)

        self.ctx.browser_effect_approval = revoke
        self.assertEqual(self.execute(preview)['status'], 'blocked')
        self.assertEqual(self.adapter.send_count, 0)

    def test_changed_payload_state_and_trace_cannot_borrow_approval(self):
        self.acquire()
        preview = self.preview()
        self.approve(preview)
        changed = self.call({**self.request, 'trace_revision': digest('trace-2')})
        self.assertEqual(changed['status'], 'blocked')
        self.adapter.payload = b'changed'
        self.assertEqual(self.execute(preview)['status'], 'blocked')
        self.adapter.revision += 1
        self.assertEqual(self.execute(preview)['status'], 'blocked')
        self.assertEqual(self.adapter.send_count, 0)

    def test_unknown_and_abort_never_resend(self):
        self.acquire()
        preview = self.preview()
        self.approve(preview)
        self.adapter.mode = 'disconnect'
        result = self.execute(preview)
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['retry_allowed'])
        self.assertEqual(self.execute(preview), result)
        self.assertEqual(self.adapter.send_count, 1)

    def test_disabled_effects_keep_inert_schema_and_no_effect_io(self):
        self.ctx.get_config = lambda key, default=None: ({'enabled': True, 'omh_home': str(self.home)}
            if key == 'browser_adapter' else default)
        register(self.ctx)
        self.assertEqual(self.schemas['omh_browser']['parameters']['properties']['operation']['enum'],
                         ['acquire', 'observe', 'act', 'release'])
        with self.ctx.browser_task('task'):
            for operation in ('effect_preview', 'effect_execute', 'effect_abort'):
                self.assertEqual(self.call({'operation': operation})['status'], 'blocked')
        self.assertFalse(self.home.exists())
        self.assertFalse(self.adapter.calls)

    def test_denied_revoked_expired_approvals_and_argument_injection(self):
        self.acquire()
        preview = self.preview()
        for updates in ({'decision': 'denied'}, {'decision': 'revoked'},
                        {'decided_at': '2020-01-01T00:00:00Z'}):
            with self.subTest(updates=updates):
                self.approve(preview, **updates)
                self.assertEqual(self.execute(preview)['status'], 'blocked')
        self.approve(preview)
        for field in ('approved', 'owner', 'event_id', 'engine', 'approval_receipt', 'store_path'):
            self.assertEqual(self.execute(preview, **{field: True})['status'], 'blocked')
        self.assertEqual(self.adapter.send_count, 0)
        self.assertFalse(AttemptStore(self.home).database_path.exists())

    def test_unapproved_event_binding_survives_refusal(self):
        self.acquire()
        first = self.preview()
        self.assertEqual(self.execute(first)['status'], 'blocked')
        second = self.preview(action='publish')
        self.approve(second)
        self.assertEqual(self.execute(second).get('reason'), 'event_binding_changed')
        self.assertEqual(self.adapter.send_count, 0)

    def test_explicit_abort_and_release_remove_held_bytes(self):
        lease = self.acquire()
        preview = self.preview()
        result = self.call({'operation': 'effect_abort', 'intent_digest': preview['intent_digest']})
        self.assertEqual(result['status'], 'cancelled')
        self.approve(preview)
        self.assertEqual(self.execute(preview), result)
        self.assertIsNone(self.adapter.pending)
        self.assertEqual(self.call({'operation': 'release', 'lease_id': lease['lease_id']})['status'], 'released')

    def test_foreign_task_adapter_and_native_mutators_are_blocked(self):
        self.acquire()
        preview = self.preview()
        self.approve(preview)
        with self.ctx.browser_task('other-task'):
            self.assertEqual(self.execute(preview).get('reason'), 'foreign_task')
        self.adapter.adapter_version = 'changed'
        self.assertEqual(self.execute(preview).get('reason'), 'foreign_adapter')
        self.adapter.adapter_version = '1'
        for name in ('browser_click', 'browser_type', 'browser_upload', 'browser_eval'):
            self.assertEqual(self.ctx.hooks['pre_tool_call'][-1](tool_name=name)['action'], 'block')
        self.assertEqual(self.adapter.send_count, 0)

    def test_opaque_stale_foreign_and_unsupported_previews_do_not_reach_host(self):
        self.acquire()
        calls = self.adapter.calls.copy()
        for changes in ({'action': 'eval'}, {'action': 'fetch'}, {'revision': 100},
                        {'handle': digest('absent')}, {'tab_id': 'foreign'}):
            with self.subTest(changes=changes):
                self.assertEqual(self.call({**self.request, **changes})['status'], 'blocked')
                self.assertEqual(self.adapter.calls, calls)
        self.assertFalse(AttemptStore(self.home).database_path.exists())

    def test_missing_interception_capability_fails_before_preview(self):
        self.adapter.cap['mutation_interception'] = 'none'
        self.acquire()
        calls = self.adapter.calls.copy()
        self.assertEqual(self.call(self.request).get('reason'), 'adapter_cannot_intercept')
        self.assertEqual(self.adapter.calls, calls)

    def test_foreign_observability_owner_and_forged_kwargs_cannot_execute(self):
        self.acquire()
        preview = self.preview()
        self.approve(preview)
        self.event._approval_session_id.set('foreign')
        self.assertEqual(self.call({'operation': 'effect_execute', 'intent_digest': preview['intent_digest']},
            event_id='event-1', tool_call_id='event-1').get('reason'), 'host_event_required')
        self.assertEqual(self.adapter.send_count, 0)

    def test_new_request_does_not_use_cached_approval_identity(self):
        self.acquire()
        preview = self.preview()
        self.approve(preview)
        self.host._SESSION_MESSAGE_ID.set('request-2')
        with self.ctx.browser_task('task'):
            self.assertEqual(self.execute(preview).get('reason'), 'approval_absent')
        self.assertEqual(self.adapter.send_count, 0)

    def test_capped_event_cannot_switch_to_a_different_lease(self):
        self.adapter.cap['limits']['actions'] = 1
        first_lease = self.acquire()
        first = self.preview()
        self.call(dict(operation='observe', lease_id=first_lease['lease_id'], tab_id='tab-1'))
        self.assertEqual(self.execute(first).get('reason'), 'action_capped')
        self.acquire(['click'])
        second = self.preview()
        self.approve(second)
        self.assertEqual(self.execute(second).get('reason'), 'event_binding_changed')
        self.assertEqual(self.adapter.send_count, 0)

    def test_re_registration_replay_does_not_start_or_charge_again(self):
        lease = self.acquire()
        preview = self.preview()
        self.approve(preview)
        result = self.execute(preview)
        self.assertEqual(result['status'], 'succeeded')
        calls = self.adapter.calls.copy()
        register(self.ctx)
        with self.ctx.browser_task('task'):
            self.assertEqual(self.execute(preview), result)
            self.assertEqual(self.adapter.calls, calls)
            row = json.loads((self.home / 'runtime/browser/leases.json').read_text())['leases'][lease['lease_id']]
            self.assertEqual(row['action_count'], 1)

    def test_concurrent_registered_replay_waits_for_the_shared_attempt(self):
        """A replay contending for the lease boundary mid-resume returns the shared terminal result.

        The registered path serializes every effect call on the lease store's lock
        (`effect_boundary`), so the replay must wait for the first attempt to finish and
        then read its stored result. That wait is bounded by the store's own lock budget;
        how long the first attempt's remaining work takes is runner load, not contract,
        so only the replay's deadline is removed here. The bound itself is pinned by the
        next test. The replay is started from the mid-resume seam and the first send is
        held until the replay is contending, so the interleaving is reached by
        construction, and both outcomes are keyed by caller, never by completion order.
        """
        self.acquire()
        preview = self.preview()
        self.approve(preview)
        replay_context = copy_context()
        contending = Event()
        outcome = {}

        def replay():
            try:
                outcome['replay'] = replay_context.run(self.execute, preview)
            finally:
                contending.set()  # A replay that never reaches the lock still releases the first.

        replay_thread = Thread(target=replay)
        real_lock = browser_lease_store.file_lock

        @contextmanager
        def replay_lock_without_deadline(path, **kwargs):
            if current_thread() is replay_thread:
                kwargs['timeout_seconds'] = float('inf')
                contending.set()
            with real_lock(path, **kwargs) as held:
                yield held

        def start_replay_mid_resume():
            self.adapter.during_resume = None
            replay_thread.start()
            contending.wait()  # No deadline: set by the replay on contention or on exit.

        self.adapter.during_resume = start_replay_mid_resume
        with patch.object(browser_lease_store, 'file_lock', replay_lock_without_deadline):
            try:
                outcome['first'] = self.execute(preview)
            finally:
                if replay_thread.ident is not None:
                    replay_thread.join()
        self.assertEqual(outcome['first']['status'], 'succeeded', outcome['first'])
        self.assertEqual(outcome['replay'], outcome['first'])
        self.assertEqual(self.adapter.send_count, 1)
        attempts = [row for row in AttemptStore(self.home).public_rows(limit=200) if row['row_type'] == 'attempt']
        self.assertEqual([row['attempt_id'] for row in attempts], [outcome['first']['attempt_id']])

    def test_registered_replay_past_the_lease_lock_budget_is_closed_unknown(self):
        """A replay whose bounded boundary wait expires fails closed and opens no attempt.

        The replay is run from inside `resume` and joined there, so the first call holds
        the lease lock for the replay's whole budget: expiry is reached by construction.
        The documented answer is a closed `unknown` that forbids a retry, before any send;
        the first attempt still completes once, and a later replay reads its result.
        """
        self.acquire()
        preview = self.preview()
        self.approve(preview)
        replay_context = copy_context()
        replays = []

        def replay_mid_resume():
            self.adapter.during_resume = None
            replays.append(dict(sends_before=self.adapter.send_count,
                                result=run_on_second_thread(lambda: replay_context.run(self.execute, preview))))

        self.adapter.during_resume = replay_mid_resume
        result = self.execute(preview)
        self.assertEqual(len(replays), 1)
        self.assertEqual(replays[0]['sends_before'], 0)
        self.assertEqual(replays[0]['result'],
                         {'status': 'unknown', 'reason': 'effect_boundary_unknown', 'retry_allowed': False})
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.execute(preview), result)
        self.assertEqual(self.adapter.send_count, 1)
        attempts = [row for row in AttemptStore(self.home).public_rows(limit=200) if row['row_type'] == 'attempt']
        self.assertEqual([row['attempt_id'] for row in attempts], [result['attempt_id']])

    def test_copied_context_and_foreign_task_do_not_retain_authority(self):
        with self.ctx.browser_task('task'):
            copied = copy_context()
        self.assertFalse(copied.run(self.ctx.checks['omh_browser']))
        self.assertEqual(copied.run(self.call, dict(operation='effect_abort', intent_digest=digest('x')))['reason'], 'admission_required')


if __name__ == '__main__':
    unittest.main()
