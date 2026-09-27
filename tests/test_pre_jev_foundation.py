"""Pre-JEV data correctness and safety regressions. No JEV/API calls or real orders."""
import json
import math
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from config import BotConfig, config_to_dict, update_config_from_dict
from core.binance_client import BinanceTrClient
from core.market_data import MarketDataEngine
from core.market_scanner import MarketScanner, MicroMomentumTracker
from core.simulator import SimulatorEngine
from test_previous_regressions import setup_bot


def candles(as_of, closes=None):
    closes = closes or [100.0] * 50
    start = (int(as_of) // 60 - len(closes)) * 60000
    return [[start+i*60000, c, c+0.1, c-0.1, c, 10, start+(i+1)*60000-1]
            for i, c in enumerate(closes)]


@pytest.fixture
def market(monkeypatch):
    now = [1_800_000_045.0]
    client = BinanceTrClient()
    client.kline_sources['SOL_TRY'] = 'synthetic_closed_candles'
    state = {'bid': 100.0, 'ask': 100.05, 'klines': candles(now[0])}
    monkeypatch.setattr(client, 'get_best_prices', lambda symbol: {'bid': state['bid'], 'ask': state['ask']})
    monkeypatch.setattr(client, 'get_klines', lambda *args, **kwargs: state['klines'])
    engine = MarketDataEngine(client, 'SOL_TRY', clock=lambda: now[0])
    return engine, state, now


def test_rsi_wilder_seed_and_recursion(market):
    engine, _, _ = market
    # Exact rational result from seven price differences: 7450/129.
    assert engine.calculate_rsi([10, 11, 9, 12, 11, 13, 10, 12], 3) == pytest.approx(57.75)


def test_atr_wilder_seed_and_recursion(market):
    engine, _, _ = market
    closes = [10, 11, 9, 12, 11, 13, 10, 12]
    # TR = [2,3,4,2,3,4,3], final Wilder ATR(3) = 253/81.
    assert engine.calculate_atr([c+1 for c in closes], [c-1 for c in closes], closes, 3) == pytest.approx(253/81, abs=1e-6)


def test_adx_is_smoothed_dx_not_latest_dx(market):
    engine, _, _ = market
    closes = [10, 11, 9, 12, 11, 13, 10, 12]
    result = engine.calculate_adx([c+1 for c in closes], [c-1 for c in closes], closes, 3)
    # DM/TR sums seed with period-1 transitions; seed ADX averages 3 DXs.
    # Exact ADX=2235704/83655; last DX=17.06666..., not ADX.
    assert result == {'adx': 26.73, 'plus_di': 39.96, 'minus_di': 28.31}


@pytest.mark.parametrize('trend,expected', [(0, 50.0), (1, 100.0), (-1, 0.0)])
def test_rsi_degenerate_series_are_finite(market, trend, expected):
    engine, _, _ = market
    assert engine.calculate_rsi([100+i*trend for i in range(30)]) == expected


def test_open_and_future_candles_cannot_change_features(market):
    engine, state, now = market
    before = engine.update_market_state(force=True)
    boundary = int(now[0]) // 60 * 60000
    state['klines'] += [[boundary, 100, 100000, 0.1, 90000, 10, boundary+59999],
                        [boundary+60000, 100, 200000, 0.1, 190000, 10, boundary+119999]]
    after = engine.update_market_state(force=True)
    for name in ('rsi', 'adx', 'atr', 'ema_fast', 'ema_slow', 'bb_middle', 'closed_candles_count'):
        assert after[name] == before[name]
    assert after['features_ready'] is True
    assert after['last_closed_candle_at'] < after['as_of']


@pytest.mark.parametrize('fault', ['nan', 'invalid_ohlc', 'missing_time', 'duplicate', 'gap', 'out_of_order'])
def test_bad_candles_are_not_ready(market, fault):
    engine, state, _ = market
    if fault == 'nan': state['klines'][20][4] = float('nan')
    elif fault == 'invalid_ohlc': state['klines'][20][2] = 90.0
    elif fault == 'missing_time': state['klines'][20] = {'open': 100, 'high': 101, 'low': 99, 'close': 100}
    elif fault == 'duplicate': state['klines'][20] = state['klines'][19][:]
    elif fault == 'gap': del state['klines'][20]
    elif fault == 'out_of_order': state['klines'][20], state['klines'][21] = state['klines'][21], state['klines'][20]
    snap = engine.update_market_state(force=True)
    assert snap['features_ready'] is False
    assert any('invalid_candles' in reason for reason in snap['data_quality_reasons'])


def test_ticks_never_substitute_for_missing_candles(market):
    engine, state, _ = market
    state['klines'] = []
    for i in range(35):
        state['bid'] = 100 + i * 0.001
        snap = engine.update_market_state(force=True)
    assert len(engine.price_history) == 35
    assert engine.candle_closes == []
    assert snap['features_ready'] is False


@pytest.mark.parametrize('case', ['short_history', 'stale_candles', 'stale_quote', 'failed_refresh'])
def test_readiness_requires_complete_fresh_inputs(market, monkeypatch, case):
    engine, state, now = market
    if case == 'short_history': state['klines'] = candles(now[0], [100] * 20)
    elif case == 'stale_candles': state['klines'] = candles(now[0] - 300)
    snap = engine.update_market_state(force=True)
    if case == 'stale_quote':
        now[0] += 6
        snap = engine.get_snapshot()
    elif case == 'failed_refresh':
        state['klines'] = []
        snap = engine.update_market_state(force=True)
    assert snap['features_ready'] is False


def test_snapshots_are_not_shared_mutable_strategy_inputs(market):
    engine, _, _ = market
    first = engine.update_market_state(force=True)
    first['rsi'] = -123
    first['radar_only_field'] = 'must not persist'
    second = engine.update_market_state()
    assert second['rsi'] == 50
    assert 'radar_only_field' not in second
    assert second['candle_source'] == 'synthetic_closed_candles'
    assert second['candle_interval'] == '1m'


def test_configured_indicator_periods_reach_market_engine(market):
    engine, state, now = market
    cfg = BotConfig()
    cfg.strategy.rsi_period = 3
    cfg.strategy.bollinger_period = 5
    cfg.strategy.ema_fast = 3
    cfg.strategy.ema_slow = 6
    engine.configure(cfg.strategy, cfg.trading)
    values = [100 + ((i * 7) % 11) for i in range(50)]
    state['klines'] = candles(now[0], values)
    snap = engine.update_market_state(force=True)
    assert snap['features_ready'] is True
    assert snap['rsi'] == engine.calculate_rsi(values, 3)
    assert snap['bb_middle'] == pytest.approx(sum(values[-5:]) / 5)
    assert snap['ema_slow'] == engine.calculate_ema(values, 6)
    assert snap['indicator_periods']['ema_slow'] == 6


@pytest.mark.parametrize('bid,ask', [(0,1), (-1,1), (1,0.9), (float('nan'),1), (1,float('inf')), ('bad',1)])
def test_client_rejects_invalid_order_book(monkeypatch, bid, ask):
    client = BinanceTrClient()
    monkeypatch.setattr(client, 'get_depth', lambda *a, **k: {'code': 0, 'data': {'bids': [[bid, 1]], 'asks': [[ask, 1]]}})
    assert client.get_best_prices('SOL_TRY') is None


def test_auto_execution_uses_local_ask_not_radar_price(setup_bot):
    bot, data, pair = setup_bot()
    pair['price'] = 80.0
    bot.step()
    pos = next(iter(bot.simulator.positions.values()))
    assert pos['entry_price'] == pytest.approx(100.05)
    assert bot.latest_snapshot['radar']['price'] == 80.0
    assert bot.latest_snapshot['change_24h_pct'] == pair['change_pct']


@pytest.mark.parametrize('auto', [False, True])
def test_entry_quality_blocks_before_strategy_evaluation(setup_bot, auto):
    bot, data, _ = setup_bot(auto=auto)
    data['available'] = False
    bot.strategy.evaluate = Mock(side_effect=AssertionError('Bad inputs reached strategy'))
    bot.step()
    bot.strategy.evaluate.assert_not_called()
    assert not bot.simulator.positions


def test_single_symbol_wide_spread_blocks_buy(setup_bot):
    bot, data, _ = setup_bot(auto=False)
    data['spread_pct'] = 0.21
    bot.step()
    assert not bot.simulator.positions


def test_spread_guard_uses_unrounded_book_not_supplied_spread(setup_bot):
    bot, _, _ = setup_bot(auto=False)
    snapshot = {'bid': 100, 'ask': 100.20001, 'timestamp': time.time(), 'quote_valid': True,
                'features_ready': True, 'spread_pct': 0.0}
    assert bot._entry_data_ok(snapshot) is False


@pytest.mark.parametrize('offset', [-30, 30])
def test_old_or_future_radar_candidate_cannot_buy(setup_bot, offset):
    bot, _, pair = setup_bot()
    pair['updated_at'] = time.time() + offset
    bot.step()
    assert not bot.simulator.positions


def test_no_implicit_cash_injection(setup_bot):
    bot, _, _ = setup_bot()
    bot.simulator.cash = 5.0
    pos = bot.force_test_buy('SOL_TRY', budget=1000)
    assert pos is None
    assert bot.simulator.cash == 5.0
    assert not bot.simulator.all_orders


def test_masked_secret_roundtrip_preserves_server_secret():
    cfg = BotConfig()
    cfg.api.secret_key = 'FAKE_SECRET_NEVER_USE'
    payload = config_to_dict(cfg, mask_secrets=True)
    assert cfg.api.secret_key not in json.dumps(payload)
    update_config_from_dict(cfg, payload)
    assert cfg.api.secret_key == 'FAKE_SECRET_NEVER_USE'


def test_account_credentials_are_only_request_local(monkeypatch):
    client = BinanceTrClient(api_key='FAKE_KEY', secret_key='FAKE_SECRET')
    assert 'X-MBX-APIKEY' not in client.session.headers
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {'code': 0, 'data': []})
    monkeypatch.setattr(client.session, 'get', get)
    client.get_account_spot()
    assert calls[-1][1]['headers']['X-MBX-APIKEY'] == 'FAKE_KEY'
    assert 'X-MBX-APIKEY' not in client.session.headers
    client.get_klines('SOL_TRY')
    for url, kwargs in calls[1:]:
        assert not kwargs.get('headers', {}).get('X-MBX-APIKEY')


