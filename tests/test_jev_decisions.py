import copy,json
import pytest
from jev_helpers import make_bot,advance,records
from decision.openrouter import ProviderError
from decision.contracts import dumps


def test_legacy_filters_are_advisory_not_vetoes(tmp_path,monkeypatch):
    b,c,f,p=make_bot(tmp_path)
    b.strategy.evaluate=lambda *a:('SELL','legacy rejects everything')
    b.scanner.watchlist.is_cooling_down=lambda *a:True
    b.step()
    assert len(b.simulator.positions)==1
    candidate=records(c,'candidate')[0]
    assert candidate['input']['state']['legacy_advisory']['signal']=='SELL'
    assert candidate['input']['state']['radar']['macro_features_available'] is False
    assert records(c,'prebuy')[0]['parent_id']==candidate['decision_id']
    assert candidate['execution'][-1]['status']=='filled'
    assert next(iter(b.simulator.positions.values()))['invested_cost']==1000
    assert not b.live_trader.positions
    c.close()

@pytest.mark.parametrize('action',['WAIT','OBSERVE','SKIP'])
def test_every_nontrade_decision_is_recorded_and_labeled(tmp_path,action):
    b,c,f,p=make_bot(tmp_path,choices={'action':action})
    b.step();first=records(c,'candidate')[0]
    assert not b.simulator.positions and first['teacher']['raw']['answers']['action']['choice']==action
    before=first['request_hash'];advance(f,price=103);b.step()
    first=records(c,'candidate')[0]
    label=next(o for o in first['outcomes'] if o['horizon_seconds']==60)
    assert label['status']=='observed' and label['data']['mid_return_pct']>0
    assert 'hypothetical_buy_hold_net_return_pct' in label['data']
    assert first['request_hash']==before and 'outcomes' not in first['input']
    c.close()

@pytest.mark.parametrize('choice',['WAIT','CANCEL'])
def test_prebuy_owns_authorization(tmp_path,choice):
    b,c,f,p=make_bot(tmp_path,choices={'prebuy_authorization':choice});b.step()
    assert not b.simulator.positions
    child=records(c,'prebuy')[0]
    assert child['teacher']['raw']['answers']['prebuy_authorization']['choice']==choice
    assert child['execution'][-1]['applied_action']==choice
    c.close()

@pytest.mark.parametrize('mode',['timeout','invalid','low_confidence','expired'])
def test_failure_does_not_fall_back_to_legacy_buy(tmp_path,mode):
    b,c,f,p=make_bot(tmp_path,choices={'action':'BUY'})
    if mode=='timeout':p.error=ProviderError('network_or_timeout')
    if mode=='invalid':p.malformed=True
    if mode=='low_confidence':p.confidence=.2
    if mode=='expired':p.hook=lambda *a:f.clock.advance_to(f.clock()+9)
    b.step();assert not b.simulator.positions
    assert any(e['status']=='fallback' for r in records(c) for e in r['execution'])
    c.close()

def test_portfolio_pause_and_flatten(tmp_path):
    b,c,f,p=make_bot(tmp_path,choices={'portfolio_action':'PAUSE_ENTRIES'});b.step()
    assert not b.simulator.positions
    p.choices['portfolio_action']='CONTINUE';advance(f,price=100);b.step();assert b.simulator.positions
    p.choices['portfolio_action']='FLATTEN';advance(f,price=101);b.step()
    assert not b.simulator.positions
    flatten=records(c,'portfolio')[-1]
    assert any(e.get('status')=='filled' for e in flatten['execution'])
    assert any(o['name'].startswith('position:') and o['status']=='observed' for o in flatten['outcomes'])
    c.close()

def test_partial_sell_hold_rollover_and_terminal_pnl(tmp_path):
    b,c,f,p=make_bot(tmp_path);b.step();initial=copy.deepcopy(next(iter(b.simulator.positions.values())))
    p.choices['action']='SELL_PARTIAL';advance(f,price=101);b.step()
    pos=next(iter(b.simulator.positions.values()));assert pos['quantity']==pytest.approx(initial['quantity']/2)
    assert pos['entry_price']==initial['entry_price']
    p.choices['action']='HOLD';advance(f,price=101.1);b.step();assert len(b.simulator.closed_trades)==1
    p.choices['action']='ROLLOVER';advance(f,price=101.2);b.step()
    assert pos['entry_price']==initial['entry_price'] and pos['risk_reference_price']==101.2
    p.choices['action']='SELL';advance(f,price=101.3);b.step();assert not b.simulator.positions
    root=records(c,'candidate')[0]
    outcome=next(o for o in root['outcomes'] if o['name'].startswith('position:'))
    assert outcome['status']=='observed'
    assert outcome['data']['net_realized_pnl_try']==pytest.approx(sum(t['net_pnl'] for t in b.simulator.closed_trades))
    assert outcome['data']['total_fees_try']==pytest.approx(sum(t['total_fees'] for t in b.simulator.closed_trades))
    for held in records(c,'position'):
        assert any(o['name'].startswith('position:') and o['status']=='observed' for o in held['outcomes'])
    c.close()

