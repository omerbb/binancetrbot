import pytest

from jev_helpers import make_bot, records
from decision.openrouter import ProviderError


def small_allocation(state, questions, response):
    if 'allocation' in questions:
        response['answers']['allocation']['confidence'] = .2


@pytest.mark.parametrize('budget,expected', [(2000, 500), (4000, 500), (400, 100)])
def test_uncertain_allocation_uses_smallest_size_and_500_cap(tmp_path, budget, expected):
    def configure(cfg):
        cfg.trading.budget_per_trade = budget
        cfg.decision.min_action_confidence = .3
    b,c,f,p = make_bot(tmp_path, configure=configure)
    p.hook = small_allocation
    b.step()
    assert len(b.simulator.positions) == 1
    assert next(iter(b.simulator.positions.values()))['invested_cost'] == expected
    candidate = records(c, 'candidate')[0]
    fallback = next(e['data'] for e in candidate['events'] if e['kind'] == 'allocation_fallback')
    assert fallback['budget_try'] == expected
    assert candidate['teacher']['raw']['answers']['allocation']['confidence'] == .2
    proposal = records(c, 'prebuy')[0]['input']['state']['proposal']
    assert proposal['allocation_fallback'] is True and proposal['budget_try'] == expected
    c.close()


@pytest.mark.parametrize('failure', ['WAIT', 'SKIP', 'low_action', 'WAIT_prebuy', 'CANCEL_prebuy', 'low_prebuy', 'spread'])
def test_allocation_fallback_never_overrides_action_prebuy_or_safety(tmp_path, failure):
    b,c,f,p = make_bot(tmp_path)
    if failure in ('WAIT', 'SKIP'):
        p.choices['action'] = failure
    if failure.endswith('_prebuy') and failure != 'low_prebuy':
        p.choices['prebuy_authorization'] = failure.split('_')[0]
    def hook(state, questions, response):
        small_allocation(state, questions, response)
        if failure == 'low_action' and 'action' in questions:
            response['answers']['action']['confidence'] = .2
        if 'prebuy_authorization' in questions:
            if failure == 'low_prebuy':
                response['answers']['prebuy_authorization']['confidence'] = .2
            if failure == 'spread':
                f.markets['SOL_TRY']['ask'] = 101
    p.hook = hook
    b.step()
    assert not b.simulator.positions
    if failure in ('WAIT', 'SKIP', 'low_action'):
        assert not records(c, 'prebuy')
    c.close()


@pytest.mark.parametrize('second', ['valid', 'invalid', 'network_error'])
def test_invalid_answer_gets_exactly_one_retry_and_keeps_audit(tmp_path, second):
    b,c,f,p = make_bot(tmp_path)
    candidate_calls = []
    def hook(state, questions, response):
        if 'action' not in questions:
            return
        candidate_calls.append(1)
        if len(candidate_calls) == 2 and second == 'network_error':
            raise ProviderError('network_or_timeout')
        if len(candidate_calls) == 1 or second == 'invalid':
            response['answers']['action']['choice'] = 'INVALID'
    p.hook = hook
    b.step()
    assert len(candidate_calls) == 2
    candidate = records(c, 'candidate')[0]
    assert sum(e['kind'] == 'decision_retry' for e in candidate['events']) == 1
    assert bool(b.simulator.positions) == (second == 'valid')
    assert candidate['teacher']['valid'] == (second == 'valid')
    responses = [e['data'] for e in candidate['events'] if e['kind'] == 'model_response']
    assert responses[0]['valid'] is False
    assert responses[0]['raw']['answers']['action']['choice'] == 'INVALID'
    c.close()


@pytest.mark.parametrize('condition', ['expired', 'stopped', 'valid_wait'])
def test_no_schema_retry_after_deadline_stop_or_valid_wait(tmp_path, condition):
    b,c,f,p = make_bot(tmp_path, choices={'action': 'WAIT'})
    calls = []
    def hook(state, questions, response):
        if 'action' not in questions:
            return
        calls.append(1)
        if condition != 'valid_wait':
            response['answers']['action']['choice'] = 'INVALID'
        if condition == 'expired':
            f.clock.advance_to(f.clock() + 9)
        if condition == 'stopped':
            b.is_running = False
    p.hook = hook
    b.step()
    assert len(calls) == 1
    assert not b.simulator.positions
    assert not any(e['kind'] == 'decision_retry' for r in records(c) for e in r['events'])
    c.close()


def test_successful_retry_does_not_reset_decision_expiry(tmp_path):
    b,c,f,p = make_bot(tmp_path)
    calls = []
    def hook(state, questions, response):
        if 'action' not in questions:
            return
        calls.append(state['as_of'])
        f.clock.advance_to(f.clock() + 5)
        if len(calls) == 1:
            response['answers']['action']['choice'] = 'INVALID'
    p.hook = hook
    b.step()
    assert len(calls) == 2 and calls[0] == calls[1]
    assert not b.simulator.positions
    assert records(c, 'candidate')[0]['execution'][-1]['override_reasons'] == ['decision_expired']
    c.close()
