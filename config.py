import os
import yaml
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List

@dataclass
class TradingConfig:
    mode: str = "simulation"
    symbol: str = "AUTO"             # "AUTO" means dynamic scanner mode
    auto_select_coin: bool = True    # Otomatik en hareketli coinleri bul ve al-sat yap
    target_coins_count: int = 5      # Portföyde daima tutulacak en az farklı coin sayısı
    max_open_positions: int = 5      # Aynı anda açık olabilecek maksimum pozisyon
    auto_fill_portfolio: bool = False # Sadece strateji BUY sinyali verdiğinde al (acele doldurma)
    require_strict_buy_signal: bool = True # Strateji teyidi olmadan işlem açmama
    top_coins_limit: int = 0         # Taranacak coin havuzu (0 = Tüm Binance TR TRY çiftlerini tara ve rotasyon yap)
    min_24h_volume_try: float = 8000000.0 # En az 8 Milyon TL 24s hacim (sığ meme coinleri ele)
    min_coin_price: float = 0.05          # 0.05 TL altındaki kuruş altı coinleri yüksek spread nedeniyle filtrele
    initial_virtual_balance: float = 10000.0
    budget_per_trade: float = 2000.0         # Her bir coine ayrılacak bütçe (TL)
    candidate_observation_seconds: int = 12 # Aday coini almadan önce 12 saniye gözlemleme süresi (sn)
    candidate_prebuy_seconds: int = 10       # Coini almadan önce 10 saniye mikro düşüş izlemesi (sn)
    min_observation_gain_pct: float = 0.20   # Aday coin için gözlem penceresinde gereken min yükseliş ivmesi (%0.20)

    candidate_min_burst_count: int = 1       # Onay için gereken ivme sayısı
    candidate_timeout_cooldown_seconds: int = 10 # İvme yakalayamayan coinin dinlenme süresi (sn)
    filter_falling_coins: bool = True        # Sürekli tepe aşağı düşen coinleri engelleme
    only_uptrend: bool = True                # Radarda sadece pozitif/yükseliş trendindeki coinleri tara
    min_24h_gain_pct: float = 0.50           # En az 24 saatlik getiri eşiği (%0.50)


    fee_rate_pct: float = 0.1
    max_market_data_age_seconds: float = 5.0 # Yeni emir için azami tahta verisi yaşı
    max_candle_age_seconds: float = 90.0     # Son kapanmış 1m mumun azami yaşı
    max_radar_age_seconds: float = 10.0      # Aday feature verisinin azami yaşı
    max_allowed_spread_pct: float = 0.20     # En fazla %0.20 alış-satış makası (Spread Guard)
    btc_dump_shield_pct: float = 0.35        # BTC 1 dakikada %0.35 düşerse alımları dondur (Market Beta Shield)
    btc_dump_cooldown_seconds: int = 120     # BTC dump sonrası dondurma süresi (120 sn)
    prevent_rebuy_churn: bool = False        # Doğrudan kâr satışı ve kâr realize etme
    loss_cooldown_seconds: int = 180         # Zarar kesilen coine 3 dk (180s) ceza beklemesi

@dataclass
class TestConfig:
    duration_minutes: int = 15
    auto_stop: bool = True

@dataclass
class StrategyConfig:
    active: str = "adaptive_regime"          # Piyasa Rejimi ve Quant Momentum Stratejisi
    take_profit_pct: float = 1.20            # Tam Kâr Al (+%1.20)
    partial_tp_pct: float = 0.60             # 1. Kademe Kâr Al Eşiği (+%0.60 TP1)
    partial_tp_ratio: float = 0.50           # 1. Kademede satılacak pozisyon oranı (%50)
    enable_partial_tp: bool = True           # Kademeli kâr almayı aktif et
    stop_loss_pct: float = 0.85              # Zarar Kes (-%0.85)
    trailing_stop_pct: float = 0.20          # İz Süren Stop Mesafesi (%0.20)
    trailing_activation_pct: float = 0.50    # Trailing Stop Devreye Girme Eşiği (+%0.50)
    breakeven_trigger_pct: float = 0.35      # Breakeven Kilit Eşiği (+%0.35 kârda stop maliyet+komisyona çekilir)
    portfolio_stop_loss_pct: float = 2.5     # Tüm Portföy Zarar Kes Eşiği (%2.50)
    max_holding_seconds: int = 300           # Durgunluk Tahliyesi (5 dk hareketsiz kalan pozisyonu kapat)

    fee_multiplier: float = 2.0              # Komisyon Çarpanı
    cooldown_seconds: int = 5                # Alımlar arası bekleme süresi (5 sn)
    symbol_cooldown_seconds: int = 30        # Aynı coine tekrar girmek için bekleme süresi (30 sn)
    loss_cooldown_seconds: int = 180         # Zarar kesilen coine ceza süresi (180 sn)
    # Additional optional strategy parameters
    rsi_period: int = 14
    rsi_oversold: float = 42.0
    rsi_overbought: float = 65.0
    bollinger_period: int = 20
    bollinger_std_dev: float = 2.0
    ema_fast: int = 9
    ema_slow: int = 21

