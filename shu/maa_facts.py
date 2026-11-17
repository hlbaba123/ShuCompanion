"""Versioned account facts and conservative, deterministic MAA feasibility.

No model, GUI, network, game API or credential access lives here. MAA export
fields follow the workspace's pinned 6.21.4 callback/copilot specifications.
Unspecified skill mastery and module branches remain unknown. In particular,
copilot ``module`` selects a branch, and ``skill`` selects a skill number.

All published JSON is bounded and kept in TaskEngine's tasks.sqlite3, in the
same transaction as its current pointer and workflow outbox event. A partial
batch merges positive facts; only a complete batch proves roster-wide absence.
An explicitly confirmed user table may also retain per-row absence without
claiming that operators outside that table are absent.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import time


_MAX_OPERATORS = 2000
_MAX_BYTES = 4 * 1024 * 1024
_SCALARS = {'rarity': (1, 6), 'elite': (0, 2), 'level': (1, 100), 'potential': (1, 6),
            'skill_level': (1, 7)}
_SOURCE_FIELDS = {'kind', 'source_version', 'observed_at', 'evidence_ref',
                  'scan_id', 'expires_at'}


def _text(value, label, limit=256, *, optional=False):
    if optional and value is None:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f'Invalid {label}')
    return value.strip()


def _integer(value, low, high, label):
    if value is None:
        return None
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'Invalid {label}')
    return value


def _epoch(value):
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            value = moment.timestamp()
        except ValueError:
            raise ValueError('Invalid observed_at') from None
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid observed_at')
    return float(value)


def _json(value):
    result = json.dumps(value, ensure_ascii=False, allow_nan=False,
                        sort_keys=True, separators=(',', ':'))
    if len(result.encode('utf-8')) > _MAX_BYTES:
        raise ValueError('Roster payload exceeds size limit')
    return result


def _source(value):
    if not isinstance(value, dict) or set(value) - _SOURCE_FIELDS:
        raise ValueError('Source must contain bounded provenance only')
    clean = {}
    for key, field in value.items():
        clean[key] = (_epoch(field) if key in {'observed_at', 'expires_at'}
                      else _text(field, key, 1000))
    for required in ('kind', 'observed_at', 'evidence_ref'):
        if required not in clean:
            raise ValueError(f'Missing source {required}')
    return clean


def _operator(value, *, complete=True):
    if not isinstance(value, dict):
        raise ValueError('Invalid operator')
    identifier = _text(value.get('id'), 'operator id', 128, optional=True)
    name = _text(value.get('name'), 'operator name', 128, optional=True)
    if not identifier and not name:
        raise ValueError('Operator needs id or name')
    owned = value.get('owned', value.get('own'))
    if owned is not None and type(owned) is not bool:
        raise ValueError('Invalid operator ownership')
    if 'owned' in value and 'own' in value and value['owned'] != value['own']:
        raise ValueError('Conflicting operator ownership')
    if not complete and owned is False:
        owned = None
    clean = {'id': identifier, 'name': name, 'owned': owned,
             'role': _text(value.get('role'), 'role', 32, optional=True)}
    if clean['role']:
        clean['role'] = clean['role'].casefold()
    for field, bounds in _SCALARS.items():
        clean[field] = _integer(value.get(field), *bounds, field)
    for field, low, high, maximum in [('mastery', 0, 3, 3), ('modules', 0, 3, 5)]:
        raw = value.get(field) or {}
        if not isinstance(raw, dict) or len(raw) > maximum:
            raise ValueError(f'Invalid {field}')
        clean[field] = {}
        for key, amount in raw.items():
            if str(key) not in {str(number) for number in range(1, maximum + 1)}:
                raise ValueError(f'Invalid {field} index')
            amount = _integer(amount, low, high, field)
            if amount is not None:
                clean[field][str(key)] = amount
    return clean


def _key(operator):
    return ('id:' + operator['id'] if operator.get('id')
            else 'name:' + operator['name'] + ':' + (operator.get('role') or ''))


def parse_maa_export(export):
    """Normalize an OperBoxInfo details dict, or its callback envelope.

    Both documented lists and a Boolean ``done`` are required. Neither truthy
    strings nor a truncated callback may silently become a complete box.
    This adapter intentionally does not guess the unrelated yituliu schema.
    """
    if not isinstance(export, dict):
        raise ValueError('Invalid MAA export')
    details = export.get('details', export)
    if (not isinstance(details, dict) or type(details.get('done')) is not bool
            or not isinstance(details.get('all_opers'), list)
            or not isinstance(details.get('own_opers'), list)
            or len(details['all_opers']) + len(details['own_opers']) > _MAX_OPERATORS * 2):
        raise ValueError('Incomplete MAA export schema')
    complete = details['done']
    rows = {}
    for raw in details['all_opers'] + details['own_opers']:
        clean = _operator(raw, complete=complete)
        key = _key(clean)
        previous = rows.get(key, {})
        rows[key] = {field: (previous.get(field) if value is None else value)
                     for field, value in clean.items()}
    return {'complete': complete, 'operators': list(rows.values())}


class RosterStore:
    """A facts view of the existing TaskEngine transaction, never another DB."""

    def __init__(self, engine):
        from .workflow_store import WorkflowStore
        self.workflow = engine if isinstance(engine, WorkflowStore) else WorkflowStore(engine)
        self.engine = self.workflow.engine
        with self.engine._transaction():
            self.engine._db.execute('''CREATE TABLE IF NOT EXISTS workflow_roster_batches (
                batch_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                box_revision INTEGER NOT NULL, content_hash TEXT NOT NULL,
                input_hash TEXT NOT NULL, scan_id TEXT, payload TEXT NOT NULL,
                UNIQUE(profile_id, box_revision), UNIQUE(profile_id, input_hash),
                UNIQUE(profile_id, scan_id))''')
            self.engine._db.execute('''CREATE TABLE IF NOT EXISTS workflow_roster_current (
                profile_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES workflow_roster_batches(batch_id))''')

    def _batch(self, profile_id, batch_id):
        row = self.engine._db.execute(
            'SELECT * FROM workflow_roster_batches WHERE profile_id=? AND batch_id=?',
            (profile_id, batch_id)).fetchone()
        if not row:
            return None
        if hashlib.sha256(row['payload'].encode('utf-8')).hexdigest() != row['content_hash']:
            raise ValueError('Roster integrity check failed; facts remain unknown')
        box = json.loads(row['payload'])
        box['content_hash'] = row['content_hash']
        return box

    def batch(self, profile_id, batch_id):
        with self.engine._lock:
            self.engine._ensure_open()
            return self._batch(profile_id, batch_id)

    def _current(self, profile_id):
        row = self.engine._db.execute(
            'SELECT batch_id FROM workflow_roster_current WHERE profile_id=?',
            (profile_id,)).fetchone()
        return self._batch(profile_id, row['batch_id']) if row else None

    def current(self, profile_id):
        with self.engine._lock:
            self.engine._ensure_open()
            return self._current(profile_id)

    def _complete_source(self, profile_id, previous):
        if previous is None:
            return None
        if 'latest_complete_source' in previous:
            return previous['latest_complete_source']
        # Older batches did not retain absence evidence across partial updates.
        # Recover it from immutable published history without rewriting records.
        source = None
        for row in self.engine._db.execute(
                'SELECT batch_id FROM workflow_roster_batches WHERE profile_id=?',
                (profile_id,)):
            batch = self._batch(profile_id, row['batch_id'])
            if batch.get('scan_complete', batch.get('complete')) is True:
                candidate = batch['source']
                if source is None or candidate['observed_at'] > source['observed_at']:
                    source = candidate
        return source

    def publish(self, profile_id, operators, complete, source, *, explicit_ownership=False):
        profile_id = _text(profile_id, 'profile id', 128)
        if (type(complete) is not bool or type(explicit_ownership) is not bool
                or not isinstance(operators, (list, tuple))):
            raise ValueError('Invalid roster batch')
        if len(operators) > _MAX_OPERATORS:
            raise ValueError('Too many operators')
        source = _source(source)
        clean = [_operator(value, complete=complete or explicit_ownership) for value in operators]
        if len({_key(value) for value in clean}) != len(clean):
            raise ValueError('Duplicate operator identity')
        clean.sort(key=_key)
        identity = [profile_id, complete, source, clean]
        if explicit_ownership:
            identity.append({'explicit_ownership': True})
        input_hash = hashlib.sha256(_json(identity).encode('utf-8')).hexdigest()
        with self.engine._transaction():
            db = self.engine._db
            repeated = db.execute('''SELECT batch_id FROM workflow_roster_batches
                WHERE profile_id=? AND input_hash=?''', (profile_id, input_hash)).fetchone()
            if repeated:
                return self._batch(profile_id, repeated['batch_id'])
            if not explicit_ownership:
                # Pre-XLSX releases ignored rarity. Replaying their identical
                # immutable source remains idempotent, without rewriting the
                # old batch or claiming that its unrecorded rarity is known.
                legacy_clean = [{key: value for key, value in row.items() if key != 'rarity'}
                                for row in clean]
                legacy_hash = hashlib.sha256(_json([profile_id, complete, source,
                    legacy_clean]).encode('utf-8')).hexdigest()
                legacy = db.execute('''SELECT batch_id FROM workflow_roster_batches
                    WHERE profile_id=? AND input_hash=?''', (profile_id, legacy_hash)).fetchone()
                if legacy:
                    return self._batch(profile_id, legacy['batch_id'])
            if source.get('scan_id') and db.execute('''SELECT 1 FROM workflow_roster_batches
                    WHERE profile_id=? AND scan_id=?''',
                    (profile_id, source['scan_id'])).fetchone():
                raise ValueError('Scan identity already published with different content')
            previous = self._current(profile_id)
            complete_source = self._complete_source(profile_id, previous)
            if complete and (complete_source is None or
                             source['observed_at'] >= complete_source['observed_at']):
                complete_source = source
            latest_observed = max(source['observed_at'], previous.get(
                'latest_observed_at', previous['source']['observed_at']) if previous else 0)
            # A delayed complete export is still historical evidence, but it
            # cannot assert today's absence after a newer observation exists.
            effective_complete = complete and source['observed_at'] >= latest_observed
            old_rows = {_key(row): row for row in previous['operators']} if previous else {}
            rows = {} if effective_complete else dict(old_rows)
            if complete and not effective_complete:
                present = {_key(row) for row in clean}
                for key, old in old_rows.items():
                    owned_source = old.get('field_sources', {}).get('owned', previous['source'])
                    if key not in present and owned_source['observed_at'] <= source['observed_at']:
                        rows.pop(key, None)
            for row in clean:
                if not effective_complete and not explicit_ownership and row['owned'] is False:
                    row = dict(row, owned=None)
                key = _key(row)
                if (key not in old_rows and complete_source is not None
                        and source['observed_at'] < complete_source['observed_at']):
                    # The newer complete scan proved this identity absent. Its
                    # delayed positive observation cannot become current again.
                    continue
                old = old_rows.get(key, {})
                merged = dict(old)
                sources = dict(old.get('field_sources', {}))
                for field, value in row.items():
                    if field in {'mastery', 'modules'}:
                        merged[field] = dict(old.get(field, {}))
                        for index, amount in value.items():
                            if sources.get(field + '.' + index, {}).get('observed_at', -1) > source['observed_at']:
                                continue
                            merged[field][index] = amount
                            sources[field + '.' + index] = source
                    elif value is not None:
                        if (field == 'owned' and value is True and old.get('owned') is not True
                                and complete_source is not None
                                and source['observed_at'] < complete_source['observed_at']):
                            continue
                        if sources.get(field, {}).get('observed_at', -1) > source['observed_at']:
                            continue
                        merged[field] = value
                        sources[field] = source
                    else:
                        merged.setdefault(field, None)
                if (row['elite'] is not None and row['elite'] != old.get('elite')
                        and row['level'] is None and merged.get('elite') == row['elite']):
                    merged['level'] = None
                    sources['level'] = source
                merged['field_sources'] = sources
                rows[key] = merged
            revision = previous['box_revision'] + 1 if previous else 1
            batch_id = 'box-' + input_hash
            box = {'profile_id': profile_id, 'box_revision': revision,
                   'batch_id': batch_id, 'scan_id': source.get('scan_id', batch_id),
                   'complete': effective_complete, 'scan_complete': complete,
                    'latest_complete_source': complete_source,
                   'latest_observed_at': latest_observed, 'source': source,
                   'operators': sorted(rows.values(), key=_key)}
            if explicit_ownership:
                box['explicit_ownership'] = True
            payload = _json(box)
            digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
            db.execute('''INSERT INTO workflow_roster_batches
                (batch_id,profile_id,box_revision,content_hash,input_hash,scan_id,payload)
                VALUES (?,?,?,?,?,?,?)''', (batch_id, profile_id, revision, digest,
                                          input_hash, source.get('scan_id'), payload))
            db.execute('''INSERT INTO workflow_roster_current(profile_id,batch_id) VALUES (?,?)
                ON CONFLICT(profile_id) DO UPDATE SET batch_id=excluded.batch_id''',
                       (profile_id, batch_id))
            self.workflow._publish_event(None, 'box.updated',
                {'profile_id': profile_id, 'batch_id': batch_id,
                 'box_revision': revision, 'content_hash': digest},
                event_key=f'box:{profile_id}:{batch_id}')
            box['content_hash'] = digest
            return box


def _same(requirement, operator):
    if requirement.get('id'):
        return requirement['id'] == operator.get('id')
    if requirement.get('name') != operator.get('name'):
        return False
    return not requirement.get('role') or requirement['role'].casefold() == (operator.get('role') or '').casefold()


def _missing(slot, requirement, field, required=None, actual=None, *, state='unknown'):
    return {'slot': slot, 'name': requirement.get('name'), 'field': field,
            'required': required, 'actual': actual, 'state': state}


def _requirement_schema(slot, requirement):
    missing = []
    wanted = requirement.get('requirements') or {}
    if not isinstance(wanted, dict):
        raise ValueError('Invalid copilot requirements')
    wanted = dict(wanted)
    if 'potentiality' in wanted:
        if 'potential' in wanted and wanted['potential'] != wanted['potentiality']:
            missing.append(_missing(slot, requirement, 'potential_alias_conflict',
                                    actual={key: wanted[key] for key in ('potential', 'potentiality')}))
        else:
            wanted['potential'] = wanted['potentiality']
        wanted.pop('potentiality')
    allowed = {'elite', 'level', 'potential', 'skill_level', 'module', 'module_level'}
    if set(wanted) - allowed:
        missing.append(_missing(slot, requirement, 'unsupported_requirements',
                                actual={key: value for key, value in wanted.items() if key not in allowed}))
    for field in ('elite', 'level', 'potential', 'skill_level', 'module', 'module_level'):
        desired = wanted.get(field)
        if desired is not None and (type(desired) is not int or desired < -1 or desired > 10000):
            raise ValueError('Invalid copilot requirement value')
    return wanted, missing


def _attributes(slot, requirement, operator, policy=None):
    wanted, missing = _requirement_schema(slot, requirement)
    if policy is not None:
        rarity, elite = operator.get('rarity'), operator.get('elite')
        if rarity in (5, 6):
            wanted['elite'] = max(2, wanted.get('elite', -1))
        elif rarity is None and (elite is None or elite < 2):
            missing.append(_missing(slot, requirement, 'rarity', 'needed to apply high-rarity elite floor'))
        for field, flag in (('level', 'ignore_level'), ('skill_level', 'ignore_skill'),
                            ('module', 'ignore_module'), ('module_level', 'ignore_module')):
            if policy.get(flag):
                wanted.pop(field, None)
    for field in ('elite', 'level', 'potential'):
        desired = wanted.get(field, -1)
        if desired < 0:
            continue
        actual = operator.get(field)
        # Level belongs to an elite stage: E1L80 is not stronger than E2L60.
        if field == 'level' and wanted.get('elite', -1) >= 0 and operator.get('elite') is not None:
            if operator['elite'] > wanted['elite']:
                continue
        if actual is None or actual < desired:
            missing.append(_missing(slot, requirement, field, desired, actual,
                                    state='unknown' if actual is None else 'insufficient'))
    desired = wanted.get('skill_level', -1)
    if desired >= 0:
        skill = requirement.get('skill', 0)
        main = operator.get('skill_level')
        if desired > 7:
            mastery = operator.get('mastery', {}).get(str(skill)) if skill else None
            actual = (main if main is not None and main < 7 else
                      7 + mastery if main == 7 and mastery is not None else None)
            field = 'mastery.' + str(skill)
        else:
            actual, field = main, 'skill_level'
        if actual is None or actual < desired:
            missing.append(_missing(slot, requirement, field, desired, actual,
                                    state='unknown' if actual is None else 'insufficient'))
    branch = wanted.get('module', -1)
    module_level = wanted.get('module_level', -1)
    if branch > 0:
        actual = operator.get('modules', {}).get(str(branch))
        desired = max(1, module_level)
        if actual is None or actual < desired:
            missing.append(_missing(slot, requirement, 'module.' + str(branch), desired,
                                    actual, state='unknown' if actual is None else 'insufficient'))
    elif module_level > 0:
        missing.append(_missing(slot, requirement, 'module_branch', module_level))
    return missing


def _valid_support(operator, box, now, max_age):
    try:
        return (operator.get('verified') is True
                and operator.get('profile_id') == box.get('profile_id')
                and bool(operator.get('source')) and bool(operator.get('evidence_ref'))
                and 0 <= now - _epoch(operator.get('observed_at')) <= max_age)
    except (ValueError, TypeError):
        return False


def _expired(source, now):
    if not isinstance(source, dict) or source.get('expires_at') is None:
        return False
    try:
        return _epoch(source['expires_at']) < now
    except ValueError:
        return True


def _fresh_operator(raw, box, now):
    clean = _operator(raw)
    sources = raw.get('field_sources') or {}
    for field in ('owned', 'rarity', 'elite', 'level', 'potential', 'skill_level'):
        if _expired(sources.get(field, box.get('source')), now):
            clean[field] = None
    for field in ('mastery', 'modules'):
        clean[field] = {index: value for index, value in clean[field].items()
                        if not _expired(sources.get(field + '.' + index, box.get('source')), now)}
    return clean


def match_copilot(copilot, box, supports=(), *, now=None, support_max_age=300, policy=None):
    """Return a feasible distinct assignment or concrete unresolved facts.

    One support may fill one slot globally. Name-only or stale support entries
    never prove availability. A single absent operator is pending verification;
    two proven absences cannot be repaired by borrowing twice. Unknown practice
    is not equivalent to a known insufficient level or mastery.
    """
    if not isinstance(copilot, dict) or not isinstance(box, dict):
        raise ValueError('Expected copilot document and roster box')
    if policy is not None:
        from .roster_match import normalize_policy
        policy = normalize_policy(policy)
    if copilot.get('requires_download') or not ({'opers', 'groups'} & copilot.keys()):
        return {'classification': 'unknown', 'profile_id': box.get('profile_id'),
                'box_revision': box.get('box_revision'), 'assignments': [],
                'missing_requirements': [{'field': 'full_document', 'state': 'unknown'}]}
    now = time.time() if now is None else _epoch(now)
    if type(support_max_age) not in (int, float) or not 0 < support_max_age <= 86400:
        raise ValueError('Invalid support freshness bound')
    roster = [_fresh_operator(value, box, now) for value in box.get('operators', [])]
    complete = box.get('complete') is True and not _expired(box.get('source'), now)
    if len(roster) > _MAX_OPERATORS or not isinstance(supports, (tuple, list)) or len(supports) > 200:
        raise ValueError('Too many roster/support entries')
    verified = [_operator(value) for value in supports if isinstance(value, dict)
                and _valid_support(value, box, now, support_max_age)]
    slots = []
    explicit = copilot.get('opers', [])
    groups = copilot.get('groups', [])
    if not isinstance(explicit, list) or not isinstance(groups, list) or len(explicit) + len(groups) > 32:
        raise ValueError('Invalid copilot slots')
    slots.extend((f'oper:{index}', [requirement]) for index, requirement in enumerate(explicit))
    for index, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get('opers'), list) or not 1 <= len(group['opers']) <= 32:
            raise ValueError('Invalid copilot group')
        slots.append((f'group:{index}', group['opers']))
    choices = []
    for slot, alternatives in slots:
        candidates = []
        for requirement in alternatives:
            if not isinstance(requirement, dict) or not (requirement.get('name') or requirement.get('id')):
                raise ValueError('Invalid copilot operator')
            if type(requirement.get('skill', 0)) is not int or not 0 <= requirement.get('skill', 0) <= 3:
                raise ValueError('Invalid copilot skill number')
            _, schema_missing = _requirement_schema(slot, requirement)
            matching = [row for row in roster if _same(requirement, row)]
            ambiguous = len({_key(row) for row in matching}) > 1 and not requirement.get('id') and not requirement.get('role')
            if ambiguous:
                matching = []
            for row in matching:
                # Ownership is evaluated first. An unowned row's old training
                # fields cannot establish the training of a future support.
                missing = (_attributes(slot, requirement, row, policy)
                           if row['owned'] is True else list(schema_missing))
                if row['owned'] is not True:
                    missing.insert(0, _missing(slot, requirement, 'owned', True,
                                   row['owned'], state='unknown' if row['owned'] is None else 'absent'))
                candidates.append((_key(row), 'owned', row, missing, requirement))
            if not matching:
                row = _operator({'id': requirement.get('id'), 'name': requirement.get('name'),
                                 'role': requirement.get('role')})
                field = 'identity' if ambiguous else 'owned'
                state = 'unknown' if ambiguous or not complete else 'absent'
                candidates.append((_key(row), 'owned', row,
                    [_missing(slot, requirement, field, True, state=state)] + schema_missing, requirement))
            for support in verified:
                if not ambiguous and _same(requirement, support):
                    # An operator has one identity even when support uses a provider id.
                    key = _key(matching[0]) if matching else _key(_operator({
                        'id': requirement.get('id'), 'name': requirement.get('name'),
                        'role': requirement.get('role')}))
                    candidates.append((key, 'support', support,
                                       _attributes(slot, requirement, support, policy), requirement))
            if not ambiguous and (not matching or all(row['owned'] is not True for row in matching)):
                row = matching[0] if matching else _operator({
                    'id': requirement.get('id'), 'name': requirement.get('name'),
                    'role': requirement.get('role')})
                candidates.append((_key(row), 'support', row,
                                   [_missing(slot, requirement, 'support', 'verified available operator')]
                                   + schema_missing, requirement))
        choices.append((slot, candidates))

    best = None
    visits = 0
    def choose(index, used, borrowed, assignments, missing, score):
        nonlocal best, visits
        visits += 1
        if visits > 100000:
            return
        if best is not None and score >= best[0]:
            return
        if index == len(choices):
            best = (score, list(assignments), list(missing))
            return
        slot, candidates = choices[index]
        for identity, origin, row, unresolved, requirement in sorted(candidates, key=lambda item: (
                sum(fact['state'] in {'absent', 'insufficient'} for fact in item[3]),
                len(item[3]), item[1] == 'support')):
            if identity in used or borrowed and origin == 'support':
                continue
            hard = sum(fact['state'] in {'absent', 'insufficient'} for fact in unresolved)
            new_score = (score[0] + hard, score[1] + len(unresolved),
                         score[2] + (origin == 'support'))
            choose(index + 1, used | {identity}, borrowed or origin == 'support',
                   assignments + [{'slot': slot, 'name': row.get('name'),
                                   'operator_id': row.get('id'), 'origin': origin,
                                   'requirement': requirement}],
                   missing + unresolved, new_score)
    choose(0, set(), False, [], [], (0, 0, 0))
    if best is None:
        classification, assignments, missing = 'unsatisfied', [], [
            {'field': 'distinct_assignment', 'state': 'insufficient'}]
    else:
        score, assignments, missing = best
        classification = ('unsatisfied' if score[0] else 'unknown' if score[1]
                          else 'needs_verified_support' if score[2] else 'satisfied')
    if visits > 100000 and classification == 'unsatisfied':
        classification = 'unknown'
        missing.append({'field': 'assignment_search_limit', 'state': 'unknown'})
    return {'classification': classification, 'profile_id': box.get('profile_id'),
            'box_revision': box.get('box_revision'), 'assignments': assignments,
            'missing_requirements': missing}
