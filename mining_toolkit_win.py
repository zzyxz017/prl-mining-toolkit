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
from dataclasses import dataclass, asdict, fields
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

    # Transfers: net effect is deducting the fee (amount sent - received = fee)
    for e in load_transfers():
        coin = e.coin
        holdings[coin] -= e.fee_amount

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
        coin = e.coin
        from_exch[coin] -= e.amount
        # Bank is a sink — don't track as a holding, just deduct from sender
        if e.to_exchange and e.to_exchange != 'Bank':
            to_exch = get_exch(e.to_exchange)
            to_exch[coin] += e.received

    return exchanges


def compute_bank_flows():
    """Compute total USD value sent to Bank and total electricity costs.

    Returns a dict:
      { 'total_usd_to_bank': float, 'total_electricity_usd': float, 'net_profit_usd': float }
    """
    total_usd_to_bank = 0.0
    for e in load_transfers():
        if e.to_exchange == 'Bank':
            if e.coin == 'USD':
                total_usd_to_bank += e.received
            elif e.coin == 'USDT':
                total_usd_to_bank += e.received
            elif e.coin == 'ARB':
                total_usd_to_bank += e.received * 0.114

    total_electricity_usd = sum(e.electricity_cost for e in load_daily())

    return {
        'total_usd_to_bank': total_usd_to_bank,
        'total_electricity_usd': total_electricity_usd,
        'net_profit_usd': total_usd_to_bank - total_electricity_usd,
    }


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
            return f"Last: Sold {last_sale.amount} PRL for {last_sale.total} USDT on {last_sale.date} — Next: Convert USDT to USD"
        elif last_sale.side == 'Buy' and last_sale.base_coin == 'ARB':
            return f"Last: Bought {last_sale.amount} ARB for {last_sale.total} USDT on {last_sale.date} — Next: Transfer ARB to Coinbase"
        elif last_sale.side == 'Sell' and last_sale.base_coin == 'USDT' and last_sale.quote_coin == 'USD':
            return f"Last: Converted {last_sale.amount} USDT to {last_sale.total} USD on {last_sale.date} — Next: Transfer USD to Bank"
        elif last_sale.side == 'Sell' and last_sale.base_coin == 'ARB':
            return f"Last: Sold {last_sale.amount} ARB for {last_sale.total} USD on {last_sale.date} — Next: Transfer USD to Bank"
        else:
            return f"Last sale: {last_sale.side} {last_sale.base_coin}/{last_sale.quote_coin} on {last_sale.date}"
    else:
        # Most recent event is a transfer
        if last_transfer.to_exchange == 'Bank':
            return f"Last: Transferred {last_transfer.received} {last_transfer.coin} to Bank on {last_transfer.date} — Pipeline complete!"
        return f"Last: Transferred {last_transfer.received} {last_transfer.coin} from {last_transfer.from_exchange} to {last_transfer.to_exchange} on {last_transfer.date} — Next: Convert USDT to USD"

# Valid trade pairs in the PRL -> USDT -> USD pipeline
TRADE_PAIRS = [
    ("PRL", "USDT"),   # Sell PRL for USDT
    ("ARB", "USDT"),   # Buy ARB with USDT
    ("USDT", "USD"),   # Convert USDT to USD (with drag/spread)
]
# Build display strings
TRADE_PAIR_LABELS = [f"{b}/{q}" for b, q in TRADE_PAIRS]

COINS = ["PRL", "BTC", "ETH", "USDT", "ARB", "USD", "EUR", "LTC", "XMR", "OTHER"]
ORDER_TYPES = ["Limit", "Market"]
SIDES = ["Buy", "Sell"]
STATUSES = ["Filled", "Canceled", "Pending", "Partial"]
EXCHANGES = ["SafeTrade", "Coinbase", "Bank", "Other"]


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
    amount: float = 0         # net amount received (after fee)
    total: float = 0          # total in quote coin (gross for buys, net for sells)
    fee_coin: str = ""        # coin in which the fee is charged (= received coin)
    fee_amount: float = 0     # fee amount in fee_coin


@dataclass
class TransferEntry:
    date: str = ""
    coin: str = ""
    from_exchange: str = ""
    to_exchange: str = ""
    amount: float = 0         # amount sent (gross)
    fee_coin: str = ""        # coin in which the fee is charged
    fee_amount: float = 0     # fee amount in fee_coin
    received: float = 0       # amount received (net of fee)
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
    """Compute fee and net for a trade entry.

    Convention: pair is BASE/QUOTE, price is in quote per base.
    For Sell PRL/USDT: base=PRL, quote=USDT, price=USDT/PRL, total=USDT recv, amount=PRL sold
    For Buy ARB/USDT:  base=ARB, quote=USDT, price=USDT/ARB, total=USDT spent, amount=ARB recv

    Fee is always deducted from the received coin (= fee_coin):
      Sell: received = quote_coin, fee = (amount * price) - total
      Buy:  received = base_coin,  fee = (total / price) - amount
    """
    if entry.side == "Sell":
        recv_coin = entry.quote_coin
        gross_recv = entry.amount * entry.price
        fee = gross_recv - entry.total
        if fee < 0:
            fee = 0
    else:
        recv_coin = entry.base_coin
        gross_recv = entry.total / entry.price
        fee = gross_recv - entry.amount
        if fee < 0:
            fee = 0

    entry.fee_coin = recv_coin
    entry.fee_amount = fee
    return entry


def calc_transfer_net(entry):
    entry.received = round(entry.amount - entry.fee_amount, 2)
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
    raw = load_json(SALES_LOG)
    valid = {f.name for f in fields(TradeEntry)}
    return [TradeEntry(**{k: v for k, v in d.items() if k in valid}) for d in raw]

def save_sales(entries):
    save_json(SALES_LOG, [asdict(e) for e in entries])

