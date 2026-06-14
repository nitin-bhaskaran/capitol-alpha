"""Telegram bot for Capitol Alpha alerts and paper execution."""

import json
import logging
from typing import Optional

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.error import BadRequest, TelegramError
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
except ModuleNotFoundError:
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None
    Update = object
    Application = None
    CallbackQueryHandler = None
    CommandHandler = None
    ContextTypes = object

    class TelegramError(Exception):
        pass

    class BadRequest(TelegramError):
        pass

from src.config import Config
from src.database import Database
from src.execution.paper import PaperTradingService
from src.execution.trading212 import Trading212Client

logger = logging.getLogger("capitol_alpha.telegram")


class _SuppressTelegramCancelledUpdateLog(logging.Filter):
    """Hide python-telegram-bot's expected polling cancellation traceback."""

    NOISY_MESSAGE = "Fetching updates was aborted due to CancelledError()"

    def filter(self, record: logging.LogRecord) -> bool:
        return self.NOISY_MESSAGE not in record.getMessage()


class TelegramBot:
    def __init__(
        self,
        config: Config,
        db: Database,
        t212: Optional[Trading212Client] = None,
        paper: Optional[PaperTradingService] = None,
    ):
        self.config = config
        self.db = db
        self.t212 = t212
        self.paper = paper
        self.app: Optional[Application] = None

    async def start(self):
        """Start the Telegram bot."""
        if not self.config.telegram.bot_token:
            logger.warning("No Telegram bot token configured - skipping bot start")
            return
        if Application is None:
            logger.warning("python-telegram-bot is not installed - skipping bot start")
            return

        self.app = Application.builder().token(self.config.telegram.bot_token).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("stats", self._cmd_stats))
        self.app.add_handler(CommandHandler("pending", self._cmd_pending))
        self.app.add_handler(CommandHandler("portfolio", self._cmd_portfolio))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        self.app.add_error_handler(self._handle_error)

        logger.info("Telegram bot starting...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram bot running")

    async def stop(self):
        if not self.app:
            return

        app_logger = logging.getLogger("telegram.ext.Application")
        suppressor = _SuppressTelegramCancelledUpdateLog()
        app_logger.addFilter(suppressor)
        try:
            if getattr(self.app, "updater", None):
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        finally:
            app_logger.removeFilter(suppressor)
            self.app = None

    async def send_signal_alert(self, signal: dict):
        """Send a model signal alert with approve/reject buttons."""
        if not self.app:
            logger.warning("Bot not running - cannot send alert")
            return

        score = signal["alpha_score"]
        action = signal["suggested_action"]
        breakdown = json.loads(signal.get("score_breakdown") or "{}")
        expected = signal.get("expected_excess_return") or 0
        confidence = signal.get("confidence") or 0

        message = (
            f"<b>{signal['direction']} {signal['ticker']}</b>\n"
            f"Politician: <b>{signal['politician_name']}</b>\n"
            f"Action: <b>{action}</b>\n"
            f"Rank: {signal.get('rank_order') or '-'}\n"
            f"Alpha score: <b>{score:.1%}</b>\n"
            f"Expected excess: <b>{expected:.2%}</b>\n"
            f"Confidence: <b>{confidence:.1%}</b>\n"
            f"Mode: <b>{self.config.execution.mode}</b>\n"
        )
        if signal.get("filing_gap_days") is not None:
            message += f"Filing gap: {signal['filing_gap_days']} days\n"
        if signal.get("decision_reason"):
            message += f"\n<i>{signal['decision_reason']}</i>\n"
        if breakdown:
            message += (
                "\n<b>Model components:</b>\n"
                f"  Bayesian ER: {breakdown.get('bayesian_expected_excess_return', 0):.2%}\n"
                f"  VIP: {breakdown.get('vip', 0):.0%} | "
                f"Committee: {breakdown.get('committee', 0):.0%}\n"
                f"  Filing: {breakdown.get('filing_speed', 0):.0%} | "
                f"Size: {breakdown.get('trade_size', 0):.0%}\n"
            )

        signal_id = signal["id"]
        keyboard = []
        if action in ("SUGGEST_TRADE", "HIGH_CONVICTION"):
            keyboard.append([
                InlineKeyboardButton("Approve", callback_data=f"approve_{signal_id}"),
                InlineKeyboardButton("Reject", callback_data=f"reject_{signal_id}"),
            ])
            keyboard.append([
                InlineKeyboardButton("More Info", callback_data=f"info_{signal_id}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("Noted", callback_data=f"noted_{signal_id}"),
                InlineKeyboardButton("Review Anyway", callback_data=f"approve_{signal_id}"),
            ])

        try:
            await self.app.bot.send_message(
                chat_id=self.config.telegram.chat_id,
                text=message,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            self.db.update_signal_status(signal_id, "alerted")
            logger.info("Alert sent for signal %s: %s", signal_id, signal["ticker"])
        except Exception as e:
            logger.error("Failed to send alert: %s", e)

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return

        if not await self._answer_callback(query):
            return

        user_id = query.from_user.id
        if self.config.telegram.admin_user_ids and user_id not in self.config.telegram.admin_user_ids:
            await self._edit_callback_message(
                query, "You are not authorised to take this action."
            )
            return

        callback_data = query.data or ""
        action, _, raw_signal_id = callback_data.partition("_")
        try:
            signal_id = int(raw_signal_id) if raw_signal_id else None
        except ValueError:
            logger.warning("Ignoring malformed Telegram callback: %s", callback_data)
            return

        if action == "approve" and signal_id:
            await self._handle_approve(query, signal_id)
        elif action == "execute" and signal_id:
            await self._handle_execute(query, signal_id)
        elif action == "reject" and signal_id:
            await self._handle_reject(query, signal_id)
        elif action == "info" and signal_id:
            await self._handle_info(query, signal_id)
        elif action == "noted" and signal_id:
            await self._edit_callback_message(
                query,
                query.message.text + "\n\nAcknowledged.",
                parse_mode="HTML",
            )

    async def _answer_callback(self, query) -> bool:
        try:
            await query.answer()
            return True
        except BadRequest as exc:
            if "query is too old" in str(exc).lower():
                logger.info("Ignoring expired Telegram callback query")
                return False
            logger.warning("Telegram callback acknowledgement rejected: %s", exc)
            return False
        except TelegramError as exc:
            logger.warning("Telegram callback acknowledgement failed: %s", exc)
            return False

    async def _edit_callback_message(
        self,
        query,
        text: str,
        parse_mode: Optional[str] = None,
        reply_markup=None,
    ) -> bool:
        try:
            await query.edit_message_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return True
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                logger.info("Ignoring duplicate Telegram callback update")
                return True
            logger.warning("Telegram callback message could not be edited: %s", exc)
            return False
        except TelegramError as exc:
            logger.warning("Telegram callback message update failed: %s", exc)
            return False

    async def _handle_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        error = getattr(context, "error", None)
        if isinstance(error, BadRequest):
            message = str(error).lower()
            if "message is not modified" in message or "query is too old" in message:
                logger.info("Ignoring benign Telegram callback error: %s", error)
                return
        if isinstance(error, TelegramError):
            logger.warning("Telegram update failed: %s", error)
            return
        logger.error(
            "Unhandled Telegram update error",
            exc_info=(
                type(error),
                error,
                getattr(error, "__traceback__", None),
            ) if error else True,
        )

    async def _handle_approve(self, query, signal_id: int):
        signal = self.db.get_signal_by_id(signal_id)
        if not signal:
            await self._edit_callback_message(query, "Signal not found.")
            return

        self.db.update_signal_status(signal_id, "approved")
        confirm_msg = (
            f"<b>APPROVED FOR PAPER REVIEW</b>\n\n"
            f"Ticker: {signal['ticker']}\n"
            f"Direction: {signal['direction']}\n"
            f"Action: {signal['suggested_action']}\n"
            f"Confidence: {(signal.get('confidence') or 0):.1%}\n"
            f"Expected excess: {(signal.get('expected_excess_return') or 0):.2%}\n"
            f"Execution mode: {self.config.execution.mode}\n"
            f"Live enabled: {self.config.execution.live_enabled}\n\n"
            "Execute will route through the configured paper/demo gate."
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("EXECUTE NOW", callback_data=f"execute_{signal_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"reject_{signal_id}"),
        ]])
        await self._edit_callback_message(
            query,
            confirm_msg,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def _handle_execute(self, query, signal_id: int):
        if not self.paper:
            await self._edit_callback_message(
                query, "Paper execution service is not configured."
            )
            return

        result = self.paper.execute_signal(signal_id)
        if result.get("ok"):
            msg = (
                f"<b>EXECUTION RECORDED</b>\n\n"
                f"Ledger order id: {result.get('order_id')}\n"
                f"Status: {result.get('status')}\n"
                f"Mode: {self.config.execution.mode}"
            )
        else:
            msg = (
                f"<b>EXECUTION REJECTED</b>\n\n"
                f"Reason: {result.get('reason')}\n"
                f"Ledger order id: {result.get('order_id', '-')}\n"
                f"Mode: {self.config.execution.mode}"
            )
        await self._edit_callback_message(query, msg, parse_mode="HTML")

    async def _handle_reject(self, query, signal_id: int):
        self.db.update_signal_status(signal_id, "rejected")
        await self._edit_callback_message(
            query,
            query.message.text + "\n\nRejected.",
            parse_mode="HTML",
        )
        logger.info("Signal %s rejected by user", signal_id)

    async def _handle_info(self, query, signal_id: int):
        signal = self.db.get_signal_by_id(signal_id)
        if not signal:
            await self._edit_callback_message(query, "Signal not found.")
            return

        breakdown = json.loads(signal.get("score_breakdown") or "{}")
        info = (
            f"<b>Signal #{signal_id}</b>\n\n"
            f"Ticker: {signal['ticker']}\n"
            f"Politician: {signal['politician_name']}\n"
            f"Direction: {signal['direction']}\n"
            f"Alpha score: {signal['alpha_score']:.1%}\n"
            f"Expected excess: {(signal.get('expected_excess_return') or 0):.2%}\n"
            f"Confidence: {(signal.get('confidence') or 0):.1%}\n"
            f"Tradeable: {bool(signal.get('tradeable'))}\n"
            f"Reason: {signal.get('decision_reason') or '-'}\n\n"
            f"<b>Components</b>\n"
        )
        for component, score in breakdown.items():
            if isinstance(score, (int, float)):
                info += f"  {component}: {score:.4f}\n"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve", callback_data=f"approve_{signal_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject_{signal_id}"),
        ]])
        await self._edit_callback_message(
            query,
            info,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "<b>Capitol Alpha</b>\n\n"
            "Commands:\n"
            "/status - System status\n"
            "/stats - Trading stats\n"
            "/pending - Pending signals\n"
            "/portfolio - T212 portfolio\n",
            parse_mode="HTML",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        stats = self.db.get_dashboard_stats()
        health = self.db.get_model_health()
        msg = (
            f"<b>Capitol Alpha Status</b>\n\n"
            f"Trades ingested: {stats['total_trades_ingested']}\n"
            f"Signals generated: {stats['total_signals']}\n"
            f"Pending signals: {stats['pending_signals']}\n"
            f"Paper orders: {stats['paper_orders']}\n"
            f"Paper P&L: GBP {stats['paper_net_pnl_gbp']:.2f}\n"
            f"Model: {health.get('active_model') or 'none'}\n"
            f"Execution mode: {self.config.execution.mode}\n"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        stats = self.db.get_dashboard_stats()
        await update.message.reply_text(json.dumps(stats, indent=2), parse_mode="HTML")

    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        pending = self.db.get_pending_signals()
        if not pending:
            await update.message.reply_text("No pending signals.")
            return
        for signal in pending[:10]:
            await self.send_signal_alert(signal)

    async def _cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.t212:
            positions = self.db.get_paper_positions()
            if not positions:
                await update.message.reply_text("Trading212 not configured. No local paper positions.")
                return
            msg = "<b>Local Paper Positions</b>\n\n"
            for pos in positions:
                msg += f"{pos['ticker']}: {pos['quantity']} shares, GBP {pos['notional_gbp']:.2f}\n"
            await update.message.reply_text(msg, parse_mode="HTML")
            return

        try:
            positions = self.t212.get_positions()
            if not positions:
                await update.message.reply_text("No open Trading212 positions.")
                return
            msg = "<b>Trading212 Portfolio</b>\n\n"
            for pos in positions:
                ticker = pos.get("ticker", "?")
                qty = pos.get("quantity", 0)
                pnl = pos.get("ppl", pos.get("pnl", 0))
                msg += f"{ticker}: {qty} shares (P&L GBP {pnl:.2f})\n"
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"Error fetching portfolio: {e}")
