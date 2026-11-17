"""Verified read-only job codes and bounded candidate search."""
import hashlib
import json
import time
import uuid
from pathlib import Path
from .leader_plan import PlanStore, read_reference
from .copilot_pool import CopilotPool
from .maa_facts import RosterStore
from .roster_match import roster_ref, requirements_hash, policy_hash, assess_metadata
from .core import ActionRejected, _utc_now
from .workflow_store import _encode
from .code_presentation import stage_label


class CodeDeliveryStore:
    def __init__(self, workflow, directory):
        self.workflow, self.engine = workflow, workflow.engine
        self.directory = Path(directory).resolve()
        self.plans = PlanStore(workflow)
        self.roster_store = RosterStore(workflow)
        with self.engine._transaction():
            self.engine._db.execute('CREATE TABLE IF NOT EXISTS code_receipts (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL, step_id TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL)')

    def _verify(self, payload, current=True):
        screen = read_reference(payload['screen_ref'], self.directory, maximum=32*1024*1024)
        pool_ref = screen['pool_ref']
        path = Path(pool_ref['path']).resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError('候选池不在资料目录')
        pool = CopilotPool(path.parent.parent).read(pool_ref)
        locator = payload['candidate_locator']
        candidates = [c for c in pool['candidates'] if c.get('candidate_locator') == locator]
        results = [r for r in screen['results'] if r.get('candidate_locator') == locator]
        if len(candidates) != 1 or len(results) != 1:
            raise ValueError('候选身份不唯一')
        c, result = candidates[0], results[0]
        stage = payload['resolved_stage']
        if (stage.get('query_key_verified') is not True or stage.get('difficulty_verified') is not True or locator.get('resolved_stage') != stage or c.get('stage_match_verified') is not True
            or c.get('difficulty_match') != 'compatible' or c.get('requirements_complete') is not True
            or c.get('schema_version') != 2 or result.get('classification') != 'satisfied'
            or result.get('requirements_sha256') != requirements_hash(c)
            or result.get('id') != c.get('id') or type(c.get('id')) is not int or c['id'] <= 0
            or c.get('source') != f"https://prts.maa.plus/copilot/get/{c['id']}"
            or c.get('mystery_code') != f"prts://{c['id']}"
            or screen.get('profile_id') != 'local-default' or screen.get('policy_sha256') != policy_hash(None) or screen.get('policy_sha256') != policy_hash(screen.get('policy'))):
            raise ValueError('作业证据不符合当前关卡、难度或阵容要求')
        if c.get('difficulty_basis') == 'unique_public_stage_mode':
            if (c.get('difficulty') not in (None, 0) or c.get('declared_difficulty') != c.get('difficulty')
                    or c.get('inferred_difficulty') != stage.get('difficulty')
                    or stage.get('difficulty_domain_complete') is not True
                    or stage.get('difficulty_domain') != [stage.get('difficulty')]
                    or not stage.get('public_level_id') or not stage.get('difficulty_domain_source_refs')
                    or c.get('difficulty_proof') != stage['difficulty_domain_source_refs']):
                raise ValueError('关卡唯一模式依据不一致')
        if current:
            box = self.roster_store.current(screen['profile_id'])
            if box is None or roster_ref(box) != screen['roster_ref'] or assess_metadata(c, box, screen['policy'])['classification'] != 'satisfied':
                raise ValueError('本地阵容已变化或不存在')
        return {'id': c['id'], 'code': c['mystery_code'], 'stage_code': stage.get('display_code'),
                'source': c.get('catalog_source'), 'resolved_stage': stage}

    def register_code(self, lease, candidate_locator, screen_ref):
        payload = {'task_id': lease['task_id'], 'revision': lease['revision'], 'step_id': lease['step_id'],
                   'resolved_stage': lease['resolved_stage'], 'candidate_locator': candidate_locator, 'screen_ref': screen_ref}
        encoded, digest = _encode(payload)
        with self.engine._transaction():
            self.plans._lease(lease)
            self._verify(payload)
            plan = self.plans._plan(lease['task_id'], lease['revision'])
            step = self.engine._db.execute('SELECT stage_key FROM leader_steps WHERE task_id=? AND revision=? AND step_id=?', (lease['task_id'], lease['revision'], lease['step_id'])).fetchone()
            actual = next(s for s in plan['stages'] if s['stage_key'] == step['stage_key'])
            if actual != lease['resolved_stage']:
                raise ActionRejected('关卡身份已变化')
            self.engine._db.execute('INSERT OR IGNORE INTO code_receipts VALUES(?,?,?,?,?,?)', (digest, lease['task_id'], lease['revision'], lease['step_id'], encoded, digest))
            self.workflow._publish_event(lease['task_id'], 'code.registered', {'step_id': lease['step_id'], 'receipt_id': digest}, event_key='code:'+digest)
        return {'receipt_id': digest, 'sha256': digest}

    def read_code(self, ref, *, verify_current=True):
        with self.engine._lock:
            row = self.engine._db.execute('SELECT * FROM code_receipts WHERE id=?', (ref.get('receipt_id'),)).fetchone()
            if not row or row['hash'] != ref.get('sha256'):
                raise ValueError('代码凭据不存在')
            payload = json.loads(row['payload'])
            if _encode(payload)[1] != row['hash']:
                raise ValueError('代码凭据已变化')
            return {**self._verify(payload, current=verify_current), 'task_id': row['task_id'], 'step_id': row['step_id'], 'revision': row['revision']}

    def summary(self, task_id, *, verify_current=True):
        plan = self.plans.current(task_id)
        items, missing, refs = [], [], []
        if not plan:
            return {'complete': False, 'items': [], 'missing': ['计划不存在'], 'summary': '计划不存在', 'code_refs': []}
        stages = {s['stage_key']: s for s in plan['stages']}
        for step in plan['steps']:
            result = step['result'] or {}
            stage = stages[step['stage_key']]
            item = {'stage_code': stage.get('display_code'), 'stage_label': stage_label(stage),
                    'stage_key': stage['stage_key'], 'stage_id': stage.get('stage_id'),
                    'difficulty': stage.get('difficulty'), 'branch': stage.get('branch', ''),
                    'status': step['status'], 'attempt': step.get('attempt', 1),
                    'reason': result.get('reason') or result.get('backup_reason'),
                    'unresolved_count': result.get('unresolved_count', 0)}
            for kind in ('primary', 'backup'):
                ref = result.get(kind+'_ref')
                if ref:
                    try:
                        code = self.read_code(ref, verify_current=verify_current)
                        if code['task_id'] != task_id or code['step_id'] != step['step_id'] or code.get('revision') != plan['revision']:
                            raise ValueError('回执不属于当前步骤')
                        item[kind+'_code'] = code['code']; refs.append(ref)
                    except (ValueError, OSError, KeyError):
                        pass
            if ('primary_code' not in item or step['status'] not in ('primary_only', 'two_verified')
                    or (step['status'] == 'two_verified' and ('backup_code' not in item or item.get('backup_code') == item.get('primary_code')))):
                missing.append(item['stage_label'])
            items.append(item)
        complete = bool(items) and not missing
        deferred = plan.get('deferred_stages', [])
        message = f"当前可查询 {len(items)-len(missing)}/{len(items)} 关有主选；{sum('backup_code' not in i for i in items)} 关缺备选"
        if deferred:
            message += f"；另 {len(deferred)} 关待开放后复查（尚未设置自动执行）"
        return {'complete': complete, 'scope_complete': complete and not deferred,
                'deferred_stages': deferred, 'deferred_count': len(deferred),
                'items': items, 'missing': missing, 'summary': message, 'code_refs': refs}


