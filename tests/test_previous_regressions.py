"""Additional deterministic checks; failing tests express expected safety behavior.
Previous eight behavioral assertions; fixture dates updated for closed-candle validation.
"""
import time
import pytest
from config import BotConfig, config_to_dict
from bot import BinanceTrBot
from core.binance_client import BinanceTrClient

@pytest.fixture
def setup_bot(monkeypatch):
    monkeypatch.setattr(BinanceTrClient,'get_symbols',lambda self:[{'symbol':'SOL_TRY','spotTradingEnable':1}])
    def deny_order(*args,**kwargs):
        raise AssertionError('Real order blocked')
    monkeypatch.setattr(BinanceTrClient,'create_order',deny_order)
    def make(auto=True):
        cfg=BotConfig();cfg.trading.mode='simulation'
        cfg.trading.symbol='AUTO' if auto else 'SOL_TRY';cfg.trading.auto_select_coin=auto
        cfg.trading.target_coins_count=1;cfg.trading.max_open_positions=1
        cfg.trading.candidate_observation_seconds=0;cfg.trading.candidate_prebuy_seconds=0
        cfg.trading.require_strict_buy_signal=True;cfg.trading.auto_fill_portfolio=False
        cfg.strategy.active='fee_recovery';cfg.strategy.enable_partial_tp=False
        cfg.trading.prevent_rebuy_churn=False
        b=BinanceTrBot(cfg)
        data={'price':100.0,'spread_pct':0.05,'available':True}
        def best(sym):
            if not data['available']: return None
            p=data['price'];a=p*(1+data['spread_pct']/100)
            return {'bid':p,'ask':a,'mid':(a+p)/2,'spread':a-p,'spread_pct':data['spread_pct']}
        monkeypatch.setattr(b.client,'get_best_prices',best)
        monkeypatch.setattr(b.client,'get_klines',lambda *a,**k:[[(int(time.time())//60-50+i)*60000,100,100.1,99.9,100,100,(int(time.time())//60-49+i)*60000-1] for i in range(50)])
        pair={'symbol':'SOL_TRY','price':100.0,'change_pct':5.0,'velocity_1m_pct':1.0,'quant_score':8.0,'volume_surge_ratio':2.0,'updated_at':time.time()}
        b.scanner.cached_top_pairs=[pair]
        monkeypatch.setattr(b.scanner,'scan_top_active_pairs',lambda **kwargs:[pair])
        b.market_data.cache_ttl_seconds=0
        return b,data,pair
    return make

@pytest.mark.parametrize('exit_price,expected_reason',[(102.0,'TAKE-PROFIT'),(98.0,'STOP-LOSS')])
def test_real_strategy_buy_and_risk_exit(setup_bot,exit_price,expected_reason):
    b,data,pair=setup_bot();b.step()
    assert len(b.simulator.positions)==1
    assert 'Komisyon Kurtaran' in next(iter(b.simulator.positions.values()))['entry_reason']
    data['price']=exit_price;b.step()
    assert not b.simulator.positions
    assert expected_reason in b.simulator.closed_trades[-1]['reason']
    result=b.stop()
    assert result['status']=='stopped' and 'error' not in result
    assert b.last_report


def test_wide_actual_spread_must_block_auto_buy(setup_bot):
    b,data,pair=setup_bot();data['spread_pct']=1.0
    assert b.config.trading.max_allowed_spread_pct==0.20
    b.step()
    assert len(b.simulator.positions)==0, 'Bought despite actual 1.00% spread and configured 0.20% maximum'


def test_mask_secrets_must_not_return_plaintext():
    cfg=BotConfig();cfg.api.secret_key='FAKE-TEST-SECRET-DO-NOT-USE'
    payload=config_to_dict(cfg,mask_secrets=True)
    assert payload['api'].get('secret_key') != cfg.api.secret_key, 'Raw secret remains in masked config'


def test_runtime_fee_change_must_update_execution_fee(setup_bot):
    b,_,_=setup_bot();b.config.trading.fee_rate_pct=0.50;b.apply_config()
    b.simulator.buy('SOL_TRY',100.0,1000.0,reason='fee update test')
    assert b.simulator.all_orders[-1]['fee']==pytest.approx(5.0), 'Execution still charges old 0.10% instead of new 0.50%'


def test_single_symbol_loss_cooldown_must_block_immediate_rebuy(setup_bot):
    b,data,_=setup_bot(auto=False)
    b.simulator.buy('SOL_TRY',100.0,2000.0,reason='seed position')
    b.risk_manager.last_trade_time=time.time()-60
    data['price']=98.0;b.step()
    assert len(b.simulator.closed_trades)==1
    assert 'SOL_TRY' in b.risk_manager.symbol_loss_exit_times
    assert not b.simulator.positions, 'Same symbol bought again in the stop-loss step despite 180 second loss cooldown'


def test_stale_market_data_must_not_open_new_position(setup_bot):
    b,data,_=setup_bot()
    engine=b.market_data;engine.update_market_state(force=True)
    engine.last_update_time=time.time()-300;data['available']=False
    b.step()
    assert not b.simulator.positions, 'Opened using 5-minute-old cached order-book snapshot after fresh-data failure'


def test_one_minute_auto_session_has_no_entry_window(setup_bot):
    """Behavior check: confirms the short-session limitation rather than assuming a bug."""
    b,_,_=setup_bot();now=time.time()
    b.is_running=True;b.session_start_time=now-30
    b.calibration_end_time=now-10;b.calibration_completed=True;b.session_duration_seconds=60
    b.step()
    assert not b.simulator.positions
    b.session_duration_seconds=120;b.step()
    assert len(b.simulator.positions)==1
    b.is_running=False