@dataclass
class ApiConfig:
    base_url: str = "https://www.binance.tr"
    ws_url: str = "wss://stream-cloud.binance.tr/ws"
    api_key: str = ""
    secret_key: str = ""

@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000

@dataclass
class AuthConfig:
    enabled: bool = True
    username: str = "admin"
    password: str = "admin123"

@dataclass
class DecisionConfig:
    # Old profiles stay explicit legacy profiles. Use config.jev.example.yaml for Jev.
    engine: str = "legacy"
    model: str = "typesafe/jev-1.13"
    api_key_env: str = "OPENROUTER_API_KEY"  # The secret itself is NEVER in YAML/state/logs.
    api_key: str = ""  # Optional local override for config.jev.demo.yaml; never serialized in journals.
    endpoint: str = "https://openrouter.ai/api/v1/systemone"
    database_path: str = "data/jev_decisions.sqlite3"
    request_timeout_seconds: float = 4.0
    max_attempts: int = 2
    retry_backoff_seconds: float = 0.25
    max_request_bytes: int = 90000
    min_action_confidence: float = 0.65
    decision_interval_seconds: float = 15.0
    max_candidates_per_step: int = 4  # Round-robin, NOT an investment-ranking filter.
    history_candles: int = 50
    history_ticks: int = 60
    outcome_horizons_seconds: List[int] = field(default_factory=lambda: [60, 300, 900])
    outcome_max_lateness_seconds: float = 60.0
    max_decision_age_seconds: float = 8.0
    max_prebuy_price_move_pct: float = 0.30
    entry_blackout_seconds: float = 60.0
    slippage_bps: float = 0.0
    # Exact, bounded execution choices; new names here acquire a real BUY-size meaning.
    allocation_fractions: Dict[str, float] = field(default_factory=lambda: {"SMALL": 0.25, "HALF": 0.5, "FULL": 1.0})

