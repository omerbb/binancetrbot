import os
import secrets
import copy
import json
from fastapi import FastAPI, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional, Dict, Any

from bot import BinanceTrBot
from decision.contracts import MODEL_ENGINES
from config import (
    load_config,
    save_config,
    config_to_dict,
    update_config_from_dict,
    get_available_config_files,
    BotConfig,
)

from contextlib import asynccontextmanager

# Bot örneğini başlat
bot_instance = BinanceTrBot()

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    bot_instance.is_running = False
    if getattr(bot_instance, "_decision_controller", None) is not None:
        try:
            bot_instance._decision_controller.end_session(reason="web_shutdown", liquidate=False)
        except Exception:
            pass
    if hasattr(bot_instance, "client") and hasattr(bot_instance.client, "session"):
        try:
            bot_instance.client.session.close()
        except Exception:
            pass

app = FastAPI(title="Binance TR Al-Sat Botu", lifespan=lifespan)

# Aktif oturum token'ları havuzu
active_sessions: set[str] = set()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(os.path.join(STATIC_DIR, "css"), exist_ok=True)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(ROOT_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/reports", StaticFiles(directory=REPORTS_DIR, html=True), name="reports")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    async def authorized_forward():
        path = request.url.path
        locked = bot_instance.is_running and (bot_instance.config.decision.engine in MODEL_ENGINES or getattr(bot_instance, "_decision_controller", None) is not None)
        if locked and request.method == "POST" and (path.startswith("/api/config") or path == "/api/start"):
            return JSONResponse(status_code=409, content={"status": "error", "message": "Model karar oturumu çalışırken ayarlar değiştirilemez. Önce durdurun; yeni oturum sürümlenerek açılır."})
        return await call_next(request)

    auth_cfg = getattr(bot_instance.config, "auth", None)
    if not auth_cfg or not getattr(auth_cfg, "enabled", True):
        return await authorized_forward()
    path = request.url.path
    if path.startswith("/static") or path in ("/login", "/api/login", "/favicon.ico"):
        return await authorized_forward()
    token = request.cookies.get("session_token")
    if token and token in active_sessions:
        return await authorized_forward()
    if path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"status": "error", "message": "Giriş yapmanız gerekiyor."})
    return RedirectResponse(url="/login", status_code=303)

class StartRequest(BaseModel):
    duration_minutes: Optional[int] = 15
    strategy: Optional[str] = None
    symbol: Optional[str] = None
    budget_per_trade: Optional[float] = None
    auto_select_coin: Optional[bool] = None
    target_coins_count: Optional[int] = None
    candidate_observation_seconds: Optional[int] = None
    min_observation_gain_pct: Optional[float] = None
    candidate_min_burst_count: Optional[int] = None
    trailing_activation_pct: Optional[float] = None
    symbol_cooldown_seconds: Optional[int] = None
    only_uptrend: Optional[bool] = None

class ConfigUpdateRequest(BaseModel):
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    trailing_activation_pct: Optional[float] = None
    symbol_cooldown_seconds: Optional[int] = None
    budget_per_trade: Optional[float] = None
    strategy: Optional[str] = None
    symbol: Optional[str] = None
    rsi_oversold: Optional[float] = None
    rsi_overbought: Optional[float] = None
    auto_select_coin: Optional[bool] = None
    target_coins_count: Optional[int] = None
    auto_fill_portfolio: Optional[bool] = None
    candidate_observation_seconds: Optional[int] = None
    min_observation_gain_pct: Optional[float] = None
    candidate_min_burst_count: Optional[int] = None
    filter_falling_coins: Optional[bool] = None
    only_uptrend: Optional[bool] = None

class FullConfigRequest(BaseModel):
    trading: Optional[Dict[str, Any]] = None
    strategy: Optional[Dict[str, Any]] = None
    test: Optional[Dict[str, Any]] = None
    api: Optional[Dict[str, Any]] = None
    server: Optional[Dict[str, Any]] = None
    auth: Optional[Dict[str, Any]] = None
    loaded_config_path: Optional[str] = None
    decision: Optional[Dict[str, Any]] = None