def test_runtime_fee_change_affects_buy_and_sell(setup_bot):
    bot, _, _ = setup_bot()
    bot.config.trading.fee_rate_pct = 0.5
    bot.apply_config()
    p = bot.simulator.buy('SOL_TRY', 100, 1000)
    trade = bot.simulator.sell(p['position_id'], 100)
    assert p['entry_fee'] == pytest.approx(5)
    assert trade['total_fees'] == pytest.approx(9.975)
    assert trade['net_pnl'] == pytest.approx(-9.975)


@pytest.mark.parametrize('value', [0.0, -1.0, float('nan'), float('inf')])
def test_invalid_buy_inputs_do_not_mutate_account(value):
    sim = SimulatorEngine()
    assert sim.buy('SOL_TRY', value, 1000) is None
    assert sim.buy('SOL_TRY', 100, value) is None
    assert sim.cash == 10000 and not sim.positions and not sim.all_orders


@pytest.mark.parametrize('fraction', [0.0, -1.0, 1.1, float('nan'), float('inf')])
def test_invalid_partial_fraction_does_not_sell(fraction):
    sim = SimulatorEngine()
    p = sim.buy('SOL_TRY', 100, 1000)
    assert sim.sell(p['position_id'], 100, fraction=fraction) is None
    assert sim.cash == 9000 and p['quantity'] == pytest.approx(9.99)


