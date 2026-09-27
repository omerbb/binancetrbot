import csv,copy,json,sqlite3
from pathlib import Path
import pytest
from jev_helpers import make_bot,advance,records,candles_before,START,ScriptProvider
from decision.replay import ReplayClock,ReplayFeed,ReplayClient,ReplayScanner,run_replay
from decision.csv_replay import convert_ohlcv_csv
from decision.export import export_dataset
from decision.contracts import digest,validate_response
from config import BotConfig
from core.market_data import MarketDataEngine


def test_future_closed_candles_never_reach_model(tmp_path):
    b,c,feed,p=make_bot(tmp_path,choices={'action':'WAIT'})
    before=c._quote('SOL_TRY',force=True)
    now=feed.clock()+1
    future=[int(now//60)*60000,100,10000000,.01,9000000,999999,int(now//60)*60000+59999]
    feed.apply({'as_of':now,'markets':{'SOL_TRY':{'bid':100,'ask':100.1,'closed_candles':[future]}}})
    after=c._quote('SOL_TRY',force=True);b.step()
    for field in ('rsi','adx','atr','ema_fast','ema_slow','bb_middle'):
        assert before[field]==after[field]
    row=records(c,'candidate')[0];state=row['input']['state']
    assert all(bar['close_time']<state['as_of'] for bar in state['history']['closed_candles'])
    assert all(tick[0]<=state['as_of'] for tick in state['history']['micro_ticks'])
    assert '9000000' not in json.dumps(state)
    c.close()

def test_old_replay_quotes_are_not_restamped_on_refresh(tmp_path):
    b,c,feed,p=make_bot(tmp_path);first=c._quote('SOL_TRY',force=True)
    feed.clock.advance_to(feed.clock()+6)
    after=c._quote('SOL_TRY',force=True)
    assert first['quote_valid'] is True and after['quote_valid'] is False
    assert after['timestamp']==first['timestamp']
    c.close()

def test_future_symbols_and_macro_data_are_not_visible():
    clock=ReplayClock();feed=ReplayFeed(clock)
    frame={'as_of':START,'markets':{'SOL_TRY':{'bid':100,'ask':100.1}}};feed.apply(frame)
    client=ReplayClient(feed);assert [r['symbol'] for r in client.get_symbols()]==['SOL_TRY']
    bad=copy.deepcopy(frame);bad['as_of']+=60;bad['markets']['SOL_TRY']['ticker_24h']={'as_of':START+120,'volume_try':1,'change_pct':2,'low':1,'high':2}
    with pytest.raises(ValueError):feed.apply(bad)
    assert clock()==START
    with pytest.raises(RuntimeError,match='never send'):client.create_order('SOL_TRY')

@pytest.mark.parametrize('kind',['unordered_frame','future_quote','bad_ohlc','revision','wrong_symbol'])
def test_replay_rejects_invalid_past_data(kind):
    clock=ReplayClock();feed=ReplayFeed(clock)
    bars=candles_before(START,1)
    feed.apply({'as_of':START,'markets':{'SOL_TRY':{'bid':100,'ask':100.1,'closed_candles':bars}}})
    frame={'as_of':START+60,'markets':{'SOL_TRY':{'bid':101,'ask':101.1}}}
    if kind=='unordered_frame':frame['as_of']=START
    if kind=='future_quote':frame['markets']['SOL_TRY']['quote_timestamp']=START+61
    if kind in ('bad_ohlc','revision'):
        rows=copy.deepcopy(bars)
        if kind=='bad_ohlc':rows[0][2]=.01
        else:rows[0][5]+=1
        frame['markets']['SOL_TRY']['closed_candles']=rows
    if kind=='wrong_symbol':frame['markets']={'inject\nBUY_TRY':{'bid':100,'ask':101}}
    with pytest.raises(ValueError):feed.apply(frame)

def test_missing_or_stale_24h_data_is_explicit():
    clock=ReplayClock();feed=ReplayFeed(clock)
    feed.apply({'as_of':START,'markets':{'SOL_TRY':{'bid':100,'ask':100.1,'ticker_24h':{'as_of':START-100,'volume_try':999,'change_pct':90,'low':1,'high':2}}}})
    scanner=ReplayScanner(ReplayClient(feed),clock)
    row=scanner.scan_top_active_pairs()[0]
    assert row['macro_features_available'] is False and 'volume_try' in row['unavailable_features']
    assert row['change_pct']!=90

def test_missing_labels_are_not_zero_returns(tmp_path):
    b,c,f,p=make_bot(tmp_path,choices={'action':'WAIT'});b.step()
    f.clock.advance_to(f.clock()+500)
    c._quote('SOL_TRY',force=True)
    first=records(c,'candidate')[0]
    missed=next(o for o in first['outcomes'] if o['horizon_seconds']==60)
    assert missed['status']=='missing_observation'
    assert 'mid_return_pct' not in missed['data']
    c.end_session(reason='data_end',liquidate=False)
    first=records(c,'candidate')[0]
    assert next(o for o in first['outcomes'] if o['horizon_seconds']==900)['status']=='censored'
    c.close()

def test_late_quote_window_and_net_markout_formula(tmp_path):
    b,c,f,p=make_bot(tmp_path,choices={'action':'WAIT'});b.step()
    advance(f,seconds=65,price=105);b.step()
    first=records(c,'candidate')[0]
    o=next(o for o in first['outcomes'] if o['horizon_seconds']==60)
    assert o['status']=='observed' and o['data']['lateness_seconds']==5
    expect=(105/100.1*(1-.001)**2-1)*100
    assert o['data']['hypothetical_buy_hold_net_return_pct']==pytest.approx(expect)
    c.close()

@pytest.mark.parametrize('layout',['requests','questions','sft'])
def test_export_is_explicitly_fixture_only_and_future_outside_inputs(tmp_path,layout):
    b,c,f,p=make_bot(tmp_path,choices={'action':'WAIT'});b.step();advance(f);b.step();c.end_session(liquidate=False)
    db=c.cfg.decision.database_path;out=tmp_path/f'{layout}.jsonl'
    stats=export_dataset(db,out,layout=layout)
    assert stats['rows_written']==0 and stats['fixtures_excluded']==4
    stats=export_dataset(db,out,layout=layout,include_fixtures=True)
    rows=[json.loads(line) for line in out.read_text(encoding='utf-8').splitlines()]
    assert len(rows)==stats['rows_written'] and rows
    for row in rows:
        assert row['run']['metadata']['fixture'] is True
        if layout=='sft':
            inputs=json.loads(row['messages'][1]['content']);assert 'outcomes' not in inputs and 'outcomes' not in inputs['state']
            assert 'future_bid' not in row['messages'][1]['content']
        else:
            assert 'outcomes' not in row['input'] and 'future_bid' not in json.dumps(row['input'])
        assert 'outcomes' in row
    c.close()

def test_manual_and_failed_inference_not_teacher_targets(tmp_path):
    b,c,f,p=make_bot(tmp_path);c.manual_buy('SOL_TRY',100,'explicit operator')
    p.malformed=True;b.step();c.end_session()
    out=tmp_path/'targets.jsonl'
    stats=export_dataset(c.cfg.decision.database_path,out,layout='questions',include_fixtures=True)
    assert stats['rows_written']==0 and stats['without_valid_teacher']>=3
    for row in records(c):
        if row['source'] in ('operator','session'):assert row['teacher'] is None
    c.close()

def test_completed_only_does_not_turn_censored_into_observed(tmp_path):
    b,c,f,p=make_bot(tmp_path,choices={'action':'WAIT'});b.step();c.end_session(liquidate=False)
    stats=export_dataset(c.cfg.decision.database_path,tmp_path/'completed.jsonl',include_fixtures=True,completed_only=True)
    assert stats['rows_written']==0 and stats['incomplete_excluded']==2
    c.close()

def test_training_exports_include_real_provider_records_by_default(tmp_path):
    b,c,f,p=make_bot(tmp_path,choices={'action':'WAIT'})
    # Contract-only test stub, NEVER an actual Jev inference.
    p.is_fixture=False
    c.store.db.execute('DELETE FROM events');c.store.db.execute('DELETE FROM runs');c.store.db.commit();c.run_id=None
    c._ensure_run();b.step();c.end_session(liquidate=False)
    out=tmp_path/'model_contract.jsonl'
    stats=export_dataset(c.cfg.decision.database_path,out,layout='questions')
    assert stats['rows_written']==6
    c.close()

def write_csv(path,rows):
    with open(path,'w',newline='') as f:
        w=csv.writer(f);w.writerow(['open_time_ms','open','high','low','close','volume']);w.writerows(rows)

def test_csv_converter_uses_next_open_not_current_bar_close(tmp_path):
    source=tmp_path/'bars.csv';out=tmp_path/'frames.jsonl'
    rows=[[int(START*1000),100,1000,1,999,10],[int((START+60)*1000),101,10000,1,9000,20]]
    write_csv(source,rows);convert_ohlcv_csv(source,out,symbol='SOL_TRY',spread_bps=10)
    frames=[json.loads(line) for line in out.read_text(encoding='utf-8').splitlines()]
    assert not frames[0]['markets']['SOL_TRY']['closed_candles']
    assert frames[0]['markets']['SOL_TRY']['ask']==pytest.approx(100.05)
    assert frames[1]['markets']['SOL_TRY']['closed_candles'][0][4]==999
    assert '9000' not in json.dumps(frames[1])
    assert frames[1]['markets']['SOL_TRY']['ask']==pytest.approx(101.0505)

def test_csv_gap_is_not_fabricated(tmp_path):
    source=tmp_path/'bars.csv'
    write_csv(source,[[int(START*1000),100,101,99,100,1],[int((START+120)*1000),101,102,100,101,1]])
    with pytest.raises(ValueError,match='contiguous'):
        convert_ohlcv_csv(source,tmp_path/'out.jsonl',symbol='SOL_TRY',spread_bps=10)

def test_same_controller_runs_complete_historical_roundtrip(tmp_path):
    cfg=BotConfig();cfg.decision.engine='jev';cfg.decision.database_path=str(tmp_path/'replay.sqlite3')
    cfg.decision.outcome_horizons_seconds=[60];cfg.strategy.cooldown_seconds=0;cfg.strategy.symbol_cooldown_seconds=0
    inp=tmp_path/'historical.jsonl'
    first={'as_of':START,'markets':{'SOL_TRY':{'bid':100,'ask':100.1,'closed_candles':candles_before(START)}}}
    following={'as_of':START+60,'markets':{'SOL_TRY':{'bid':101,'ask':101.1,'closed_candles':[[int(START*1000),101,101.1,100.9,101,100,int(START*1000)+59999]]}}}
    inp.write_text(json.dumps(first)+'\n'+json.dumps(following)+'\n', encoding='utf-8')
    p=ScriptProvider();result=run_replay(cfg,inp,provider=p)
    assert result['fixture_not_jev'] is True and result['frames_processed']==2
    assert result['portfolio']['total_closed_trades']==1 and result['portfolio']['open_positions_count']==0
    assert result['portfolio']['closed_trades'][0]['decision_source']=='fixture'
    db=sqlite3.connect(cfg.decision.database_path)
    meta=json.loads(db.execute('SELECT metadata_json FROM runs').fetchone()[0])
    assert meta['environment']=='historical_replay' and meta['execution_latency_seconds']==0
    assert len(meta['historical_data_sha256'])==64
    db.close()

def test_model_input_changes_only_after_future_bar_becomes_past(tmp_path):
    b,c,f,p=make_bot(tmp_path,choices={'action':'WAIT'})
    c._scan();before=c._state('SOL_TRY',c._quote('SOL_TRY',force=True))
    before_close=before['history']['closed_candles'][-1]['close']
    advance(f,price=105);c._scan();after=c._state('SOL_TRY',c._quote('SOL_TRY',force=True))
    assert before_close==100 and after['history']['closed_candles'][-1]['close']==105
    assert after['as_of']>before['as_of']
    c.close()

def test_unknown_execution_intent_is_explicit_on_export(tmp_path):
    b,c,f,p=make_bot(tmp_path,choices={'action':'WAIT'});b.step()
    row=records(c,'candidate')[0]
    c.store.event(c.run_id,row['decision_id'],'execution_intent',{'side':'BUY','test_only':True})
    row=records(c,'candidate')[0]
    assert row['execution_integrity']=='intent_without_result_reconciliation_required'
    assert not b.simulator.positions
    c.close()