class SwitchModeRequest(BaseModel):
    mode: str

class TestApiRequest(BaseModel):
    api_key: Optional[str] = None
    secret_key: Optional[str] = None
    base_url: Optional[str] = None

class LoginRequest(BaseModel):
    username: str
    password: str

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    token = request.cookies.get("session_token")
    if token and token in active_sessions:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/api/login")
async def api_login(req: LoginRequest):
    auth_cfg = getattr(bot_instance.config, "auth", None)
    expected_user = getattr(auth_cfg, "username", "admin") if auth_cfg else "admin"
    expected_pass = getattr(auth_cfg, "password", "admin123") if auth_cfg else "admin123"

    if req.username == expected_user and req.password == expected_pass:
        token = secrets.token_hex(32)
        active_sessions.add(token)
        response = JSONResponse(content={"status": "success", "username": req.username})
        response.set_cookie(
            key="session_token",
            value=token,
            httponly=True,
            samesite="lax",
            max_age=86400 * 7,  # 7 gün geçerli
        )
        return response

    return JSONResponse(
        status_code=401,
        content={"status": "error", "message": "Kullanıcı adı veya parola hatalı!"}
    )

@app.get("/logout")
@app.post("/api/logout")
async def logout_endpoint(request: Request):
    token = request.cookies.get("session_token")
    if token in active_sessions:
        active_sessions.discard(token)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_token")
    return response

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/api/state")
async def get_state():
    return bot_instance.get_dashboard_state()

@app.get("/api/config/full")
async def get_full_config():
    """
    Tüm konfigürasyonu, yüklü dosya yolunu ve mevcut profil dosyalarını döndürür.
    """
    return {
        "status": "success",
        "config": config_to_dict(bot_instance.config, mask_secrets=True),
        "loaded_config_path": getattr(bot_instance.config, "loaded_config_path", "config.test.yaml"),
        "available_files": get_available_config_files(),
        "active_mode": bot_instance.config.trading.mode,
    }

@app.post("/api/config/full")
async def update_full_config(req: FullConfigRequest):
    """
    Tüm ayarları kaydeder, YAML dosyasına yazar ve bota anında uygular.
    """
    payload = req.model_dump(exclude_unset=True)
    candidate = copy.deepcopy(bot_instance.config)
    try:
        update_config_from_dict(candidate, payload)
        if candidate.decision.engine in MODEL_ENGINES:
            from decision.contracts import validate_config
            validate_config(candidate)
            if candidate.trading.mode != "simulation": raise ValueError("JEV yalnızca simulation/replay modunda çalışır.")
        bot_instance.apply_config(candidate)
        saved_path = save_config(candidate)
    except (ValueError, TypeError) as exc:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})
    return {
        "status": "success",
        "message": f"Tüm ayarlar başarıyla kaydedildi ({saved_path}) ve bota uygulandı.",
        "config": config_to_dict(bot_instance.config, mask_secrets=True),
        "loaded_config_path": saved_path,
    }

@app.post("/api/config/switch_mode")
async def switch_mode(req: SwitchModeRequest):
    """
    Bot modunu (simulation / live) değiştirir ve ilgili profili yükler.
    """
    new_mode = req.mode.lower()
    if new_mode not in ("simulation", "test", "live", "prod"):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Geçersiz mod."})
    
    new_cfg = load_config(mode=new_mode)
    bot_instance.apply_config(new_cfg)
    return {
        "status": "success",
        "message": f"Mod başarıyla '{new_mode}' olarak değiştirildi.",
        "mode": bot_instance.config.trading.mode,
        "config": config_to_dict(bot_instance.config, mask_secrets=True),
        "loaded_config_path": bot_instance.config.loaded_config_path,
    }

@app.post("/api/config/reset_default")
async def reset_default_config(req: SwitchModeRequest):
    """
    Yapılandırmayı varsayılan şablona sıfırlar.
    """
    mode = "live" if req.mode in ("live", "prod") else "simulation"
    new_cfg = BotConfig()
    new_cfg.trading.mode = mode
    saved_path = save_config(new_cfg)
    bot_instance.apply_config(new_cfg)
    return {
        "status": "success",
        "message": f"Yapılandırma varsayılan ayarlara sıfırlandı.",
        "config": config_to_dict(bot_instance.config, mask_secrets=True),
        "loaded_config_path": saved_path,
    }