def test_almost_full_partial_sale_preserves_remainder_and_pnl():
    sim = SimulatorEngine()
    p = sim.buy('SOL_TRY', 100, 1000)
    first = sim.sell(p['position_id'], 102, fraction=0.999)
    assert first['is_partial'] is True
    assert p['position_id'] in sim.positions
    assert p['quantity'] == pytest.approx(0.00999)
    summary = sim.get_summary()
    assert summary['total_pnl'] == pytest.approx(summary['realized_pnl'] + summary['unrealized_pnl'], abs=0.02)
    sim.sell(p['position_id'], 102)
    assert not sim.positions
    assert sim.cash - sim.initial_balance == pytest.approx(sum(t['net_pnl'] for t in sim.closed_trades))


def test_missing_book_never_fabricates_exit_fills(setup_bot):
    bot, data, _ = setup_bot()
    p = bot.simulator.buy('SOL_TRY', 100, 1000)
    data['available'] = False
    bot.step()
    assert bot.close_single_position(p['position_id']) is None
    assert bot.force_close_all() == []
    assert p['position_id'] in bot.simulator.positions
    assert not bot.simulator.closed_trades


def scanner_with(monkeypatch, payload, *, status=200):
    c = BinanceTrClient()
    monkeypatch.setattr(c, 'get_symbols', lambda: [{'symbol': 'SOL_TRY', 'spotTradingEnable': 1}])
    scanner = MarketScanner(client=c, min_volume_try=0)
    monkeypatch.setattr('core.market_scanner.requests.get', lambda *a, **k: SimpleNamespace(status_code=status, json=lambda: payload))
    return scanner


