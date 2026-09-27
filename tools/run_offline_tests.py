#!/usr/bin/env python3
"""Run the suite in a disposable copy: synthetic HTTP, no internet or real orders.

The fixtures are deliberately synthetic, not a Binance connectivity/backtest result.
Usage: python tools/run_offline_tests.py [pytest arguments]
"""
from __future__ import annotations
import json
import ipaddress
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

SYMBOLS = ('SOL', 'ETH', 'BTC', 'AVAX', 'XRP', 'ADA', 'DOT', 'LINK')
NETWORK_AUDIT_EVENTS = frozenset(('socket.getaddrinfo', 'socket.connect', 'socket.sendto'))


def is_internet_socket_event(event, args):
    """Return whether an audit event could reach a non-loopback network peer.

    Audit hooks see unrelated events too (including ctypes events on Windows),
    whose first argument may have arbitrary attribute access behavior.
    """
    if event not in NETWORK_AUDIT_EVENTS:
        return False
    if event == 'socket.getaddrinfo':
        return True
    if not args or getattr(args[0], 'family', None) not in (socket.AF_INET, socket.AF_INET6):
        return False
    if len(args) < 2 or not isinstance(args[1], tuple) or not args[1]:
        return True
    try:
        return not ipaddress.ip_address(args[1][0]).is_loopback
    except ValueError:
        # Hostnames have not been resolved in this audit event, so deny them.
        return True


def synthetic_http(self, method, url, **kwargs):
    import requests
    if method.upper() != 'GET':
        raise AssertionError(f'Offline runner blocked non-GET request: {method} {url}')
    parsed = urlparse(url)
    path = parsed.path
    params = dict(parse_qs(parsed.query))
    params.update(kwargs.get('params') or {})
    def value(key, default):
        v = params.get(key, default)
        return v[0] if isinstance(v, list) else v
    now_ms = int(time.time() * 1000)
    if path.endswith('/common/symbols'):
        payload = {'code': 0, 'data': {'list': [
            {'symbol': f'{s}_TRY', 'spotTradingEnable': 1, 'baseAsset': s,
             'quoteAsset': 'TRY', 'minNotional': '10', 'stepSize': '0.00000001'}
            for s in (*SYMBOLS, 'USDT')]}}
    elif path.endswith('/common/time'):
        payload = {'code': 0, 'timestamp': now_ms}
    elif path.endswith('/market/depth'):
        payload = {'code': 0, 'data': {'bids': [['100', '1000']], 'asks': [['100.05', '1000']]}}
    elif path.endswith('/klines'):
        n = int(value('limit', 50))
        start = (now_ms // 60000 - n) * 60000
        payload = [[start+i*60000, '100', '100.1', '99.9', '100', '100',
                    start+(i+1)*60000-1, '10000', 100, '50', '5000', '0'] for i in range(n)]
        if '/open/' in path:
            payload = {'code': 0, 'data': {'list': payload}}
    elif path.endswith('/ticker/24hr'):
        payload = [{'symbol': f'{s}TRY', 'quoteVolume': '50000000',
                    'priceChangePercent': '8', 'lastPrice': '100',
                    'highPrice': '110', 'lowPrice': '90', 'closeTime': now_ms}
                   for s in SYMBOLS]
    else:
        raise AssertionError(f'Offline runner has no fixture for {url}')
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response._content = json.dumps(payload).encode('utf-8')
    response.headers['Content-Type'] = 'application/json'
    return response


def main() -> int:
    source = Path(__file__).resolve().parents[1]
    os.environ.update(BOT_TESTING='1', BOT_MODE='test', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    os.environ.pop('BOT_CONFIG_FILE', None)
    handling_audit_event = False
    def deny_network(event, args):
        nonlocal handling_audit_event
        # Attribute access and IP parsing can themselves trigger audit events on
        # Windows.  The nested event has no independent socket operation to
        # authorize, so it must not recursively re-enter this guard.
        if handling_audit_event:
            return
        handling_audit_event = True
        try:
            if is_internet_socket_event(event, args):
                raise OSError('Offline test guard: external network disabled')
        finally:
            handling_audit_event = False
    sys.addaudithook(deny_network)
    import requests
    import pytest
    with tempfile.TemporaryDirectory(prefix='binancetrbot-offline-') as td:
        work = Path(td) / 'project'
        shutil.copytree(source, work, ignore=shutil.ignore_patterns(
            '.git', '.venv', 'venv', '__pycache__', '.pytest_cache', 'reports',
            '.env', 'config.yaml', 'config.live.yaml', 'config.test.yaml'))
        try:
            os.chdir(work)
            sys.path.insert(0, str(work))
            with patch.object(requests.Session, 'request', synthetic_http):
                from core.binance_client import BinanceTrClient
                with patch.object(BinanceTrClient, 'create_order', side_effect=AssertionError('Real orders blocked')):
                    return int(pytest.main(sys.argv[1:] or ['-q', '--tb=short', 'tests']))
        finally:
            # Windows cannot remove the current working directory, unlike Unix.
            os.chdir(source)


if __name__ == '__main__':
    raise SystemExit(main())
