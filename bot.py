import io
import logging
import MetaTrader5 as mt5
import pandas as pd
import mplfinance as mpf
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from config import TELEGRAM_TOKEN, OWNER_ID, DEFAULT_SYMBOL, DEFAULT_LOT, MAGIC, MAX_SLIPPAGE

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------- MT5 Init ----------
if not mt5.initialize():
    log.error(f"MT5 initialize failed: {mt5.last_error()}")
    raise SystemExit("MT5 connect nahi hua. Terminal open + demo login check karein.")

SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "USDJPY"]
TIMEFRAMES = {
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
}

# ---------- Owner Guard ----------
def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id != OWNER_ID:
            if update.callback_query:
                await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
            return
        return await func(update, context)
    return wrapper

# ---------- Keyboards ----------
def main_menu():
    sym = "EURUSD"
    kb = [
        [InlineKeyboardButton("📈 Live Chart", callback_data="menu_chart"),
         InlineKeyboardButton("💼 Positions", callback_data="menu_positions")],
        [InlineKeyboardButton("🟢 BUY", callback_data="menu_buy"),
         InlineKeyboardButton("🔴 SELL", callback_data="menu_sell")],
        [InlineKeyboardButton("💵 Account", callback_data="menu_account"),
         InlineKeyboardButton("❌ Close All", callback_data="menu_closeall")],
        [InlineKeyboardButton("🔁 Change Symbol", callback_data="menu_symbol")],
    ]
    return InlineKeyboardMarkup(kb)

def symbol_menu():
    kb = [[InlineKeyboardButton(f"• {s}", callback_data=f"setsym_{s}")] for s in SYMBOLS]
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(kb)