def ticker(gain='0.2'):
    return {'symbol': 'SOLTRY', 'quoteVolume': '10000000', 'priceChangePercent': gain,
            'lastPrice': '100', 'highPrice': '105', 'lowPrice': '95', 'closeTime': int(time.time()*1000)}


def test_failed_scan_cannot_reuse_stale_leaders(monkeypatch):
    sc = scanner_with(monkeypatch, None, status=503)
    sc.cached_top_pairs = [{'symbol': 'SOL_TRY', 'change_pct': 10, 'updated_at': time.time()-100}]
    sc.last_scan_time = time.time()-100
    assert sc.scan_top_active_pairs() == []


def test_successful_empty_scan_clears_old_candidates(monkeypatch):
    sc = scanner_with(monkeypatch, [])
    sc.cached_top_pairs = [{'symbol': 'SOL_TRY', 'change_pct': 10, 'updated_at': time.time()-100}]
    assert sc.scan_top_active_pairs(force_refresh=True) == []
    assert sc.cached_top_pairs == []


def test_min_gain_filter_is_not_silently_relaxed(monkeypatch):
    sc = scanner_with(monkeypatch, [ticker('0.2')])
    assert sc.scan_top_active_pairs(min_gain_pct=0.5) == []
    assert sc.scan_top_active_pairs(min_gain_pct=0.5) == []  # cache branch too


def test_unverified_local_listing_cannot_be_assumed(monkeypatch):
    sc = scanner_with(monkeypatch, [ticker('5')])
    sc.tr_listed_symbols.clear()
    monkeypatch.setattr(sc.client, 'get_symbols', lambda: [])
    assert sc.scan_top_active_pairs() == []


def test_rolling_24h_volume_is_identified_as_proxy(monkeypatch):
    sc = scanner_with(monkeypatch, [ticker('5')])
    result = sc.scan_top_active_pairs()
    assert result[0]['volume_surge_is_proxy'] is True
    assert result[0]['true_rvol_available'] is False
    assert result[0]['source'].startswith('https://api.binance.')


def test_stale_micro_history_is_not_reused_as_current_momentum(monkeypatch):
    tracker = MicroMomentumTracker()
    now = time.time()
    tracker.record_tick('SOL_TRY', 100, timestamp=now-120)
    tracker.record_tick('SOL_TRY', 105, timestamp=now-90)
    metrics = tracker.get_micro_metrics('SOL_TRY')
    assert metrics['is_qualified'] is False
    assert metrics['velocity_1m_pct'] == 0.0


def test_epoch_zero_and_ordering_are_respected_in_ticks():
    tracker = MicroMomentumTracker()
    tracker.record_tick('SOL_TRY', 100, timestamp=0.0)
    tracker.record_tick('SOL_TRY', 101, timestamp=1.0)
    tracker.record_tick('SOL_TRY', 500, timestamp=0.5)
    assert list(tracker.history['SOL_TRY']) == [(0.0,100,0.0),(1.0,101,0.0)]