@app.post("/api/test_api")
async def test_api_connection(req: TestApiRequest):
    """
    Binance TR API bağlantısını ve kimlik doğrulamasını test eder.
    """
    api_k = req.api_key or bot_instance.config.api.api_key
    sec_k = req.secret_key or bot_instance.config.api.secret_key
    base_u = req.base_url or bot_instance.config.api.base_url

    if not api_k or api_k == "BURAYA_BINANCE_TR_API_KEY_GIRINIZ":
        return JSONResponse(status_code=400, content={"status": "error", "message": "Geçerli bir API Key girilmedi."})

    from core.binance_client import BinanceTrClient
    test_client = BinanceTrClient(api_key=api_k, secret_key=sec_k, base_url=base_u)
    try:
        # 1. Ping testi
        ping_ok = test_client.ping()
        if not ping_ok:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Binance TR sunucusuna erişilemedi (Ping başarısız)."})
        
        # 2. Borsa saati testi
        server_time = test_client.get_server_time()
        
        # 3. Eğer secret key varsa hesap bakiyesi sorgulayarak yetkiyi test et
        has_auth = False
        balances = []
        if sec_k and sec_k != "BURAYA_BINANCE_TR_SECRET_KEY_GIRINIZ" and sec_k != "********":
            try:
                account = test_client.get_account_info()
                if isinstance(account, dict) and (account.get("code") == 0 or "balances" in account or "data" in account):
                    has_auth = True
                    raw_bal = account.get("data", {}).get("balances", account.get("balances", []))
                    if isinstance(raw_bal, list):
                        balances = [b for b in raw_bal if float(b.get("free", 0)) > 0 or float(b.get("locked", 0)) > 0][:5]
            except Exception:
                pass

        return {
            "status": "success",
            "message": "Binance TR API bağlantısı başarılı!",
            "server_time": server_time,
            "authenticated": has_auth,
            "sample_balances": balances,
        }
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": f"API Test Hatası: {str(e)}"})
    finally:
        try:
            test_client.session.close()
        except Exception:
            pass

@app.post("/api/start")
async def start_bot(req: StartRequest):
    candidate = copy.deepcopy(bot_instance.config)
    if req.strategy:
        candidate.strategy.active = req.strategy
    if req.symbol:
        candidate.trading.symbol = req.symbol
    if req.budget_per_trade:
        candidate.trading.budget_per_trade = req.budget_per_trade
    if req.auto_select_coin is not None:
        candidate.trading.auto_select_coin = req.auto_select_coin
    if req.target_coins_count is not None:
        candidate.trading.target_coins_count = req.target_coins_count
        candidate.trading.max_open_positions = req.target_coins_count
    if req.candidate_observation_seconds is not None:
        candidate.trading.candidate_observation_seconds = req.candidate_observation_seconds
    if req.min_observation_gain_pct is not None:
        candidate.trading.min_observation_gain_pct = req.min_observation_gain_pct
    if req.candidate_min_burst_count is not None:
        candidate.trading.candidate_min_burst_count = req.candidate_min_burst_count
    if req.trailing_activation_pct is not None:
        candidate.strategy.trailing_activation_pct = req.trailing_activation_pct
    if req.symbol_cooldown_seconds is not None:
        candidate.strategy.symbol_cooldown_seconds = req.symbol_cooldown_seconds
    if req.only_uptrend is not None:
        candidate.trading.only_uptrend = req.only_uptrend

    try:
        if candidate.decision.engine in MODEL_ENGINES:
            from decision.contracts import validate_config
            from decision.providers import preflight
            validate_config(candidate)
            if candidate.trading.mode != "simulation": raise ValueError("JEV live execution disabled")
            if bot_instance._decision_provider is None:
                preflight(candidate.decision)
        bot_instance.apply_config(candidate)
        bot_instance.start(duration_minutes=req.duration_minutes)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})
    return {"status": "started", "duration": req.duration_minutes}

