#!/usr/bin/env python3
"""
PRL Mining Toolkit - Windows Desktop App (tkinter)
6 tabs: Profitability, Breakeven, Daily, Sales, Transfers, Drag Analysis, Data

Data persisted to ~/.hermes/mining_toolkit/
  daily_log.json    - daily mining entries
  sales_log.json    - trade entries (sells + purchases)
  transfers_log.json - exchange-to-exchange transfers
"""

import json
import tkinter as tk
from tkinter import ttk, messagebox
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

# ==============================================================
# CALCULATION ENGINE
# ==============================================================

DATA_DIR = Path.home() / ".hermes" / "mining_toolkit"
DAILY_LOG = DATA_DIR / "daily_log.json"
SALES_LOG = DATA_DIR / "sales_log.json"
TRANSFERS_LOG = DATA_DIR / "transfers_log.json"

# Fee schedule — PRL -> USDT -> ARB -> USD pipeline
# PRL -> USDT (SafeTrade): 0.1% of USDT received, paid in USDT
DEFAULT_PRL_USDT_FEE_PCT = 0.1
# USDT -> ARB (SafeTrade): 0.1% of ARB received, paid in ARB
DEFAULT_USDT_ARB_FEE_PCT = 0.1
# Transfer ARB SafeTrade -> Coinbase: flat 2 ARB
DEFAULT_ARB_TRANSFER_FEE = 2.0
# ARB -> USD (Coinbase): $0.46 flat + 0.1% of USD received, paid in USD
DEFAULT_ARB_USD_FLAT_FEE = 0.46
DEFAULT_ARB_USD_FEE_PCT = 0.1

# Banner font sizes (20% larger than default)
BANNER_FONT_SIZE = 10  # base size for labels
BANNER_VALUE_SIZE = 11  # slightly larger for values
BANNER_TITLE_SIZE = 12  # section titles


def compute_holdings():
    """Compute current coin holdings from daily log, sales, and transfers.

    Returns a dict:
      { 'PRL': float, 'USDT': float, 'ARB': float, 'USD': float }
    Tracks mined PRL, USDT from sales, ARB from purchases, minus transfers.
    """
    holdings = {'PRL': 0.0, 'USDT': 0.0, 'ARB': 0.0, 'USD': 0.0}

    # PRL mined
    for e in load_daily():
        if e.coin == 'PRL':
            holdings['PRL'] += e.coins_mined

    # Sales
    for e in load_sales():
        if e.side == 'Sell':
            # Selling base_coin for quote_coin
            holdings[e.base_coin] -= e.amount
            holdings[e.quote_coin] += e.total
        else:
            # Buying base_coin with quote_coin
            holdings[e.base_coin] += e.amount
            holdings[e.quote_coin] -= e.total

    # Transfers (move coins between exchanges, deduct fees)
    for e in load_transfers():
        coin = e.coin
        holdings[coin] -= e.fee_amount  # transfer fee deducted

    return holdings


def compute_exchange_holdings():
    """Compute holdings per exchange.

    Returns a dict:
      { 'SafeTrade': {'PRL': x, 'USDT': y, 'ARB': z}, 'Coinbase': {...}, 'Wallet': {...}, ... }
    """
    exchanges = {}
    def get_exch(name):
        if name not in exchanges:
            exchanges[name] = {'PRL': 0.0, 'USDT': 0.0, 'ARB': 0.0, 'USD': 0.0}
        return exchanges[name]

    # PRL mined — assume to SafeTrade (mining exchange)
    for e in load_daily():
        if e.coin == 'PRL':
            get_exch('SafeTrade')['PRL'] += e.coins_mined

    # Sales — assume SafeTrade for PRL/USDT and USDT/ARB
    for e in load_sales():
        exch = get_exch(e.exchange or 'SafeTrade')
        if e.side == 'Sell':
            exch[e.base_coin] -= e.amount
            exch[e.quote_coin] += e.total
        else:
            exch[e.base_coin] += e.amount
            exch[e.quote_coin] -= e.total

    # Transfers
    for e in load_transfers():
        from_exch = get_exch(e.from_exchange or 'SafeTrade')
        to_exch = get_exch(e.to_exchange or 'Coinbase')
        coin = e.coin
        from_exch[coin] -= e.amount
        to_exch[coin] += e.received

    return exchanges


def get_last_daily_entry():
    """Return the most recent daily mining entry, or None."""
    entries = load_daily()
    if not entries:
        return None
    return max(entries, key=lambda e: e.date)


def compute_breakeven_price(arb_usd, elec_cost, coins_mined, d1, d2, d3, f1, f2, f3, fee3_flt, xfer_arb):
    """Compute the breakeven PRL price given parameters.

    Returns (be_price, arb_after_xfer, arb_before_xfer, usdt_needed).
    """
    if arb_usd <= 0 or coins_mined <= 0:
        return float('inf'), 0, 0, 0
    arb_after_xfer = (elec_cost + fee3_flt) / (arb_usd * d3 * f3)
    arb_before_xfer = arb_after_xfer + xfer_arb
    usdt_needed = arb_before_xfer * arb_usd / (d2 * f2)
    be_price = usdt_needed / (coins_mined * d1 * f1)
    return be_price, arb_after_xfer, arb_before_xfer, usdt_needed


def get_last_activity():
    """Get the most recent sale and transfer to determine pipeline position.

    Returns (last_sale, last_transfer) where each is a dict or None.
    """
    sales = load_sales()
    transfers = load_transfers()

    last_sale = None
    last_transfer = None

    for e in sales:
        if last_sale is None or e.date > last_sale.date:
            last_sale = e

    for e in transfers:
        if last_transfer is None or e.date > last_transfer.date:
            last_transfer = e

    return last_sale, last_transfer


def get_pipeline_position():
    """Determine where we are in the PRL -> USDT -> ARB -> USD pipeline.

    Returns a string describing the current position.
    """
    last_sale, last_transfer = get_last_activity()

    if last_sale is None and last_transfer is None:
        return "No activity yet — start by mining PRL"

    # Determine the most recent event
    sale_date = last_sale.date if last_sale else ""
    xfer_date = last_transfer.date if last_transfer else ""

    if sale_date >= xfer_date:
        # Most recent event is a sale
        if last_sale.side == 'Sell' and last_sale.base_coin == 'PRL':
            return f"Last: Sold {last_sale.amount} PRL for {last_sale.total} USDT on {last_sale.date} — Next: Buy ARB with USDT"
        elif last_sale.side == 'Buy' and last_sale.quote_coin == 'ARB':
            return f"Last: Bought {last_sale.amount} ARB for {last_sale.total} USDT on {last_sale.date} — Next: Transfer ARB to Coinbase"
        elif last_sale.side == 'Sell' and last_sale.base_coin == 'ARB':
            return f"Last: Sold {last_sale.amount} ARB for {last_sale.total} USD on {last_sale.date} — Pipeline complete!"
        else:
            return f"Last sale: {last_sale.side} {last_sale.base_coin}/{last_sale.quote_coin} on {last_sale.date}"
    else:
        # Most recent event is a transfer
        return f"Last: Transferred {last_transfer.received} {last_transfer.coin} from {last_transfer.from_exchange} to {last_transfer.to_exchange} on {last_transfer.date} — Next: Sell ARB for USD"

COINS = ["PRL", "BTC", "ETH", "USDT", "ARB", "USD", "EUR", "LTC", "XMR", "OTHER"]
ORDER_TYPES = ["Limit", "Market"]
SIDES = ["Buy", "Sell"]
STATUSES = ["Filled", "Canceled", "Pending", "Partial"]
EXCHANGES = ["SafeTrade", "Coinbase", "Binance", "Kraken", "Wallet", "Other"]


@dataclass
class DailyMiningEntry:
    date: str; coin: str; coins_mined: float; price: float; power: float
    elec_price: float; time_hours: float
    gross_revenue: float = 0; electricity_cost: float = 0; net_profit: float = 0


@dataclass
class TradeEntry:
    date: str = ""
    base_coin: str = ""
    quote_coin: str = ""
    order_type: str = "Limit"
    side: str = "Sell"
    status: str = "Filled"
    exchange: str = ""
    price: float = 0
    basis: float = 0          # expected coin amount (what you tried to trade)
    amount: float = 0         # actual coin amount traded
    filled_pct: float = 100
    total: float = 0
    fee_coin: str = ""        # coin in which the fee is charged (quote coin by default)
    fee_amount: float = 0     # fee amount in fee_coin
    fee_usd: float = 0        # computed: fee_amount * price_of_fee_coin
    net_usd: float = 0        # computed: total - fee_usd
    drag_pct: float = 0       # (basis - amount) / basis * 100


@dataclass
class TransferEntry:
    date: str = ""
    coin: str = ""
    from_exchange: str = ""
    to_exchange: str = ""
    amount: float = 0
    fee_coin: str = "ETH"
    fee_amount: float = 0
    fee_usd: float = 0
    received: float = 0
    notes: str = ""
    status: str = "Completed"


@dataclass
class DragResult:
    step1_drag_pct: float = 0; step2_drag_pct: float = 0
    step3_drag_pct: float = 0; total_drag_pct: float = 0; effective_rate: float = 0


def calc_daily_mining(coin, coins_mined, price, power, elec_price, time_h):
    elec = (power * time_h / 1000.0) * elec_price
    gross = coins_mined * price
    return DailyMiningEntry("", coin, coins_mined, price, power, elec_price, time_h, gross, elec, gross - elec)


def calc_trade_net(entry):
    # Compute fee_usd from fee_coin and fee_amount.
    # The fee is always in the quote coin (the "new" coin received in the trade).
    # Conversion to USD:
    #   - Stablecoins (USDT, USD, EUR): 1 unit ≈ 1 USD
    #   - Crypto: derive USD price from the trade itself if base is a stablecoin,
    #     otherwise use stored price.  price = quote_coin per base_coin.
    #     If base is USDT/USD/EUR: 1 quote = (1/price) USD
    if not entry.fee_coin:
        entry.fee_coin = entry.quote_coin or "USDT"
    if entry.fee_amount > 0:
        if entry.fee_coin.upper() in ("USDT", "USD", "EUR"):
            entry.fee_usd = entry.fee_amount
        elif entry.base_coin.upper() in ("USDT", "USD", "EUR") and entry.price > 0:
            # price = quote_coin per base_coin, base is stable => 1 quote = (1/price) USD
            entry.fee_usd = entry.fee_amount / entry.price
        elif entry.total > 0 and entry.amount > 0:
            # total / amount = effective price in quote coin per base coin
            # If quote is USD-stable, fee_usd = fee_amount
            # Otherwise approximate: use total as USD proxy (best-effort for stable-quote pairs)
            entry.fee_usd = entry.fee_amount  # assume quote ≈ USD (common case: PRL/USDT)
        else:
            entry.fee_usd = 0
    else:
        entry.fee_usd = 0
    if entry.total > 0:
        entry.net_usd = entry.total - entry.fee_usd
    # drag % = (basis - amount) / basis * 100
    if entry.basis > 0 and entry.amount > 0:
        entry.drag_pct = round((entry.basis - entry.amount) / entry.basis * 100, 3)
    return entry


def calc_transfer_net(entry):
    entry.received = entry.amount - entry.fee_amount
    return entry