def test_observation_and_btc_settings_reach_real_fields(setup_bot):
    bot, _, _ = setup_bot()
    bot.config.trading.min_observation_gain_pct = 0.7
    bot.config.trading.candidate_timeout_cooldown_seconds = 25
    bot.config.trading.btc_dump_shield_pct = 0.9
    bot.apply_config()
    assert bot.scanner.tracker.min_momentum_pct == 0.7
    assert bot.scanner.tracker.cooldown_seconds == 25
    assert bot.scanner.btc_dump_shield_pct == 0.9


def test_observation_duration_and_positive_tick_count_are_enforced(monkeypatch):
    now = time.time()
    monkeypatch.setattr('core.market_scanner.time.time', lambda: now)
    tracker = MicroMomentumTracker()
    tracker.min_observation_seconds = 12
    tracker.min_burst_count = 2
    tracker.record_tick('SOL_TRY', 100, timestamp=now-5)
    tracker.record_tick('SOL_TRY', 102, timestamp=now-1)
    assert tracker.get_micro_metrics('SOL_TRY')['is_qualified'] is False
    tracker.record_tick('SOL_TRY', 103, timestamp=now+10)
    monkeypatch.setattr('core.market_scanner.time.time', lambda: now+10)
    assert tracker.get_micro_metrics('SOL_TRY')['is_qualified'] is True


def test_auto_buy_marks_position_at_bid_including_spread(setup_bot):
    bot, _, _ = setup_bot()
    bot.step()
    p = next(iter(bot.simulator.positions.values()))
    assert p['entry_price'] == pytest.approx(100.05)
    assert p['current_price'] == pytest.approx(100.0)
    assert p['unrealized_pnl'] < -p['entry_fee'] * 2


@pytest.mark.parametrize('offset_ms', [-300000, 300000])
def test_scanner_rejects_old_or_future_exchange_timestamps(monkeypatch, offset_ms):
    item = ticker('5')
    item['closeTime'] += offset_ms
    sc = scanner_with(monkeypatch, [item])
    assert sc.scan_top_active_pairs() == []


def test_future_ticks_are_not_current_features(monkeypatch):
    now = time.time()
    tracker = MicroMomentumTracker()
    tracker.record_tick('SOL_TRY', 100, timestamp=now-10)
    tracker.record_tick('SOL_TRY', 100, timestamp=now-5)
    tracker.record_tick('SOL_TRY', 200, timestamp=now+10)
    metrics = tracker.get_micro_metrics('SOL_TRY')
    assert metrics['velocity_1m_pct'] == 0.0
    assert metrics['is_qualified'] is False


def test_secret_not_exposed_by_authenticated_config_or_state(monkeypatch):
    from fastapi.testclient import TestClient
    from web.app import app, bot_instance
    fake = 'FAKE_API_SECRET_ENDPOINT_TEST'
    monkeypatch.setattr(bot_instance.config.api, 'secret_key', fake)
    with TestClient(app) as client:
        auth = bot_instance.config.auth
        response = client.post('/api/login', json={'username': auth.username, 'password': auth.password})
        assert response.status_code == 200
        for path in ('/api/config/full', '/api/state'):
            response = client.get(path)
            assert response.status_code == 200
            assert fake not in response.text


def test_rollover_preserves_execution_and_cost_fields(setup_bot):
    bot, data, pair = setup_bot()
    bot.config.trading.prevent_rebuy_churn = True
    bot.apply_config()
    bot.step()
    pos = next(iter(bot.simulator.positions.values()))
    execution_price, cost, quantity = pos['entry_price'], pos['invested_cost'], pos['quantity']
    data['price'] = 102.5
    bot.step()
    assert pos['entry_price'] == execution_price
    assert pos['invested_cost'] == cost
    assert pos['quantity'] == quantity
    assert pos['risk_reference_price'] == 102.5
    assert not bot.simulator.closed_trades
    # At the unchanged new risk reference there is no immediate repeat TP/BE exit.
    should_close, reason, _ = bot.risk_manager.evaluate_exit(pos, 102.5)
    assert should_close is False
