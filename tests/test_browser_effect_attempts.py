"""Last-mile behavior contracts, independent of unsettled plugin admission."""
from copy import deepcopy
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import unittest
from threading import Event, Thread
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from _browser_adapter_support import Adapter, request
from omh.workflows.browser_lease_store import BrowserLeaseStore, BrowserSessionManager
from omh.workflows.external_effect_receipts import validate_external_effect_receipt
from omh.workflows.approval_receipts import APPROVAL_TTL_SECONDS, build_approval_receipt
from omh.workflows.browser_adapter import BrowserContractError, digest
from omh.workflows.browser_effect_attempts import BrowserEffectEngine
from omh.workflows.browser_effect_attempts_contract import approval_scope, classify, OPERATIONS
from omh.workflows.external_effect_receipts import receipt_satisfies_success_claim


def _outcome(call):
    try:
        return ('returned', call())
    except BaseException as exc:  # Re-raised by the joining thread; see run_on_second_thread.
        return ('raised', exc)


def run_on_second_thread(call):
    """Run `call` on another thread and hand its outcome back, joining without a deadline.

    A deadline here would be a wall clock masquerading as a contract: the thread's
    only work is one `execute`, whose sole blocking primitive is SQLite's own busy
    budget, so it always terminates and `join()` cannot expire. A loaded shared
    runner therefore delays this helper instead of failing it, and an exception is
    re-raised in the caller rather than dying unread inside the thread.
    """
    outcome = []
    thread = Thread(target=lambda: outcome.append(_outcome(call)))
    thread.start()
    thread.join()
    disposition, payload = outcome[0]
    if disposition == 'raised':
        raise payload
    return payload


class EffectAdapter:
    """Stateful held-byte surface; real SQLite is checked at the send boundary."""
    adapter_id = Adapter.adapter_id
    adapter_version = Adapter.adapter_version
    key: str = ''
    def __init__(self):
        base = Adapter()
        self.calls, self.cap = base.calls, base.cap
        self.elements, self.revision = base.elements, base.revision
        self.engine: BrowserEffectEngine | None = None
        self.cap['mutation_interception'] = 'last_mile'
        self.payload = b'PRIVATE credential form content'
        self.send_count = 0
        self.mode = 'success'
        self.pending = None
        self.confirmation = None
        self.resume_entered = Event()
        self.during_resume = None

    def preview(self, lease_id, handle, operation):
        self.calls['preview'] += 1
        pending = dict(schema_version='browser_pending_effect/v1', held=True, method='POST',
                       origin='http://localhost', payload_bytes=len(self.payload),
                       payload_digest=__import__('hashlib').sha256(self.payload).hexdigest(),
                       payload_shape='form', target_role='button',
                       target_name_digest=digest(self.elements[0]['name']),
                       opens_new_tab=False, redirected=False,
                       preview_ref=digest([lease_id, handle, operation, self.payload.hex()]))
        self.pending = pending
        return deepcopy(pending)

    def resume(self, lease_id, preview_ref, attempt_id):
        assert self.engine is not None
        rows = self.engine.attempts.public_rows(limit=200)
        assert any(r['attempt_id'] == attempt_id and r['row_type'] == 'attempt' for r in rows)
        assert self.engine.store.result(self.key)['attempt_id'] == attempt_id
        self.resume_entered.set()
        # Mid-resume seam: the durable attempt and its unknown binding are committed,
        # and this send has not yet been permitted. A caller hooking here reaches that
        # window by construction rather than by winning a race against a barrier.
        if self.during_resume is not None:
            self.during_resume()
        self.send_count += 1
        if self.mode in {'timeout', 'disconnect', 'crash'}:
            raise {'timeout': TimeoutError, 'disconnect': ConnectionError, 'crash': RuntimeError}[self.mode]('PRIVATE ERROR')
        if self.mode == 'success':
            self.revision += 1
            self.confirmation = dict(schema_version='browser_effect_readback/v1', observed=True,
                                     attempt_id=attempt_id, preview_ref=preview_ref,
                                     postcondition='confirmation', object_digest=digest('object-1'))
        return {'status': 'succeeded', 'text': 'PRIVATE untrusted delivery claim'}

    def observe(self, lease_id, tab_id, deadline):
        self.calls['observe'] += 1
        raw = {'url':'http://localhost/page?secret=PRIVATE', 'revision':self.revision,
               'readback':True, 'elements':deepcopy(self.elements), 'dom':'PRIVATE DOM',
               'cookies':'PRIVATE COOKIE'}
        if self.confirmation:
            return {**raw, 'effect_readback': self.confirmation}
        return raw

    def abort(self, lease_id, preview_ref):
        self.calls['abort'] += 1
        self.pending = None
        return {'aborted': True}

    def capabilities(self):
        self.calls['capabilities'] += 1
        return deepcopy(self.cap)

    def start(self, lease_id, scope, deadline):
        self.calls['start'] += 1
        return {'tabs':['tab-1']}


class BrowserEffectAttemptsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.adapter = EffectAdapter()
        self.now = 1800000000.0
        self.manager = BrowserSessionManager(BrowserLeaseStore(self.home), self.adapter, clock=lambda: self.now)
        self.lease = self.manager.acquire('owner', request(actions=['read', 'click', 'upload']))
        self.approvals = {}
        self.engine = BrowserEffectEngine(self.home, self.adapter, lambda owner, key: deepcopy(self.lease),
                                          self.approvals.get, clock=lambda: self.now)
        self.adapter.engine = self.engine
        page = self.lease['page']
        self.request = dict(lease_id=self.lease['lease_id'], tab_id=page['tab_id'],
                            revision=page['revision'], handle=page['elements'][0]['handle'],
                            operation='submit', trace_revision=None, expected_postcondition='confirmation')

    def prepare(self, **updates):
        preview = self.engine.preview('owner', {**self.request, **updates})
        self.adapter.key = preview['intent_digest']
        return preview

    def approve(self, preview, **updates):
        receipt = build_approval_receipt(**approval_scope(preview['intent_digest'], preview['intent']),
                                        confirmation_ladder='operator_confirmation',
                                        decided_at=datetime.fromtimestamp(self.now, timezone.utc).isoformat().replace('+00:00', 'Z'))
        receipt.update(updates)
        self.approvals[preview['intent_digest']] = receipt

    def test_preview_holds_without_effect(self):
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                preview = self.prepare(operation=operation)
                self.assertEqual(preview['status'], 'awaiting_approval')
                self.assertEqual(self.adapter.send_count, 0)
                self.assertFalse(self.engine.attempts.database_path.exists())

    def test_durable_attempt_and_readback_receipt_once_across_replay(self):
        preview = self.prepare()
        self.approve(preview)
        result = self.engine.execute('owner', preview['intent_digest'], 'event-1')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.engine.execute('owner', preview['intent_digest'], 'event-1'), result)
        self.assertEqual(self.engine.execute('owner', preview['intent_digest'], 'event-2'), result)
        receipt = self.engine.store.receipt(result['attempt_id'])
        self.assertEqual(validate_external_effect_receipt(receipt), [])
        self.assertEqual(receipt['receipt_id'], result['receipt_id'])
        self.assertEqual(receipt['effect_id'], preview['intent_digest'])
        self.assertEqual(self.adapter.send_count, 1)
        for kind in ('review', 'ci', 'merge'):
            self.assertFalse(receipt_satisfies_success_claim(receipt, kind=kind, run_id='run-1'))

    def test_missing_denied_expired_and_model_approval_fail_before_attempt(self):
        preview = self.prepare()
        for updates in ({'decision': 'denied'}, {'decided_at': '2020-01-01T00:00:00Z'},
                        {'approved': True}, {'owner': digest('foreign')}, {'scope_ref': digest('foreign')},
                        {'safety_profile_revision': digest('foreign')}, {'decision': 'revoked'}):
            with self.subTest(updates=updates):
                self.approve(preview, **updates)
                with self.assertRaises(BrowserContractError):
                    self.engine.execute('owner', preview['intent_digest'], 'event')
        self.approvals.clear()
        with self.assertRaises(BrowserContractError):
            self.engine.execute('owner', preview['intent_digest'], 'event')
        self.assertEqual(self.adapter.send_count, 0)
        self.assertFalse(self.engine.attempts.database_path.exists())

    def test_opaque_and_unsupported_paths_are_refused_locally(self):
        calls = self.adapter.calls.copy()
        for operation in ('eval', 'script', 'GET', 'click', 'fetch', 'navigate', 'service_worker', 'unknown'):
            self.assertEqual(classify(operation), 'blocked')
            with self.assertRaises(BrowserContractError):
                self.prepare(operation=operation)
        self.assertEqual(self.adapter.calls, calls)
        self.lease['capabilities']['mutation_interception'] = 'none'
        with self.assertRaises(BrowserContractError):
            self.prepare()
        self.assertEqual(self.adapter.calls, calls)

    def _expiring_preview(self):
        preview = self.prepare()
        # The real approval window remains unchanged; this grant has one second left.
        decided = self.now - APPROVAL_TTL_SECONDS + 1
        self.approve(preview, decided_at=datetime.fromtimestamp(decided, timezone.utc).isoformat().replace('+00:00', 'Z'))
        return preview

    def _advance_authorization(self, change, key):
        if change == 'approval':
            self.now += 3
        elif change == 'lease':
            self.now = self.lease['expires_at']
        elif change == 'capability':
            self.now = self.lease['capabilities']['expires_at']
        elif change == 'released':
            self.lease['status'] = 'released'
        elif change == 'context':
            self.lease['pages']['tab-1']['revision'] += 1
        else:
            self.approvals[key]['decision'] = change

    def _assert_callback_refusal(self, change):
        preview = self._expiring_preview()
        if change != 'approval':
            self.approve(preview)
        key = preview['intent_digest']

        def approval_source(intent_digest):
            self._advance_authorization(change, intent_digest)
            return self.approvals[intent_digest]

        self.engine.approval_source = approval_source
        try:
            result = self.engine.execute('owner', key, 'callback-expiry')
        except BrowserContractError:
            result = {'status': 'blocked'}
        # Separate subtests expose state, dispatch, durable-attempt and cleanup REDs.
        for field, actual, expected in (
                ('status', result['status'], 'blocked'),
                ('resume', self.adapter.resume_entered.is_set(), False),
                ('attempts', self.engine.attempts.public_rows(limit=200), []),
                ('abort', self.adapter.calls['abort'], 1),
                ('held', self.adapter.pending, None)):
            with self.subTest(field=field):
                self.assertEqual(actual, expected)

    def test_expiry_approval_callback_aborts_before_attempt(self):
        self._assert_callback_refusal('approval')

    def test_expiry_lease_callback_blocks_before_attempt(self):
        self._assert_callback_refusal('lease')

    def test_expiry_capability_callback_blocks_before_attempt(self):
        self._assert_callback_refusal('capability')

    def test_expiry_callback_rechecks_released_lease(self):
        self._assert_callback_refusal('released')

    def test_expiry_callback_rechecks_current_context(self):
        self._assert_callback_refusal('context')

    def _assert_durable_refusal(self, change, seam):
        preview = self._expiring_preview()
        if change != 'approval':
            self.approve(preview)
        key = preview['intent_digest']
        open_attempt = self.engine.attempts.open_attempt
        transaction = self.engine.store.transaction
        committed = Event()

        def delayed_attempt(**kwargs):
            result = open_attempt(**kwargs)  # The real FULL commit and fsync run.
            self._advance_authorization(change, key)
            return result

        @contextmanager
        def delayed_binding():
            with transaction() as db:
                yield db
            if not committed.is_set():
                committed.set()
                self._advance_authorization(change, key)  # After the real browser commit.

        target, method, replacement = ((self.engine.attempts, 'open_attempt', delayed_attempt)
            if seam == 'attempt' else (self.engine.store, 'transaction', delayed_binding))
        with patch.object(target, method, replacement):
            result = self.engine.execute('owner', key, 'durable-expiry')
        rows = self.engine.attempts.public_rows(limit=200)
        for field, actual, expected in (
                ('status', result['status'], 'unknown'),
                ('resume', self.adapter.resume_entered.is_set(), False),
                ('send_count', self.adapter.send_count, 0),
                ('receipt_id', result['receipt_id'], ''),
                ('receipt', self.engine.store.receipt(result['attempt_id']), None),
                ('retry_allowed', result['retry_allowed'], False),
                ('attempt_count', sum(row['row_type'] == 'attempt' for row in rows), 1),
                ('terminal', [row['terminal_state'] for row in rows if row['row_type'] == 'terminal'], ['unknown']),
                ('abort', self.adapter.calls['abort'], 1),
                ('held', self.adapter.pending, None)):
            with self.subTest(field=field):
                self.assertEqual(actual, expected)
        # A new grant and a fresh engine cannot turn the reserved attempt into a retry.
        self.approve(preview)
        reopened = BrowserEffectEngine(self.home, self.adapter, self.engine.lease_source,
                                       self.approvals.get, clock=lambda: self.now)
        calls = self.adapter.calls.copy()
        self.assertEqual(reopened.execute('owner', key, 'durable-expiry'), result)
        self.assertEqual(reopened.execute('owner', key, 'other-event'), result)
        self.assertEqual(self.adapter.calls, calls)
        self.assertEqual(reopened.attempts.public_rows(limit=200), rows)

    def test_expiry_approval_after_durable_attempt(self):
        self._assert_durable_refusal('approval', 'attempt')

    def test_expiry_approval_after_durable_binding(self):
        self._assert_durable_refusal('approval', 'binding')

    def test_expiry_lease_after_durable_binding(self):
        self._assert_durable_refusal('lease', 'binding')

    def test_expiry_capability_after_durable_attempt(self):
        self._assert_durable_refusal('capability', 'attempt')

    def test_expiry_durable_delay_rechecks_approval_revocation(self):
        self._assert_durable_refusal('revoked', 'binding')

    def test_expiry_durable_delay_rechecks_approval_denial(self):
        self._assert_durable_refusal('denied', 'attempt')

    def test_expiry_durable_delay_rechecks_released_lease(self):
        self._assert_durable_refusal('released', 'binding')

    def test_expiry_durable_delay_rechecks_current_context(self):
        self._assert_durable_refusal('context', 'attempt')

    def test_stale_and_ambiguous_handles_fail_before_preview(self):
        for updates in ({'revision': 2}, {'handle': digest('missing')}):
            with self.assertRaises(BrowserContractError):
                self.prepare(**updates)
        self.lease['pages']['tab-1']['elements'].append(deepcopy(self.lease['pages']['tab-1']['elements'][0]))
        with self.assertRaises(BrowserContractError):
            self.prepare()
        self.assertEqual(self.adapter.calls['preview'], 0)

    def test_live_state_and_payload_drift_invalidate_approval(self):
        preview = self.prepare()
        self.approve(preview)
        self.adapter.revision += 1
        with self.assertRaises(BrowserContractError):
            self.engine.execute('owner', preview['intent_digest'], 'event')
        self.adapter.revision -= 1
        self.adapter.payload = b'changed private payload'
        with self.assertRaises(BrowserContractError):
            self.engine.execute('owner', preview['intent_digest'], 'event')
        self.assertEqual(self.adapter.send_count, 0)

    def test_changed_binding_dimensions_cannot_borrow_approval(self):
        preview = self.prepare()
        self.approve(preview)
        for updates in ({'lease_id': digest('other')}, {'tab_id': 'tab-2'}, {'revision': 2},
                        {'handle': digest('other')}, {'operation': 'publish'},
                        {'trace_revision': digest('trace-2')}):
            with self.subTest(updates=updates):
                try:
                    changed = self.prepare(**updates)
                except BrowserContractError:
                    continue
                with self.assertRaises(BrowserContractError):
                    self.engine.execute('owner', changed['intent_digest'], 'event')
        self.assertEqual(self.adapter.send_count, 0)

    def test_event_identity_cannot_change_intent(self):
        first = self.prepare()
        second = self.prepare(operation='publish')
        self.approve(first)
        self.approve(second)
        self.adapter.key = first['intent_digest']
        self.engine.execute('owner', first['intent_digest'], 'event')
        with self.assertRaises(BrowserContractError):
            self.engine.execute('owner', second['intent_digest'], 'event')
        self.assertEqual(self.adapter.send_count, 1)

    def test_unknown_outcomes_never_resend_or_claim_success(self):
        for mode, operation in zip(('timeout', 'disconnect', 'crash', 'ambiguous'),
                                   ('submit', 'send', 'publish', 'payment')):
            with self.subTest(mode=mode):
                self.adapter.mode = mode
                preview = self.prepare(operation=operation, trace_revision=digest(mode))
                self.approve(preview)
                before = self.adapter.send_count
                result = self.engine.execute('owner', preview['intent_digest'], mode)
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(self.engine.execute('owner', preview['intent_digest'], mode), result)
                self.assertIsNone(self.engine.store.receipt(result['attempt_id']))
                self.assertEqual(self.adapter.send_count - before, 1)

    def test_read_only_and_disabled_touch_no_effect_store_or_host(self):
        calls = self.adapter.calls.copy()
        self.assertEqual(self.engine.preview('owner', {'operation': 'read'}), {'status': 'read_only'})
        self.engine.enabled = False
        self.assertEqual(self.engine.preview('owner', self.request), {'status': 'disabled'})
        self.assertEqual(self.engine.execute('owner', 'invalid', 'event'), {'status': 'disabled'})
        self.assertFalse(self.engine.store.path.exists())
        self.assertFalse(self.engine.attempts.database_path.exists())
        self.assertEqual(self.adapter.calls, calls)

    def test_disabled_abort_returns_without_creating_storage(self):
        self.engine.enabled = False
        calls = self.adapter.calls.copy()
        try:
            result = self.engine.abort('owner', digest('absent-intent'))
        except BrowserContractError as error:
            self.assertFalse(self.engine.store.path.exists(), 'Disabled abort created browser-effect storage')
            self.fail(f'Disabled abort must return before intent lookup: {error}')
        self.assertEqual(result, {'status': 'disabled'})
        self.assertFalse(self.engine.store.path.exists())
        self.assertFalse(self.engine.attempts.database_path.exists())
        self.assertEqual(self.adapter.calls, calls)

    def test_indexed_bounded_redacted_stores(self):
        preview = self.prepare()
        self.approve(preview)
        self.engine.execute('owner', preview['intent_digest'], 'event')
        with closing(sqlite3.connect(self.engine.store.path)) as db:
            for table, column, value in (('bindings', 'digest', preview['intent_digest']),
                                         ('events', 'event_ref', digest('event')),
                                         ('receipts', 'attempt_id', 'attempt')):
                plan = db.execute(f'EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE {column}=?', (value,)).fetchall()
                self.assertTrue(all('SEARCH' in row[3] for row in plan))
            content = json.dumps(db.execute('SELECT * FROM bindings').fetchall())
            content += json.dumps(db.execute('SELECT * FROM receipts').fetchall())
        content += json.dumps(self.engine.attempts.public_rows(limit=200))
        for raw in ('PRIVATE', 'credential form content', 'tab-1', 'secret=', 'cookies', 'dom', 'headers'):
            self.assertNotIn(raw, content)

    def test_concurrent_replay_returns_unknown_then_stable_result(self):
        """A replay landing mid-resume is forced by the call graph, never raced on a clock.

        The engine's guard is durable state, not thread identity: `execute` commits the
        unknown binding row before `_resume` permits the adapter to send. So the second
        `execute` is issued from inside `resume`, on its own thread, while the first is
        suspended on that stack -- the same interleaving the old two-event barrier tried
        to reach by timed rendezvous, now reached by construction. Nothing waits on a
        deadline, so a loaded runner cannot report this contract as broken, and no worker
        can outlive the test method holding the temporary directory's SQLite handle open.
        """
        preview = self.prepare()
        self.approve(preview)
        replays = []

        def replay_mid_resume():
            self.adapter.during_resume = None  # One-shot: a second resume stays countable.
            before = dict(self.adapter.calls)
            replays.append(dict(sends_before=self.adapter.send_count, host_calls_before=before,
                                result=run_on_second_thread(lambda: self.engine.execute(
                                    'owner', preview['intent_digest'], 'event')),
                                host_calls_after=dict(self.adapter.calls)))

        self.adapter.during_resume = replay_mid_resume
        result = self.engine.execute('owner', preview['intent_digest'], 'event')
        self.assertEqual(len(replays), 1)
        self.assertEqual(replays[0]['sends_before'], 0)
        self.assertEqual(replays[0]['result']['status'], 'unknown')
        self.assertEqual(replays[0]['result']['attempt_id'], result['attempt_id'])
        # A replay never reobserves, reapproves or resends: it reads the durable row only.
        self.assertEqual(replays[0]['host_calls_after'], replays[0]['host_calls_before'])
        self.assertEqual(self.engine.execute('owner', preview['intent_digest'], 'event'), result)
        self.assertEqual(self.adapter.send_count, 1)

    def test_abort_cannot_be_executed(self):
        """An aborted held intent cannot inherit a previously granted approval."""
        preview = self.prepare()
        self.approve(preview)
        result = self.engine.abort('owner', preview['intent_digest'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.engine.execute('owner', preview['intent_digest'], 'event'), result)
        self.assertEqual(self.adapter.send_count, 0)

    def test_browser_receipt_vocabulary(self):
        from omh.workflows.external_effect_receipts import build_external_effect_receipt
        record = build_external_effect_receipt(
            effect_id='effect-1', action='message_sent', acting_surface='adapter_quality_delivery',
            observed_result='succeeded', external_ref='object-1')
        record.update(action='browser_mutation', target_class='endpoint',
                      acting_surface='browser_adapter_readback')
        self.assertEqual(validate_external_effect_receipt(record), [])

    def test_existing_receipt_reader_observes_the_linked_receipt(self):
        from omh.system.paths import OmhPaths
        from omh.workflows.external_effect_receipts import read_external_effect_receipts
        preview = self.prepare()
        self.approve(preview)
        result = self.engine.execute('owner', preview['intent_digest'], 'event')
        receipts = read_external_effect_receipts(OmhPaths(self.home, self.home), effect_id=preview['intent_digest'])
        self.assertEqual(receipts, [self.engine.store.receipt(result['attempt_id'])])

    def test_crash_after_durable_reservation_reopens_unknown_without_resume(self):
        preview = self.prepare()
        self.approve(preview)
        with patch.object(self.engine, '_resume', side_effect=SystemExit('simulated host exit')):
            with self.assertRaises(SystemExit):
                self.engine.execute('owner', preview['intent_digest'], 'event')
        reopened = BrowserEffectEngine(self.home, self.adapter, self.engine.lease_source,
                                       self.approvals.get, clock=lambda: self.now)
        result = reopened.execute('owner', preview['intent_digest'], 'event')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.adapter.send_count, 0)
        self.assertEqual(len(reopened.attempts.public_rows(limit=200)), 1)

    def test_uncertain_generic_directory_fsync_never_resends(self):
        from omh.plugin_bundle.omh.egress_attempt_receipts import AttemptStoreError
        preview = self.prepare()
        self.approve(preview)
        with patch('omh.plugin_bundle.omh.egress_attempt_receipts._fsync_parent_directory',
                   side_effect=OSError('fault after SQLite FULL commit')):
            with self.assertRaises(AttemptStoreError):
                self.engine.execute('owner', preview['intent_digest'], 'event')
        result = self.engine.execute('owner', preview['intent_digest'], 'event')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.adapter.send_count, 0)
        self.assertEqual(len(self.engine.attempts.public_rows(limit=200)), 1)

    def test_preview_metadata_rejects_redirect_origin_opaque_and_raw_fields(self):
        pending = self.adapter.preview(self.lease['lease_id'], self.request['handle'], 'submit')
        for updates in ({'origin':'https://other.invalid'}, {'opens_new_tab':True}, {'redirected':True},
                        {'held':False}, {'payload_shape':'opaque'}, {'payload_bytes':True},
                        {'payload_digest':'not-a-hash'}, {'target_name_digest':digest('other')},
                        {'body':'PRIVATE credential'}, {'headers':{'cookie':'PRIVATE'}}):
            with self.subTest(updates=updates):
                with patch.object(self.adapter, 'preview', return_value={**pending, **updates}):
                    with self.assertRaises(BrowserContractError):
                        self.prepare()
        self.assertFalse(self.engine.store.path.exists())
        self.assertEqual(self.adapter.send_count, 0)

    def test_binding_capacity_refuses_without_eviction_or_send(self):
        with patch('omh.workflows.browser_effect_attempts_store.MAX_BINDINGS', 1):
            preview = self.prepare()
            with self.assertRaises(BrowserContractError):
                self.prepare(operation='send')
            self.approve(preview)
            result = self.engine.execute('owner', preview['intent_digest'], 'event')
            self.assertEqual(result['status'], 'succeeded')
            with self.assertRaises(BrowserContractError):
                self.engine.execute('owner', preview['intent_digest'], 'other-event')
            self.assertEqual(self.engine.execute('owner', preview['intent_digest'], 'event'), result)
        self.assertEqual(self.adapter.send_count, 1)

    def test_host_launch_failure_removes_its_temporary_home(self):
        from tools.browser_last_mile_adapter import LocalLastMileAdapter
        adapter = LocalLastMileAdapter(Path('unused'), Path('unused'), 'submit')
        with patch('tools.browser_last_mile_adapter.subprocess.Popen', side_effect=OSError('missing node')):
            with self.assertRaises(OSError):
                adapter.__enter__()
        assert adapter.home is not None
        self.assertFalse(Path(adapter.home.name).exists())