def test_hard_stop_overrides_hold_and_blocks_instant_rebuy(tmp_path):
    def configure(cfg):cfg.strategy.stop_loss_pct=.85;cfg.strategy.symbol_cooldown_seconds=0;cfg.strategy.loss_cooldown_seconds=180
    b,c,f,p=make_bot(tmp_path,configure=configure);b.step()
    advance(f,price=98);b.step() # fixture says BUY again, but loss cooldown must veto it.
    assert not b.simulator.positions and len(b.simulator.closed_trades)==1
    safety=records(c,'hard_position_stop')[0]
    assert safety['source']=='safety' and safety['teacher'] is None
    assert any('cooldown' in reason for r in records(c,'prebuy') for e in r['execution'] for reason in e.get('override_reasons',[]))
    c.close()

def test_portfolio_hard_stop_and_terminal_state(tmp_path,monkeypatch):
    def configure(cfg):cfg.strategy.stop_loss_pct=20;cfg.strategy.portfolio_stop_loss_pct=.1
    b,c,f,p=make_bot(tmp_path,configure=configure);monkeypatch.setattr(b,'_generate_final_report',lambda:{})
    b.step();advance(f,price=98);b.step()
    assert not b.is_running and b.stop_completed and c.finished
    assert not b.simulator.positions
    assert records(c,'hard_portfolio_stop')[0]['source']=='safety'
    c.close()

@pytest.mark.parametrize('failure',['spread','features','price_drift','operator_stop'])
def test_revalidate_immediately_before_fill(tmp_path,failure):
    b,c,f,p=make_bot(tmp_path,ready=failure!='features')
    if failure=='features':p.choices['action']='BUY'
    def hook(state,qs,response):
        if 'prebuy_authorization' not in qs:return
        if failure=='spread':f.markets['SOL_TRY']['ask']=101.0
        if failure=='price_drift':f.markets['SOL_TRY'].update(bid=100.8,ask=100.9)
        if failure=='operator_stop':b.is_running=False
    p.hook=hook;b.step();assert not b.simulator.positions
    assert any(e['status']=='blocked' for r in records(c,'prebuy') for e in r['execution'])
    c.close()

def test_no_liquidation_from_stale_quote_at_end(tmp_path):
    b,c,f,p=make_bot(tmp_path);b.step()
    f.clock.advance_to(f.clock()+100)
    c.end_session(reason='end_of_data')
    assert b.simulator.positions and not b.simulator.closed_trades
    root=records(c,'candidate')[0]
    assert any(o['name'].startswith('position_open:') and o['status']=='censored' for o in root['outcomes'])
    c.close()

def test_run_config_is_immutable_and_fault_does_not_hang_cli(tmp_path):
    b,c,f,p=make_bot(tmp_path);b.config.trading.budget_per_trade=1999
    with pytest.raises(ValueError,match='Configuration changed'):b.step()
    assert not b.is_running and b.stop_completed and c.faulted
    c.close()

def test_journal_failure_before_intent_never_fills(tmp_path,monkeypatch):
    b,c,f,p=make_bot(tmp_path)
    original=c.store.event
    def event(run,did,kind,data,**kwargs):
        if kind=='execution_intent':raise OSError('simulated disk full')
        return original(run,did,kind,data,**kwargs)
    monkeypatch.setattr(c.store,'event',event)
    with pytest.raises(OSError):b.step()
    assert not b.simulator.positions and c.faulted and not b.is_running and b.stop_completed
    c.close()

def test_no_credentials_in_state_journal_or_teacher(tmp_path):
    b,c,f,p=make_bot(tmp_path)
    b.config.api.api_key='PRIVATE_BINANCE_KEY';b.config.api.secret_key='PRIVATE_BINANCE_SECRET';b.config.auth.password='PRIVATE_PASSWORD'
    b.step()
    text=dumps(records(c));assert all(value not in text for value in ['PRIVATE_BINANCE_KEY','PRIVATE_BINANCE_SECRET','PRIVATE_PASSWORD'])
    assert 'api' not in records(c)[0]['run']['metadata']['configuration']
    c.close()

