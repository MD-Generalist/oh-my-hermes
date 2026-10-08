"""Public dispatcher session scenarios using real local protocol processes only."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
import json
import re
from five_issue_process_fixture import fixture_executable_transport, fixture_system_environment, write_fixture_executable
import os
from uuid import NAMESPACE_URL, uuid5
from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _local_package import load_local_package
load_local_package()
from omh.coding.fanout import build_fanout_contract
from omh.coding.fanout_artifacts import write_fanout_contract, fanout_run_journal_path
from omh.coding.fanout_dispatch import build_dispatch_argv, dispatch_fanout
from omh.coding.fanout_status import project_fanout_status
from omh.coding.fanout_journal import read_fanout_run_journal
from omh.system.paths import OmhPaths
from omh.wrapper.executor_sessions import build_fanout_session_followup
from _cli_harness import run_cli
from _fanout_host_fence import host_cannot_fence_fanout
from . import CaseResult, JsonValue


decode: Callable[[str], JsonValue] = json.loads


def record(value: object) -> dict[str, JsonValue]:
    parsed = decode(json.dumps(value))
    assert isinstance(parsed, dict)
    return parsed


def rows_of(value: object) -> list[dict[str, JsonValue]]:
    units = record(value)['units']
    assert isinstance(units, list)
    return [record(unit) for unit in units]


def text(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


def run_case(case_id: str) -> CaseResult:
    if case_id not in {'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7'}:
        raise ValueError('unknown_sessions_case')
    commands: list[list[str]] = []
    observations: dict[str, JsonValue] = {}
    passed = False
    with TemporaryDirectory(prefix="five-session-'quoted-") as directory, ExitStack() as resources:
        root = Path(directory)
        repo = root / 'repo'
        repo.mkdir()
        def git(*args: str) -> str:
            command = ['git', *args]
            commands.append(command)
            return subprocess.run(command, cwd=repo, capture_output=True, text=True,
                                  check=True).stdout.strip()
        _ = git('init', '-q')
        _ = (repo / 'seed').write_text('seed\n')
        _ = git('add', 'seed')
        _ = git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-qm', 'fixture')
        base = git('rev-parse', 'HEAD')
        paths = OmhPaths(omh_home=root / 'omh', hermes_home=root / 'hermes')
        environment = {**fixture_system_environment(), 'PATH': os.environ.get('PATH', ''), 'HOME': str(root / 'home'),
                       'CODEX_HOME': str(root / 'codex-state'), 'CLAUDE_CONFIG_DIR': str(root / 'claude-state')}
        for key in ('HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
            Path(environment[key]).mkdir()
        executables: dict[str, list[str]] = {}
        for owner in ('codex', 'claude-code', 'unsupported'):
            executables[owner] = write_fixture_executable(root / owner,
                'import sys\nsys.path.insert(0, ' +
                repr(str(Path(__file__).resolve().parents[1])) + ')\n' +
                'from five_issue_process_fixture import executor_main\n' +
                'raise SystemExit(executor_main(' + repr('codex' if owner == 'unsupported' else owner) + ', ' +
                ("['--fixture-unsupported-help', *sys.argv[1:]]" if owner == 'unsupported' else 'sys.argv[1:]') + '))\n')
        fail_b = case_id == 'S3'
        expected_ids: dict[str, str] = {}
        _ = resources.enter_context(fixture_executable_transport(list(executables.values())))
        intake_paths: list[Path] = []
        def fixture_argv(owner: str, prompt: str,
                         route: Mapping[str, object] | None = None) -> list[str]:
            sidecar = re.search(r'JSON sidecar to exactly (.+)\.', prompt)
            identity = re.search(r'schema_version=fanout_unit_result/v1, unit_id=([a-z0-9-]+),', prompt)
            assert sidecar is not None and identity is not None
            intake_paths.append(Path(sidecar[1]))
            expected_ids[identity[1]] = ('12345678-1234-4234-8234-123456789abc'
                if case_id == 'S4' else str(uuid5(NAMESPACE_URL, sidecar[1])))
            argv = build_dispatch_argv(owner, prompt, route)
            assert argv is not None
            argv[0:1] = executables['unsupported' if 'Work unit: Unsupported' in prompt else owner]
            if fail_b and owner == 'claude-code':
                argv.append('--fixture-fail')
            # Record only safe argv metadata, never the actual prompt.
            commands.append([part if part != prompt else '<fixture prompt>' for part in argv])
            return argv
        def ready(_paths: OmhPaths, _owner: str) -> dict[str, object]:
            return {'status': 'ready'}
        goal = 'fixture-duplicate' if case_id == 'S4' else 'session integration fixture'
        units: list[dict[str, object]] = [
            {'unit_id': 'a', 'title': 'A', 'owner': 'codex', 'file_scope': ['src/']},
            {'unit_id': 'b', 'title': 'B', 'owner': 'claude-code', 'file_scope': ['tests/']},
        ]
        if case_id == 'S4':
            units.extend([
                {'unit_id': 'c', 'title': 'Malformed', 'owner': 'codex', 'file_scope': ['c/']},
                {'unit_id': 'd', 'title': 'Unsupported', 'owner': 'codex', 'file_scope': ['d/']},
            ])
        contract = write_fanout_contract(paths, build_fanout_contract(goal, units))
        fanout_id = str(contract['fanout_id'])
        with patch('omh.coding.fanout_dispatch.build_dispatch_argv', fixture_argv):
            summary = dispatch_fanout(paths, contract, goal_text=goal, repo_root=repo,
                                      base_sha=base, concurrency=1, readiness=ready, max_retries=0, env=environment,
                                      allow_unconfined=host_cannot_fence_fanout())
        rows = rows_of(summary)
        # This assertion fails against the pre-integration dispatcher, not a fabricated fixture result.
        assert all('executor_session' in row for row in rows), 'dispatcher receipt missing'
        for row in rows:
            write_roots = record(row['filesystem_confinement'])['write_roots']
            assert isinstance(write_roots, list)
            assert all(Path(text(path)).is_relative_to(root.resolve()) or
                       any(Path(text(path)) == intake.parent.resolve() for intake in intake_paths)
                       for path in write_roots), 'fixture owner state escaped invocation root'
        receipts = [record(row['executor_session']) for row in rows]
        if any(receipt['state'] != 'observed' for receipt in receipts[:2]):
            probes = []
            for owner in ('codex', 'claude-code'):
                for restore_system_environment in (False, True):
                    probe_env = dict(environment)
                    if restore_system_environment:
                        probe_env.update({key: os.environ[key] for key in
                            ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT') if key in os.environ})
                    probe = subprocess.run([*executables[owner], '--version'], env=probe_env,
                        capture_output=True, timeout=5, check=False)
                    probes.append({'owner': owner, 'system_environment': restore_system_environment,
                                   'exit': probe.returncode,
                                   'stdout': probe.stdout[:256].decode('utf-8', 'replace'),
                                   'stderr': probe.stderr[-768:].decode('utf-8', 'replace')})
            raise AssertionError({'receipts': receipts[:2], 'version_probes': probes})
        assert all(receipt['reference'] == expected_ids[text(receipt['unit_id'])] for receipt in receipts[:2])
        assert intake_paths and all(not path.exists() and not path.is_relative_to(paths.omh_home) for path in intake_paths)
        if case_id == 'S4':
            assert receipts[2]['state'] == 'not_observed' and receipts[2]['reason'] == 'invalid_reference'
            assert receipts[3]['state'] == 'not_available' and receipts[3]['reason'] == 'unsupported_protocol'
            assert receipts[2]['reference'] is None and receipts[3]['reference'] is None
        assert all(receipt['end_head'] == base and receipt['launch_head'] == base for receipt in receipts)
        assert all(receipt['unit_id'] == row['unit_id'] and receipt['attempt_id'] == row['attempt_id']
                   and receipt['worktree_path'] == row['worktree_path'] for receipt, row in zip(receipts, rows))
        before = {str(path): path.read_bytes() for path in paths.omh_home.rglob('*') if path.is_file()}
        roster = project_fanout_status(paths, fanout_id)
        journal = read_fanout_run_journal(fanout_run_journal_path(paths, fanout_id))
        roster_rows = rows_of(roster)
        assert [row['executor_session'] for row in roster_rows] == receipts
        assert [row['executor_session'] for row in rows_of(journal)] == receipts
        assert before == {str(path): path.read_bytes() for path in paths.omh_home.rglob('*') if path.is_file()}
        for row in roster_rows:
            resume = record(row['resume'])
            assert resume['execution_policy'] == 'copy_only'
            if case_id == 'S4':
                assert not resume['available']
                assert resume['reason'] == {'a': 'duplicate_reference', 'b': 'duplicate_reference',
                    'c': 'invalid_reference', 'd': 'unsupported_protocol'}[text(row['unit_id'])]
            else:
                assert resume['available'], resume['reason']
                argv = resume['argv']
                assert isinstance(argv, list)
                assert resume['cwd'] == row['worktree_path']
                if os.name == 'posix':
                    assert shlex.split(text(resume['shell_command'])) == ['cd', '--', row['worktree_path'], '&&', *argv]
                else:
                    assert resume['shell_command'] is None
                session = record(row['executor_session'])
                binary = record(session['binary_identity'])['resolved_path']
                reference = text(session['reference'])
                expected = ([binary, 'exec', 'resume', reference, '-']
                            if row['owner'] == 'codex' else [binary, '--resume=' + reference])
                assert argv == expected

        assert all(row['input_tokens'] == 12 and row['output_tokens'] == 7 for row in rows[:2])
        persisted = b'\n'.join(before.values())
        assert all(marker not in persisted for marker in (
            b'PROMPT_PRIVATE_SENTINEL', b'REASONING_PRIVATE_SENTINEL', b'EVENT_PRIVATE_SENTINEL', b'STDERR_PRIVATE_SENTINEL'))
        if case_id in ('S2', 'S7'):
            selected = project_fanout_status(paths, fanout_id, unit_id='a')
            assert len(rows_of(selected)) == 1 and rows_of(selected)[0]['executor_session'] == receipts[0]
            followup = build_fanout_session_followup(paths, fanout_id=fanout_id, unit_id='b')
            assert followup['executor_session'] == receipts[1]
            assert followup['resume'] == roster_rows[1]['resume']
            command = ['--omh-home', str(paths.omh_home), '--hermes-home', str(paths.hermes_home),
                       'coding', 'fanout', 'status', '--fanout-id', fanout_id, '--unit', 'a', '--json']
            commands.append(['omh', *command])
            code, output, error = run_cli(command)
            assert code == 0, error
            cli = decode(output)
            assert isinstance(cli, dict) and cli['unit_count'] == 1
            observations['cli_exit'] = code
            assert before == {str(path): path.read_bytes() for path in paths.omh_home.rglob('*') if path.is_file()}
        if case_id == 'S2':
            workspace_file = Path(text(rows[0]['worktree_path'])) / 'seed'
            _ = workspace_file.write_text('changed after observation\n')
            stale = rows_of(project_fanout_status(paths, fanout_id, unit_id='a'))[0]
            assert record(stale['resume'])['reason'] == 'recovery_snapshot_mismatch'
            assert stale['executor_session'] == receipts[0]
            _ = workspace_file.write_text('seed\n')
            git_dir = git('-C', str(workspace_file.parent), 'rev-parse', '--absolute-git-dir')
            index_path = Path(git_dir) / 'index'
            os.utime(workspace_file, (1, 1))
            index_before = index_path.read_bytes()
            current = rows_of(project_fanout_status(paths, fanout_id, unit_id='a'))[0]
            assert index_path.read_bytes() == index_before, 'read-only status refreshed Git index'
            assert record(current['resume'])['available']
            observations['stale_workspace_refused'] = True
        if case_id == 'S3':
            assert rows[1]['exit_code'] == 3
            old_b = receipts[1]
            _ = git('worktree', 'remove', '--force', text(rows[1]['worktree_path']))
            _ = git('branch', '-D', 'agent/b')
            fail_b = False
            with patch('omh.coding.fanout_dispatch.build_dispatch_argv', fixture_argv):
                fresh = dispatch_fanout(paths, contract, goal_text=goal, repo_root=repo,
                    base_sha=base, concurrency=1, readiness=ready, max_retries=0,
                    only_units=['b'], resume_journal=journal, env=environment,
                    allow_unconfined=host_cannot_fence_fanout())
            fresh_rows = rows_of(fresh)
            assert 'executor_session' in fresh_rows[0], 'held receipt lost'
            assert fresh_rows[0]['executor_session'] == receipts[0]
            new_b = record(fresh_rows[1]['executor_session'])
            assert new_b['attempt_id'] != old_b['attempt_id']
            assert new_b['predecessor_attempt_id'] == old_b['attempt_id']
            assert new_b['reference'] != old_b['reference']
            after_journal = read_fanout_run_journal(fanout_run_journal_path(paths, fanout_id))
            assert rows_of(after_journal)[0]['executor_session'] == receipts[0]
            assert rows_of(project_fanout_status(paths, fanout_id, unit_id='b'))[0]['executor_session'] == new_b
            observations['fresh_receipt'] = decode(json.dumps(new_b))
        if case_id == 'S4':
            binary = Path(executables['codex'][-1])
            _ = binary.write_text(binary.read_text() + '# changed executable\n')
            changed = rows_of(project_fanout_status(paths, fanout_id, unit_id='a'))[0]
            resume = record(changed['resume'])
            assert not resume['available'] and resume['reason'] == 'binary_changed'
            assert changed['executor_session'] == receipts[0]
            # A current read never rewrites the historical observation.
            assert before == {str(path): path.read_bytes() for path in paths.omh_home.rglob('*') if path.is_file()}
        if case_id == 'S6':
            event_path = paths.runtime_journal_events_path
            events = [record(decode(line)) for line in event_path.read_text().splitlines()]
            for event in events:
                _ = event.pop('executor_session', None)
                _ = event.pop('session_recovery_snapshot', None)
                event['session_ref'] = '12345678-1234-4234-8234-123456789abc'
            _ = event_path.write_text(''.join(json.dumps(event) + '\n' for event in events))
            legacy_bytes = event_path.read_bytes()
            legacy = project_fanout_status(paths, fanout_id)
            assert all('executor_session' not in row and record(row['resume'])['reason'] == 'legacy_missing'
                       and row['process_succeeded'] for row in rows_of(legacy))
            assert event_path.read_bytes() == legacy_bytes
            observations['legacy_missing'] = True
        if case_id == 'S7':
            assert all(row['result_schema_valid'] and row['process_succeeded'] for row in rows)
        observations['receipts'] = decode(json.dumps(receipts))
        observations['roster'] = decode(json.dumps(roster))
        observations['privacy_absent'] = True
        observations['read_only'] = True
        observations['process_results'] = [row['exit_code'] for row in rows]
        passed = True
    return {'case': case_id, 'commands': commands, 'inputs_metadata': {'owners': ['codex', 'claude-code']},
            'observations': observations, 'pass': passed, 'blocked_reason': None,
            'provenance': {'kind': 'fixture', 'scope': 'surface', 'native_required': False, 'native_available': False},
            'cleanup': {'owned_resources': [directory], 'terminated_processes': [], 'removed_paths': [directory],
                        'verified_absent': not root.exists(), 'errors': []}}