@app.post("/api/stop")
async def stop_bot():
    from starlette.concurrency import run_in_threadpool
    res = await run_in_threadpool(bot_instance.stop)
    return res

@app.post("/api/force_buy")
async def force_buy(payload: Optional[Dict[str, Any]] = Body(default=None)):
    payload = payload or {}
    symbol = payload.get("symbol")
    budget = payload.get("budget")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float))):
        return JSONResponse(status_code=400, content={"status": "error", "message": "budget sayısal olmalı"})
    if symbol is not None and (not isinstance(symbol, str) or not __import__('re').fullmatch(r"[A-Z0-9]+_TRY", symbol)):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Geçerli açık TRY paritesi gerekli"})
    if bot_instance.config.decision.engine in MODEL_ENGINES and not symbol:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Manuel JEV dışı alımda symbol açıkça belirtilmeli (ör. SOL_TRY)."})
    pos = bot_instance.force_test_buy(symbol=symbol, budget=budget, reason="Kullanıcı Test Alımı")
    if pos:
        return {"status": "success", "position": pos}
    return JSONResponse(status_code=400, content={"status": "error", "message": "Pozisyon açılamadı. Fiyat alınamadı veya bütçe yetersiz."})

class ClosePositionRequest(BaseModel):
    position_id: str

@app.post("/api/force_close")
@app.post("/api/close_all")
async def close_all_positions():
    closed = bot_instance.force_close_all(reason="Kullanıcı Tümünü Sat Emri")
    return {"status": "success", "closed_count": len(closed), "trades": closed}

@app.post("/api/close_position")
async def close_single_position(req: ClosePositionRequest):
    trade = bot_instance.close_single_position(req.position_id, reason="Kullanıcı Manuel Satış")
    if trade:
        return {"status": "success", "trade": trade}
    return JSONResponse(status_code=400, content={"status": "error", "message": "Pozisyon kapatılamadı"})

@app.post("/api/config")
async def update_config(req: ConfigUpdateRequest):
    cfg = copy.deepcopy(bot_instance.config)
    if req.take_profit_pct is not None:
        cfg.strategy.take_profit_pct = req.take_profit_pct
    if req.stop_loss_pct is not None:
        cfg.strategy.stop_loss_pct = req.stop_loss_pct
    if req.trailing_stop_pct is not None:
        cfg.strategy.trailing_stop_pct = req.trailing_stop_pct
    if req.trailing_activation_pct is not None:
        cfg.strategy.trailing_activation_pct = req.trailing_activation_pct
    if req.symbol_cooldown_seconds is not None:
        cfg.strategy.symbol_cooldown_seconds = req.symbol_cooldown_seconds
    if req.budget_per_trade is not None:
        cfg.trading.budget_per_trade = req.budget_per_trade
    if req.strategy is not None:
        cfg.strategy.active = req.strategy
    if req.symbol is not None:
        cfg.trading.symbol = req.symbol
    if req.rsi_oversold is not None:
        cfg.strategy.rsi_oversold = req.rsi_oversold
    if req.rsi_overbought is not None:
        cfg.strategy.rsi_overbought = req.rsi_overbought
    if req.auto_select_coin is not None:
        cfg.trading.auto_select_coin = req.auto_select_coin
    if req.target_coins_count is not None:
        cfg.trading.target_coins_count = req.target_coins_count
        cfg.trading.max_open_positions = req.target_coins_count
    if req.auto_fill_portfolio is not None:
        cfg.trading.auto_fill_portfolio = req.auto_fill_portfolio
    if req.candidate_observation_seconds is not None:
        cfg.trading.candidate_observation_seconds = req.candidate_observation_seconds
    if req.min_observation_gain_pct is not None:
        cfg.trading.min_observation_gain_pct = req.min_observation_gain_pct
    if req.candidate_min_burst_count is not None:
        cfg.trading.candidate_min_burst_count = req.candidate_min_burst_count
    if req.filter_falling_coins is not None:
        cfg.trading.filter_falling_coins = req.filter_falling_coins
    if req.only_uptrend is not None:
        cfg.trading.only_uptrend = req.only_uptrend

    try:
        if cfg.decision.engine in MODEL_ENGINES:
            from decision.contracts import validate_config
            validate_config(cfg)
        bot_instance.apply_config(cfg)
        save_config(cfg)
    except (ValueError, TypeError) as exc:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})
    return {"status": "updated", "config": bot_instance.get_dashboard_state()["config"]}