def test_live_jev_is_explicitly_disabled(tmp_path):
    b,c,f,p=make_bot(tmp_path);c.cfg.trading.mode='live'
    with pytest.raises(ValueError,match='paper/replay only'):c.prepare_start()
    with pytest.raises(ValueError,match='live execution disabled'):c.step()
    assert not b.simulator.positions;c.cfg.trading.mode='simulation';c.close()

def test_round_robin_is_not_quant_score_gate(tmp_path):
    def config(cfg):cfg.decision.max_candidates_per_step=1
    b,c,f,p=make_bot(tmp_path,symbols=('ZZZ_TRY','AAA_TRY','BBB_TRY'),choices={'action':'SKIP'},configure=config)
    for _ in range(3):b.step()
    assert [r['symbol'] for r in records(c,'candidate')]==['AAA_TRY','BBB_TRY','ZZZ_TRY']
    event=c.store.db.execute("SELECT data_json FROM events WHERE kind='candidate_scheduling' LIMIT 1").fetchone()
    assert json.loads(event[0])['deferred_is_not_model_rejection'] is True
    c.close()

def test_slippage_and_fees_apply_to_realized_fills(tmp_path):
    def configure(cfg):cfg.decision.slippage_bps=10
    b,c,f,p=make_bot(tmp_path,configure=configure);b.step()
    initial=next(iter(b.simulator.positions.values()));assert initial['entry_price']==pytest.approx(100.1*1.001)
    p.choices['action']='SELL';advance(f,price=101);b.step()
    trade=b.simulator.closed_trades[0]
    assert trade['exit_price']==pytest.approx(101*.999)
    expected=1000*(1-.001)/(100.1*1.001)*(101*.999)*(1-.001)-1000
    assert trade['net_pnl']==pytest.approx(expected)
    c.close()

def test_rollover_ratcheted_reference_does_not_erase_peaks(tmp_path):
    b,c,f,p=make_bot(tmp_path);b.step()
    p.choices['action']='ROLLOVER';advance(f,price=101);b.step()
    p.choices['action']='HOLD';advance(f,price=103);b.step();advance(f,price=102);b.step()
    pos=next(iter(b.simulator.positions.values()))
    assert pos['risk_highest_price']==103 and pos['risk_reference_price']==101
    assert pos['entry_price']==100.1
    c.close()

def test_custom_allocation_has_an_executable_meaning(tmp_path):
    def configure(cfg):cfg.decision.allocation_fractions={'TINY':.1,'HALF':.5}
    b,c,f,p=make_bot(tmp_path,configure=configure,choices={'allocation':'TINY'})
    b.step();pos=next(iter(b.simulator.positions.values()));assert pos['invested_cost']==200
    assert records(c,'candidate')[0]['teacher']['raw']['answers']['allocation']['choice']=='TINY'
    c.close()

def test_manual_buy_is_never_mislabeled_as_model(tmp_path):
    b,c,f,p=make_bot(tmp_path)
    assert c.manual_buy(None,100,'ambiguous') is None
    pos=c.manual_buy('SOL_TRY',100,'explicit user intent')
    assert pos and not p.calls
    c.manual_close(pos['position_id'],'user closes')
    for row in records(c):
        assert row['source']=='operator' and row['teacher'] is None
    assert b.simulator.closed_trades[0]['decision_source']=='operator'
    c.close()

def test_selling_decision_can_manage_position_when_candles_missing(tmp_path):
    b,c,f,p=make_bot(tmp_path);b.step();p.choices['action']='SELL'
    advance(f,price=101);f.candles['SOL_TRY']={}
    b.step();assert not b.simulator.positions
    held=records(c,'position')[0]
    assert held['input']['state']['market']['features_ready'] is False
    assert held['execution'][-1]['applied_action']=='SELL'
    c.close()

def test_finished_stopped_controller_does_not_start_empty_new_run(tmp_path):
    b,c,f,p=make_bot(tmp_path,choices={'action':'WAIT'});b.step();b.is_running=False;c.end_session(liquidate=False)
    run=c.run_id;b.step()
    assert c.run_id==run and c.store.db.execute('SELECT count(*) FROM runs').fetchone()[0]==1
    c.close();c.close()

def test_bad_engine_never_silently_uses_legacy(tmp_path):
    from config import BotConfig
    from bot import BinanceTrBot
    cfg=BotConfig();cfg.decision.engine='jve'
    with pytest.raises(ValueError,match='no silent legacy fallback'):BinanceTrBot(cfg)