def load_transfers():
    raw = load_json(TRANSFERS_LOG)
    valid = {f.name for f in fields(TransferEntry)}
    return [TransferEntry(**{k: v for k, v in d.items() if k in valid}) for d in raw]

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
            # price = quote per base (e.g., USDT/ARB)
            # expected_base = total_quote / price
            if e.total > 0 and e.price > 0:
                expected = e.total / e.price
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
        self.root.minsize(720, 520)

        # ── Modern theme ──────────────────────────────────────────
        style = ttk.Style()

        # Pick the best available native theme
        available = style.theme_names()
        # Prefer clam for full color control on dark theme
        if "clam" in available:
            style.theme_use("clam")
        else:
            for preferred in ("vista", "xpnative", "winnative"):
                if preferred in available:
                    style.theme_use(preferred)
                    break

        # ── Color palette (very dark grey + red accents) ──────────
        C = {
            "bg":           "#111111",   # near-black background
            "surface":      "#1A1A1A",   # card / frame bg
            "surface2":     "#222222",   # elevated surface
            "border":       "#333333",   # subtle borders
            "text":         "#E0E0E0",   # primary text (light grey)
            "text_alt":     "#888888",   # secondary / muted text
            "accent":       "#DC2626",   # red accent
            "accent_hover": "#EF4444",
            "success":       "#34D399",  # emerald green (kept for stats)
            "danger":        "#F87171",  # soft red (kept for stats)
            "warning":       "#FBBF24",  # amber (kept for stats)
            "info":          "#60A5FA",  # sky blue (kept for stats)
            "row_alt":       "#161616",  # alternating row bg
            "select_bg":     "#7F1D1D",  # dark red selection
            "select_fg":     "#FCA5A5",
        }

        # ── Global defaults ───────────────────────────────────────
        style.configure(".",
            font=("Segoe UI", 10),
            background=C["bg"],
            foreground=C["text"],
            bordercolor=C["border"],
            troughcolor=C["border"],
            selectbackground=C["select_bg"],
            selectforeground=C["select_fg"],
            focuscolor=C["accent"],
        )

        # ── Frames ────────────────────────────────────────────────
        style.configure("TLabel",       font=("Segoe UI", 11), background=C["bg"], foreground=C["text"])
        style.configure("TFrame",       background=C["bg"])
        style.configure("TLabelframe",  background=C["surface"], foreground=C["text"],
                                         borderwidth=1, relief="solid")
        style.configure("TLabelframe.Label",
                                         font=("Segoe UI", 11, "bold"),
                                         foreground=C["accent"],
                                         background=C["surface"])
        # Fix clam theme's labelframe interior (field area)
        style.configure("TLabelframe.Field", background=C["surface"])

        # ── Buttons ───────────────────────────────────────────────
        # Custom dark button style with visible red text
        style.configure("TButton",
            font=("Segoe UI", 11, "bold"),
            padding=(14, 7),
            background="#2A2A2A",
            foreground="#FF4444",
            borderwidth=1,
        )
        style.map("TButton",
            background=[("active", "#333333"), ("pressed", "#1A1A1A")],
            foreground=[("active", "#FF6666"), ("pressed", "#CC0000")],
        )

        # ── Entry / Combobox ─────────────────────────────────────
        style.configure("TEntry",
            font=("Segoe UI", 11),
            padding=(8, 5),
            fieldbackground=C["surface"],
            bordercolor=C["border"],
            lightcolor=C["surface"],
            darkcolor=C["border"],
        )
        style.configure("TCombobox",
            font=("Segoe UI", 11),
            padding=(8, 5),
            fieldbackground=C["surface"],
            background=C["surface"],
            bordercolor=C["border"],
        )
        style.map("TCombobox",
            fieldbackground=[("readonly", C["surface"])],
            selectbackground=[("readonly", C["select_bg"])],
        )

        # ── Treeview ──────────────────────────────────────────────
        style.configure("Treeview",
            font=("Consolas", 11),
            rowheight=28,
            background=C["surface"],
            foreground=C["text"],
            fieldbackground=C["surface"],
            borderwidth=0,
        )
        style.configure("Treeview.Heading",
            font=("Segoe UI", 10, "bold"),
            padding=(6, 4),
            background="#1E1E1E",
            foreground="#CCCCCC",
            borderwidth=0,
        )
        style.map("Treeview",
            background=[("selected", C["select_bg"])],
            foreground=[("selected", C["select_fg"])],
        )

        # ── Notebook (tabs) ───────────────────────────────────────
        style.configure("TNotebook",
            background=C["bg"],
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure("TNotebook.Tab",
            font=("Segoe UI", 11, "bold"),
            padding=(16, 10),
            foreground="#888888",
            background="#1A1A1A",
            borderwidth=0,
        )
        style.map("TNotebook.Tab",
            foreground=[("selected", "#FFFFFF"), ("active", "#CCCCCC")],
            background=[("selected", "#DC2626"), ("active", "#2A2A2A")],
            expand=[("selected", [1, 1, 1, 0])],
        )
        # Remove the default dashed focus ring on tabs
        style.layout("TNotebook.Tab", [
            ("Notebook.tab", {"sticky": "nswe", "children": [
                ("Notebook.padding", {"sticky": "nswe", "children": [
                    ("Notebook.label", {"sticky": "nswe"})
                ]})
            ]})
        ])

        # ── Separator ─────────────────────────────────────────────
        style.configure("TSeparator", background=C["border"])

        # ── PanedWindow ───────────────────────────────────────────
        style.configure("TPanedWindow", background=C["bg"])
        style.configure("Sash", sashthickness=4, background=C["border"])

        # ── Scrollbar ─────────────────────────────────────────────
        style.configure("Vertical.TScrollbar",
            troughcolor=C["bg"],
            background="#444444",
            borderwidth=0,
            arrowsize=12,
        )
        style.map("Vertical.TScrollbar",
            background=[("active", "#555555")],
        )
        style.configure("Horizontal.TScrollbar",
            troughcolor=C["bg"],
            background="#444444",
            borderwidth=0,
            arrowsize=12,
        )

        # Store colours for use elsewhere
        self._c = C

        # ==========================================================
        # BANNER — Holdings + Breakeven
        # ==========================================================
        self.banner_frame = ttk.Frame(root, padding=6)
        self.banner_frame.pack(fill="x", padx=4, pady=(4, 0))

        # === ROW 1: Per-exchange holdings ===
        row1 = ttk.Frame(self.banner_frame)
        row1.pack(fill="x", pady=(0, 4))
        row1.grid_columnconfigure(0, weight=1)
        row1.grid_columnconfigure(1, weight=1)
        row1.grid_columnconfigure(2, weight=1)

        # SafeTrade
        st_inner = ttk.LabelFrame(row1, text="  SafeTrade  ", padding=6)
        st_inner.grid(row=0, column=0, sticky="nsew", padx=(0, 3))

        self.banner_st = {}
        for i, coin in enumerate(["PRL", "USDT", "ARB"]):
            ttk.Label(st_inner, text=coin + ":", font=("Segoe UI", 10, "bold")).grid(row=0, column=i * 2, padx=(2, 1), pady=2)
            lbl = ttk.Label(st_inner, text="—", font=("Consolas", 12, "bold"), foreground=C["success"], anchor="e")
            lbl.grid(row=0, column=i * 2 + 1, padx=(0, 4), pady=2)
            self.banner_st[coin] = lbl
        for j in range(6):
            st_inner.grid_columnconfigure(j, weight=1)

        # Coinbase
        cb_inner = ttk.LabelFrame(row1, text="  Coinbase  ", padding=6)
        cb_inner.grid(row=0, column=1, sticky="nsew", padx=(0, 3))

        self.banner_cb = {}
        for i, coin in enumerate(["USDT", "ARB", "USD"]):
            ttk.Label(cb_inner, text=coin + ":", font=("Segoe UI", 10, "bold")).grid(row=0, column=i * 2, padx=(2, 1), pady=2)
            lbl = ttk.Label(cb_inner, text="—", font=("Consolas", 12, "bold"), foreground=C["info"], anchor="e")
            lbl.grid(row=0, column=i * 2 + 1, padx=(0, 4), pady=2)
            self.banner_cb[coin] = lbl
        for j in range(6):
            cb_inner.grid_columnconfigure(j, weight=1)

        # Total USD value
        usd_inner = ttk.LabelFrame(row1, text="  Total USD Value  ", padding=6)
        usd_inner.grid(row=0, column=2, sticky="nsew")

        self.banner_usd_val = ttk.Label(usd_inner, text="—", font=("Consolas", 13, "bold"), foreground=C["text"], anchor="center")
        self.banner_usd_val.pack(fill="x", padx=2, pady=2)

        # === ROW 2: Breakeven + Pipeline ===
        row2 = ttk.Frame(self.banner_frame)
        row2.pack(fill="x")
        row2.grid_columnconfigure(0, weight=1)

        # Breakeven section — live from last daily entry
        be_inner = ttk.LabelFrame(row2, text="  Breakeven (last day)  ", padding=4)
        be_inner.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        self.banner_be_labels = {}
        be_fields = [
            ("be_price", "BE Price:", "$—"),
            ("be_coins", "BE Coins:", "—"),
            ("tokens_above", "Above BE:", "—"),
            ("margin", "Margin:", "—"),
        ]
        for i, (key, lbl_text, dflt) in enumerate(be_fields):
            ttk.Label(be_inner, text=lbl_text, font=("Segoe UI", 10, "bold")).grid(row=0, column=i * 2, padx=(2, 1), pady=2)
            lbl = ttk.Label(be_inner, text=dflt, font=("Consolas", 12), anchor="e")
            lbl.grid(row=0, column=i * 2 + 1, padx=(0, 4), pady=2)
            self.banner_be_labels[key] = lbl
        for j in range(8):
            be_inner.grid_columnconfigure(j, weight=1)

        # Refresh button
        ttk.Button(self.banner_frame, text="↻ Refresh", command=self._refresh_banner).pack(side="right", padx=4)

        # ==========================================================
        # NOTEBOOK
        # ==========================================================
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=4, pady=4)

        self.tab_frames = []
        for name in ["Profitability", "Breakeven", "Daily Mining", "Sales", "Transfers", "Drag Analysis", "Trends", "Data"]:
            f = ttk.Frame(self.notebook)
            self.notebook.add(f, text=f"  {name}  ")
            self.tab_frames.append(f)

        self._build_profit_tab()
        self._build_breakeven_tab()
        self._build_daily_tab()
        self._build_sales_tab()
        self._build_transfers_tab()
        self._build_drag_tab()
        self._build_trends_tab()
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
        """Update the banner with per-exchange holdings, bank flows, and breakeven."""
        from mining_toolkit_win import (
            compute_exchange_holdings,
            get_last_daily_entry, compute_breakeven_price,
            DEFAULT_PRL_USDT_FEE_PCT, DEFAULT_USDT_ARB_FEE_PCT,
            DEFAULT_ARB_USD_FEE_PCT, DEFAULT_ARB_USD_FLAT_FEE,
            DEFAULT_ARB_TRANSFER_FEE, compute_step_drag_sales,
            load_transfers, load_daily,
        )

        # Per-exchange holdings
        ex = compute_exchange_holdings()

        # SafeTrade
        st = ex.get('SafeTrade', {})
        for coin, lbl in self.banner_st.items():
            val = st.get(coin, 0.0)
            lbl.config(text=f"{val:,.4f}")

        # Coinbase
        cb = ex.get('Coinbase', {})
        for coin, lbl in self.banner_cb.items():
            val = cb.get(coin, 0.0)
            lbl.config(text=f"{val:,.4f}")

        # Bank flows and net profit (inline to avoid PyInstaller import issues)
        total_usd_to_bank = 0.0
        for e in load_transfers():
            if e.to_exchange == 'Bank':
                if e.coin in ('USD', 'USDT'):
                    total_usd_to_bank += e.received
                elif e.coin == 'ARB':
                    total_usd_to_bank += round(e.received * 0.114, 2)
        total_usd_to_bank = round(total_usd_to_bank, 2)
        total_electricity_usd = sum(e.electricity_cost for e in load_daily())
        net = round(total_usd_to_bank - total_electricity_usd, 2)

        net_color = self._c["success"] if net >= 0 else self._c["danger"]
        self.banner_usd_val.config(
            text=f"Bank: ${total_usd_to_bank:,.2f}  Net: ${net:,.2f}",
            foreground=net_color, font=("Consolas", 13, "bold"),
        )

        # --- Breakeven from last daily mining entry ---
        last = get_last_daily_entry()
        if last:
            elec_cost = (last.power * last.time_hours / 1000.0) * last.elec_price
            coins_mined = last.coins_mined

            drag1 = compute_step_drag_sales("PRL/USDT")
            d1 = 1.0 - drag1 / 100.0
            f1 = 1.0 - DEFAULT_PRL_USDT_FEE_PCT / 100.0

            # Try full chain with default ARB price; fall back to PRL->USDT only
            arb_usd = 0.30  # default estimate
            drag2 = compute_step_drag_sales("USDT/ARB")
            drag3 = compute_step_drag_sales("ARB/USD")
            d2 = 1.0 - drag2 / 100.0
            d3 = 1.0 - drag3 / 100.0
            f2 = 1.0 - DEFAULT_USDT_ARB_FEE_PCT / 100.0
            f3 = 1.0 - DEFAULT_ARB_USD_FEE_PCT / 100.0

            be_price, _, _, _ = compute_breakeven_price(
                arb_usd, elec_cost, coins_mined,
                d1, d2, d3, f1, f2, f3,
                DEFAULT_ARB_USD_FLAT_FEE, DEFAULT_ARB_TRANSFER_FEE)

            if be_price == float('inf'):
                # ARB chain unavailable — breakeven on PRL->USDT only
                be_price = elec_cost / (coins_mined * d1 * f1) if (coins_mined * d1 * f1) > 0 else float('inf')
                be_coins = elec_cost / (last.price * d1 * f1) if (last.price * d1 * f1) > 0 else float('inf')
                tokens_above = coins_mined - be_coins
                effective_usd = last.price * d1 * f1
                net_per_coin = effective_usd - (elec_cost / coins_mined) if coins_mined > 0 else 0
            else:
                chain_eff = d1 * f1 * d2 * f2 * d3 * f3
                flat_per_coin = (DEFAULT_ARB_TRANSFER_FEE * arb_usd + DEFAULT_ARB_USD_FLAT_FEE) / coins_mined if coins_mined > 0 else 0
                be_coins = elec_cost / (last.price * chain_eff - flat_per_coin) if (last.price * chain_eff - flat_per_coin) > 0 else float('inf')
                tokens_above = coins_mined - be_coins
                effective_usd = last.price * chain_eff - flat_per_coin
                net_per_coin = effective_usd - (elec_cost / coins_mined) if coins_mined > 0 else 0

            total_net = net_per_coin * coins_mined
            margin_pct = (total_net / elec_cost * 100.0) if elec_cost > 0 else 0.0

            # Update labels with color coding
            c = self._c
            self.banner_be_labels["be_price"].config(
                text=f"${be_price:.4f}" if be_price != float('inf') else "$—",
                foreground=c["info"], font=("Consolas", 12, "bold"))
            self.banner_be_labels["be_coins"].config(
                text=f"{be_coins:.1f} PRL" if be_coins != float('inf') else "— PRL",
                foreground=c["info"], font=("Consolas", 12, "bold"))

            above_color = c["success"] if tokens_above >= 0 else c["danger"]
            self.banner_be_labels["tokens_above"].config(
                text=f"{tokens_above:+.1f} PRL" if tokens_above != float('inf') else "—",
                foreground=above_color, font=("Consolas", 12, "bold"))

            margin_color = c["success"] if margin_pct >= 0 else c["danger"]
            self.banner_be_labels["margin"].config(
                text=f"{margin_pct:+.1f}%", foreground=margin_color,
                font=("Consolas", 12, "bold"))
        else:
            for key in self.banner_be_labels:
                self.banner_be_labels[key].config(text="—", foreground=self._c["text_alt"])

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

    def _init_text_tags(self, widget):
        """Configure bold+color tags for a tk.Text result widget (dark grey + red)."""
        widget.configure(bg="#1A1A1A", fg="#E0E0E0", insertbackground="#E0E0E0",
                         selectbackground="#7F1D1D", selectforeground="#FCA5A5",
                         font=("Consolas", 12))
        widget.tag_configure("bold_green", font=("Consolas", 12, "bold"), foreground="#34D399")
        widget.tag_configure("bold_red", font=("Consolas", 12, "bold"), foreground="#F87171")
        widget.tag_configure("bold_blue", font=("Consolas", 12, "bold"), foreground="#60A5FA")
        widget.tag_configure("bold_orange", font=("Consolas", 12, "bold"), foreground="#FBBF24")
        widget.tag_configure("bold_purple", font=("Consolas", 12, "bold"), foreground="#A78BFA")
        widget.tag_configure("bold_dark", font=("Consolas", 12, "bold"), foreground="#E0E0E0")
        widget.tag_configure("section", font=("Consolas", 12, "bold"), foreground="#888888")
        widget.tag_configure("header", font=("Consolas", 13, "bold"), foreground="#EF4444")
        widget.tag_configure("result_box", font=("Consolas", 13, "bold"), foreground="#E0E0E0")

    def _write_tagged(self, widget, lines, tag_rules):
        """Write lines to a text widget with tags applied.

        tag_rules: list of (pattern, tag_name) — if pattern matches a line,
        the value portion (after ":") gets the tag.
        """
        widget.config(state="normal")
        widget.delete("1.0", "end")
        for i, line in enumerate(lines):
            # Check if this line matches any tag rule
            matched = False
            for pattern, tag in tag_rules:
                if pattern in line and ":" in line:
                    # Split at first ": " to separate label from value
                    parts = line.split(": ", 1)
                    if len(parts) == 2:
                        label = parts[0] + ": "
                        value = parts[1]
                        widget.insert("end", label)
                        widget.insert("end", value + "\n", tag)
                        matched = True
                        break
            if not matched:
                widget.insert("end", line + "\n")
        widget.config(state="disabled")
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
        ]
        for i, (lbl, dflt) in enumerate(defaults):
            self._grid_label(inp, i, lbl)
            self._profit_vars.append(self._grid_entry(inp, i, dflt))

        ttk.Button(inp, text="Calculate", command=self._calc_profit).grid(row=5, column=0, columnspan=2, padx=6, pady=8, sticky="ew")

        res = ttk.LabelFrame(f, text="Results", padding=8)
        res.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.profit_result = tk.Text(res, wrap="word", width=60, height=30, font=("Consolas", 12))
        self.profit_result.pack(fill="both", expand=True)
        self._init_text_tags(self.profit_result)
        self.profit_result.insert("1.0", "Enter values and click Calculate.")
        self.profit_result.config(state="disabled")

    def _calc_profit(self):
        from mining_toolkit_win import (
            compute_step_drag_sales,
            DEFAULT_PRL_USDT_FEE_PCT,
        )
        coins_mined = self._float(self._profit_vars[0])
        time_h      = self._float(self._profit_vars[1])
        power_w     = self._float(self._profit_vars[2])
        elec_kwh    = self._float(self._profit_vars[3])
        prl_price   = self._float(self._profit_vars[4])

        elec_cost = (power_w * time_h / 1000.0) * elec_kwh

        drag1 = compute_step_drag_sales("PRL/USDT")
        d1 = 1.0 - drag1 / 100.0

        fee1_pct = DEFAULT_PRL_USDT_FEE_PCT
        f1 = 1.0 - fee1_pct / 100.0

        # Forward calculation — PRL -> USDT only
        gross_usdt  = coins_mined * prl_price
        step1_usdt  = gross_usdt * d1 * f1

        net_profit      = step1_usdt - elec_cost
        profit_per_coin = net_profit / coins_mined if coins_mined > 0 else 0

        # --- Profitability vs electricity (margin on costs) ---
        profit_pct = (net_profit / elec_cost * 100.0) if elec_cost > 0 else 0.0

        # --- Breakeven comparison ---
        be_price = elec_cost / (coins_mined * d1 * f1) if (coins_mined * d1 * f1) > 0 else float('inf')
        price_above_be = prl_price - be_price
        price_above_be_pct = ((prl_price / be_price) - 1) * 100.0 if be_price > 0 and be_price != float('inf') else 0.0
        be_coins_at_this_price = elec_cost / (prl_price * d1 * f1) if (prl_price * d1 * f1) > 0 else float('inf')
        tokens_above_be = coins_mined - be_coins_at_this_price

        def drag_note(val):
            return f"{val:.4f}%" if val > 0 else "0% (no data)"

        profit_color = " PROFITABLE + " if net_profit > 0 else " UNPROFITABLE "

        lines = [
            "=== CONVERSION: PRL -> USDT ===",
            "",
            f"  Mine:     {coins_mined:.2f} PRL  @ ${prl_price:.4f} USDT/PRL",
            "",
            "--- Step 1: PRL -> USDT (SafeTrade) ---",
            f"  Gross:   ${gross_usdt:.4f} USDT  ({coins_mined:.2f} x ${prl_price:.4f})",
            f"  Drag:    {drag_note(drag1)}",
            f"  Fee:     {fee1_pct:.2f}% in USDT",
            f"  Net:     ${step1_usdt:.4f} USDT",
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
            f" Breakeven price:     ${be_price:.6f} USDT/PRL" if be_price != float('inf') else " Breakeven price:     —",
            f" Price above BE:      ${price_above_be:+.6f} ({price_above_be_pct:+.1f}%)",
            f" BE coins (this day): {be_coins_at_this_price:.2f} PRL" if be_coins_at_this_price != float('inf') else " BE coins (this day): —",
            f" Tokens above BE:     {tokens_above_be:+.2f} PRL" if tokens_above_be != float('inf') else " Tokens above BE:     —",
            f"",
            f"  >>> {profit_color} <<<",
        ]
        self._write_tagged_profit_colored(lines)

    def _write_tagged_profit_colored(self, lines):
        """Write profit results with per-line color+bold detection."""
        w = self.profit_result
        w.config(state="normal")
        w.delete("1.0", "end")
        for line in lines:
            # Section headers
            if line.startswith("===") and "===" in line[3:]:
                w.insert("end", line + "\n", "header")
            # Bold section dividers
            elif line.startswith("---") and "---" in line[3:]:
                w.insert("end", line + "\n", "section")
            # Net profit / profit indicators - color by content
            elif "PROFITABLE" in line:
                w.insert("end", line + "\n", "bold_green")
            elif "UNPROFITABLE" in line:
                w.insert("end", line + "\n", "bold_red")
            elif "Net profit:" in line:
                # Extract value to determine color
                val_str = line.split(":")[-1].strip().replace("$", "").replace(",", "")
                try:
                    val = float(val_str)
                    tag = "bold_green" if val >= 0 else "bold_red"
                except:
                    tag = "bold_green"
                w.insert("end", line + "\n", tag)
            elif "Profit/coin:" in line:
                val_str = line.split(":")[-1].strip().replace("$", "").replace(",", "")
                try:
                    val = float(val_str)
                    tag = "bold_green" if val >= 0 else "bold_red"
                except:
                    tag = "bold_green"
                w.insert("end", line + "\n", tag)
            elif "Margin on costs:" in line:
                val_str = line.split(":")[-1].strip().replace("%", "").replace("+", "")
                try:
                    val = float(val_str)
                    tag = "bold_green" if val >= 0 else "bold_red"
                except:
                    tag = "bold_green"
                w.insert("end", line + "\n", tag)
            elif "Tokens above BE:" in line and "—" not in line:
                val_str = line.split(":")[-1].strip().replace("PRL", "").replace("+", "")
                try:
                    val = float(val_str)
                    tag = "bold_green" if val >= 0 else "bold_red"
                except:
                    tag = "bold_dark"
                w.insert("end", line + "\n", tag)
            elif "Price above BE:" in line:
                # Get the $ value
                val_part = line.split(":")[-1].strip()
                val_str = val_part.split("(")[0].replace("$", "").replace("+", "").strip()
                try:
                    val = float(val_str)
                    tag = "bold_green" if val >= 0 else "bold_red"
                except:
                    tag = "bold_blue"
                w.insert("end", line + "\n", tag)
            elif "Breakeven price:" in line and "—" not in line:
                w.insert("end", line + "\n", "bold_blue")
            elif "BE coins" in line and "—" not in line:
                w.insert("end", line + "\n", "bold_blue")
            elif "Gross:" in line:
                w.insert("end", line + "\n", "bold_dark")
            elif line.strip().startswith("Net:") and "$" in line:
                w.insert("end", line + "\n", "bold_green")
            elif line.strip().startswith("Drag:"):
                w.insert("end", line + "\n", "bold_orange")
            elif line.strip().startswith("Fee:"):
                w.insert("end", line + "\n", "bold_red")
            elif "Electricity:" in line or "Total costs:" in line:
                w.insert("end", line + "\n", "bold_red")
            else:
                w.insert("end", line + "\n")
        w.config(state="disabled")
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
            ("Power (watts):", "1700"),
            ("Electricity (USD/kWh):", "0.15"),
            ("ARB spot (USD, 0 or blank to skip):", ""),
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
        self.breakeven_result = tk.Text(res, wrap="word", width=60, height=30, font=("Consolas", 12))
        self.breakeven_result.pack(fill="both", expand=True)
        self._init_text_tags(self.breakeven_result)
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

        skip_arb = arb_usd <= 0

        if not skip_arb:
            # --- Full chain: PRL -> USDT -> ARB -> USD ---
            # Backward approach: work from USD needed back to PRL price
            #   arb_after_xfer = (elec + fee3_flt) / (arb_usd * d3 * f3)
            #   arb_before_xfer = arb_after_xfer + xfer_arb
            #   usdt_for_arb = arb_before_xfer * arb_usd / (d2 * f2)
            arb_after_xfer = (elec_cost + fee3_flt) / (arb_usd * d3 * f3)
            arb_before_xfer = arb_after_xfer + xfer_arb
            usdt_for_arb = arb_before_xfer * arb_usd / (d2 * f2)
        else:
            # --- PRL -> USDT only (no ARB conversion) ---
            # Breakeven: elec_cost = coins * price * d1 * f1
            # No ARB/USD fees or transfer fees apply
            usdt_for_arb = elec_cost
            arb_after_xfer = 0
            arb_before_xfer = 0
            fee3_flt = 0
            fee3_pct = 0
            xfer_arb = 0

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

        # Forward verification
        s1_usdt = N * P * d1 * f1
        if not skip_arb:
            s2_arb  = s1_usdt / arb_usd * d2 * f2
            s3_arb  = s2_arb - xfer_arb
            s4_usd  = s3_arb * arb_usd * d3 * f3 - fee3_flt
        else:
            s2_arb  = 0
            s3_arb  = 0
            s4_usd  = s1_usdt  # USDT is the final currency
        net = s4_usd - elec_cost

        def drag_note(val):
            return f"{val:.4f}%" if val > 0 else "0% (no data)"

        chain_label = "PRL -> USDT -> ARB -> USD" if not skip_arb else "PRL -> USDT (ARB step skipped)"
        lines = [
            f"=== BREAKEVEN: {chain_label} ===",
            f"  Mode: {'Price' if mode == 'price' else 'Coin count'}",
            "",
            "--- Inputs ---",
            f"  Mining time:    {time_h:.1f} hours",
            f"  Power:          {power_w:.0f} W",
            f"  Electricity:    ${elec_kwh:.4f}/kWh  →  ${elec_cost:.4f} total",
        ]
        if not skip_arb:
            lines.append(f"  ARB spot:       ${arb_usd:.4f} USD")
        else:
            lines.append("  ARB spot:       (skipped)")
        if mode == "price":
            lines.append(f"  Coins mined:    {coins_in:.2f} PRL")
        else:
            lines.append(f"  PRL price:      ${price_in:.4f} USDT")

        lines += [
            "",
            "--- Per-Step Drag (from sales data) ---",
            f"  PRL/USDT:  {drag_note(drag1)}",
        ]
        if not skip_arb:
            lines += [
                f"  USDT/ARB:  {drag_note(drag2)}",
                f"  ARB/USD:   {drag_note(drag3)}",
            ]
        lines += [
            "",
            "--- Fee Schedule ---",
            f"  PRL->USDT:  {fee1_pct:.2f}% in USDT  {'(override)' if self._breakeven_fee1.get().strip() else '(default)'}",
        ]
        if not skip_arb:
            lines += [
                f"  USDT->ARB:  {fee2_pct:.2f}% in ARB   {'(override)' if self._breakeven_fee2.get().strip() else '(default)'}",
                f"  Transfer:   {xfer_arb:.2f} ARB flat  {'(override)' if self._breakeven_xfer.get().strip() else '(default)'}",
                f"  ARB->USD:   ${fee3_flt:.2f} + {fee3_pct:.2f}% in USD  {'(override)' if self._breakeven_fee3.get().strip() or self._breakeven_fee_flat.get().strip() else '(default)'}",
            ]
        lines += [
            "",
            "=========================================",
            f"  {result_label}:  {be_result:.6f} {result_unit}",
            "=========================================",
            "",
            "--- Forward Verification ---",
            f"  {N:.4f} PRL x ${P:.6f} = ${N * P:.4f}",
            f"  Step 1: ${s1_usdt:.4f} USDT  (drag {drag1:.4f}%, fee {fee1_pct:.2f}%)",
        ]
        if not skip_arb:
            lines += [
                f"  Step 2: {s2_arb:.4f} ARB   (drag {drag2:.4f}%, fee {fee2_pct:.2f}%)",
                f"  Xfer:   {s3_arb:.4f} ARB   (sent {s2_arb:.4f} - {xfer_arb:.2f} fee)",
                f"  Step 3: ${s4_usd:.4f} USD   (drag {drag3:.4f}%, fee ${fee3_flt:.2f}+{fee3_pct:.2f}%)",
            ]
        lines.append(f"  Profit: ${net:.6f} (should be ~0)")

        # --- Last daily mining entry comparison ---
        last_daily = get_last_daily_entry()
        if last_daily and not skip_arb:
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

        self._write_tagged_breakeven_colored(lines)

    def _write_tagged_breakeven_colored(self, lines):
        """Write breakeven results with per-line color+bold detection."""
        w = self.breakeven_result
        w.config(state="normal")
        w.delete("1.0", "end")
        for line in lines:
            if line.startswith("===") and "===" in line[3:]:
                w.insert("end", line + "\n", "header")
            elif line.startswith("---") and "---" in line[3:]:
                w.insert("end", line + "\n", "section")
            elif "BREAK EVEN" in line and "===" not in line:
                w.insert("end", line + "\n", "result_box")
            elif "Price above BE:" in line:
                val_part = line.split(":")[-1].strip()
                val_str = val_part.split("(")[0].replace("$", "").replace("+", "").strip()
                try:
                    val = float(val_str)
                    tag = "bold_green" if val >= 0 else "bold_red"
                except:
                    tag = "bold_blue"
                w.insert("end", line + "\n", tag)
            elif "Tokens above BE:" in line and "—" not in line:
                val_str = line.split(":")[-1].strip().replace("PRL", "").replace("+", "")
                try:
                    val = float(val_str)
                    tag = "bold_green" if val >= 0 else "bold_red"
                except:
                    tag = "bold_dark"
                w.insert("end", line + "\n", tag)
            elif "BE price" in line and "—" not in line:
                w.insert("end", line + "\n", "bold_blue")
            elif "BE coins" in line and "—" not in line:
                w.insert("end", line + "\n", "bold_blue")
            elif "Profit:" in line and "should be" in line:
                w.insert("end", line + "\n", "bold_dark")
            elif line.strip().startswith(("Drag:", "  Drag:")):
                w.insert("end", line + "\n", "bold_orange")
            elif line.strip().startswith(("Fee:", "  Fee:")):
                w.insert("end", line + "\n", "bold_red")
            elif "Electricity:" in line and "$" in line and "kWh" not in line:
                w.insert("end", line + "\n", "bold_red")
            elif "Step 1:" in line or "Step 2:" in line or "Step 3:" in line or "Xfer:" in line:
                w.insert("end", line + "\n", "bold_dark")
            elif "Forward Verification" in line:
                w.insert("end", line + "\n", "section")
            else:
                w.insert("end", line + "\n")
        w.config(state="disabled")

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
        self.daily_summary = ttk.Label(right, text="", font=("Consolas", 11), justify="left")
        self.daily_summary.pack(fill="x", padx=4, pady=4)

        cols = ("date", "coin", "amount", "price", "gross", "electric", "net")
        self.daily_tree = ttk.Treeview(right, columns=cols, show="headings", height=18)
        for c, h, w, a in [("date","Date",110,"center"),("coin","Coin",60,"center"),
                        ("amount","Amount",90,"e"),("price","Price",80,"e"),("gross","Gross $",90,"e"),
                        ("electric","Electric $",90,"e"),("net","Net $",90,"e")]:
            self.daily_tree.heading(c, text=h, command=lambda _c=c: self._sort_treeview(self.daily_tree, _c, False))
            self.daily_tree.column(c, width=w, anchor=a)  # type: ignore[arg-type]
        # Button frame at top — always visible above the treeview
        bf = ttk.Frame(right)
        bf.pack(fill="x", padx=4, pady=(4, 2))
        self._daily_edit_btn = ttk.Button(bf, text="Edit Selected", command=self._edit_daily)
        self._daily_edit_btn.pack(side="left", padx=4)
        ttk.Button(bf, text="Delete Selected", command=self._delete_daily).pack(side="left", padx=4)
        ttk.Button(bf, text="Refresh", command=self._refresh_daily).pack(side="left", padx=4)

        self.daily_tree.pack(fill="both", expand=True, padx=4, pady=(2, 4))
        self._refresh_daily()

        # Double-click to edit
        self.daily_tree.bind("<Double-1>", lambda e: self._edit_daily())

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
            self._daily_edit_btn.config(text="Editing #{}".format(idx + 1))

    def _clear_daily_form(self):
        self._daily_edit_idx = -1
        self._daily_coin.set("PRL")
        self._daily_date.set(date.today().isoformat())
        self._daily_amt.set("")
        self._daily_price.set("")
        self._daily_power.set("1600")
        self._daily_elec.set("0.15")
        self._daily_time.set("24")
        self._daily_edit_btn.config(text="Edit Selected")

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
        coin_summary = " | ".join(f"{c}: {a:>8.2f}" for c, a in sorted(coin_totals.items()))
        net_color = "#2E7D32" if total_net >= 0 else "#C62828"
        summary_text = (
            f"  Days: {len(entries):>3}  |  {coin_summary}\n"
            f"    Net: ${total_net:>10.4f}  |  Avg/day: ${avg:>10.4f}"
        )
        self.daily_summary.config(
            text=summary_text,
            foreground=net_color, font=("Consolas", 11, "bold"), justify="left",
        )

    # ==========================================================
    # TAB 4: SALES - Trades (sells + purchases)
    # ==========================================================
    def _build_sales_tab(self):
        f = self.tab_frames[3]
        paned = ttk.PanedWindow(f, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        LEFT = ttk.LabelFrame(paned, text="Trade Entry", padding=6)
        paned.add(LEFT, weight=1)

        # Two-column grid: col 0 = labels (left-aligned), col 1 = inputs (left-aligned)
        LEFT.grid_columnconfigure(0, weight=0, minsize=70)
        LEFT.grid_columnconfigure(1, weight=0)

        # Row 0: Pair
        ttk.Label(LEFT, text="Pair:", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_pair = tk.StringVar(value="PRL/USDT")
        self._tr_pair_lbl = tk.StringVar(value="PRL/USDT")
        ttk.Combobox(LEFT, textvariable=self._tr_pair, values=TRADE_PAIR_LABELS, width=12, state="readonly").grid(row=0, column=1, sticky="w", pady=3)
        self._tr_base = tk.StringVar(value="PRL")
        self._tr_quote = tk.StringVar(value="USDT")

        # Row 1: Type
        ttk.Label(LEFT, text="Type:", font=("Segoe UI", 10, "bold")).grid(row=1, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_type = tk.StringVar(value="Limit")
        ttk.Combobox(LEFT, textvariable=self._tr_type, values=ORDER_TYPES, width=10, state="readonly").grid(row=1, column=1, sticky="w", pady=3)

        # Row 2: Side
        ttk.Label(LEFT, text="Side:", font=("Segoe UI", 10, "bold")).grid(row=2, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_side = tk.StringVar(value="Sell")
        ttk.Combobox(LEFT, textvariable=self._tr_side, values=SIDES, width=8, state="readonly").grid(row=2, column=1, sticky="w", pady=3)

        # Row 3: Status
        ttk.Label(LEFT, text="Status:", font=("Segoe UI", 10, "bold")).grid(row=3, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_status = tk.StringVar(value="Filled")
        ttk.Combobox(LEFT, textvariable=self._tr_status, values=STATUSES, width=10, state="readonly").grid(row=3, column=1, sticky="w", pady=3)

        # Row 4: Exchange
        ttk.Label(LEFT, text="Exchange:", font=("Segoe UI", 10, "bold")).grid(row=4, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_exchange = tk.StringVar(value="SafeTrade")
        ttk.Combobox(LEFT, textvariable=self._tr_exchange, values=EXCHANGES, width=12, state="readonly").grid(row=4, column=1, sticky="w", pady=3)

        # Row 5: Date
        ttk.Label(LEFT, text="Date:", font=("Segoe UI", 10, "bold")).grid(row=5, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_date = tk.StringVar(value=date.today().isoformat())
        ttk.Entry(LEFT, textvariable=self._tr_date, width=16).grid(row=5, column=1, sticky="w", pady=3)

        # Row 6: Price
        self._tr_price_lbl = ttk.Label(LEFT, text="Price:", font=("Segoe UI", 10, "bold"))
        self._tr_price_lbl.grid(row=6, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_price = tk.StringVar()
        ttk.Entry(LEFT, textvariable=self._tr_price, width=16).grid(row=6, column=1, sticky="w", pady=3)

        # Row 7: Amount
        self._tr_amt_lbl = ttk.Label(LEFT, text="Amount:", font=("Segoe UI", 10, "bold"))
        self._tr_amt_lbl.grid(row=7, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_amt = tk.StringVar()
        ttk.Entry(LEFT, textvariable=self._tr_amt, width=16).grid(row=7, column=1, sticky="w", pady=3)

        # Row 8: Total
        self._tr_total_lbl = ttk.Label(LEFT, text="Total:", font=("Segoe UI", 10, "bold"))
        self._tr_total_lbl.grid(row=8, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_total = tk.StringVar()
        ttk.Entry(LEFT, textvariable=self._tr_total, width=16).grid(row=8, column=1, sticky="w", pady=3)

        # Row 9: Fee coin
        ttk.Label(LEFT, text="Fee coin:", font=("Segoe UI", 10, "bold")).grid(row=9, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_fee_coin = tk.StringVar(value="USDT")
        ttk.Label(LEFT, textvariable=self._tr_fee_coin, font=("Segoe UI", 10)).grid(row=9, column=1, sticky="w", pady=3)

        # Row 10: Fee amount
        ttk.Label(LEFT, text="Fee:", font=("Segoe UI", 10, "bold")).grid(row=10, column=0, sticky="w", padx=(0, 4), pady=3)
        self._tr_fee_amt = tk.StringVar(value="0")
        ttk.Entry(LEFT, textvariable=self._tr_fee_amt, width=16).grid(row=10, column=1, sticky="w", pady=3)

        # Row 11: Fee display
        self._tr_fee_display_lbl = tk.Label(LEFT, text="—", fg="#888888", bg="#1A1A1A")
        self._tr_fee_display_lbl.grid(row=11, column=0, columnspan=2, sticky="w", padx=0, pady=2)

        # Row 12: Status line
        self._tr_status_lbl = tk.Label(LEFT, text="Pair: PRL/USDT  |  Side: Sell", fg="#888888", bg="#1A1A1A")
        self._tr_status_lbl.grid(row=12, column=0, columnspan=2, sticky="w", padx=0, pady=2)

        # Row 13: Buttons
        bf = ttk.Frame(LEFT)
        bf.grid(row=13, column=0, columnspan=2, pady=6, sticky="w")
        ttk.Button(bf, text="Save / Update", command=self._save_trade).pack(side="left", padx=(0, 4))
        ttk.Button(bf, text="Clear Form", command=self._clear_trade_form).pack(side="left")

        # Traces
        self._tr_pair.trace_add("write", lambda *a: self._on_pair_change())
        self._tr_side.trace_add("write", lambda *a: self._update_trade_labels())
        self._tr_fee_amt.trace_add("write", lambda *a: self._update_fee_display())
        self._tr_fee_coin.trace_add("write", lambda *a: self._update_fee_display())
        self._tr_total.trace_add("write", lambda *a: self._update_fee_display())
        self._tr_price.trace_add("write", lambda *a: self._update_fee_display())

        # Right: list
        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self.sales_summary = tk.Text(right, height=6, font=("Consolas", 11), wrap="none",
                                      bg="#1A1A1A", fg="#E0E0E0", relief="flat", bd=0,
                                      selectbackground="#1A1A1A", selectforeground="#E0E0E0",
                                      cursor="arrow")
        self.sales_summary.pack(fill="x", padx=4, pady=4)
        self.sales_summary.tag_configure("bold", font=("Consolas", 11, "bold"), foreground="#E0E0E0")
        self.sales_summary.tag_configure("blue", foreground="#60A5FA")
        self.sales_summary.tag_configure("green", foreground="#34D399")
        self.sales_summary.tag_configure("red", foreground="#F87171")
        self.sales_summary.config(state="disabled")

        cols = ("date","pair","side","type","status","exchange","price","amount","total","fee","net")
        self.sales_tree = ttk.Treeview(right, columns=cols, show="headings", height=14)
        col_config = [
            ("date","Date",120,"center"),("pair","Pair",70,"center"),("side","Side",50,"center"),
            ("type","Type",55,"center"),("status","Status",70,"center"),("exchange","Exchange",80,"center"),
            ("price","Price",80,"e"),("amount","Traded",85,"e"),("total","Total",85,"e"),
            ("fee","Fee",100,"e"),("net","Net Recv",100,"e"),
        ]
        for c, h, w, a in col_config:
            self.sales_tree.heading(c, text=h, command=lambda _c=c: self._sort_treeview(self.sales_tree, _c, False))
            self.sales_tree.column(c, width=w, anchor=a)  # type: ignore[arg-type]
        self.sales_tree.tag_configure("canceled", background="#4A1C1C", foreground="#FCA5A5")
        self.sales_tree.tag_configure("partial", background="#4A3B1C", foreground="#FDE68A")
        self.sales_tree.tag_configure("filled", background="#1C3A2A", foreground="#6EE7B7")
        self.sales_tree.tag_configure("buy", background="#1C3A4A", foreground="#7DD3FC")

        # Button frame — always visible above treeview
        bf2 = ttk.Frame(right)
        bf2.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Button(bf2, text="Edit Selected", command=self._edit_trade).pack(side="left", padx=4)
        ttk.Button(bf2, text="Delete Selected", command=self._delete_trade).pack(side="left", padx=4)
        ttk.Button(bf2, text="Refresh", command=self._refresh_sales).pack(side="left", padx=4)

        self.sales_tree.pack(fill="both", expand=True, padx=4, pady=(2, 4))

        self._tr_edit_idx = -1
        self._update_trade_labels()
        self._refresh_sales()

        # Double-click to edit
        self.sales_tree.bind("<Double-1>", lambda e: self._edit_trade())

    def _on_pair_change(self):
        """When the pair selector changes, update base/quote and labels."""
        pair = self._tr_pair.get()
        if "/" in pair:
            bc, qc = pair.split("/", 1)
        else:
            bc, qc = "?", "?"
        self._tr_base.set(bc)
        self._tr_quote.set(qc)
        self._update_trade_labels()

    def _update_trade_labels(self):
        bc = self._tr_base.get() or "?"
        qc = self._tr_quote.get() or "?"
        side = self._tr_side.get()
        pair = self._tr_pair.get()
        self._tr_price_lbl.config(text=f"Price (per 1 {bc} in {qc}):")
        self._tr_amt_lbl.config(text=f"{bc} amount:")
        self._tr_total_lbl.config(text=f"Total ({qc}):")
        # Determine fee coin: received coin (quote for sell, base for buy)
        if side == "Sell":
            fee_coin = qc
        else:
            fee_coin = bc
        self._tr_fee_coin.set(fee_coin)
        self._update_fee_display()
        # Update status line
        self._tr_status_lbl.config(text=f"Pair: {pair}  |  Side: {side}  |  {fee_coin} received")

    def _update_fee_display(self):
        """Update the fee display label to show fee in the appropriate coin."""
        fee_amt = self._float(self._tr_fee_amt)
        fee_coin = self._tr_fee_coin.get().upper()
        if fee_amt > 0:
            self._tr_fee_display_lbl.config(text=f"Fee: {fee_amt:.4f} {fee_coin}", foreground="#E0E0E0")
        else:
            # Show the auto-computed default fee
            amt = self._float(self._tr_amt)
            total = self._float(self._tr_total)
            price = self._float(self._tr_price)
            side = self._tr_side.get()
            if side == "Sell" and amt > 0 and price > 0:
                default_fee = amt * price * 0.001
                self._tr_fee_display_lbl.config(text=f"Fee: {default_fee:.4f} {fee_coin} (auto)", foreground="#888888")
            elif side == "Buy" and total > 0 and price > 0:
                default_fee = (total / price) * 0.001
                self._tr_fee_display_lbl.config(text=f"Fee: {default_fee:.4f} {fee_coin} (auto)", foreground="#888888")
            else:
                self._tr_fee_display_lbl.config(text=f"Fee: — {fee_coin}", foreground="#888888")

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
            amount=amt,
            total=self._float(self._tr_total),
        )
        # Determine fee: use user-entered value if non-zero, else auto-compute 0.1%
        user_fee = self._float(self._tr_fee_amt)
        if entry.side == "Sell":
            entry.fee_coin = entry.quote_coin
            if user_fee > 0:
                entry.fee_amount = user_fee
            else:
                gross_recv = entry.amount * entry.price
                entry.fee_amount = gross_recv * 0.001  # 0.1% default
            entry.total = entry.amount * entry.price - entry.fee_amount
        else:
            # Buy: user enters gross amount (e.g., ARB) and total (e.g., USDT spent)
            # Fee is 0.1% of gross received, stored separately (not deducted from amount)
            entry.fee_coin = entry.base_coin
            if user_fee > 0:
                entry.fee_amount = user_fee
            else:
                entry.fee_amount = entry.amount * 0.001  # 0.1% of gross received
            # total is what user entered (USDT spent), amount is gross ARB received
            # (fee is charged separately, not deducted from received amount)
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
            pair_label = f"{e.base_coin}/{e.quote_coin}"
            # Validate pair is in our allowed list; fallback if legacy data
            if pair_label not in TRADE_PAIR_LABELS:
                pair_label = TRADE_PAIR_LABELS[0]
            self._tr_pair.set(pair_label)
            self._tr_base.set(e.base_coin or "PRL")
            self._tr_quote.set(e.quote_coin or "USDT")
            self._tr_type.set(e.order_type or "Limit")
            self._tr_side.set(e.side or "Sell")
            self._tr_status.set(e.status or "Filled")
            self._tr_exchange.set(e.exchange or "SafeTrade")
            self._tr_date.set(e.date)
            self._tr_price.set(str(e.price))
            self._tr_amt.set(str(e.amount))
            self._tr_total.set(str(e.total))
            self._tr_fee_coin.set(e.fee_coin or e.quote_coin or "USDT")
            self._tr_fee_amt.set(str(e.fee_amount))
            self._update_trade_labels()
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
        self._tr_pair.set("PRL/USDT")
        self._tr_base.set("PRL"); self._tr_quote.set("USDT")
        self._tr_type.set("Limit"); self._tr_side.set("Sell")
        self._tr_status.set("Filled"); self._tr_exchange.set("SafeTrade")
        self._tr_date.set(date.today().isoformat())
        for v in [self._tr_price, self._tr_amt, self._tr_total, self._tr_fee_amt]:
            v.set("")
        self._tr_fee_coin.set("USDT")
        self._tr_edit_idx = -1
        self._update_trade_labels()

    def _refresh_sales(self):
        entries = load_sales()
        self.sales_tree.delete(*self.sales_tree.get_children())
        # Accumulate net flow and fees per coin
        net_per_coin = {}
        fee_per_coin = {}
        pair_stats = {}
        for e in entries:
            tag = "canceled" if e.status == "Canceled" else ("partial" if e.status == "Partial" else ("buy" if e.side == "Buy" else "filled"))
            pair = f"{e.base_coin}/{e.quote_coin}"

            # Compute fee from trade data: fee is always in the received coin
            if e.side == "Sell":
                # Sold base_coin, received quote_coin. Fee in quote_coin.
                recv_coin = e.quote_coin
                gross_recv = e.amount * e.price
                fee = gross_recv - e.total
                if fee < 0:
                    fee = 0
                net_recv = e.total  # already net of fee
                fee_str = f"{fee:.4f} {recv_coin}" if fee > 0 else "—"
                net_str = f"{net_recv:.4f} {recv_coin}"
                # Accumulate
                net_per_coin[recv_coin] = net_per_coin.get(recv_coin, 0.0) + net_recv
                fee_per_coin[recv_coin] = fee_per_coin.get(recv_coin, 0.0) + fee
            else:
                # Bought base_coin with quote_coin. Received base_coin.
                # Fee is stored in fee_amount (may be in base or quote coin depending on exchange)
                recv_coin = e.base_coin
                fee = e.fee_amount
                net_recv = e.amount  # gross received (fee charged separately)
                fee_str = f"{fee:.4f} {e.fee_coin}" if fee > 0 else "—"
                net_str = f"{net_recv:.4f} {recv_coin}"
                # Accumulate
                net_per_coin[recv_coin] = net_per_coin.get(recv_coin, 0.0) + net_recv
                fee_per_coin[recv_coin] = fee_per_coin.get(recv_coin, 0.0) + fee

            pair_stats[pair] = pair_stats.get(pair, 0) + e.amount

            self.sales_tree.insert("", "end", values=(
                e.date, pair, e.side, e.order_type, e.status, e.exchange,
                f"{e.price:.6f}", f"{e.amount:.4f}",
                f"{e.total:.4f}", fee_str, net_str,
            ), tags=(tag,))

        # Store (label, raw_value) tuples for proper alignment
        pair_data = [(p, a) for p, a in sorted(pair_stats.items())]
        net_data = [(c, net_per_coin.get(c, 0.0)) for c in sorted(net_per_coin.keys()) if abs(net_per_coin.get(c, 0.0)) > 1e-9]
        fee_data = [(c, fee_per_coin.get(c, 0.0)) for c in sorted(fee_per_coin.keys()) if fee_per_coin.get(c, 0.0) > 1e-9]

        # Pad to same row count
        max_rows = max(len(pair_data), len(net_data), len(fee_data), 1)
        while len(pair_data) < max_rows: pair_data.append(("", 0))
        while len(net_data) < max_rows: net_data.append(("", 0))
        while len(fee_data) < max_rows: fee_data.append(("", 0))

        pair_lw = 10  # label width for pairs
        pair_vw = 12  # value width for pairs
        net_lw = 8    # label width for net
        net_vw = 14   # value width for net
        fee_lw = 8    # label width for fees
        fee_vw = 14   # value width for fees
        gap = "   "   # 3-space gap between columns

        self.sales_summary.config(state="normal")
        self.sales_summary.delete("1.0", "end")
        self.sales_summary.insert("end", f"Trades: {len(entries):>3}\n", "bold")
        hdr = (f"{'Pairs':<{pair_lw+pair_vw}}"
               f"{gap}"
               f"{'Net':<{net_lw+net_vw}}"
               f"{gap}"
               f"{'Fees':<{fee_lw+fee_vw}}")
        self.sales_summary.insert("end", hdr + "\n", "bold")

        for (p_l, p_v), (n_l, n_v), (f_l, f_v) in zip(pair_data, net_data, fee_data):
            if p_l:
                c1 = f"{p_l + ':':<{pair_lw}}{p_v:>{pair_vw}.2f}"
            else:
                c1 = ""
            if n_l:
                c2 = f"{n_l + ':':<{net_lw}}{n_v:>{net_vw}.4f}"
            else:
                c2 = ""
            if f_l:
                c3 = f"{f_l + ':':<{fee_lw}}{f_v:>{fee_vw}.4f}"
            else:
                c3 = ""
            self.sales_summary.insert("end", c1 + gap + c2 + gap + c3 + "\n", "blue")
        self.sales_summary.config(state="disabled")

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

        # Row 7: Status
        self._grid_label(LEFT, 7, "Status:")
        self._xfer_status = self._grid_combo(LEFT, 7, ["Completed", "Pending", "Failed"], "Completed")

        # Row 8: Notes
        self._grid_label(LEFT, 8, "Notes:")
        self._xfer_notes = self._grid_entry(LEFT, 8, "")

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
        self.xfer_summary = tk.Text(right, height=5, font=("Consolas", 11), wrap="none",
                                     bg="#1A1A1A", fg="#E0E0E0", relief="flat", bd=0,
                                     selectbackground="#1A1A1A", selectforeground="#E0E0E0",
                                     cursor="arrow")
        self.xfer_summary.pack(fill="x", padx=4, pady=4)
        self.xfer_summary.tag_configure("bold", font=("Consolas", 11, "bold"), foreground="#E0E0E0")
        self.xfer_summary.tag_configure("blue", foreground="#60A5FA")
        self.xfer_summary.tag_configure("green", foreground="#34D399")
        self.xfer_summary.tag_configure("red", foreground="#F87171")
        self.xfer_summary.config(state="disabled")

        cols = ("date", "coin", "from", "to", "amount", "fee", "received", "status", "notes")
        self.xfer_tree = ttk.Treeview(right, columns=cols, show="headings", height=14)
        for c, h, w, a in [("date","Date",120,"center"),("coin","Coin",60,"center"),
                        ("from","From",90,"center"),("to","To",90,"center"),
                        ("amount","Amount",90,"e"),("fee","Fee",105,"e"),
                        ("received","Received",95,"e"),("status","Status",80,"center"),("notes","Notes",120,"w")]:
            self.xfer_tree.heading(c, text=h, command=lambda _c=c: self._sort_treeview(self.xfer_tree, _c, False))
            self.xfer_tree.column(c, width=w, anchor=a)  # type: ignore[arg-type]
        self.xfer_tree.tag_configure("completed", background="#1C3A2A", foreground="#6EE7B7")
        self.xfer_tree.tag_configure("pending", background="#4A3B1C", foreground="#FDE68A")
        self.xfer_tree.tag_configure("failed", background="#4A1C1C", foreground="#FCA5A5")

        # Button frame — always visible above treeview
        bf2 = ttk.Frame(right)
        bf2.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Button(bf2, text="Edit Selected", command=self._edit_transfer).pack(side="left", padx=4)
        ttk.Button(bf2, text="Delete Selected", command=self._delete_transfer).pack(side="left", padx=4)
        ttk.Button(bf2, text="Refresh", command=self._refresh_transfers).pack(side="left", padx=4)

        self.xfer_tree.pack(fill="both", expand=True, padx=4, pady=(2, 4))

        self._xfer_edit_idx = -1
        self._update_xfer_labels()
        self._refresh_transfers()

        # Double-click to edit
        self.xfer_tree.bind("<Double-1>", lambda e: self._edit_transfer())

    def _update_xfer_labels(self):
        c = self._xfer_coin.get() or "?"
        self._xfer_amt_lbl.config(text=f"{c} amount:")
        self._xfer_status_lbl.config(text=f"Transfer {c} from {self._xfer_from.get()} to {self._xfer_to.get()}.")

    def _save_transfer(self):
        amt = self._float(self._xfer_amt)
        if amt <= 0:
            messagebox.showerror("Error", "Amount must be > 0")
            return
        to_exch = self._xfer_to.get()
        # Round to 2 decimal places for bank transfers — discard residual
        if to_exch == 'Bank':
            amt = round(amt, 2)
        entry = TransferEntry(
            date=self._xfer_date.get() or date.today().isoformat(),
            coin=self._xfer_coin.get(),
            from_exchange=self._xfer_from.get(),
            to_exchange=to_exch,
            amount=amt,
            fee_coin=self._xfer_fee_coin.get(),
            fee_amount=round(self._float(self._xfer_fee_amt), 2) if to_exch == 'Bank' else self._float(self._xfer_fee_amt),
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
        self._xfer_fee_amt.set("")
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
                f"{e.received:.4f}", e.status, e.notes,
            ), tags=(tag,))
            total_amount += e.amount
            total_fee += e.fee_amount
            coin_totals[e.coin] = coin_totals.get(e.coin, 0) + e.amount
        coin_lines = [f"{c}: {a:>10.2f}" for c, a in sorted(coin_totals.items())]

        fee_per_coin = {}
        for e in entries:
            if e.fee_amount > 0:
                fee_per_coin[e.fee_coin] = fee_per_coin.get(e.fee_coin, 0.0) + e.fee_amount
        fee_lines = [f"{c}: {fee_per_coin.get(c, 0):>10.4f}" for c in sorted(fee_per_coin.keys())]

        # Store (label, raw_value) for proper decimal alignment
        coin_data = [(c, a) for c, a in sorted(coin_totals.items())]
        fee_data = [(c, fee_per_coin.get(c, 0.0)) for c in sorted(fee_per_coin.keys())]

        max_rows = max(len(coin_data), len(fee_data), 1)
        while len(coin_data) < max_rows: coin_data.append(("", 0))
        while len(fee_data) < max_rows: fee_data.append(("", 0))

        col1_w = 20
        col_lw = 8   # label width
        col_vw = 12  # value width
        gap = "   "  # 3-space gap between columns

        self.xfer_summary.config(state="normal")
        self.xfer_summary.delete("1.0", "end")
        self.xfer_summary.insert("end", f"Count: {len(entries):>3}\n", "bold")
        hdr = f"{'Transfers':<{col_lw+col_vw}}{gap}{'Fees':<{col_lw+col_vw}}"
        self.xfer_summary.insert("end", hdr + "\n", "bold")

        for (t_l, t_v), (f_l, f_v) in zip(coin_data, fee_data):
            c1 = f"{t_l + ':':<{col_lw}}{t_v:>{col_vw}.2f}" if t_l else ""
            c2 = f"{f_l + ':':<{col_lw}}{f_v:>{col_vw}.4f}" if f_l else ""
            self.xfer_summary.insert("end", c1 + gap + c2 + "\n", "blue")
        self.xfer_summary.config(state="disabled")

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
        self.drag_result = tk.Text(res, wrap="word", width=55, height=30, font=("Consolas", 12))
        self.drag_result.pack(fill="both", expand=True)
        self._init_text_tags(self.drag_result)
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
        self._write_tagged_drag_colored(lines)

    def _write_tagged_drag_colored(self, lines):
        """Write drag analysis results with per-line color+bold detection."""
        w = self.drag_result
        w.config(state="normal")
        w.delete("1.0", "end")
        for line in lines:
            if line.startswith("===") and "===" in line[3:]:
                w.insert("end", line + "\n", "header")
            elif "TOTAL DRAG:" in line:
                # Color total drag: higher = worse (red)
                val_str = line.split(":")[-1].strip().replace("%", "")
                try:
                    val = float(val_str)
                    tag = "bold_red" if val > 1.0 else "bold_orange" if val > 0.1 else "bold_green"
                except:
                    tag = "bold_orange"
                w.insert("end", line + "\n", tag)
            elif line.strip().startswith("Drag:"):
                val_str = line.split("%")[0].split()[-1]
                try:
                    val = float(val_str)
                    tag = "bold_red" if val > 1.0 else "bold_orange" if val > 0.1 else "bold_green"
                except:
                    tag = "bold_orange"
                w.insert("end", line + "\n", tag)
            elif "Effective:" in line:
                w.insert("end", line + "\n", "bold_blue")
            elif "Raw price:" in line:
                w.insert("end", line + "\n", "bold_dark")
            elif line.strip().startswith("Expected:"):
                w.insert("end", line + "\n", "section")
            elif line.strip().startswith("Received:"):
                w.insert("end", line + "\n", "bold_green")
            elif "==============================" in line:
                w.insert("end", line + "\n", "header")
            else:
                w.insert("end", line + "\n")
        w.config(state="disabled")

    # ==========================================================
    # TAB 8: DATA — Import / Export / Summary
    # ==========================================================
    def _build_data_tab(self):
        f = self.tab_frames[7]

        main = tk.Frame(f, bg="#1A1A1A")
        main.pack(fill="both", expand=True, padx=8, pady=8)

        tk.Label(main, text="DATA SUMMARY", font=("Segoe UI", 11, "bold"),
                 fg="#FF4444", bg="#1A1A1A").pack(anchor="w", pady=(0, 4))
        self._data_summary = tk.Label(main, text="", font=("Consolas", 11),
                                       fg="#E0E0E0", bg="#2A2A2A", justify="left", padx=10, pady=8)
        self._data_summary.pack(fill="x", pady=(0, 12))
        self._refresh_data_summary()

        tk.Label(main, text="EXPORT", font=("Segoe UI", 11, "bold"),
                 fg="#FF4444", bg="#1A1A1A").pack(anchor="w", pady=(0, 4))
        tk.Button(main, text="Export Data...", command=self._export_data,
                  font=("Segoe UI", 10, "bold"), fg="#FF4444", bg="#2A2A2A",
                  activebackground="#333", activeforeground="#FF6666",
                  relief="flat", padx=12, pady=4).pack(anchor="w", pady=(0, 12))

        tk.Label(main, text="IMPORT", font=("Segoe UI", 11, "bold"),
                 fg="#FF4444", bg="#1A1A1A").pack(anchor="w", pady=(0, 4))
        self._import_mode = tk.StringVar(value="merge")
        rb_frame = tk.Frame(main, bg="#1A1A1A")
        rb_frame.pack(fill="x", pady=(0, 4))
        tk.Radiobutton(rb_frame, text="Merge (skip duplicates)", variable=self._import_mode,
                       value="merge", fg="#E0E0E0", bg="#1A1A1A", selectcolor="#333",
                       font=("Segoe UI", 10)).pack(side="left", padx=(0, 12))
        tk.Radiobutton(rb_frame, text="Replace all data", variable=self._import_mode,
                       value="replace", fg="#E0E0E0", bg="#1A1A1A", selectcolor="#333",
                       font=("Segoe UI", 10)).pack(side="left")
        tk.Button(main, text="Import Data...", command=self._import_data,
                  font=("Segoe UI", 10, "bold"), fg="#FF4444", bg="#2A2A2A",
                  activebackground="#333", activeforeground="#FF6666",
                  relief="flat", padx=12, pady=4).pack(anchor="w", pady=(0, 12))

        tk.Label(main, text="DATA DIRECTORY", font=("Segoe UI", 11, "bold"),
                 fg="#FF4444", bg="#1A1A1A").pack(anchor="w", pady=(0, 2))
        self._data_dir_lbl = tk.Label(main, text=str(DATA_DIR), font=("Consolas", 9),
                                       fg="#888", bg="#1A1A1A", wraplength=400, justify="left")
        self._data_dir_lbl.pack(anchor="w")

    def _refresh_data_summary(self):
        daily = load_daily()
        sales = load_sales()
        transfers = load_transfers()
        text = (f"  Daily entries:    {len(daily)}\n"
                f"  Sales entries:    {len(sales)}\n"
                f"  Transfer entries: {len(transfers)}\n"
                f"  Total records:    {len(daily) + len(sales) + len(transfers)}")
        self._data_summary.config(text=text)

    def _export_data(self):
        import tkinter.filedialog as fd
        path = fd.asksaveasfilename(title="Export Data", defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile=f"mining_toolkit_backup_{date.today().isoformat()}.json")
        if not path:
            return
        backup = {"version": 1, "exported_at": date.today().isoformat(),
            "daily_log": [asdict(e) for e in load_daily()],
            "sales_log": [asdict(e) for e in load_sales()],
            "transfers_log": [asdict(e) for e in load_transfers()]}
        Path(path).write_text(json.dumps(backup, indent=2))
        messagebox.showinfo("Export Complete",
            f"Exported {len(backup['daily_log'])} daily, {len(backup['sales_log'])} sales, {len(backup['transfers_log'])} transfers.")

    def _import_data(self):
        import tkinter.filedialog as fd
        path = fd.askopenfilename(title="Import Data", filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            raw = json.loads(Path(path).read_text())
        except Exception as e:
            messagebox.showerror("Import Error", f"Could not read file:\n{e}")
            return
        if not isinstance(raw, dict):
            messagebox.showerror("Import Error", "Unrecognized file format.")
            return
        daily_in = raw.get("daily_log", []); sales_in = raw.get("sales_log", []); transfers_in = raw.get("transfers_log", [])
        if not any([daily_in, sales_in, transfers_in]):
            messagebox.showerror("Import Error", "No data found.")
            return
        if self._import_mode.get() == "replace":
            save_daily([DailyMiningEntry(**e) for e in daily_in])
            save_sales([TradeEntry(**e) for e in sales_in])
            save_transfers([TransferEntry(**e) for e in transfers_in])
            msg = f"Replaced with {len(daily_in)} daily, {len(sales_in)} sales, {len(transfers_in)} transfers."
        else:
            ed = {(e.date, e.coin, round(e.coins_mined, 8)) for e in load_daily()}
            es = {(e.date, e.base_coin, e.quote_coin, e.side, round(e.amount, 8), round(e.total, 4)) for e in load_sales()}
            ex = {(e.date, e.coin, e.from_exchange, e.to_exchange, round(e.amount, 8)) for e in load_transfers()}
            nd, ns, nx = list(load_daily()), list(load_sales()), list(load_transfers())
            da = sa = xa = 0
            for e in daily_in:
                k = (e["date"], e["coin"], round(e["coins_mined"], 8))
                if k not in ed: nd.append(DailyMiningEntry(**e)); da += 1
            for e in sales_in:
                k = (e["date"], e["base_coin"], e["quote_coin"], e["side"], round(e["amount"], 8), round(e["total"], 4))
                if k not in es: ns.append(TradeEntry(**e)); sa += 1
            for e in transfers_in:
                k = (e["date"], e["coin"], e["from_exchange"], e["to_exchange"], round(e["amount"], 8))
                if k not in ex: nx.append(TransferEntry(**e)); xa += 1
            save_daily(nd); save_sales(ns); save_transfers(nx)
            msg = f"Merge: Daily +{da}, Sales +{sa}, Transfers +{xa}"
        self._refresh_data_summary(); self._refresh_sales(); self._refresh_banner()
        messagebox.showinfo("Import Complete", msg)

    # ==========================================================
    # TAB 7: TRENDS — Graphs
    # ==========================================================
    def _build_trends_tab(self):
        f = self.tab_frames[6]

        # Controls frame at top
        ctrl = ttk.Frame(f, padding=4)
        ctrl.pack(fill="x")

        ttk.Label(ctrl, text="Metric:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 4))
        self._trend_metric = tk.StringVar(value="prl_mined")
        metrics = [
            ("prl_mined", "PRL Mined / Day"),
            ("prl_price", "PRL Price (USDT)"),
            ("usdt_revenue", "USDT Revenue / Day"),
            ("elec_cost", "Electricity Cost / Day"),
            ("net_daily", "Net Profit / Day"),
            ("be_price", "BE Price vs Actual"),
            ("be_coins", "BE Coins vs Mined"),
        ]
        ttk.Combobox(ctrl, textvariable=self._trend_metric,
                     values=[m[0] for m in metrics], width=18, state="readonly").pack(side="left", padx=4)
        ttk.Button(ctrl, text="Refresh", command=self._refresh_trend).pack(side="left", padx=8)

        # Chart frame
        chart_frame = ttk.Frame(f)
        chart_frame.pack(fill="both", expand=True, padx=4, pady=4)

        # Create matplotlib figure
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        self._trend_fig = Figure(figsize=(10, 5.5), dpi=100)
        self._trend_ax = self._trend_fig.add_subplot(111)
        self._trend_canvas = FigureCanvasTkAgg(self._trend_fig, master=chart_frame)
        self._trend_canvas.get_tk_widget().pack(fill="both", expand=True)

        # Summary text below chart
        self._trend_summary = ttk.Label(f, text="", font=("Consolas", 12), justify="left", padding=4)
        self._trend_summary.pack(fill="x", padx=4, pady=(0, 4))

        # Initial draw
        self._refresh_trend()

    def _refresh_trend(self):
        """Redraw the trend chart based on selected metric."""
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.ticker import MaxNLocator

        metric = self._trend_metric.get()
        # Recreate subplot to guarantee clean slate (removes any twin axes)
        self._trend_fig.clear()
        ax = self._trend_fig.add_subplot(111)

        daily = load_daily()
        if not daily:
            ax.text(0.5, 0.5, "No daily mining data yet", ha="center", va="center", fontsize=14)
            self._trend_canvas.draw()
            return

        # Sort by date
        daily.sort(key=lambda e: e.date)
        dates = [e.date[5:] for e in daily]  # MM-DD format

        if metric == "be_price":
            self._draw_be_price_chart(ax)
            return
        if metric == "be_coins":
            self._draw_be_coins_chart(ax)
            return

        metric_labels = {
            "prl_mined": ("PRL Mined / Day", "PRL", "#2E7D32", "bar"),
            "prl_price": ("PRL Price (USDT)", "USDT/PRL", "#1565C0", "line"),
            "usdt_revenue": ("USDT Revenue / Day", "USDT", "#FF8F00", "bar"),
            "elec_cost": ("Electricity Cost / Day", "USD", "#C62828", "bar"),
            "net_daily": ("Net Profit / Day", "USD", "#6A1B9A", "line"),
        }

        label, ylabel, color, chart_type = metric_labels[metric]

        if metric == "prl_mined":
            values = [e.coins_mined for e in daily]
        elif metric == "prl_price":
            # Use actual sales prices (average per day) instead of mining estimates
            from collections import defaultdict
            sales = load_sales()
            prices_by_date = defaultdict(list)
            for s in sales:
                if s.side == "Sell" and s.base_coin == "PRL":
                    prices_by_date[s.date].append(s.price)
            values = []
            for e in daily:
                if e.date in prices_by_date and prices_by_date[e.date]:
                    values.append(sum(prices_by_date[e.date]) / len(prices_by_date[e.date]))
                else:
                    values.append(e.price)  # fallback to mining price
        elif metric == "usdt_revenue":
            values = [e.gross_revenue for e in daily]
        elif metric == "elec_cost":
            values = [e.electricity_cost for e in daily]
        elif metric == "net_daily":
            values = [e.net_profit for e in daily]

        if chart_type == "bar":
            bars = ax.bar(dates, values, color=color, alpha=0.8, edgecolor="white", linewidth=0.5)
            for bar, val in zip(bars, values):
                if val > 0:
                    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                            f"{val:.2f}", ha="center", va="bottom", fontsize=7)
        else:
            ax.plot(dates, values, color=color, marker="o", linewidth=2, markersize=5)
            ax.fill_between(range(len(dates)), values, alpha=0.15, color=color)
            for i, (d, v) in enumerate(zip(dates, values)):
                ax.annotate(f"{v:.4f}" if metric == "prl_price" else f"{v:.2f}",
                            (i, v), textcoords="offset points", xytext=(0, 8),
                            ha="center", fontsize=7)

        ax.set_title(label, fontsize=14, fontweight="bold")
        ax.set_ylabel(ylabel, fontsize=12)
        ax.set_xlabel("Date", fontsize=12)
        ax.tick_params(axis="x", rotation=45, labelsize=10)
        ax.tick_params(axis="y", labelsize=10)
        ax.grid(axis="y", alpha=0.3)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=15))

        self._trend_fig.tight_layout()
        self._trend_canvas.draw()

        if values:
            avg = sum(values) / len(values)
            total = sum(values)
            mn = min(values)
            mx = max(values)
            self._trend_summary.config(
                text=f"  Total: {total:,.4f}  |  Avg: {avg:,.4f}  |  Min: {mn:,.4f}  |  Max: {mx:,.4f}  |  Days: {len(values)}",
                font=("Consolas", 11, "bold")
            )

    def _draw_be_price_chart(self, ax):
        """Draw breakeven PRL price vs actual price."""
        from matplotlib.ticker import MaxNLocator
        from collections import defaultdict

        daily = load_daily()
        if not daily:
            ax.text(0.5, 0.5, "No daily mining data yet", ha="center", va="center", fontsize=14)
            self._trend_canvas.draw()
            return

        # Build actual sales price lookup (average per day)
        from collections import defaultdict
        sales = load_sales()
        sales_prices_by_date = defaultdict(list)
        for s in sales:
            if s.side == "Sell" and s.base_coin == "PRL":
                sales_prices_by_date[s.date].append(s.price)

        daily.sort(key=lambda e: e.date)
        dates = [e.date[5:] for e in daily]

        be_prices = []
        actual_prices = []

        for e in daily:
            elec_cost = (e.power * e.time_hours / 1000.0) * e.elec_price
            coins_mined = e.coins_mined
            arb_usd = 0.114

            d1 = 1.0 - compute_step_drag_sales("PRL/USDT") / 100.0
            d2 = 1.0 - compute_step_drag_sales("USDT/ARB") / 100.0
            d3 = 1.0 - compute_step_drag_sales("ARB/USD") / 100.0
            f1 = 1.0 - DEFAULT_PRL_USDT_FEE_PCT / 100.0
            f2 = 1.0 - DEFAULT_USDT_ARB_FEE_PCT / 100.0
            f3 = 1.0 - DEFAULT_ARB_USD_FEE_PCT / 100.0

            be_price, _, _, _ = compute_breakeven_price(
                arb_usd, elec_cost, coins_mined,
                d1, d2, d3, f1, f2, f3,
                DEFAULT_ARB_USD_FLAT_FEE, DEFAULT_ARB_TRANSFER_FEE)

            # Use actual sales price if available, else fallback to mining price
            if e.date in sales_prices_by_date and sales_prices_by_date[e.date]:
                actual_price = sum(sales_prices_by_date[e.date]) / len(sales_prices_by_date[e.date])
            else:
                actual_price = e.price

            if be_price != float('inf') and coins_mined > 0:
                be_prices.append(be_price)
                actual_prices.append(actual_price)
            else:
                be_prices.append(0)
                actual_prices.append(actual_price)

        ax.plot(dates, be_prices, color="#C62828", marker="o", linewidth=2,
                markersize=5, label="BE Price (USDT/PRL)")
        ax.fill_between(range(len(dates)), be_prices, alpha=0.1, color="#C62828")

        ax.plot(dates, actual_prices, color="#1565C0", marker="s", linewidth=1.5,
                markersize=4, linestyle="--", label="Actual Price")
        ax.fill_between(range(len(dates)), actual_prices, alpha=0.05, color="#1565C0")

        ax.set_ylabel("PRL Price (USDT)", fontsize=12)
        ax.set_xlabel("Date", fontsize=12)
        ax.tick_params(axis="x", rotation=45, labelsize=10)
        ax.tick_params(axis="y", labelsize=10)
        ax.grid(axis="y", alpha=0.3)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=15))
        ax.legend(loc="upper right", fontsize=10)
        ax.set_title("Breakeven PRL Price vs Actual", fontsize=14, fontweight="bold")

        self._trend_fig.tight_layout()
        self._trend_canvas.draw()

        if be_prices and actual_prices:
            avg_be = sum(be_prices) / len(be_prices)
            avg_actual = sum(actual_prices) / len(actual_prices)
            margin = ((avg_actual - avg_be) / avg_be * 100) if avg_be > 0 else 0
            margin_color = "#2E7D32" if margin >= 0 else "#C62828"
            self._trend_summary.config(
                text=f"  Avg BE Price: ${avg_be:.4f}  |  Avg Actual: ${avg_actual:.4f}  |  Margin: {margin:+.1f}%",
                foreground=margin_color, font=("Consolas", 11, "bold")
            )

    def _draw_be_coins_chart(self, ax):
        """Draw breakeven coins vs actual coins mined."""
        from matplotlib.ticker import MaxNLocator
        from collections import defaultdict

        daily = load_daily()
        if not daily:
            ax.text(0.5, 0.5, "No daily mining data yet", ha="center", va="center", fontsize=14)
            self._trend_canvas.draw()
            return

        # Build actual sales price lookup
        from collections import defaultdict
        sales = load_sales()
        sales_prices_by_date = defaultdict(list)
        for s in sales:
            if s.side == "Sell" and s.base_coin == "PRL":
                sales_prices_by_date[s.date].append(s.price)

        daily.sort(key=lambda e: e.date)
        dates = [e.date[5:] for e in daily]

        be_coins = []
        actual_coins = []

        for e in daily:
            elec_cost = (e.power * e.time_hours / 1000.0) * e.elec_price
            coins_mined = e.coins_mined
            arb_usd = 0.114

            # Use actual sales price for breakeven calculation
            if e.date in sales_prices_by_date and sales_prices_by_date[e.date]:
                effective_price = sum(sales_prices_by_date[e.date]) / len(sales_prices_by_date[e.date])
            else:
                effective_price = e.price

            d1 = 1.0 - compute_step_drag_sales("PRL/USDT") / 100.0
            d2 = 1.0 - compute_step_drag_sales("USDT/ARB") / 100.0
            d3 = 1.0 - compute_step_drag_sales("ARB/USD") / 100.0
            f1 = 1.0 - DEFAULT_PRL_USDT_FEE_PCT / 100.0
            f2 = 1.0 - DEFAULT_USDT_ARB_FEE_PCT / 100.0
            f3 = 1.0 - DEFAULT_ARB_USD_FEE_PCT / 100.0

            chain_eff = d1 * f1 * d2 * f2 * d3 * f3
            flat_per_coin = (DEFAULT_ARB_TRANSFER_FEE * arb_usd + DEFAULT_ARB_USD_FLAT_FEE) / coins_mined if coins_mined > 0 else 0
            denom = effective_price * chain_eff - flat_per_coin
            be_coin = elec_cost / denom if denom > 0 and coins_mined > 0 else 0

            be_coins.append(be_coin)
            actual_coins.append(coins_mined)

        x = range(len(dates))
        width = 0.35
        ax.bar([i - width / 2 for i in x], be_coins, width, color="#C62828", alpha=0.8,
               label="BE Coins", edgecolor="white", linewidth=0.5)
        ax.bar([i + width / 2 for i in x], actual_coins, width, color="#2E7D32", alpha=0.8,
               label="Actual Mined", edgecolor="white", linewidth=0.5)

        ax.set_ylabel("Coins", fontsize=12)
        ax.set_xlabel("Date", fontsize=12)
        ax.set_xticks(x)
        ax.set_xticklabels(dates, rotation=45, fontsize=10)
        ax.tick_params(axis="y", labelsize=10)
        ax.grid(axis="y", alpha=0.3)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=15))
        ax.legend(loc="upper right", fontsize=10)
        ax.set_title("Breakeven Coins vs Actual Mined", fontsize=14, fontweight="bold")

        self._trend_fig.tight_layout()
        self._trend_canvas.draw()

        if be_coins and actual_coins:
            avg_be = sum(be_coins) / len(be_coins)
            avg_actual = sum(actual_coins) / len(actual_coins)
            margin = ((avg_actual - avg_be) / avg_be * 100) if avg_be > 0 else 0
            margin_color = "#2E7D32" if margin >= 0 else "#C62828"
            self._trend_summary.config(
                text=f"  Avg BE Coins: {avg_be:.1f}  |  Avg Mined: {avg_actual:.1f}  |  Margin: {margin:+.1f}%",
                foreground=margin_color, font=("Consolas", 11, "bold")
            )


def main():
    root = tk.Tk()
    app = PRLMiningApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