class CodeTools:
    def __init__(self, store, plans, research, rosters):
        self.store, self.plans, self.research, self.rosters = store, plans, research, rosters

    def find_pair(self, lease, *, cancel_event=None, clock=None):
        from .research_tools import ResearchToolError
        clock = clock or time.monotonic
        stage = lease['resolved_stage']
        attempt = lease.get('attempt', 1)
        if type(attempt) is not int or attempt not in (1, 2):
            raise ActionRejected('作业检索轮次超出已授权范围')
        if stage.get('query_key_verified') is not True or stage.get('difficulty_verified') is not True:
            return {'status': 'search_incomplete', 'primary_ref': None, 'backup_ref': None,
                    'backup_reason': 'unverified_stage', 'reason': 'unverified_stage',
                    'search_coverage': {}, 'unresolved_count': 1, 'budget_used': {},
                    'attempt': attempt, 'pages_attempted': 0, 'pages_fetched': 0,
                    'error_classification': None, 'errors': []}
        selected, ids, coverage, resume = [], set(), {}, None
        counts = {}
        exhausted = False
        errors, pages_attempted, pages_fetched = [], 0, 0
        refresh_pending = attempt == 2
        with self.store.engine._lock:
            rows = self.store.engine._db.execute(
                'SELECT id,hash FROM code_receipts WHERE task_id=? AND revision=? AND step_id=? ORDER BY rowid',
                (lease['task_id'], lease['revision'], lease['step_id'])).fetchall()
        for row in rows:
            ref = {'receipt_id': row['id'], 'sha256': row['hash']}
            try:
                code = self.store.read_code(ref)
                if code['id'] not in ids:
                    selected.append(ref); ids.add(code['id'])
                    # Only the first registered receipt is a recoverable primary.
                    # A later registration may have completed after its deadline.
                    break
            except (ValueError, OSError, KeyError):
                continue

        def measured(phase, operation):
            remaining = self.plans.budget(lease)[phase]['milliseconds_remaining']
            if not remaining:
                return None
            reservation = self.plans.reserve_budget(lease, phase=phase, pages=0, milliseconds=remaining)
            start = clock()
            try:
                return operation()
            finally:
                elapsed = max(0, int((clock() - start) * 1000))
                self.plans.settle_budget(reservation, elapsed_ms=elapsed)

        def query_stage(phase, **kwargs):
            nonlocal pages_fetched
            try:
                response = self.research.candidates_for_stage(stage, cancel_event=cancel_event, **kwargs)
            except (ResearchToolError, OSError) as exc:
                record_error(phase, exc, default='catalog_unavailable')
                return None
            pages_fetched += response.get('pages_fetched', 0)
            if response.get('error'):
                errors.append({'phase': phase, 'classification': 'catalog_unavailable', 'type': 'catalog_response_error'})
            return response

        def record_error(phase, exc, *, default):
            classification = getattr(exc, 'error_classification', None)
            if isinstance(exc, OSError):
                classification = 'local_io_error'
            if classification not in ('local_io_error', 'evidence_unavailable'):
                classification = default
            record = {'phase': phase, 'classification': classification, 'type': type(exc).__name__}
            if classification == 'local_io_error':
                operation = getattr(exc, 'io_operation', None)
                if operation in ('read', 'write', 'resolve'):
                    record['io_operation'] = operation
                for field in ('errno', 'winerror'):
                    value = getattr(exc, field, None)
                    if type(value) is int:
                        record[field] = value
            errors.append(record)

        def measured_evidence(phase, operation):
            try:
                return measured(phase, operation)
            except (ValueError, OSError, KeyError) as exc:
                record_error(phase, exc, default='evidence_unavailable')
                return None

        phase = 'backup' if selected else 'primary'
        response = measured(phase, lambda: query_stage(phase, cache_only=True))
        while response is not None:
            resume = response['pool_ref']; coverage = response['coverage']
            phase = 'backup' if selected else 'primary'
            def screen_candidates():
                screened = self.rosters.screen_pool('local-default', resume)
                evidence = read_reference(screened['screen_ref'], self.store.directory, maximum=32*1024*1024)
                return screened, evidence['counts'], sorted(evidence['results'], key=lambda x: x['id'])
            screened = measured_evidence(phase, screen_candidates)
            if screened is None or not self.plans.budget(lease)[phase]['milliseconds_remaining']:
                exhausted = not errors
                break
            screen, counts, candidates = screened
            for candidate in candidates:
                if candidate['classification'] == 'satisfied' and candidate['id'] not in ids:
                    phase = 'backup' if selected else 'primary'
                    ref = measured_evidence(phase, lambda: self.store.register_code(lease, candidate['candidate_locator'], screen['screen_ref']))
                    if ref is None or not self.plans.budget(lease)[phase]['milliseconds_remaining']:
                        exhausted = not errors
                        break
                    selected.append(ref); ids.add(candidate['id'])
                    if len(selected) == 2: break
            if (errors or exhausted or len(selected) == 2 or coverage.get('complete') and not refresh_pending
                    or (cancel_event and cancel_event.is_set())): break
            phase = 'backup' if selected else 'primary'
            remaining = self.plans.budget(lease)[phase]
            if not remaining['pages_remaining'] or not remaining['milliseconds_remaining']:
                exhausted = True
                break
            reservation = None; start = None
            def before_fetch(page):
                nonlocal reservation, start, pages_attempted
                reservation = self.plans.reserve_budget(lease, phase=phase, pages=1, milliseconds=remaining['milliseconds_remaining'])
                pages_attempted += 1
                start = clock()
                return True
            next_response = None
            try:
                refresh = refresh_pending
                refresh_pending = False
                next_response = query_stage(phase, resume_ref=resume, before_fetch=before_fetch,
                    timeout_seconds=remaining['milliseconds_remaining']/1000, refresh=refresh)
            finally:
                if reservation:
                    self.plans.settle_budget(reservation, elapsed_ms=max(0, int((clock()-start)*1000)), received_ref=next_response.get('pool_ref') if next_response and next_response.get('pages_fetched') else None)
            response = next_response
            if response is None: break
            if not reservation or not response.get('pages_fetched'):
                coverage = response.get('coverage', coverage)
                break
        budget = self.plans.budget(lease)
        used = {phase+'_'+key: value[key+'_used'] for phase, value in budget.items() for key in ('pages','milliseconds')}
        incomplete = errors or exhausted or not coverage.get('complete') or counts.get('unknown', 0) or counts.get('conditional', 0) or counts.get('needs_verified_support', 0)
        status = ('two_verified' if len(selected)==2 else 'primary_only' if selected else 'search_incomplete' if incomplete else 'no_box_match' if sum(counts.values()) else 'no_jobs_yet')
        reason = ('two_verified' if len(selected) == 2 else
                  'network_error' if errors and errors[-1]['classification'] == 'catalog_unavailable' else
                  'local_io_error' if errors and errors[-1]['classification'] == 'local_io_error' else
                  'evidence_unavailable' if errors else 'budget_exhausted' if exhausted else
                  'catalog_unverified' if coverage.get('exhausted') and not coverage.get('complete') else
                  'coverage_incomplete' if not coverage.get('complete') else
                  'unresolved_requirements' if incomplete else 'catalog_exhausted')
        return {'status': status, 'primary_ref': selected[0] if selected else None, 'backup_ref': selected[1] if len(selected)>1 else None,
                'backup_reason': None if len(selected)==2 else reason, 'reason': reason,
                'search_coverage': coverage, 'unresolved_count': counts.get('unknown',0)+counts.get('conditional',0)+counts.get('needs_verified_support',0), 'budget_used': used,
                'attempt': attempt, 'pages_attempted': pages_attempted, 'pages_fetched': pages_fetched,
                'error_classification': errors[-1]['classification'] if errors else None, 'errors': errors}
