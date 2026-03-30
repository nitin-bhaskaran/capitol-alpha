"""
Telegram Bot for Capitol Alpha.

Sends trade signals as rich messages with inline keyboards:
  - Approve (execute via Trading212)
  - Reject (skip)
  - Info (show score breakdown)

Also supports commands:
  /status   — Current portfolio and pending signals
  /stats    — Daily/weekly stats
  /trump    — Add a manual Trump family trade
"""

import json
import logging
import asyncio
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config import Config
from src.database import Database
from src.execution.trading212 import Trading212Client

logger = logging.getLogger("capitol_alpha.telegram")


class TelegramBot:
    def __init__(self, config: Config, db: Database, t212: Optional[Trading212Client] = None):
        self.config = config
        self.db = db
        self.t212 = t212
        self.app: Optional[Application] = None

    async def start(self):
        """Start the Telegram bot."""
        if not self.config.telegram.bot_token:
            logger.warning("No Telegram bot token configured — skipping bot start")
            return

        self.app = (
            Application.builder()
            .token(self.config.telegram.bot_token)
            .build()
        )

        # Register handlers
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("stats", self._cmd_stats))
        self.app.add_handler(CommandHandler("pending", self._cmd_pending))
        self.app.add_handler(CommandHandler("portfolio", self._cmd_portfolio))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

        logger.info("Telegram bot starting...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram bot running")

    async def stop(self):
        """Stop the Telegram bot."""
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    # ── Send Alert ────────────────────────────────────────────────

    async def send_signal_alert(self, signal: dict):
        """
        Send a trade signal alert with approve/reject buttons.
        This is the core function called by the main loop when
        new signals are generated.
        """
        if not self.app:
            logger.warning("Bot not running — cannot send alert")
            return

        # Build the alert message
        direction_emoji = "🟢" if signal["direction"] == "BUY" else "🔴"
        score = signal["alpha_score"]
        action = signal["suggested_action"]

        # Score bar visualisation
        filled = int(score * 10)
        score_bar = "█" * filled + "░" * (10 - filled)

        # VIP badge
        vip_badge = ""
        if signal.get("vip_tier") == 1:
            vip_badge = " ⭐ VIP TIER 1"
        elif signal.get("vip_tier") == 2:
            vip_badge = " 🔷 VIP TIER 2"

        # Score breakdown
        breakdown = json.loads(signal.get("score_breakdown", "{}"))

        message = (
            f"{direction_emoji} <b>{signal['direction']} {signal['ticker']}</b>{vip_badge}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>{signal['politician_name']}</b>\n"
            f"📊 Alpha Score: <b>{score:.0%}</b> [{score_bar}]\n"
            f"📋 Action: <b>{action}</b>\n"
        )

        if signal.get("filing_gap_days") is not None:
            message += f"📅 Filing Gap: {signal['filing_gap_days']} days\n"

        if breakdown:
            message += (
                f"\n<b>Score Breakdown:</b>\n"
                f"  VIP: {breakdown.get('vip', 0):.0%} | "
                f"Committee: {breakdown.get('committee', 0):.0%}\n"
                f"  Filing Gap: {breakdown.get('filing_gap', 0):.0%} | "
                f"Size: {breakdown.get('trade_size', 0):.0%}\n"
                f"  Historical: {breakdown.get('historical', 0):.0%}\n"
            )

        # Build inline keyboard
        signal_id = signal["id"]
        keyboard = []

        if action in ("SUGGEST_TRADE", "HIGH_CONVICTION"):
            keyboard.append([
                InlineKeyboardButton(
                    "✅ Approve Trade", callback_data=f"approve_{signal_id}"
                ),
                InlineKeyboardButton(
                    "❌ Reject", callback_data=f"reject_{signal_id}"
                ),
            ])
            keyboard.append([
                InlineKeyboardButton(
                    "📊 More Info", callback_data=f"info_{signal_id}"
                ),
            ])
        else:
            # Alert only — just acknowledge
            keyboard.append([
                InlineKeyboardButton(
                    "👁 Noted", callback_data=f"noted_{signal_id}"
                ),
                InlineKeyboardButton(
                    "✅ Trade Anyway", callback_data=f"approve_{signal_id}"
                ),
            ])

        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await self.app.bot.send_message(
                chat_id=self.config.telegram.chat_id,
                text=message,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            self.db.update_signal_status(signal_id, "alerted")
            logger.info(f"Alert sent for signal {signal_id}: {signal['ticker']}")
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")

    # ── Callback Handler (button presses) ─────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses."""
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        if (
            self.config.telegram.admin_user_ids
            and user_id not in self.config.telegram.admin_user_ids
        ):
            await query.edit_message_text("⛔ You are not authorised to take this action.")
            return

        data = query.data
        parts = data.split("_", 1)
        action = parts[0]
        signal_id = int(parts[1]) if len(parts) > 1 else None

        if action == "approve" and signal_id:
            await self._handle_approve(query, signal_id)
        elif action == "reject" and signal_id:
            await self._handle_reject(query, signal_id)
        elif action == "info" and signal_id:
            await self._handle_info(query, signal_id)
        elif action == "noted" and signal_id:
            await query.edit_message_text(
                query.message.text + "\n\n✅ <i>Acknowledged</i>",
                parse_mode="HTML",
            )

    async def _handle_approve(self, query, signal_id: int):
        """Handle trade approval."""
        signal = self.db.get_signal_by_id(signal_id)
        if not signal:
            await query.edit_message_text("⚠️ Signal not found")
            return

        self.db.update_signal_status(signal_id, "approved")

        if self.t212:
            # Find the T212 instrument
            instrument = self.t212.find_instrument(signal["ticker"])
            if not instrument:
                await query.edit_message_text(
                    f"⚠️ Could not find {signal['ticker']} on Trading212.\n"
                    f"You may need to trade this manually."
                )
                return

            t212_ticker = instrument.get("ticker", "")

            # Calculate order size
            account = self.t212.get_account_summary()
            available = account.get("cash", {}).get("availableToTrade", 0)
            order_size = min(self.config.trading212.max_order_gbp, available * 0.2)

            # For now, send a confirmation message rather than auto-executing
            confirm_msg = (
                f"✅ <b>APPROVED</b>\n\n"
                f"Ready to execute:\n"
                f"  Ticker: {t212_ticker}\n"
                f"  Direction: {signal['direction']}\n"
                f"  Max size: £{order_size:.2f}\n"
                f"  Type: {self.config.trading212.default_order_type}\n"
                f"  Environment: {self.config.trading212.environment}\n\n"
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🚀 EXECUTE NOW", callback_data=f"execute_{signal_id}"
                    ),
                    InlineKeyboardButton(
                        "⏸ Cancel", callback_data=f"reject_{signal_id}"
                    ),
                ]
            ])

            await query.edit_message_text(
                confirm_msg, parse_mode="HTML", reply_markup=keyboard
            )
        else:
            await query.edit_message_text(
                f"✅ Signal {signal_id} approved.\n"
                f"Trading212 not configured — trade manually:\n"
                f"  {signal['direction']} {signal['ticker']}"
            )

    async def _handle_reject(self, query, signal_id: int):
        """Handle trade rejection."""
        self.db.update_signal_status(signal_id, "rejected")
        await query.edit_message_text(
            query.message.text + "\n\n❌ <i>Rejected</i>",
            parse_mode="HTML",
        )
        logger.info(f"Signal {signal_id} rejected by user")

    async def _handle_info(self, query, signal_id: int):
        """Show detailed info about a signal."""
        signal = self.db.get_signal_by_id(signal_id)
        if not signal:
            await query.edit_message_text("⚠️ Signal not found")
            return

        breakdown = json.loads(signal.get("score_breakdown", "{}"))
        info = (
            f"📊 <b>Signal #{signal_id} Details</b>\n\n"
            f"Ticker: {signal['ticker']}\n"
            f"Politician: {signal['politician_name']}\n"
            f"Direction: {signal['direction']}\n"
            f"Alpha Score: {signal['alpha_score']:.1%}\n"
            f"Filing Gap: {signal.get('filing_gap_days', 'N/A')} days\n\n"
            f"<b>Score Components:</b>\n"
        )
        for component, score in breakdown.items():
            bar = "█" * int(score * 5) + "░" * (5 - int(score * 5))
            info += f"  {component}: {score:.0%} [{bar}]\n"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{signal_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{signal_id}"),
            ]
        ])

        await query.edit_message_text(info, parse_mode="HTML", reply_markup=keyboard)

    # ── Command Handlers ──────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🏛️ <b>Capitol Alpha</b>\n\n"
            "Congress trades alpha generator.\n\n"
            "Commands:\n"
            "/status — System status\n"
            "/stats — Trading stats\n"
            "/pending — Pending signals\n"
            "/portfolio — T212 portfolio\n",
            parse_mode="HTML",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        stats = self.db.get_dashboard_stats()
        msg = (
            f"🏛️ <b>Capitol Alpha Status</b>\n\n"
            f"Trades ingested: {stats['total_trades_ingested']}\n"
            f"Signals generated: {stats['total_signals']}\n"
            f"Pending signals: {stats['pending_signals']}\n"
            f"Approved: {stats['approved_signals']}\n"
            f"Executed: {stats['executed_trades']}\n"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        stats = self.db.get_dashboard_stats()
        await update.message.reply_text(
            f"📊 {json.dumps(stats, indent=2)}", parse_mode="HTML"
        )

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        pending = self.db.get_pending_signals()
        if not pending:
            await update.message.reply_text("No pending signals.")
            return

        for signal in pending[:10]:
            await self.send_signal_alert(signal)

    async def _cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.t212:
            await update.message.reply_text("Trading212 not configured.")
            return

        try:
            positions = self.t212.get_positions()
            if not positions:
                await update.message.reply_text("No open positions.")
                return

            msg = "💼 <b>Portfolio</b>\n\n"
            for pos in positions:
                ticker = pos.get("ticker", "?")
                qty = pos.get("quantity", 0)
                pnl = pos.get("ppl", 0)
                emoji = "🟢" if pnl >= 0 else "🔴"
                msg += f"{emoji} {ticker}: {qty} shares (P&L: £{pnl:.2f})\n"

            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Error fetching portfolio: {e}")