@dataclass
class BotConfig:
    trading: TradingConfig = field(default_factory=TradingConfig)
    test: TestConfig = field(default_factory=TestConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    loaded_config_path: str = "config.test.yaml"

def resolve_config_path(config_path: Optional[str] = None, mode: Optional[str] = None) -> str:
    """
    Belirtilen parametrelere, çevre değişkenlerine veya çalışma moduna göre
    en uygun yapılandırma dosya yolunu belirler.
    """
    # 1. Doğrudan parametre olarak geçildiyse
    if config_path:
        return config_path
    
    # 2. Çevre değişkeninden belirtilmişse
    env_config = os.environ.get("BOT_CONFIG_FILE")
    if env_config:
        return env_config

    # 3. Mod bazlı çözümleme (Parametre veya BOT_MODE çevre değişkeni)
    active_mode = (mode or os.environ.get("BOT_MODE", "test")).lower()

    if active_mode in ("live", "prod", "production"):
        candidates = [
            "config.live.yaml",
            "config-live.yaml",
            "config.yaml",
            "config.live.example.yaml",
            "config.example.yaml",
        ]
        default_target = "config.live.yaml"
    else:  # test, simulation
        candidates = [
            "config.test.yaml",
            "config-test.yaml",
            "config.yaml",
            "config.test.example.yaml",
            "config.example.yaml",
        ]
        default_target = "config.test.yaml"

    for cand in candidates:
        if os.path.exists(cand):
            return cand

    return default_target

def mask_string(val: str, keep_chars: int = 4) -> str:
    if not val:
        return ""
    if len(val) <= keep_chars * 2:
        return "*" * len(val)
    return val[:keep_chars] + "*" * (len(val) - keep_chars * 2) + val[-keep_chars:]

def config_to_dict(config: BotConfig, mask_secrets: bool = False) -> Dict[str, Any]:
    """
    BotConfig nesnesini JSON/REST API için sözlüğe çevirir.
    mask_secrets True ise API anahtarları ve parola maskelenir.
    """
    data = asdict(config)
    if mask_secrets:
        data["api"]["api_key"] = "********" if data["api"].get("api_key") else ""
        if data.get("api", {}).get("secret_key"):
            data["api"]["secret_key_masked"] = mask_string(data["api"]["secret_key"])
            data["api"]["has_secret_key"] = bool(data["api"]["secret_key"])
        else:
            data["api"]["secret_key_masked"] = ""
            data["api"]["has_secret_key"] = False

        # İstemciye yalnızca sabit bir placeholder gider; kısmi secret bile gönderilmez.
        data["api"]["secret_key"] = "********" if data["api"]["has_secret_key"] else ""
        data["api"]["secret_key_masked"] = data["api"]["secret_key"]

        if data.get("decision", {}).get("api_key"):
            data["decision"]["api_key"] = "********"

        if data.get("auth", {}).get("password"):
            data["auth"]["has_password"] = bool(data["auth"]["password"])
            data["auth"]["password"] = "********"
    return data

def update_config_from_dict(config: BotConfig, data: Dict[str, Any]) -> BotConfig:
    """
    Sözlük verilerini doğrular ve mevcut BotConfig nesnesine uygular.
    """
    def _update_section(obj, section_dict):
        if not section_dict or not isinstance(section_dict, dict):
            return
        fields = obj.__dataclass_fields__
        for k, v in section_dict.items():
            if k in fields:
                field_type = fields[k].type
                try:
                    # Özel durum: Parola veya Secret Key '********' ise güncelleme yapma (eskiyi koru)
                    if (k in ("api_key", "secret_key", "password")) and (v == "********" or v == "" or v is None):
                        continue
                    if field_type == int:
                        setattr(obj, k, int(v))
                    elif field_type == float:
                        setattr(obj, k, float(v))
                    elif field_type == bool:
                        if isinstance(v, str):
                            setattr(obj, k, v.lower() in ("true", "1", "yes"))
                        else:
                            setattr(obj, k, bool(v))
                    elif field_type == str:
                        setattr(obj, k, str(v))
                    else:
                        setattr(obj, k, v)
                except (ValueError, TypeError):
                    setattr(obj, k, v)

    _update_section(config.trading, data.get("trading"))
    _update_section(config.strategy, data.get("strategy"))
    _update_section(config.test, data.get("test"))
    _update_section(config.api, data.get("api"))
    _update_section(config.server, data.get("server"))
    _update_section(config.auth, data.get("auth"))
    _update_section(config.decision, data.get("decision"))

    if "loaded_config_path" in data and data["loaded_config_path"]:
        config.loaded_config_path = str(data["loaded_config_path"])

    return config

def get_available_config_files() -> List[Dict[str, Any]]:
    """
    Mevcut yapılandırma dosyalarını listeler.
    """
    known_files = [
        {"name": "config.test.yaml", "mode": "simulation", "description": "Sanal Para Test Profili"},
        {"name": "config.live.yaml", "mode": "live", "description": "Binance TR Canlı Borsa Profili"},
        {"name": "config.yaml", "mode": "simulation", "description": "Genel Yapılandırma"},
    ]
    result = []
    for k in known_files:
        exists = os.path.exists(k["name"])
        result.append({
            **k,
            "exists": exists,
            "path": os.path.abspath(k["name"]) if exists else None
        })
    return result

def load_config(config_path: Optional[str] = None, mode: Optional[str] = None) -> BotConfig:
    """
    Yapılandırma dosyasını yükler. 'test' veya 'live' moduna göre ilgili YAML dosyasını seçer.
    """
    resolved_path = resolve_config_path(config_path=config_path, mode=mode)
    
    if not os.path.exists(resolved_path):
        cfg = BotConfig()
        cfg.loaded_config_path = resolved_path
        if mode:
            cfg.trading.mode = "live" if mode.lower() in ("live", "prod") else "simulation"
        return cfg
    
    with open(resolved_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    trading_data = data.get("trading", {})
    test_data = data.get("test", {})
    strategy_data = data.get("strategy", {})
    api_data = data.get("api", {})
    server_data = data.get("server", {})
    auth_data = data.get("auth", {})
    decision_data = data.get("decision", {})

    cfg = BotConfig(
        trading=TradingConfig(**{k: v for k, v in trading_data.items() if k in TradingConfig.__dataclass_fields__}),
        test=TestConfig(**{k: v for k, v in test_data.items() if k in TestConfig.__dataclass_fields__}),
        strategy=StrategyConfig(**{k: v for k, v in strategy_data.items() if k in StrategyConfig.__dataclass_fields__}),
        api=ApiConfig(**{k: v for k, v in api_data.items() if k in ApiConfig.__dataclass_fields__}),
        server=ServerConfig(**{k: v for k, v in server_data.items() if k in ServerConfig.__dataclass_fields__}),
        auth=AuthConfig(**{k: v for k, v in auth_data.items() if k in AuthConfig.__dataclass_fields__}),
        decision=DecisionConfig(**{k: v for k, v in decision_data.items() if k in DecisionConfig.__dataclass_fields__}),
        loaded_config_path=resolved_path,
    )

    if mode:
        if mode.lower() in ("live", "prod"):
            cfg.trading.mode = "live"
        elif mode.lower() in ("test", "simulation"):
            cfg.trading.mode = "simulation"

    # TEST EMNİYETİ: Eğer pytest veya test ortamı çalışıyorsa modu KESİNLİKLE 'simulation' yap!
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("BOT_TESTING") == "1":
        cfg.trading.mode = "simulation"

    return cfg

def save_config(config: BotConfig, config_path: Optional[str] = None) -> str:
    """
    Yapılandırmayı YAML dosyasına eksiksiz kaydeder. Dosya belirtilmemişse aktif yüklenen dosyaya kaydeder.
    """
    target_path = config_path or getattr(config, "loaded_config_path", None) or resolve_config_path(mode=config.trading.mode)

    data = {
        "trading": asdict(config.trading),
        "test": asdict(config.test),
        "strategy": asdict(config.strategy),
        "api": asdict(config.api),
        "server": asdict(config.server),
        "auth": asdict(config.auth),
        "decision": asdict(config.decision),
    }

    with open(target_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    config.loaded_config_path = target_path
    return target_path
