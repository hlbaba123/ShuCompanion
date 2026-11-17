"""Numeric-only comparison of the frozen EXE's unchanged formal cases."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / 'artifacts/20261112-thinking-three-rounds'
PREVIOUS = ROOT / 'artifacts/20261112-continuity-three-rounds'


def thinking_metrics(items):
    return {
        'requests': len(items),
        'thinking_enabled_requests': sum(m.get('thinking_enabled') is True for m in items),
        'auto_tool_choice_requests': sum(m.get('tool_choice') == 'auto' for m in items),
        'reasoning_content_chars': sum(m.get('reasoning_content_chars', 0) for m in items),
        'reasoning_tokens': sum(m.get('usage', {}).get('completion_tokens_details', {}).get('reasoning_tokens', 0)
                                for m in items),
    }


def batch(directory):
    rounds = []
    for name in ('round-01', 'round-02', 'round-03'):
        report = json.loads((directory / name / 'report.json').read_text(encoding='utf-8-sig'))
        if len(report.get('turns', [])) != 7 or 'battle_start_submissions' not in report:
            raise ValueError(f'{name} is not a finished formal round')
        turns = report['turns']
        rounds.append({
            'round': name,
            'version': report['runtime']['version'],
            'turns': len(turns),
            'elapsed_seconds': round(sum(t['elapsed_seconds'] for t in turns), 3),
            'model_requests': sum(t['usage']['model_requests'] for t in turns),
            'total_tokens': sum(t['usage']['total_tokens'] for t in turns),
            'battle_start_submissions': report['battle_start_submissions'],
            'intent': thinking_metrics(report.get('intent_metrics', [])),
            'leader': thinking_metrics(report.get('role_metrics', {}).get('leader', [])),
            'turn_usage': [{
                'turn': index + 1, 'seconds': t['elapsed_seconds'], **t['usage'],
                'errors': t['errors'], 'timed_out': t['timed_out'],
            } for index, t in enumerate(turns)],
        })
    return {'rounds': rounds, 'totals': {
        key: round(sum(r[key] for r in rounds), 3)
        for key in ('turns', 'elapsed_seconds', 'model_requests', 'total_tokens', 'battle_start_submissions')
    }}


if __name__ == '__main__':
    result = {'current': batch(CURRENT), 'previous': batch(PREVIOUS),
              'note': 'Numeric request evidence only; correctness is judged in independent-audit.json.'}
    (CURRENT / 'numeric-comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({name: result[name]['totals'] for name in ('current', 'previous')}, ensure_ascii=False))