@app.get("/api/reports")
async def list_reports():
    if not os.path.exists(REPORTS_DIR):
        return []
    files = [f for f in os.listdir(REPORTS_DIR) if f.endswith(".html")]
    files.sort(reverse=True)
    return [{"filename": f, "path": f"/reports/{f}"} for f in files]



@app.get("/api/decisions/status")
async def decision_status():
    controller = getattr(bot_instance, "_decision_controller", None)
    return controller.status() if controller else {"engine": bot_instance.config.decision.engine, "run_id": None}

@app.get("/api/decisions/recent")
async def recent_decisions(limit: int = 20):
    controller = getattr(bot_instance, "_decision_controller", None)
    if controller is None: return {"decisions": []}
    # Separate read-only connection: no waiting for an inference lock in the HTTP event loop.
    import sqlite3
    from pathlib import Path
    if controller.cfg.decision.database_path == ":memory:": return {"decisions": [], "message": "Use a file journal for web inspection"}
    database = Path(controller.cfg.decision.database_path).resolve()
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in connection.execute("SELECT decision_id,stage,symbol,as_of,source,parent_id FROM decisions WHERE run_id=? ORDER BY rowid DESC LIMIT ?", (controller.run_id, max(1, min(limit, 100))))]
        for row in rows:
            row.update(model_choice=None, confidence=None, valid=None, applied_action=None, reasons=[], execution=None,
                       allocation_choice=None, allocation_confidence=None, allocation_fallback=None, retry_count=0,
                       expected_value=None)
            for event in connection.execute("SELECT kind,data_json FROM events WHERE decision_id=? ORDER BY sequence", (row["decision_id"],)):
                data = json.loads(event["data_json"])
                if event["kind"] == "model_response":
                    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
                    answers = raw.get("answers") if isinstance(raw.get("answers"), dict) else {}
                    answer = next((answers[k] for k in ("portfolio_action", "action", "prebuy_authorization") if isinstance(answers.get(k), dict)), {})
                    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
                    row.update(model_choice=answer.get("choice"), confidence=answer.get("confidence"),
                               valid=data.get("valid"), validation_error=data.get("validation_error"),
                               model=raw.get("model"), cost_usd=usage.get("cost"))
                    allocation = answers.get("allocation") if isinstance(answers.get("allocation"), dict) else {}
                    row.update(allocation_choice=allocation.get("choice"), allocation_confidence=allocation.get("confidence"))
                elif event["kind"] == "expected_value_assessment":
                    costs = data.get("costs") if isinstance(data.get("costs"), dict) else {}
                    row["expected_value"] = {"side": data.get("side"), "expected_mid_return_pct": data.get("expected_mid_return_pct"),
                                             "cost_pct": costs.get("total_pct"), "edge_pct": data.get("edge_pct"),
                                             "threshold_pct": data.get("threshold_pct"), "action": data.get("action")}
                elif event["kind"] == "allocation_fallback":
                    row["allocation_fallback"] = {"budget_try": data["budget_try"], "selected_allocation": data["selected_allocation"]}
                elif event["kind"] == "decision_retry":
                    row["retry_count"] += 1
                elif event["kind"] == "decision_disposition":
                    row.update(applied_action=data.get("applied_action"), reasons=data.get("override_reasons", []), disposition=data.get("status"))
                elif event["kind"] == "inference_failed":
                    row.update(valid=False, validation_error=data.get("error_code"))
                elif event["kind"] == "execution_result":
                    row["execution"] = data.get("status")
        return {"decisions": rows}
    finally: connection.close()