def analyze_drag(from_amount, from_price, mid_amount, mid_price, to_amount, to_price, usd_received):
    d = DragResult()
    if from_amount <= 0 or from_price <= 0:
        return d
    exp_mid = from_amount * from_price
    if exp_mid > 0:
        d.step1_drag_pct = ((exp_mid - mid_amount) / exp_mid) * 100
    if mid_amount > 0 and mid_price > 0:
        exp_to = mid_amount * mid_price
        if exp_to > 0:
            d.step2_drag_pct = ((exp_to - to_amount) / exp_to) * 100
    if to_amount > 0 and to_price > 0:
        exp_usd = to_amount * to_price
        if exp_usd > 0:
            d.step3_drag_pct = ((exp_usd - usd_received) / exp_usd) * 100
    initial = from_amount * from_price
    if initial > 0:
        d.total_drag_pct = ((initial - usd_received) / initial) * 100
        d.effective_rate = usd_received / from_amount
    return d


# -- Persistence --
def load_json(path):
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))

def load_daily():
    raw = load_json(DAILY_LOG)
    entries = []
    for e in raw:
        if "coin" not in e:
            e["coin"] = "PRL"
        entries.append(DailyMiningEntry(**e))
    return entries

def save_daily(entries):
    save_json(DAILY_LOG, [asdict(e) for e in entries])

def load_sales():
    return [TradeEntry(**e) for e in load_json(SALES_LOG)]

def save_sales(entries):
    save_json(SALES_LOG, [asdict(e) for e in entries])

def load_transfers():
    return [TransferEntry(**e) for e in load_json(TRANSFERS_LOG)]

def save_transfers(entries):
    save_json(TRANSFERS_LOG, [asdict(e) for e in entries])


# ==============================================================
# DRAG ANALYSIS FROM SALES DATA
# ==============================================================

def compute_avg_drag_from_sales(pair_filter=None):
    """Analyse saved sales_log trades to compute average drag per trading pair.

    Drag is the % loss between expected and actual received amount, computed
    in the coin you receive at each step:

      Sell PRL/USDT  -> receive USDT:
          expected = amount * price,  actual = total

      Buy USDT/ARB   -> receive ARB:
          expected = total * price,   actual = amount
          (price is ARB per USDT, so total_USDT * price = expected_ARB)

      Sell ARB/USD   -> receive USD:
          expected = amount * price,  actual = total

    drag_pct = (expected - actual) / expected * 100  (positive = loss)

    Returns a dict: { "PRL/USDT": 0.012, "USDT/ARB": 0.008, ... }
    Values are None if no qualifying trades exist for that pair.
    """
    entries = load_sales()
    pair_drag = {}   # pair -> list of drag_pct values
    for e in entries:
        pair = f"{e.base_coin}/{e.quote_coin}"
        if pair_filter and pair != pair_filter:
            continue
        expected = actual = 0.0
        if e.side == "Buy":
            # Buying base_coin with quote_coin: receive base_coin
            # price = base_coin per quote_coin (e.g., ARB/USDT)
            # expected_base = total_quote * price
            if e.total > 0 and e.price > 0:
                expected = e.total * e.price
                actual = e.amount
        else:
            # Selling base_coin for quote_coin: receive quote_coin
            # price = quote_coin per base_coin (e.g., USDT/PRL)
            # expected_quote = amount_base * price
            if e.amount > 0 and e.price > 0:
                expected = e.amount * e.price
                actual = e.total
        if expected > 0 and actual > 0:
            drag_pct = (expected - actual) / expected * 100.0
            pair_drag.setdefault(pair, []).append(drag_pct)
    result = {}
    for pair, vals in pair_drag.items():
        if vals:
            result[pair] = sum(vals) / len(vals)
        else:
            result[pair] = None
    return result


def compute_step_drag_sales(pair_str):
    """Return average drag_pct for a specific pair from sales data, or 0.0."""
    d = compute_avg_drag_from_sales(pair_filter=pair_str)
    val = d.get(pair_str)
    if val is not None:
        return val
    return 0.0

class PRLMiningApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PRL Mining Toolkit")
        self.root.geometry("1280x820")
        self.root.minsize(1024, 680)

        style = ttk.Style()
        style.theme_use("clam")

        # Increase default font sizes by 20%
        default_font = ("Segoe UI", 10)
        style.configure(".", font=default_font)
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("TEntry", font=("Segoe UI", 10))
        style.configure("TCombobox", font=("Segoe UI", 10))
        style.configure("Treeview", font=("Consolas", 10), rowheight=22)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TNotebook.Tab", font=("Segoe UI", 11, "bold"))
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))

        # ==========================================================
        # BANNER — Holdings + Pipeline Position
        # ==========================================================
        self.banner_frame = ttk.Frame(root, padding=6)
        self.banner_frame.pack(fill="x", padx=4, pady=(4, 0))

        # Holdings section
        holdings_inner = ttk.LabelFrame(self.banner_frame, text="  Current Holdings  ", padding=6)
        holdings_inner.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.banner_holdings = {}
        for i, coin in enumerate(["PRL", "USDT", "ARB", "USD"]):
            ttk.Label(holdings_inner, text=coin + ":", font=("Segoe UI", 10, "bold")).grid(row=0, column=i * 2, padx=(4, 1), pady=2)
            lbl = ttk.Label(holdings_inner, text="—", font=("Consolas", 11), foreground="#2E7D32", width=14, anchor="e")
            lbl.grid(row=0, column=i * 2 + 1, padx=(0, 8), pady=2)
            self.banner_holdings[coin] = lbl

        # Exchange breakdown
        exch_inner = ttk.LabelFrame(self.banner_frame, text="  By Exchange  ", padding=6)
        exch_inner.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.banner_exchanges = {}
        for i, exch in enumerate(["SafeTrade", "Coinbase"]):
            ttk.Label(exch_inner, text=exch + ":", font=("Segoe UI", 10, "bold")).grid(row=0, column=i * 2, padx=(4, 1), pady=2)
            lbl = ttk.Label(exch_inner, text="—", font=("Consolas", 10), foreground="#1565C0", width=22, anchor="w")
            lbl.grid(row=0, column=i * 2 + 1, padx=(0, 4), pady=2)
            self.banner_exchanges[exch] = lbl

        # Pipeline position
        pos_inner = ttk.LabelFrame(self.banner_frame, text="  Pipeline Status  ", padding=6)
        pos_inner.pack(side="left", fill="x", expand=True)

        self.banner_position = ttk.Label(pos_inner, text="—", font=("Segoe UI", 10), foreground="#4E342E", wraplength=340, anchor="w")
        self.banner_position.pack(fill="x", padx=4, pady=2)

        # Refresh button
        ttk.Button(self.banner_frame, text="↻ Refresh", command=self._refresh_banner, width=10).pack(side="right", padx=4)

        # ==========================================================
        # NOTEBOOK
        # ==========================================================
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=4, pady=4)

        self.tab_frames = []
        for name in ["Profitability", "Breakeven", "Daily Mining", "Sales", "Transfers", "Drag Analysis", "Data"]:
            f = ttk.Frame(self.notebook)
            self.notebook.add(f, text=f"  {name}  ")
            self.tab_frames.append(f)

        self._build_profit_tab()
        self._build_breakeven_tab()
        self._build_daily_tab()
        self._build_sales_tab()
        self._build_transfers_tab()
        self._build_drag_tab()
        self._build_data_tab()

        # Populate banner on startup
        self._refresh_banner()

    @staticmethod
    def _float(var):
        try:
            return float(var.get() or "0")
        except ValueError:
            return 0.0

    def _refresh_banner(self):
        """Update the banner with current holdings and pipeline position."""
        from mining_toolkit_win import compute_holdings, compute_exchange_holdings, get_pipeline_position

        # Total holdings
        h = compute_holdings()
        for coin, lbl in self.banner_holdings.items():
            val = h.get(coin, 0.0)
            lbl.config(text=f"{val:,.4f}")

        # Exchange breakdown
        ex = compute_exchange_holdings()
        for exch, lbl in self.banner_exchanges.items():
            eh = ex.get(exch, {})
            parts = []
            for coin in ["PRL", "USDT", "ARB", "USD"]:
                v = eh.get(coin, 0.0)
                if abs(v) > 0.0001:
                    parts.append(f"{coin}:{v:,.2f}")
            lbl.config(text="  ".join(parts) if parts else "empty")

        # Pipeline position
        pos = get_pipeline_position()
        self.banner_position.config(text=pos)

    def _sort_treeview(self, tree, col, reverse):
        """Sort a treeview column by clicking the heading.
        Handles numeric columns (strips $, commas, %) and string/date columns.
        Toggles sort direction on repeated clicks.
        """
        items = [(tree.set(k, col), k) for k in tree.get_children("")]

        # Check if column is numeric
        is_numeric = False
        for val, _ in items:
            if val and val not in ("—", "$0.00", "$0.0000"):
                clean = val.replace("$", "").replace(",", "").replace("%", "").strip()
                try:
                    float(clean)
                    is_numeric = True
                except ValueError:
                    pass
                break

        if is_numeric:
            def sort_key(item):
                val = item[0].replace("$", "").replace(",", "").replace("%", "").strip()
                try:
                    return float(val)
                except ValueError:
                    return float("-inf")
            items.sort(key=sort_key, reverse=reverse)
        else:
            items.sort(key=lambda item: item[0].lower(), reverse=reverse)

        for idx, (_, k) in enumerate(items):
            tree.move(k, "", idx)

        # Toggle direction for next click
        tree.heading(col, command=lambda: self._sort_treeview(tree, col, not reverse))

    @staticmethod
    def _grid_label(parent, row, text):
        lbl = ttk.Label(parent, text=text)
        lbl.grid(row=row, column=0, sticky="w", padx=6, pady=3)
        return lbl

    @staticmethod
    def _grid_entry(parent, row, default="", width=18):
        var = tk.StringVar(value=default)
        ent = ttk.Entry(parent, textvariable=var, width=width)
        ent.grid(row=row, column=1, sticky="w", padx=6, pady=3)
        return var

    @staticmethod
    def _grid_combo(parent, row, values, default=None, width=15):
        var = tk.StringVar(value=default or values[0])
        combo = ttk.Combobox(parent, textvariable=var, values=values, width=width, state="readonly")
        combo.grid(row=row, column=1, sticky="w", padx=6, pady=3)
        return var

    def _text_set(self, widget, text):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    # ==========================================================
    # TAB 1: PROFITABILITY — PRL -> USDT -> ARB -> USD
    # ==========================================================
    def _build_profit_tab(self):
        f = self.tab_frames[0]
        inp = ttk.LabelFrame(f, text="Inputs", padding=8)
        inp.pack(side="left", fill="y", padx=6, pady=6)

        self._profit_vars = []
        # Pre-fill from last daily mining entry
        last_daily = get_last_daily_entry()
        defaults = [
            ("Coins mined:", str(last_daily.coins_mined) if last_daily else "100"),
            ("Mining time (hours):", str(last_daily.time_hours) if last_daily else "24"),
            ("Power (watts):", str(last_daily.power) if last_daily else "1600"),
            ("Electricity (USD/kWh):", str(last_daily.elec_price) if last_daily else "0.15"),
            ("PRL price (USDT):", str(last_daily.price) if last_daily else "0.05"),
            ("ARB spot (USD):", "0.30"),
        ]
        for i, (lbl, dflt) in enumerate(defaults):
            self._grid_label(inp, i, lbl)
            self._profit_vars.append(self._grid_entry(inp, i, dflt))

        ttk.Button(inp, text="Calculate", command=self._calc_profit).grid(row=6, column=0, columnspan=2, padx=6, pady=8, sticky="ew")

        res = ttk.LabelFrame(f, text="Results", padding=8)
        res.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.profit_result = tk.Text(res, wrap="word", width=60, height=30, font=("Consolas", 10))
        self.profit_result.pack(fill="both", expand=True)
        self.profit_result.insert("1.0", "Enter values and click Calculate.")
        self.profit_result.config(state="disabled")

    def _calc_profit(self):
        from mining_toolkit_win import (
            compute_step_drag_sales,
            DEFAULT_PRL_USDT_FEE_PCT, DEFAULT_USDT_ARB_FEE_PCT,
            DEFAULT_ARB_USD_FEE_PCT, DEFAULT_ARB_USD_FLAT_FEE,
            DEFAULT_ARB_TRANSFER_FEE,
        )
        coins_mined = self._float(self._profit_vars[0])
        time_h      = self._float(self._profit_vars[1])
        power_w     = self._float(self._profit_vars[2])
        elec_kwh    = self._float(self._profit_vars[3])
        prl_price   = self._float(self._profit_vars[4])
        arb_usd     = self._float(self._profit_vars[5])

        elec_cost = (power_w * time_h / 1000.0) * elec_kwh

        drag1 = compute_step_drag_sales("PRL/USDT")
        drag2 = compute_step_drag_sales("USDT/ARB")
        drag3 = compute_step_drag_sales("ARB/USD")

        d1 = 1.0 - drag1 / 100.0
        d2 = 1.0 - drag2 / 100.0
        d3 = 1.0 - drag3 / 100.0

        fee1_pct = DEFAULT_PRL_USDT_FEE_PCT
        fee2_pct = DEFAULT_USDT_ARB_FEE_PCT
        fee3_pct = DEFAULT_ARB_USD_FEE_PCT
        fee3_flt = DEFAULT_ARB_USD_FLAT_FEE
        xfer_arb = DEFAULT_ARB_TRANSFER_FEE

        f1 = 1.0 - fee1_pct / 100.0
        f2 = 1.0 - fee2_pct / 100.0
        f3 = 1.0 - fee3_pct / 100.0

        # Forward calculation
        gross_usdt  = coins_mined * prl_price
        step1_usdt  = gross_usdt * d1 * f1
        if arb_usd > 0:
            step2_arb   = step1_usdt / arb_usd * d2 * f2
            step2_after = step2_arb - xfer_arb
            step3_gross = step2_after * arb_usd * d3 * f3
            step3_net   = step3_gross - fee3_flt
        else:
            step2_arb = step2_after = step3_gross = step3_net = 0.0

        net_profit      = step3_net - elec_cost
        profit_per_coin = net_profit / coins_mined if coins_mined > 0 else 0
        cost_ratio      = (elec_cost / step3_gross * 100.0) if step3_gross > 0 else 0.0

        # --- Profitability vs electricity (margin on costs) ---
        profit_pct = (net_profit / elec_cost * 100.0) if elec_cost > 0 else 0.0

        # --- Breakeven comparison ---
        be_price, _, _, _ = compute_breakeven_price(
            arb_usd, elec_cost, coins_mined, d1, d2, d3, f1, f2, f3, fee3_flt, xfer_arb)
        price_above_be = prl_price - be_price
        price_above_be_pct = ((prl_price / be_price) - 1) * 100.0 if be_price > 0 and be_price != float('inf') else 0.0
        # Tokens per day above breakeven: how many extra coins beyond breakeven threshold
        # At breakeven price, coins_mined covers electricity. At current price, the "excess" is:
        # excess_coins = coins_mined - (elec_cost / (prl_price_usd_per_coin_effective))
        # effective_usd_per_coin = prl_price * d1*f1 * d2*f2 / arb_usd * d3*f3 * arb_usd ... simplified:
        # usd_per_prl_effective = (step3_net + elec_cost) / coins_mined = step3_gross_usd_equivalent / coins_mined
        # Actually simpler: breakeven coins for this config = elec_cost / (net_usd_per_coin_at_current_price)
        # net_usd_per_coin = step3_net / coins_mined... no that's after elec.
        # breakeven_coins = be_price equivalent... let me just compute from the be formula:
        # be_coins = elec_cost / (prl_effective_usd_per_coin) where prl_effective = price * chain_efficiency
        chain_eff = d1 * f1 * d2 * f2 * d3 * f3
        effective_usd_per_coin = prl_price * chain_eff  # how much USD each PRL is worth after the full chain (before flat fees)
        # But flat fees (xfer + arb_usd_flat) are batch-level. For per-coin: effective = prl_price * chain_eff - flat_fees_per_coin
        flat_per_coin = (xfer_arb * arb_usd + fee3_flt) / coins_mined if coins_mined > 0 else 0
        net_usd_per_coin = effective_usd_per_coin - flat_per_coin - (elec_cost / coins_mined) if coins_mined > 0 else 0
        be_coins_at_this_price = elec_cost / (prl_price * chain_eff - flat_per_coin) if (prl_price * chain_eff - flat_per_coin) > 0 else float('inf')
        tokens_above_be = coins_mined - be_coins_at_this_price

        def drag_note(val):
            return f"{val:.4f}%" if val > 0 else "0% (no data)"

        # Color indicators for display (using text markers since tk.Text supports tags)
        profit_color = " PROFITABLE + " if net_profit > 0 else " UNPROFITABLE "

        lines = [
            "=== CONVERSION: PRL -> USDT -> ARB -> USD ===",
            "",
            f"  Mine:     {coins_mined:.2f} PRL  @ ${prl_price:.4f} USDT/PRL",
            f"  ARB spot: ${arb_usd:.4f} USD",
            "",
            "--- Step 1: PRL -> USDT (SafeTrade) ---",
            f"  Gross:   ${gross_usdt:.4f} USDT  ({coins_mined:.2f} x ${prl_price:.4f})",
            f"  Drag:    {drag_note(drag1)}",
            f"  Fee:     {fee1_pct:.2f}% in USDT",
            f"  Net:     ${step1_usdt:.4f} USDT",
            "",
            "--- Step 2: USDT -> ARB (SafeTrade) ---",
            f"  Drag:    {drag_note(drag2)}",
            f"  Fee:     {fee2_pct:.2f}% in ARB",
            f"  Net:     {step2_arb:.4f} ARB",
            "",
            "--- Step 3: Transfer ARB (SafeTrade -> Coinbase) ---",
            f"  Sent:    {step2_arb:.4f} ARB",
            f"  Fee:     {xfer_arb:.2f} ARB flat",
            f"  Arrive:  {step2_after:.4f} ARB",
            "",
            "--- Step 4: ARB -> USD (Coinbase) ---",
            f"  Drag:    {drag_note(drag3)}",
            f"  Fee:     ${fee3_flt:.2f} + {fee3_pct:.2f}% in USD",
            f"  Gross:   ${step3_gross:.4f} USD",
            f"  Net:     ${step3_net:.4f} USD",
            "",
            "=== COSTS ===",
            f"  Electricity:         ${elec_cost:.4f}",
            f"  Total costs:         ${elec_cost:.4f}",
            "",
            " === RESULT ===",
            f" Net profit:          ${net_profit:.4f}",
            f" Profit/coin:         ${profit_per_coin:.6f}",
            f" Margin on costs:     {profit_pct:+.1f}%",
            f"",
            f" Breakeven price:     ${be_price:.6f} USDT/PRL",
            f" Price above BE:      ${price_above_be:+.6f} ({price_above_be_pct:+.1f}%)",
            f" BE coins (this day): {be_coins_at_this_price:.2f} PRL",
            f" Tokens above BE:     {tokens_above_be:+.2f} PRL",
            f"",
            f"  >>> {profit_color} <<<",
        ]
        self._text_set(self.profit_result, "\n".join(lines))

    # ==========================================================
    # TAB 2: BREAKEVEN — PRL -> USDT -> ARB -> USD
    # Fees: 0.1% USDT + 0.1% ARB + 2 ARB xfer + $0.46+0.1% USD
    # ==========================================================
    def _build_breakeven_tab(self):
        f = self.tab_frames[1]
        inp = ttk.LabelFrame(f, text="Inputs", padding=8)
        inp.pack(side="left", fill="y", padx=6, pady=6)

        # Mode selector
        self._breakeven_mode = tk.StringVar(value="price")
        ttk.Label(inp, text="Calculate:", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w", padx=6, pady=2)
        ttk.Radiobutton(inp, text="Breakeven PRL price", variable=self._breakeven_mode, value="price",
                        command=self._on_breakeven_mode_change).grid(row=0, column=1, sticky="w", padx=6, pady=2)
        ttk.Radiobutton(inp, text="Breakeven coin count", variable=self._breakeven_mode, value="coins",
                        command=self._on_breakeven_mode_change).grid(row=1, column=1, sticky="w", padx=6, pady=2)

        # Shared inputs
        self._breakeven_vars = []
        for i, (lbl, dflt) in enumerate([
            ("Mining time (hours):", "24"),
            ("Power (watts):", "800"),
            ("Electricity (USD/kWh):", "0.10"),
            ("ARB spot (USD):", "0.30"),
        ]):
            self._grid_label(inp, i + 2, lbl)
            self._breakeven_vars.append(self._grid_entry(inp, i + 2, dflt))

        # Mode-specific inputs
        self._grid_label(inp, 6, "Coins mined:")
        self._breakeven_coins = self._grid_entry(inp, 6, "100")
        self._grid_label(inp, 7, "PRL price (USDT):")
        self._breakeven_prl_price = self._grid_entry(inp, 7, "0.05")

        # Fee overrides
        ttk.Separator(inp, orient="horizontal").grid(row=8, column=0, columnspan=2, sticky="ew", pady=6)
        self._grid_label(inp, 9, "PRL/USDT fee %:")
        self._breakeven_fee1 = self._grid_entry(inp, 9, "")
        self._grid_label(inp, 10, "USDT/ARB fee %:")
        self._breakeven_fee2 = self._grid_entry(inp, 10, "")
        self._grid_label(inp, 11, "ARB/USD fee %:")
        self._breakeven_fee3 = self._grid_entry(inp, 11, "")
        self._grid_label(inp, 12, "ARB/USD flat $:")
        self._breakeven_fee_flat = self._grid_entry(inp, 12, "")
        self._grid_label(inp, 13, "Transfer ARB fee:")
        self._breakeven_xfer = self._grid_entry(inp, 13, "")

        ttk.Button(inp, text="Calculate", command=self._calc_breakeven).grid(row=14, column=0, columnspan=2, padx=6, pady=8, sticky="ew")

        res = ttk.LabelFrame(f, text="Results", padding=8)
        res.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.breakeven_result = tk.Text(res, wrap="word", width=60, height=30, font=("Consolas", 10))
        self.breakeven_result.pack(fill="both", expand=True)
        self.breakeven_result.insert("1.0", "Select mode and enter values, then click Calculate.")
        self.breakeven_result.config(state="disabled")

        self._on_breakeven_mode_change()

    def _on_breakeven_mode_change(self):
        """Enable/disable mode-specific input fields."""
        mode = self._breakeven_mode.get()
        # We can't easily disable grid entries, so we just update the result text
        self.breakeven_result.config(state="normal")
        self.breakeven_result.delete("1.0", "end")
        if mode == "price":
            self.breakeven_result.insert("1.0", "Mode: Enter coins mined + mining params → get breakeven PRL price.\nLeave fee fields blank for defaults.")
        else:
            self.breakeven_result.insert("1.0", "Mode: Enter PRL price + mining params → get breakeven coin count.\nLeave fee fields blank for defaults.")
        self.breakeven_result.config(state="disabled")

    def _calc_breakeven(self):
        from mining_toolkit_win import (
            compute_step_drag_sales,
            DEFAULT_PRL_USDT_FEE_PCT, DEFAULT_USDT_ARB_FEE_PCT,
            DEFAULT_ARB_USD_FEE_PCT, DEFAULT_ARB_USD_FLAT_FEE,
            DEFAULT_ARB_TRANSFER_FEE,
        )
        time_h   = self._float(self._breakeven_vars[0])
        power_w  = self._float(self._breakeven_vars[1])
        elec_kwh = self._float(self._breakeven_vars[2])
        arb_usd  = self._float(self._breakeven_vars[3])
        coins_in = self._float(self._breakeven_coins)
        price_in = self._float(self._breakeven_prl_price)

        elec_cost = (power_w * time_h / 1000.0) * elec_kwh

        # --- Drag from sales data ---
        drag1 = compute_step_drag_sales("PRL/USDT")
        drag2 = compute_step_drag_sales("USDT/ARB")
        drag3 = compute_step_drag_sales("ARB/USD")

        d1 = 1.0 - drag1 / 100.0
        d2 = 1.0 - drag2 / 100.0
        d3 = 1.0 - drag3 / 100.0

        # --- Fees ---
        fee1_pct = float(self._breakeven_fee1.get()) if self._breakeven_fee1.get().strip() else DEFAULT_PRL_USDT_FEE_PCT
        fee2_pct = float(self._breakeven_fee2.get()) if self._breakeven_fee2.get().strip() else DEFAULT_USDT_ARB_FEE_PCT
        fee3_pct = float(self._breakeven_fee3.get()) if self._breakeven_fee3.get().strip() else DEFAULT_ARB_USD_FEE_PCT
        fee3_flt = float(self._breakeven_fee_flat.get()) if self._breakeven_fee_flat.get().strip() else DEFAULT_ARB_USD_FLAT_FEE
        xfer_arb = float(self._breakeven_xfer.get()) if self._breakeven_xfer.get().strip() else DEFAULT_ARB_TRANSFER_FEE

        f1 = 1.0 - fee1_pct / 100.0
        f2 = 1.0 - fee2_pct / 100.0
        f3 = 1.0 - fee3_pct / 100.0

        if arb_usd <= 0:
            self._text_set(self.breakeven_result, "Error: ARB spot price must be > 0")
            return

        # --- Per-coin USD value after the full chain ---
        # 1 PRL -> prl_price USDT -> prl_price/arb_usd ARB -> (prl_price/arb_usd - xfer_per_coin) ARB -> USD
        # But xfer_arb is flat per batch, not per coin. So we compute batch-level.
        #
        # For N coins:
        #   usdt = N * prl_price * d1 * f1
        #   arb  = usdt / arb_usd * d2 * f2
        #   arb_after_xfer = arb - xfer_arb
        #   usd  = arb_after_xfer * arb_usd * d3 * f3 - fee3_flt
        #
        # Breakeven: usd = elec_cost
        #
        # Mode A (solve for price):
        #   elec = (N * price * d1*f1 / arb_usd * d2*f2 - xfer) * arb_usd * d3*f3 - fee3_flt
        #   => price = (elec + fee3_flt + xfer * arb_usd * d3*f3) / (N * d1*f1 * d2*f2 * d3*f3 * arb_usd / arb_usd)
        #   => price = (elec + fee3_flt + xfer * arb_usd * d3*f3) / (N * d1*f1 * d2*f2 * d3*f3)
        #   Wait, let me redo this more carefully:
        #   usdt = N * P * d1 * f1
        #   arb  = usdt / arb_usd * d2 * f2 = N * P * d1*f1*d2*f2 / arb_usd
        #   arb_after = arb - xfer
        #   usd = arb_after * arb_usd * d3 * f3 - fee3_flt
        #   at breakeven: usd = elec
        #   elec = (N * P * d1*f1*d2*f2 / arb_usd - xfer) * arb_usd * d3*f3 - fee3_flt
        #   elec + fee3_flt = (N * P * d1*f1*d2*f2 / arb_usd - xfer) * arb_usd * d3*f3
        #   (elec + fee3_flt) / (arb_usd * d3*f3) = N * P * d1*f1*d2*f2 / arb_usd - xfer
        #   (elec + fee3_flt) / (arb_usd * d3*f3) + xfer = N * P * d1*f1*d2*f2 / arb_usd
        #   P = ((elec + fee3_flt) / (arb_usd * d3*f3) + xfer) * arb_usd / (N * d1*f1*d2*f2)
        #   P = (elec + fee3_flt + xfer * arb_usd * d3*f3) / (N * d1*f1*d2*f2 * d3*f3 * arb_usd / arb_usd)
        #   P = (elec + fee3_flt + xfer * arb_usd * d3*f3) / (N * d1*f1*d2*f2 * d3*f3)
        #
        # Hmm, that's getting messy. Let me just use the backward approach:
        #   arb_after_xfer = (elec + fee3_flt) / (arb_usd * d3 * f3)
        #   arb_before_xfer = arb_after_xfer + xfer_arb
        #   usdt_needed = arb_before_xfer * arb_usd / (d2 * f2)
        #   P = usdt_needed / (N * d1 * f1)
        #
        # Mode B (solve for N):
        #   From above: usdt_needed = N * P * d1 * f1
        #   arb_before_xfer = usdt_needed / arb_usd * d2 * f2 = N * P * d1*f1*d2*f2 / arb_usd
        #   arb_after_xfer = arb_before_xfer - xfer
        #   elec = arb_after_xfer * arb_usd * d3*f3 - fee3_flt
        #   elec + fee3_flt = (N * P * d1*f1*d2*f2 / arb_usd - xfer) * arb_usd * d3*f3
        #   (elec + fee3_flt) / (arb_usd * d3*f3) = N * P * d1*f1*d2*f2 / arb_usd - xfer
        #   N = ((elec + fee3_flt) / (arb_usd * d3*f3) + xfer) * arb_usd / (P * d1*f1*d2*f2)

        # Common: ARB needed after transfer to cover elec + flat fee
        arb_after_xfer = (elec_cost + fee3_flt) / (arb_usd * d3 * f3)
        arb_before_xfer = arb_after_xfer + xfer_arb
        usdt_for_arb = arb_before_xfer * arb_usd / (d2 * f2)

        mode = self._breakeven_mode.get()

        if mode == "price":
            # Solve for breakeven PRL price
            if coins_in <= 0 or d1 * f1 <= 0:
                self._text_set(self.breakeven_result, "Error: Coins mined must be > 0")
                return
            be_result = usdt_for_arb / (coins_in * d1 * f1)
            result_label = "BREAK EVEN PRL PRICE"
            result_unit = "USDT/PRL"
            # Forward verify
            N = coins_in
            P = be_result
        else:
            # Solve for breakeven coin count
            if price_in <= 0 or d1 * f1 <= 0:
                self._text_set(self.breakeven_result, "Error: PRL price must be > 0")
                return
            be_result = usdt_for_arb / (price_in * d1 * f1)
            result_label = "BREAK EVEN COINS"
            result_unit = "PRL coins"
            # Forward verify
            N = be_result
            P = price_in

        # Forward verification (same for both modes)
        s1_usdt = N * P * d1 * f1
        s2_arb  = s1_usdt / arb_usd * d2 * f2
        s3_arb  = s2_arb - xfer_arb
        s4_usd  = s3_arb * arb_usd * d3 * f3 - fee3_flt
        net     = s4_usd - elec_cost

        def drag_note(val):
            return f"{val:.4f}%" if val > 0 else "0% (no data)"

        lines = [
            f"=== BREAKEVEN: PRL -> USDT -> ARB -> USD ===",
            f"  Mode: {'Price' if mode == 'price' else 'Coin count'}",
            "",
            "--- Inputs ---",
            f"  Mining time:    {time_h:.1f} hours",
            f"  Power:          {power_w:.0f} W",
            f"  Electricity:    ${elec_kwh:.4f}/kWh  →  ${elec_cost:.4f} total",
            f"  ARB spot:       ${arb_usd:.4f} USD",
        ]
        if mode == "price":
            lines.append(f"  Coins mined:    {coins_in:.2f} PRL")
        else:
            lines.append(f"  PRL price:      ${price_in:.4f} USDT")

        lines += [
            "",
            "--- Per-Step Drag (from sales data) ---",
            f"  PRL/USDT:  {drag_note(drag1)}",
            f"  USDT/ARB:  {drag_note(drag2)}",
            f"  ARB/USD:   {drag_note(drag3)}",
            "",
            "--- Fee Schedule ---",
            f"  PRL->USDT:  {fee1_pct:.2f}% in USDT  {'(override)' if self._breakeven_fee1.get().strip() else '(default)'}",
            f"  USDT->ARB:  {fee2_pct:.2f}% in ARB   {'(override)' if self._breakeven_fee2.get().strip() else '(default)'}",
            f"  Transfer:   {xfer_arb:.2f} ARB flat  {'(override)' if self._breakeven_xfer.get().strip() else '(default)'}",
            f"  ARB->USD:   ${fee3_flt:.2f} + {fee3_pct:.2f}% in USD  {'(override)' if self._breakeven_fee3.get().strip() or self._breakeven_fee_flat.get().strip() else '(default)'}",
            "",
            "=========================================",
            f"  {result_label}:  {be_result:.6f} {result_unit}",
            "=========================================",
            "",
            "--- Forward Verification ---",
            f"  {N:.4f} PRL x ${P:.6f} = ${N * P:.4f}",
            f"  Step 1: ${s1_usdt:.4f} USDT  (drag {drag1:.4f}%, fee {fee1_pct:.2f}%)",
            f"  Step 2: {s2_arb:.4f} ARB   (drag {drag2:.4f}%, fee {fee2_pct:.2f}%)",
            f"  Xfer:   {s3_arb:.4f} ARB   (sent {s2_arb:.4f} - {xfer_arb:.2f} fee)",
            f"  Step 3: ${s4_usd:.4f} USD   (drag {drag3:.4f}%, fee ${fee3_flt:.2f}+{fee3_pct:.2f}%)",
            f"  Profit: ${net:.6f} (should be ~0)",
        ]

        # --- Last daily mining entry comparison ---
        last_daily = get_last_daily_entry()
        if last_daily:
            last_coins = last_daily.coins_mined
            last_elec = (last_daily.power * last_daily.time_hours / 1000.0) * last_daily.elec_price
            # Compute breakeven for last day's actual mining params
            be_last, _, _, _ = compute_breakeven_price(
                arb_usd, last_elec, last_coins, d1, d2, d3, f1, f2, f3, fee3_flt, xfer_arb)
            # If in price mode, use the price the user entered; if coin mode, use last daily's implied price
            if mode == "price":
                ref_price = price_in
            else:
                ref_price = be_result  # the breakeven price we just computed
            price_above = ref_price - be_last
            price_above_pct = ((ref_price / be_last) - 1) * 100.0 if be_last > 0 and be_last != float('inf') else 0.0
            chain_eff = d1 * f1 * d2 * f2 * d3 * f3
            flat_per_coin_last = (xfer_arb * arb_usd + fee3_flt) / last_coins if last_coins > 0 else 0
            be_coins_last = last_elec / (ref_price * chain_eff - flat_per_coin_last) if (ref_price * chain_eff - flat_per_coin_last) > 0 else float('inf')
            tokens_above = last_coins - be_coins_last
            lines += [
                "",
                f"--- Last Daily Entry ({last_daily.date}) ---",
                f"  Mined:           {last_coins:.4f} PRL  |  Power: {last_daily.power:.0f}W  |  Hours: {last_daily.time_hours:.1f}",
                f"  Electricity:     ${last_elec:.4f}  (@ ${last_daily.elec_price}/kWh)",
                f"  BE price (that day): ${be_last:.6f} USDT/PRL",
                f"  Price above BE:  ${price_above:+.6f} ({price_above_pct:+.1f}%)",
                f"  BE coins (that day): {be_coins_last:.2f} PRL",
                f"  Tokens above BE: {tokens_above:+.2f} PRL",
            ]
        else:
            lines += [
                "",
                "--- Last Daily Entry ---",
                "  No daily mining entries yet.",
            ]

        self._text_set(self.breakeven_result, "\n".join(lines))

    # ==========================================================
    # TAB 3: DAILY MINING
    # ==========================================================
    def _build_daily_tab(self):
        f = self.tab_frames[2]
        paned = ttk.PanedWindow(f, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        left = ttk.LabelFrame(paned, text="Daily Mining Entry", padding=8)
        paned.add(left, weight=1)

        self._daily_edit_idx = -1

        self._grid_label(left, 0, "Coin:")
        self._daily_coin = self._grid_combo(left, 0, COINS, "PRL")
        self._grid_label(left, 1, "Date:")
        self._daily_date = self._grid_entry(left, 1, date.today().isoformat())
        self._daily_amt_lbl = self._grid_label(left, 2, "Coins mined:")
        self._daily_amt = self._grid_entry(left, 2, "")
        self._daily_price_lbl = self._grid_label(left, 3, "Spot price (USD):")
        self._daily_price = self._grid_entry(left, 3, "")
        self._grid_label(left, 4, "Power (watts):")
        self._daily_power = self._grid_entry(left, 4, "1600")
        self._grid_label(left, 5, "Electricity (USD/kWh):")
        self._daily_elec = self._grid_entry(left, 5, "0.15")
        self._grid_label(left, 6, "Mining time (hours):")
        self._daily_time = self._grid_entry(left, 6, "24")
        self._daily_coin.trace_add("write", lambda *a: self._update_daily_labels())
        ttk.Button(left, text="Add Entry", command=self._add_daily).grid(row=7, column=0, columnspan=2, padx=6, pady=8, sticky="ew")

        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self.daily_summary = ttk.Label(right, text="", font=("Consolas", 10))
        self.daily_summary.pack(fill="x", padx=4, pady=4)

        cols = ("date", "coin", "amount", "price", "gross", "electric", "net")
        self.daily_tree = ttk.Treeview(right, columns=cols, show="headings", height=18)
        for c, h, w in [("date","Date",110),("coin","Coin",60),("amount","Amount",90),
                        ("price","Price",80),("gross","Gross $",90),("electric","Electric $",90),("net","Net $",90)]:
            self.daily_tree.heading(c, text=h, command=lambda _c=c: self._sort_treeview(self.daily_tree, _c, False))
            self.daily_tree.column(c, width=w)
        self.daily_tree.pack(fill="both", expand=True, padx=4, pady=4)

        bf = ttk.Frame(right)
        bf.pack(fill="x", padx=4, pady=4)
        ttk.Button(bf, text="Edit Selected", command=self._edit_daily).pack(side="left", padx=4)
        ttk.Button(bf, text="Delete Selected", command=self._delete_daily).pack(side="left", padx=4)
        ttk.Button(bf, text="Refresh", command=self._refresh_daily).pack(side="left", padx=4)
        self._refresh_daily()

    def _update_daily_labels(self):
        c = self._daily_coin.get()
        self._daily_amt_lbl.config(text=f"{c} mined:")
        self._daily_price_lbl.config(text=f"{c} spot price (USD):")

    def _add_daily(self):
        amt = self._float(self._daily_amt)
        if amt <= 0:
            messagebox.showerror("Error", "Amount must be > 0")
            return
        entry = calc_daily_mining(self._daily_coin.get(), amt, self._float(self._daily_price),
                                  self._float(self._daily_power), self._float(self._daily_elec),
                                  self._float(self._daily_time))
        entry.date = self._daily_date.get() or date.today().isoformat()
        entries = load_daily()
        if 0 <= self._daily_edit_idx < len(entries):
            entries[self._daily_edit_idx] = entry
            self._daily_edit_idx = -1
        else:
            entries.append(entry)
        save_daily(entries)
        self._clear_daily_form()
        self._refresh_daily()
        self._refresh_banner()

    def _edit_daily(self):
        sel = self.daily_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Select an entry to edit.")
            return
        idx = self.daily_tree.index(sel[0])
        entries = load_daily()
        if 0 <= idx < len(entries):
            e = entries[idx]
            self._daily_edit_idx = idx
            self._daily_coin.set(e.coin or "PRL")
            self._daily_date.set(e.date)
            self._daily_amt.set(str(e.coins_mined))
            self._daily_price.set(str(e.price))
            self._daily_power.set(str(e.power))
            self._daily_elec.set(str(e.elec_price))
            self._daily_time.set(str(e.time_hours))

    def _clear_daily_form(self):
        self._daily_edit_idx = -1
        self._daily_coin.set("PRL")
        self._daily_date.set(date.today().isoformat())
        self._daily_amt.set("")
        self._daily_price.set("")
        self._daily_power.set("1600")
        self._daily_elec.set("0.15")
        self._daily_time.set("24")

    def _delete_daily(self):
        sel = self.daily_tree.selection()
        if not sel:
            return
        entries = load_daily()
        idx = self.daily_tree.index(sel[0])
        entries.pop(idx)
        save_daily(entries)
        if self._daily_edit_idx == idx:
            self._clear_daily_form()
        self._refresh_daily()
        self._refresh_banner()

    def _refresh_daily(self):
        entries = load_daily()
        self.daily_tree.delete(*self.daily_tree.get_children())
        total_net = 0
        coin_totals = {}
        for e in entries:
            self.daily_tree.insert("", "end", values=(
                e.date, e.coin, f"{e.coins_mined:.4f}", f"{e.price:.4f}",
                f"${e.gross_revenue:.4f}", f"${e.electricity_cost:.4f}", f"${e.net_profit:.4f}",
            ))
            total_net += e.net_profit
            coin_totals[e.coin] = coin_totals.get(e.coin, 0) + e.coins_mined
        avg = total_net / len(entries) if entries else 0
        coin_summary = "  |  ".join(f"{c}: {a:.2f}" for c, a in sorted(coin_totals.items()))
        self.daily_summary.config(
            text=f"  Days: {len(entries)}  |  {coin_summary}  |  Net: ${total_net:.4f}  |  Avg/day: ${avg:.4f}"
        )

    # ==========================================================
    # TAB 4: SALES - Trades (sells + purchases)
    # ==========================================================
    def _build_sales_tab(self):
        f = self.tab_frames[3]
        paned = ttk.PanedWindow(f, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        LEFT = ttk.LabelFrame(paned, text="Trade Entry", padding=8)
        paned.add(LEFT, weight=1)

        # Row 0: Coin pair
        pair_frame = ttk.Frame(LEFT)
        pair_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=6)
        ttk.Label(pair_frame, text="Base:", font=("Segoe UI", 10, "bold")).pack(side="left", padx=4)
        self._tr_base = tk.StringVar(value="PRL")
        ttk.Combobox(pair_frame, textvariable=self._tr_base, values=COINS, width=8, state="readonly").pack(side="left", padx=4)
        ttk.Label(pair_frame, text="/", font=("Segoe UI", 12)).pack(side="left", padx=4)
        self._tr_quote = tk.StringVar(value="USDT")
        ttk.Combobox(pair_frame, textvariable=self._tr_quote, values=COINS, width=8, state="readonly").pack(side="left", padx=4)

        # Row 1: Type + Side
        row1 = ttk.Frame(LEFT)
        row1.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=2)
        ttk.Label(row1, text="Type:").pack(side="left", padx=2)
        self._tr_type = tk.StringVar(value="Limit")
        ttk.Combobox(row1, textvariable=self._tr_type, values=ORDER_TYPES, width=7, state="readonly").pack(side="left", padx=2)
        ttk.Label(row1, text="Side:").pack(side="left", padx=(12, 2))
        self._tr_side = tk.StringVar(value="Sell")
        ttk.Combobox(row1, textvariable=self._tr_side, values=SIDES, width=6, state="readonly").pack(side="left", padx=2)

        # Row 2: Status + Exchange
        row2 = ttk.Frame(LEFT)
        row2.grid(row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=2)
        ttk.Label(row2, text="Status:").pack(side="left", padx=2)
        self._tr_status = tk.StringVar(value="Filled")
        ttk.Combobox(row2, textvariable=self._tr_status, values=STATUSES, width=8, state="readonly").pack(side="left", padx=2)
        ttk.Label(row2, text="Exchange:").pack(side="left", padx=(12, 2))
        self._tr_exchange = tk.StringVar(value="SafeTrade")
        ttk.Combobox(row2, textvariable=self._tr_exchange, values=EXCHANGES, width=9, state="readonly").pack(side="left", padx=2)

        # Row 3: Date
        self._grid_label(LEFT, 3, "Date:")
        self._tr_date = self._grid_entry(LEFT, 3, date.today().isoformat())

        # Row 4: Price (dynamic)
        self._tr_price_lbl = ttk.Label(LEFT, text="Price (per 1 PRL in USDT):")
        self._tr_price_lbl.grid(row=4, column=0, sticky="w", padx=6, pady=2)
        self._tr_price = self._grid_entry(LEFT, 4)

        # Row 5: Basis (expected coin amount)
        self._tr_basis_lbl = ttk.Label(LEFT, text="Basis (PRL):")
        self._tr_basis_lbl.grid(row=5, column=0, sticky="w", padx=6, pady=2)
        self._tr_basis = self._grid_entry(LEFT, 5, "")

        # Row 6: Amount traded (dynamic)
        self._tr_amt_lbl = ttk.Label(LEFT, text="PRL amount:")
        self._tr_amt_lbl.grid(row=6, column=0, sticky="w", padx=6, pady=2)
        self._tr_amt = self._grid_entry(LEFT, 6)

        # Row 7: Drag % (auto-calculated)
        self._tr_drag_lbl = ttk.Label(LEFT, text="Drag %: —", foreground="gray")
        self._tr_drag_lbl.grid(row=7, column=0, columnspan=2, sticky="w", padx=6, pady=1)

        # Row 8: Filled %
        self._grid_label(LEFT, 8, "Filled %:")
        self._tr_filled = self._grid_entry(LEFT, 8, "100")

        # Row 9: Total (dynamic)
        self._tr_total_lbl = ttk.Label(LEFT, text="Total (USDT):")
        self._tr_total_lbl.grid(row=9, column=0, sticky="w", padx=6, pady=2)
        self._tr_total = self._grid_entry(LEFT, 9)

        # Row 10: Fee coin
        self._grid_label(LEFT, 10, "Fee coin:")
        self._tr_fee_coin = tk.StringVar(value="USDT")
        ttk.Combobox(LEFT, textvariable=self._tr_fee_coin, values=COINS, width=8, state="readonly").grid(row=10, column=1, sticky="w", padx=6, pady=2)

        # Row 11: Fee amount
        self._tr_fee_amt_lbl = ttk.Label(LEFT, text="Fee amount (USDT):")
        self._tr_fee_amt_lbl.grid(row=11, column=0, sticky="w", padx=6, pady=2)
        self._tr_fee_amt = self._grid_entry(LEFT, 11, "0")

        # Row 12: Fee USD (auto-computed display)
        self._tr_fee_usd_lbl = ttk.Label(LEFT, text="Fee (USD): $0.0000", foreground="gray")
        self._tr_fee_usd_lbl.grid(row=12, column=0, columnspan=2, sticky="w", padx=6, pady=1)

        # Row 13: Traces
        self._tr_base.trace_add("write", lambda *a: self._update_trade_labels())
        self._tr_quote.trace_add("write", lambda *a: self._update_trade_labels())
        self._tr_side.trace_add("write", lambda *a: self._update_trade_labels())
        self._tr_basis.trace_add("write", lambda *a: self._update_drag_display())
        self._tr_amt.trace_add("write", lambda *a: self._update_drag_display())
        self._tr_fee_amt.trace_add("write", lambda *a: self._update_fee_usd_display())
        self._tr_fee_coin.trace_add("write", lambda *a: self._update_fee_usd_display())
        self._tr_total.trace_add("write", lambda *a: self._update_fee_usd_display())
        self._tr_price.trace_add("write", lambda *a: self._update_fee_usd_display())

        self._tr_status_lbl = ttk.Label(LEFT, text="Pair: PRL/USDT  |  Side: Sell  |  Fill in trade details.", foreground="gray")
        self._tr_status_lbl.grid(row=13, column=0, columnspan=2, sticky="w", padx=6, pady=3)

        bf = ttk.Frame(LEFT)
        bf.grid(row=14, column=0, columnspan=2, pady=4, sticky="ew")
        ttk.Button(bf, text="Save / Update", command=self._save_trade).pack(side="left", padx=4)
        ttk.Button(bf, text="Clear Form", command=self._clear_trade_form).pack(side="left", padx=4)

        # Right: list
        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self.sales_summary = ttk.Label(right, text="", font=("Consolas", 10), wraplength=550)
        self.sales_summary.pack(fill="x", padx=4, pady=4)

        cols = ("date","pair","side","type","status","exchange","price","basis","amount","drag","total","fee","net")
        self.sales_tree = ttk.Treeview(right, columns=cols, show="headings", height=14)
        for c, h, w in [("date","Date",120),("pair","Pair",70),("side","Side",45),("type","Type",55),
                        ("status","Status",65),("exchange","Exchange",75),("price","Price",75),
                        ("basis","Basis",70),("amount","Traded",70),("drag","Drag%",55),
                        ("total","Total",80),("fee","Fee",85),("net","Net $",80)]:
            self.sales_tree.heading(c, text=h, command=lambda _c=c: self._sort_treeview(self.sales_tree, _c, False))
            self.sales_tree.column(c, width=w)
        self.sales_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.sales_tree.tag_configure("canceled", background="#F8D7DA")
        self.sales_tree.tag_configure("partial", background="#FFF3CD")
        self.sales_tree.tag_configure("filled", background="#D4EDDA")
        self.sales_tree.tag_configure("buy", background="#D1ECF1")

        bf2 = ttk.Frame(right)
        bf2.pack(fill="x", padx=4, pady=4)
        ttk.Button(bf2, text="Edit Selected", command=self._edit_trade).pack(side="left", padx=4)
        ttk.Button(bf2, text="Delete Selected", command=self._delete_trade).pack(side="left", padx=4)
        ttk.Button(bf2, text="Refresh", command=self._refresh_sales).pack(side="left", padx=4)

        self._tr_edit_idx = -1
        self._update_trade_labels()
        self._refresh_sales()

    def _update_drag_display(self):
        basis = self._float(self._tr_basis)
        amt = self._float(self._tr_amt)
        if basis > 0 and amt > 0:
            drag = round((basis - amt) / basis * 100, 3)
            self._tr_drag_lbl.config(text=f"Drag: {basis:.4f} -> {amt:.4f} = {drag}%", foreground="red" if drag > 0 else "green")
        else:
            self._tr_drag_lbl.config(text="Drag %: —", foreground="gray")

    def _update_trade_labels(self):
        bc = self._tr_base.get() or "?"
        qc = self._tr_quote.get() or "?"
        side = self._tr_side.get()
        self._tr_price_lbl.config(text=f"Price (per 1 {bc} in {qc}):")
        self._tr_basis_lbl.config(text=f"Basis ({bc}):")
        self._tr_amt_lbl.config(text=f"{bc} amount:")
        self._tr_total_lbl.config(text=f"Total ({qc}):")
        self._tr_fee_amt_lbl.config(text=f"Fee amount ({qc}):")
        # Auto-set fee coin to quote coin
        self._tr_fee_coin.set(qc)
        self._tr_status_lbl.config(text=f"Pair: {bc}/{qc}  |  Side: {side}  |  Exchange: {self._tr_exchange.get()}")
        self._update_fee_usd_display()

    def _update_fee_usd_display(self):
        """Auto-compute fee USD from fee_amount, fee_coin, and available price data."""
        fee_amt = self._float(self._tr_fee_amt)
        fee_coin = self._tr_fee_coin.get().upper()
        price = self._float(self._tr_price)
        base = self._tr_base.get().upper()
        total = self._float(self._tr_total)
        amt = self._float(self._tr_amt)
        if fee_amt <= 0:
            self._tr_fee_usd_lbl.config(text="Fee (USD): $0.0000", foreground="gray")
            return
        if fee_coin in ("USDT", "USD", "EUR"):
            fee_usd = fee_amt
        elif base in ("USDT", "USD", "EUR") and price > 0:
            fee_usd = fee_amt / price
        elif total > 0 and amt > 0:
            fee_usd = fee_amt  # assume quote ≈ USD
        else:
            fee_usd = fee_amt  # fallback
        self._tr_fee_usd_lbl.config(text=f"Fee (USD): ${fee_usd:.4f}", foreground="darkorange" if fee_usd != fee_amt else "gray")

    def _save_trade(self):
        amt = self._float(self._tr_amt)
        if amt <= 0:
            messagebox.showerror("Error", "Amount must be > 0")
            return
        entry = TradeEntry(
            date=self._tr_date.get() or date.today().isoformat(),
            base_coin=self._tr_base.get(), quote_coin=self._tr_quote.get(),
            order_type=self._tr_type.get(), side=self._tr_side.get(),
            status=self._tr_status.get(), exchange=self._tr_exchange.get(),
            price=self._float(self._tr_price),
            basis=self._float(self._tr_basis),
            amount=amt,
            filled_pct=self._float(self._tr_filled),
            total=self._float(self._tr_total),
            fee_coin=self._tr_fee_coin.get(),
            fee_amount=self._float(self._tr_fee_amt),
        )
        calc_trade_net(entry)
        entries = load_sales()
        if 0 <= self._tr_edit_idx < len(entries):
            entries[self._tr_edit_idx] = entry
            self._tr_status_lbl.config(text="Trade updated.", foreground="green")
        else:
            entries.append(entry)
            self._tr_status_lbl.config(text=f"Trade saved: {entry.side} {entry.base_coin}/{entry.quote_coin}", foreground="blue")
        save_sales(entries)
        self._clear_trade_form()
        self._refresh_sales()
        self._refresh_banner()

    def _edit_trade(self):
        sel = self.sales_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Select a trade to edit.")
            return
        idx = self.sales_tree.index(sel[0])
        entries = load_sales()
        if 0 <= idx < len(entries):
            e = entries[idx]
            self._tr_edit_idx = idx
            self._tr_base.set(e.base_coin or "PRL")
            self._tr_quote.set(e.quote_coin or "USDT")
            self._tr_type.set(e.order_type or "Limit")
            self._tr_side.set(e.side or "Sell")
            self._tr_status.set(e.status or "Filled")
            self._tr_exchange.set(e.exchange or "SafeTrade")
            self._tr_date.set(e.date)
            self._tr_price.set(str(e.price))
            self._tr_basis.set(str(e.basis))
            self._tr_amt.set(str(e.amount))
            self._tr_filled.set(str(e.filled_pct))
            self._tr_total.set(str(e.total))
            # Handle both old format (fee_usd only) and new format (fee_coin + fee_amount)
            if hasattr(e, 'fee_coin') and e.fee_coin:
                self._tr_fee_coin.set(e.fee_coin)
                self._tr_fee_amt.set(str(e.fee_amount))
            else:
                # Old data: fee was stored as fee_usd, assume quote coin
                self._tr_fee_coin.set(e.quote_coin or "USDT")
                self._tr_fee_amt.set(str(e.fee_usd))
            self._update_trade_labels()
            self._update_drag_display()
            self._tr_status_lbl.config(text=f"Editing trade {idx+1}. Modify and Save.", foreground="orange")

    def _delete_trade(self):
        sel = self.sales_tree.selection()
        if not sel:
            return
        entries = load_sales()
        idx = self.sales_tree.index(sel[0])
        if 0 <= idx < len(entries):
            entries.pop(idx)
            save_sales(entries)
        self._clear_trade_form()
        self._refresh_sales()
        self._refresh_banner()

    def _clear_trade_form(self):
        self._tr_base.set("PRL"); self._tr_quote.set("USDT")
        self._tr_type.set("Limit"); self._tr_side.set("Sell")
        self._tr_status.set("Filled"); self._tr_exchange.set("SafeTrade")
        self._tr_date.set(date.today().isoformat())
        for v in [self._tr_price, self._tr_basis, self._tr_amt, self._tr_total, self._tr_fee_amt]:
            v.set("")
        self._tr_fee_coin.set("USDT")
        self._tr_filled.set("100")
        self._tr_edit_idx = -1
        self._update_trade_labels()
        self._update_drag_display()
        self._tr_status_lbl.config(text="Pair: PRL/USDT  |  Side: Sell  |  Fill in trade details.", foreground="gray")

    def _refresh_sales(self):
        entries = load_sales()
        self.sales_tree.delete(*self.sales_tree.get_children())
        total_net = total_fee = total_drag = 0
        pair_stats = {}
        for e in entries:
            tag = "canceled" if e.status == "Canceled" else ("partial" if e.status == "Partial" else ("buy" if e.side == "Buy" else "filled"))
            pair = f"{e.base_coin}/{e.quote_coin}"
            drag_str = f"{e.drag_pct:.3f}%" if e.basis > 0 else "—"
            fee_coin = getattr(e, 'fee_coin', None) or e.quote_coin or "USDT"
            fee_amt = getattr(e, 'fee_amount', None) if hasattr(e, 'fee_amount') else None
            if fee_amt is not None and fee_amt > 0:
                fee_str = f"{fee_amt:.4f} {fee_coin}"
            elif e.fee_usd > 0:
                fee_str = f"${e.fee_usd:.4f}"
            else:
                fee_str = "$0.00"
            self.sales_tree.insert("", "end", values=(
                e.date, pair, e.side, e.order_type, e.status, e.exchange,
                f"{e.price:.6f}", f"{e.basis:.4f}", f"{e.amount:.4f}", drag_str,
                f"{e.total:.4f}", fee_str, f"${e.net_usd:.4f}",
            ), tags=(tag,))
            total_net += e.net_usd
            total_fee += e.fee_usd
            if e.basis > 0 and e.amount > 0:
                total_drag += (e.basis - e.amount)
            pair_stats[pair] = pair_stats.get(pair, 0) + e.amount
        pair_summary = "  ".join(f"{p}: {a:.2f}" for p, a in sorted(pair_stats.items()))
        self.sales_summary.config(
            text=f"  Trades: {len(entries)}  |  {pair_summary}  |  "
                 f"Net USD: ${total_net:.4f}  |  Fees: ${total_fee:.4f}  |  Drag coins: {total_drag:.4f}"
        )

    # ==========================================================
    # TAB 5: TRANSFERS - Exchange to exchange
    # ==========================================================
    def _build_transfers_tab(self):
        f = self.tab_frames[4]
        paned = ttk.PanedWindow(f, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        LEFT = ttk.LabelFrame(paned, text="Transfer Entry", padding=8)
        paned.add(LEFT, weight=1)

        # Row 0: Coin selector
        self._grid_label(LEFT, 0, "Coin:")
        self._xfer_coin = self._grid_combo(LEFT, 0, COINS, "ARB")

        # Row 1: From exchange
        self._grid_label(LEFT, 1, "From exchange:")
        self._xfer_from = self._grid_combo(LEFT, 1, EXCHANGES, "SafeTrade")

        # Row 2: To exchange
        self._grid_label(LEFT, 2, "To exchange:")
        self._xfer_to = self._grid_combo(LEFT, 2, EXCHANGES, "Coinbase")

        # Row 3: Date
        self._grid_label(LEFT, 3, "Date:")
        self._xfer_date = self._grid_entry(LEFT, 3, date.today().isoformat())

        # Row 4: Amount (dynamic label)
        self._xfer_amt_lbl = ttk.Label(LEFT, text="ARB amount:")
        self._xfer_amt_lbl.grid(row=4, column=0, sticky="w", padx=6, pady=3)
        self._xfer_amt = self._grid_entry(LEFT, 4, "")

        # Row 5: Fee coin
        self._grid_label(LEFT, 5, "Fee coin:")
        self._xfer_fee_coin = self._grid_combo(LEFT, 5, COINS, "")

        # Row 6: Fee amount
        self._grid_label(LEFT, 6, "Fee amount:")
        self._xfer_fee_amt = self._grid_entry(LEFT, 6, "")

        # Row 7: Fee USD value
        self._grid_label(LEFT, 7, "Fee value (USD):")
        self._xfer_fee_usd = self._grid_entry(LEFT, 7, "")

        # Row 8: Status
        self._grid_label(LEFT, 8, "Status:")
        self._xfer_status = self._grid_combo(LEFT, 8, ["Completed", "Pending", "Failed"], "Completed")

        # Row 9: Notes
        self._grid_label(LEFT, 9, "Notes:")
        self._xfer_notes = self._grid_entry(LEFT, 9, "")

        # Trace coin -> update labels
        self._xfer_coin.trace_add("write", lambda *a: self._update_xfer_labels())

        self._xfer_status_lbl = ttk.Label(LEFT, text="Transfer ARB from SafeTrade to Coinbase. Flat $3 ETH network fee.", foreground="gray")
        self._xfer_status_lbl.grid(row=10, column=0, columnspan=2, sticky="w", padx=6, pady=4)

        bf = ttk.Frame(LEFT)
        bf.grid(row=11, column=0, columnspan=2, pady=6, sticky="ew")
        ttk.Button(bf, text="Save / Update", command=self._save_transfer).pack(side="left", padx=4)
        ttk.Button(bf, text="Clear Form", command=self._clear_transfer_form).pack(side="left", padx=4)

        # Right: list
        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self.xfer_summary = ttk.Label(right, text="", font=("Consolas", 10), wraplength=550)
        self.xfer_summary.pack(fill="x", padx=4, pady=4)

        cols = ("date", "coin", "from", "to", "amount", "fee", "fee_usd", "received", "status", "notes")
        self.xfer_tree = ttk.Treeview(right, columns=cols, show="headings", height=14)
        for c, h, w in [("date","Date",130),("coin","Coin",60),("from","From",90),("to","To",90),
                        ("amount","Amount",90),("fee","Fee",80),("fee_usd","Fee $",70),
                        ("received","Received",90),("status","Status",80),("notes","Notes",120)]:
            self.xfer_tree.heading(c, text=h, command=lambda _c=c: self._sort_treeview(self.xfer_tree, _c, False))
            self.xfer_tree.column(c, width=w)
        self.xfer_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.xfer_tree.tag_configure("completed", background="#D4EDDA")
        self.xfer_tree.tag_configure("pending", background="#FFF3CD")
        self.xfer_tree.tag_configure("failed", background="#F8D7DA")

        bf2 = ttk.Frame(right)
        bf2.pack(fill="x", padx=4, pady=4)
        ttk.Button(bf2, text="Edit Selected", command=self._edit_transfer).pack(side="left", padx=4)
        ttk.Button(bf2, text="Delete Selected", command=self._delete_transfer).pack(side="left", padx=4)
        ttk.Button(bf2, text="Refresh", command=self._refresh_transfers).pack(side="left", padx=4)

        self._xfer_edit_idx = -1
        self._update_xfer_labels()
        self._refresh_transfers()

    def _update_xfer_labels(self):
        c = self._xfer_coin.get() or "?"
        self._xfer_amt_lbl.config(text=f"{c} amount:")
        self._xfer_status_lbl.config(text=f"Transfer {c} from {self._xfer_from.get()} to {self._xfer_to.get()}.")

    def _save_transfer(self):
        amt = self._float(self._xfer_amt)
        if amt <= 0:
            messagebox.showerror("Error", "Amount must be > 0")
            return
        entry = TransferEntry(
            date=self._xfer_date.get() or date.today().isoformat(),
            coin=self._xfer_coin.get(),
            from_exchange=self._xfer_from.get(),
            to_exchange=self._xfer_to.get(),
            amount=amt,
            fee_coin=self._xfer_fee_coin.get(),
            fee_amount=self._float(self._xfer_fee_amt),
            fee_usd=self._float(self._xfer_fee_usd),
            status=self._xfer_status.get(),
            notes=self._xfer_notes.get(),
        )
        calc_transfer_net(entry)
        entries = load_transfers()
        if 0 <= self._xfer_edit_idx < len(entries):
            entries[self._xfer_edit_idx] = entry
            self._xfer_edit_idx = -1
            self._xfer_status_lbl.config(text="Transfer updated.", foreground="green")
        else:
            entries.append(entry)
            self._xfer_status_lbl.config(text=f"Transfer saved: {amt:.4f} {entry.coin} {entry.from_exchange}->{entry.to_exchange}", foreground="blue")
        save_transfers(entries)
        self._refresh_transfers()
        self._refresh_banner()

    def _edit_transfer(self):
        sel = self.xfer_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Select a transfer to edit.")
            return
        idx = self.xfer_tree.index(sel[0])
        entries = load_transfers()
        if 0 <= idx < len(entries):
            e = entries[idx]
            self._xfer_edit_idx = idx
            self._xfer_coin.set(e.coin or "ARB")
            self._xfer_from.set(e.from_exchange or "SafeTrade")
            self._xfer_to.set(e.to_exchange or "Coinbase")
            self._xfer_date.set(e.date)
            self._xfer_amt.set(str(e.amount))
            self._xfer_fee_coin.set(e.fee_coin or "")
            self._xfer_fee_amt.set(str(e.fee_amount))
            self._xfer_fee_usd.set(str(e.fee_usd))
            self._xfer_status.set(e.status or "Completed")
            self._xfer_notes.set(e.notes or "")
            self._update_xfer_labels()
            self._xfer_status_lbl.config(text=f"Editing transfer {idx+1}. Modify and Save.", foreground="orange")

    def _delete_transfer(self):
        sel = self.xfer_tree.selection()
        if not sel:
            return
        idx = self.xfer_tree.index(sel[0])
        entries = load_transfers()
        if 0 <= idx < len(entries):
            entries.pop(idx)
            save_transfers(entries)
            if self._xfer_edit_idx == idx:
                self._xfer_edit_idx = -1
                self._clear_transfer_form()
            self._refresh_transfers()
            self._refresh_banner()

    def _clear_transfer_form(self):
        self._xfer_coin.set("ARB"); self._xfer_from.set("SafeTrade")
        self._xfer_to.set("Coinbase"); self._xfer_date.set(date.today().isoformat())
        self._xfer_amt.set(""); self._xfer_fee_coin.set("")
        self._xfer_fee_amt.set(""); self._xfer_fee_usd.set("")
        self._xfer_status.set("Completed"); self._xfer_notes.set("")
        self._xfer_edit_idx = -1
        self._update_xfer_labels()
        self._xfer_status_lbl.config(text="Transfer ARB from SafeTrade to Coinbase. Flat $3 ETH network fee.", foreground="gray")

    def _refresh_transfers(self):
        entries = load_transfers()
        self.xfer_tree.delete(*self.xfer_tree.get_children())
        total_amount = total_fee = 0
        coin_totals = {}
        for e in entries:
            tag = "completed" if e.status == "Completed" else ("pending" if e.status == "Pending" else "failed")
            self.xfer_tree.insert("", "end", values=(
                e.date, e.coin, e.from_exchange, e.to_exchange,
                f"{e.amount:.4f}", f"{e.fee_amount:.4f} {e.fee_coin}",
                f"${e.fee_usd:.4f}", f"{e.received:.4f}", e.status, e.notes,
            ), tags=(tag,))
            total_amount += e.amount
            total_fee += e.fee_usd
            coin_totals[e.coin] = coin_totals.get(e.coin, 0) + e.amount
        coin_summary = "  ".join(f"{c}: {a:.2f}" for c, a in sorted(coin_totals.items()))
        self.xfer_summary.config(
            text=f"  Transfers: {len(entries)}  |  {coin_summary}  |  Total fees: ${total_fee:.4f}"
        )

    # ==========================================================
    # TAB 6: DRAG ANALYSIS
    # ==========================================================
    def _build_drag_tab(self):
        f = self.tab_frames[5]
        inp = ttk.LabelFrame(f, text="Actual Results (enter what you observed)", padding=8)
        inp.pack(side="left", fill="y", padx=6, pady=6)

        # Step 1 pair
        pair1 = ttk.Frame(inp)
        pair1.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        ttk.Label(pair1, text="Step 1:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=2)
        self._drag_from_coin = tk.StringVar(value="PRL")
        ttk.Combobox(pair1, textvariable=self._drag_from_coin, values=COINS, width=7, state="readonly").pack(side="left", padx=2)
        ttk.Label(pair1, text="->").pack(side="left", padx=4)
        self._drag_mid_coin = tk.StringVar(value="USDT")
        ttk.Combobox(pair1, textvariable=self._drag_mid_coin, values=COINS, width=7, state="readonly").pack(side="left", padx=2)

        # Step 2 pair
        pair2 = ttk.Frame(inp)
        pair2.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        ttk.Label(pair2, text="Step 2:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=2)
        ttk.Label(pair2, textvariable=self._drag_mid_coin).pack(side="left", padx=2)
        ttk.Label(pair2, text="->").pack(side="left", padx=4)
        self._drag_to_coin = tk.StringVar(value="ARB")
        ttk.Combobox(pair2, textvariable=self._drag_to_coin, values=COINS, width=7, state="readonly").pack(side="left", padx=2)

        # Step 3
        pair3 = ttk.Frame(inp)
        pair3.grid(row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        ttk.Label(pair3, text="Step 3:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=2)
        ttk.Label(pair3, textvariable=self._drag_to_coin).pack(side="left", padx=2)
        ttk.Label(pair3, text="->").pack(side="left", padx=4)
        ttk.Label(pair3, text="USD").pack(side="left", padx=2)

        self._drag_labels = {}
        lbl = ttk.Label(inp, text="PRL amount sent:")
        lbl.grid(row=3, column=0, sticky="w", padx=6, pady=3)
        self._drag_labels["from_amt"] = lbl
        self._drag_from_amt = self._grid_entry(inp, 3)

        lbl = ttk.Label(inp, text="PRL price (USD):")
        lbl.grid(row=4, column=0, sticky="w", padx=6, pady=3)
        self._drag_labels["from_price"] = lbl
        self._drag_from_price = self._grid_entry(inp, 4)

        lbl = ttk.Label(inp, text="USDT received:")
        lbl.grid(row=5, column=0, sticky="w", padx=6, pady=3)
        self._drag_labels["mid_amt"] = lbl
        self._drag_mid_amt = self._grid_entry(inp, 5)

        lbl = ttk.Label(inp, text="USDT price (USD):")
        lbl.grid(row=6, column=0, sticky="w", padx=6, pady=3)
        self._drag_labels["mid_price"] = lbl
        self._drag_mid_price = self._grid_entry(inp, 6, "1.0")

        lbl = ttk.Label(inp, text="ARB received:")
        lbl.grid(row=7, column=0, sticky="w", padx=6, pady=3)
        self._drag_labels["to_amt"] = lbl
        self._drag_to_amt = self._grid_entry(inp, 7)

        lbl = ttk.Label(inp, text="ARB price (USD):")
        lbl.grid(row=8, column=0, sticky="w", padx=6, pady=3)
        self._drag_labels["to_price"] = lbl
        self._drag_to_price = self._grid_entry(inp, 8)

        lbl = ttk.Label(inp, text="Final USD received:")
        lbl.grid(row=9, column=0, sticky="w", padx=6, pady=3)
        self._drag_labels["usd"] = lbl
        self._drag_usd = self._grid_entry(inp, 9)

        self._drag_from_coin.trace_add("write", lambda *a: self._update_drag_labels())
        self._drag_mid_coin.trace_add("write", lambda *a: self._update_drag_labels())
        self._drag_to_coin.trace_add("write", lambda *a: self._update_drag_labels())

        ttk.Button(inp, text="Analyze Drag", command=self._calc_drag).grid(row=10, column=0, columnspan=2, padx=6, pady=8, sticky="ew")

        res = ttk.LabelFrame(f, text="Per-Step Drag Analysis", padding=8)
        res.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.drag_result = tk.Text(res, wrap="word", width=55, height=30, font=("Consolas", 10))
        self.drag_result.pack(fill="both", expand=True)
        self.drag_result.insert("1.0", "Select coin pairs, enter observed amounts, and click Analyze.")
        self.drag_result.config(state="disabled")
        self._update_drag_labels()

    def _update_drag_labels(self):
        fc = self._drag_from_coin.get() or "?"
        mc = self._drag_mid_coin.get() or "?"
        tc = self._drag_to_coin.get() or "?"
        self._drag_labels["from_amt"].config(text=f"{fc} amount sent:")
        self._drag_labels["from_price"].config(text=f"{fc} price (USD):")
        self._drag_labels["mid_amt"].config(text=f"{mc} received:")
        self._drag_labels["mid_price"].config(text=f"{mc} price (USD):")
        self._drag_labels["to_amt"].config(text=f"{tc} received:")
        self._drag_labels["to_price"].config(text=f"{tc} price (USD):")
        if mc in ("USDT", "USD", "EUR"):
            self._drag_mid_price.set("1.0")
        if tc == "USD":
            self._drag_to_price.set("1.0")

    def _calc_drag(self):
        fa = self._float(self._drag_from_amt)
        fp = self._float(self._drag_from_price)
        ma = self._float(self._drag_mid_amt)
        mp = self._float(self._drag_mid_price)
        ta = self._float(self._drag_to_amt)
        tp = self._float(self._drag_to_price)
        usd = self._float(self._drag_usd)
        d = analyze_drag(fa, fp, ma, mp, ta, tp, usd)
        fc = self._drag_from_coin.get()
        mc = self._drag_mid_coin.get()
        tc = self._drag_to_coin.get()
        exp_mid = fa * fp
        exp_to = ma * mp if mp > 0 else 0
        exp_usd = ta * tp if tp > 0 else 0
        lines = [
            f"=== Step 1: {fc} -> {mc} ===",
            f"  Expected:  {exp_mid:.4f} {mc}  ({fa:.4f} x ${fp:.4f})",
            f"  Received:  {ma:.4f} {mc}",
            f"  Drag:      {d.step1_drag_pct:.3f}%  ({exp_mid - ma:.4f} {mc})",
            "", f"=== Step 2: {mc} -> {tc} ===",
            f"  Expected:  {exp_to:.4f} {tc}  ({ma:.4f} x ${mp:.4f})",
            f"  Received:  {ta:.4f} {tc}",
            f"  Drag:      {d.step2_drag_pct:.3f}%  ({exp_to - ta:.4f} {tc})",
            "", f"=== Step 3: {tc} -> USD ===",
            f"  Expected:  ${exp_usd:.4f} USD  ({ta:.4f} x ${tp:.4f})",
            f"  Received:  ${usd:.4f} USD",
            f"  Drag:      {d.step3_drag_pct:.3f}%  (${exp_usd - usd:.4f})",
            "", "==============================",
            f"  TOTAL DRAG:  {d.total_drag_pct:.3f}%",
            f"  Effective:   ${d.effective_rate:.6f} USD per {fc}",
            f"  Raw price:   ${fp:.6f} USD",
        ]
        self._text_set(self.drag_result, "\n".join(lines))

    # ==========================================================
    # TAB 7: DATA — Import / Export / Summary
    # ==========================================================
    def _build_data_tab(self):
        f = self.tab_frames[6]

        # Left panel: summary + actions
        left = ttk.LabelFrame(f, text="Data Management", padding=8)
        left.pack(side="left", fill="y", padx=6, pady=6)

        # Summary
        self._data_summary = ttk.Label(left, text="", font=("Consolas", 10), justify="left")
        self._data_summary.grid(row=0, column=0, columnspan=2, sticky="w", padx=6, pady=6)
        self._refresh_data_summary()

        # Separator
        ttk.Separator(left, orient="horizontal").grid(row=1, column=0, columnspan=2, sticky="ew", pady=8)

        # Export section
        ttk.Label(left, text="Export all data to a single JSON file:", font=("Segoe UI", 9, "bold")).grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        ttk.Button(left, text="Export Data...", command=self._export_data).grid(row=3, column=0, padx=6, pady=4, sticky="ew")

        # Separator
        ttk.Separator(left, orient="horizontal").grid(row=4, column=0, columnspan=2, sticky="ew", pady=8)

        # Import section
        ttk.Label(left, text="Import data from file:", font=("Segoe UI", 9, "bold")).grid(row=5, column=0, columnspan=2, sticky="w", padx=6, pady=2)

        self._import_mode = tk.StringVar(value="merge")
        ttk.Radiobutton(left, text="Merge (skip duplicates)", variable=self._import_mode, value="merge").grid(row=6, column=0, columnspan=2, sticky="w", padx=6, pady=1)
        ttk.Radiobutton(left, text="Replace all data", variable=self._import_mode, value="replace").grid(row=7, column=0, columnspan=2, sticky="w", padx=6, pady=1)
        ttk.Button(left, text="Import Data...", command=self._import_data).grid(row=8, column=0, padx=6, pady=4, sticky="ew")

        # Separator
        ttk.Separator(left, orient="horizontal").grid(row=9, column=0, columnspan=2, sticky="ew", pady=8)

        # Data directory info
        ttk.Label(left, text=f"Data directory:", font=("Segoe UI", 9, "bold")).grid(row=10, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        self._data_dir_lbl = ttk.Label(left, text=str(DATA_DIR), foreground="gray")
        self._data_dir_lbl.grid(row=11, column=0, columnspan=2, sticky="w", padx=6, pady=2)

        # Right panel: log / recent activity
        right = ttk.LabelFrame(f, text="Recent Sales", padding=8)
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)

        self._data_log = tk.Text(right, wrap="word", width=55, height=25, font=("Consolas", 9))
        self._data_log.pack(fill="both", expand=True)
        self._data_log.insert("1.0", "Use Export to back up all data.\nUse Import to restore or merge from a backup file.\n\nMerge mode: imports only records with dates/values not already present.\nReplace mode: clears all existing data before importing.")
        self._data_log.config(state="disabled")

    def _refresh_data_summary(self):
        daily = load_daily()
        sales = load_sales()
        transfers = load_transfers()
        text = (
            f"  Daily entries:    {len(daily)}\n"
            f"  Sales entries:    {len(sales)}\n"
            f"  Transfer entries: {len(transfers)}\n"
            f"  Total records:    {len(daily) + len(sales) + len(transfers)}"
        )
        self._data_summary.config(text=text)

    def _export_data(self):
        import tkinter.filedialog as fd
        path = fd.asksaveasfilename(
            title="Export Data",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile=f"mining_toolkit_backup_{date.today().isoformat()}.json"
        )
        if not path:
            return
        backup = {
            "version": 1,
            "exported_at": date.today().isoformat(),
            "daily_log": [asdict(e) for e in load_daily()],
            "sales_log": [asdict(e) for e in load_sales()],
            "transfers_log": [asdict(e) for e in load_transfers()],
        }
        Path(path).write_text(json.dumps(backup, indent=2))
        messagebox.showinfo("Export Complete", f"Exported {len(backup['daily_log'])} daily, {len(backup['sales_log'])} sales, {len(backup['transfers_log'])} transfer records to:\n{path}")

    def _import_data(self):
        import tkinter.filedialog as fd
        path = fd.askopenfilename(
            title="Import Data",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text())
        except Exception as e:
            messagebox.showerror("Import Error", f"Could not read file:\n{e}")
            return

        # Validate structure
        if not isinstance(raw, dict):
            # Maybe it's an old single-array format
            messagebox.showerror("Import Error", "Unrecognized file format. Expected a JSON object with daily_log, sales_log, and transfers_log keys.")
            return

        daily_in = raw.get("daily_log", [])
        sales_in = raw.get("sales_log", [])
        transfers_in = raw.get("transfers_log", [])

        if not any([daily_in, sales_in, transfers_in]):
            messagebox.showerror("Import Error", "No data found in file. Expected daily_log, sales_log, or transfers_log arrays.")
            return

        mode = self._import_mode.get()
        if mode == "replace":
            if not messagebox.askyesno("Confirm Replace", "This will DELETE all existing data and replace it with the imported data.\n\nAre you sure?"):
                return
            save_daily([DailyMiningEntry(**e) for e in daily_in])
            save_sales([TradeEntry(**e) for e in sales_in])
            save_transfers([TransferEntry(**e) for e in transfers_in])
            msg = f"Replaced all data with {len(daily_in)} daily, {len(sales_in)} sales, {len(transfers_in)} transfer records."
        else:
            # Merge: skip records that already exist (match by all fields)
            existing_daily = load_daily()
            existing_sales = load_sales()
            existing_transfers = load_transfers()

            def make_key_daily(e):
                return (e.date, e.coin, round(e.coins_mined, 8))
            def make_key_sales(e):
                return (e.date, e.base_coin, e.quote_coin, e.side, round(e.amount, 8), round(e.total, 4))
            def make_key_xfer(e):
                return (e.date, e.coin, e.from_exchange, e.to_exchange, round(e.amount, 8))

            existing_d_keys = {make_key_daily(e) for e in existing_daily}
            existing_s_keys = {make_key_sales(e) for e in existing_sales}
            existing_x_keys = {make_key_xfer(e) for e in existing_transfers}

            new_daily = existing_daily[:]
            new_sales = existing_sales[:]
            new_transfers = existing_transfers[:]
            d_added = s_added = x_added = 0

            for e in daily_in:
                entry = DailyMiningEntry(**e)
                k = make_key_daily(entry)
                if k not in existing_d_keys:
                    new_daily.append(entry)
                    existing_d_keys.add(k)
                    d_added += 1

            for e in sales_in:
                entry = TradeEntry(**e)
                k = make_key_sales(entry)
                if k not in existing_s_keys:
                    new_sales.append(entry)
                    existing_s_keys.add(k)
                    s_added += 1

            for e in transfers_in:
                entry = TransferEntry(**e)
                k = make_key_xfer(entry)
                if k not in existing_x_keys:
                    new_transfers.append(entry)
                    existing_x_keys.add(k)
                    x_added += 1

            save_daily(new_daily)
            save_sales(new_sales)
            save_transfers(new_transfers)
            msg = f"Merge complete:\n  Daily: {d_added} new (skipped {len(daily_in) - d_added})\n  Sales: {s_added} new (skipped {len(sales_in) - s_added})\n  Transfers: {x_added} new (skipped {len(transfers_in) - x_added})"

        # Refresh all tabs
        self._refresh_data_summary()
        self._refresh_sales()
        self._refresh_banner()
        try:
            self._refresh_daily()
        except Exception:
            pass

        # Log to data tab
        self._data_log.config(state="normal")
        self._data_log.delete("1.0", "end")
        self._data_log.insert("1.0", msg)
        self._data_log.config(state="disabled")

        messagebox.showinfo("Import Complete", msg)


def main():
    root = tk.Tk()
    app = PRLMiningApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