def chart_tf_menu(symbol):
    kb = [
        [InlineKeyboardButton("M5", callback_data=f"chart_{symbol}_M5"),
         InlineKeyboardButton("M15", callback_data=f"chart_{symbol}_M15")],
        [InlineKeyboardButton("H1", callback_data=f"chart_{symbol}_H1"),
         InlineKeyboardButton("H4", callback_data=f"chart_{symbol}_H4")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(kb)

def trade_confirm_menu(side, symbol):
    kb = [
        [InlineKeyboardButton(f"✅ Confirm {side} {DEFAULT_LOT} {symbol}",
                              callback_data=f"exec_{side}_{symbol}")],
        [InlineKeyboardButton("⬅️ Cancel", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(kb)

# ---------- Chart Generation ----------
def generate_chart(symbol: str, tf_name: str) -> io.BytesIO:
    tf = TIMEFRAMES[tf_name]
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, 120)
    if rates is None or len(rates) == 0:
        raise ValueError(f"No data for {symbol} {tf_name}: {mt5.last_error()}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)

    buf = io.BytesIO()
    mpf.plot(
        df,
        type="candle",
        style="charles",
        title=f"{symbol} — {tf_name}",
        ylabel="Price",
        volume=True,
        savefig=dict(fname=buf, format="png", dpi=120, bbox_inches="tight"),
    )
    buf.seek(0)
    return buf

# ---------- Trading ----------
def place_market_order(symbol: str, side: str, lot: float):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False, f"Tick nahi mila: {mt5.last_error()}"

    if side.upper() == "BUY":
        order_type, price = mt5.ORDER_TYPE_BUY, tick.ask
    else:
        order_type, price = mt5.ORDER_TYPE_SELL, tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "deviation": MAX_SLIPPAGE,
        "magic": MAGIC,
        "comment": "tg-demo-bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None:
        return False, f"order_send None: {mt5.last_error()}"
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return False, f"Retcode {result.retcode}: {result.comment}"
    return True, f"{side} {lot} {symbol} @ {price} (ticket {result.order})"

def close_all_positions():
    positions = mt5.positions_get()
    if not positions:
        return "Koi open position nahi hai."
    closed, failed = 0, 0
    for p in positions:
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            failed += 1
            continue
        close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": p.ticket,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": close_type,
            "price": price,
            "deviation": MAX_SLIPPAGE,
            "magic": MAGIC,
            "comment": "tg-closeall",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        r = mt5.order_send(req)
        if r and r.retcode == mt5.TRADE_RETCODE_DONE:
            closed += 1
        else:
            failed += 1
    return f"Closed: {closed}, Failed: {failed}"

def positions_text():
    positions = mt5.positions_get()
    if not positions:
        return "📊 Koi open position nahi."
    lines = ["📊 *Open Positions*"]
    for p in positions:
        side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
        lines.append(
            f"• {p.symbol} {side} {p.volume} @ {p.price_open:.5f} "
            f"| PnL: {p.profit:.2f}"
        )
    return "\n".join(lines)

def account_text():
    info = mt5.account_info()
    if info is None:
        return "Account info nahi mila."
    return (
        f"💵 *Account Info*\n"
        f"Login: {info.login}\n"
        f"Server: {info.server}\n"
        f"Balance: {info.balance:.2f} {info.currency}\n"
        f"Equity: {info.equity:.2f}\n"
        f"Margin: {info.margin:.2f}\n"
        f"Free Margin: {info.margin_free:.2f}\n"
        f"Type: {'DEMO' if info.trade_mode == 0 else 'LIVE'}"
    )

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return
    context.user_data["symbol"] = DEFAULT_SYMBOL
    await update.message.reply_text(
        f"🤖 *Demo Trading Bot Ready*\nSymbol: `{DEFAULT_SYMBOL}`\nLot: `{DEFAULT_LOT}`",
        reply_markup=main_menu(),
        parse_mode="Markdown",
    )

@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    symbol = context.user_data.get("symbol", DEFAULT_SYMBOL)

    try:
        # ---- Navigation ----
        if data == "menu_main":
            await q.edit_message_text(
                f"🤖 *Main Menu*\nSymbol: `{symbol}`\nLot: `{DEFAULT_LOT}`",
                reply_markup=main_menu(), parse_mode="Markdown"
            )
            return

        if data == "menu_symbol":
            await q.edit_message_text("🔁 Symbol chunein:", reply_markup=symbol_menu())
            return

        if data.startswith("setsym_"):
            sym = data.split("_", 1)[1]
            context.user_data["symbol"] = sym
            await q.edit_message_text(
                f"✅ Symbol set: `{sym}`",
                reply_markup=main_menu(), parse_mode="Markdown"
            )
            return

        # ---- Chart ----
        if data == "menu_chart":
            await q.edit_message_text(
                f"📈 {symbol} — Timeframe chunein:",
                reply_markup=chart_tf_menu(symbol)
            )
            return

        if data.startswith("chart_"):
            _, sym, tf = data.split("_")
            await q.edit_message_text(f"⏳ {sym} {tf} chart ban raha hai...")
            try:
                buf = generate_chart(sym, tf)
                await q.message.reply_photo(
                    photo=buf, caption=f"📊 {sym} — {tf}",
                    reply_markup=main_menu()
                )
            except Exception as e:
                await q.message.reply_text(f"❌ Chart error: {e}", reply_markup=main_menu())
            return

        # ---- Trade ----
        if data in ("menu_buy", "menu_sell"):
            side = "BUY" if data == "menu_buy" else "SELL"
            await q.edit_message_text(
                f"⚠️ Confirm karein: {side} {DEFAULT_LOT} {symbol} (DEMO)",
                reply_markup=trade_confirm_menu(side, symbol)
            )
            return

        if data.startswith("exec_"):
            _, side, sym = data.split("_")
            ok, msg = place_market_order(sym, side, DEFAULT_LOT)
            prefix = "✅" if ok else "❌"
            await q.edit_message_text(f"{prefix} {msg}", reply_markup=main_menu())
            return

        # ---- Positions / Account / Close ----
        if data == "menu_positions":
            await q.edit_message_text(
                positions_text(), reply_markup=main_menu(), parse_mode="Markdown"
            )
            return

        if data == "menu_account":
            await q.edit_message_text(
                account_text(), reply_markup=main_menu(), parse_mode="Markdown"
            )
            return

        if data == "menu_closeall":
            result = close_all_positions()
            await q.edit_message_text(f"❌ {result}", reply_markup=main_menu())
            return

    except Exception as e:
        log.exception("handler error")
        await q.message.reply_text(f"⚠️ Error: {e}", reply_markup=main_menu())

# ---------- Run ----------
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()