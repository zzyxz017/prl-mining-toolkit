#!/usr/bin/env python3
"""
PRL Mining Toolkit — Curses TUI
Tabs:
  1. Profitability Calculator  (PRL -> USDT -> ARB -> USD, full path)
  2. Breakeven Calculator
  3. Daily Mining Tracker      (coins + electricity only, no conversion)
  4. Sales Tracker             (record actual conversion results; drafts OK)
  5. Drag Analysis             (back-calculate drag from real results)

Data persisted to ~/.hermes/mining_toolkit/
  daily_log.json   — daily mining entries
  sales_log.json   — sale entries (draft + finalized)
"""

from __future__ import annotations

import curses
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# ══════════════════════════════════════════════════════════════
# SHARED CALCULATION ENGINE
# ══════════════════════════════════════════════════════════════

DEFAULT_USDT_TO_ARB_FEE = 0.10
DEFAULT_COINBASE_FEE_ARB = 2.0
DEFAULT_ARB_TO_USD_PCT = 1.0
DEFAULT_ARB_TO_USD_FLAT = 0.46

DATA_DIR = Path.home() / ".hermes" / "mining_toolkit"
DAILY_LOG = DATA_DIR / "daily_log.json"
SALES_LOG = DATA_DIR / "sales_log.json"


# ── Full conversion calc (tabs 1 & 2) ────────────────────────

@dataclass
class MiningResult:
    coins_mined: float
    time_hours: float
    prl_price_usdt: float
    arb_price_usd: float
    power_watts: float
    electricity_price_kwh: float
    usdt_to_arb_fee: float
    coinbase_fee_arb: float
    arb_to_usd_pct: float
    arb_to_usd_flat: float
    electricity_cost: float = 0.0
    gross_usdt: float = 0.0
    net_usdt: float = 0.0
    arb_amount: float = 0.0
    arb_after_withdrawal: float = 0.0
    gross_usd_from_arb: float = 0.0
    arb_pct_fee: float = 0.0
    arb_conv_fee_total: float = 0.0
    final_usd: float = 0.0
    net_profit: float = 0.0
    profit_per_hour: float = 0.0
    profit_per_coin: float = 0.0
    cost_ratio: float = 0.0
    total_fees_usd: float = 0.0


@dataclass
class BreakevenResult:
    coins_mined: float
    time_hours: float
    power_watts: float
    electricity_price_kwh: float
    arb_price_usd: float
    electricity_cost: float = 0.0
    breakeven_price: float = 0.0
    total_cost_usd: float = 0.0


def run_calculation(
    coins: float, time_h: float, power: float,
    elec_price: float, prl_price: float, arb_price: float,
    usdt_arb_fee: float = DEFAULT_USDT_TO_ARB_FEE,
    coinbase_fee: float = DEFAULT_COINBASE_FEE_ARB,
    arb_pct: float = DEFAULT_ARB_TO_USD_PCT,
    arb_flat: float = DEFAULT_ARB_TO_USD_FLAT,
) -> MiningResult:
    r = MiningResult(
        coins_mined=coins, time_hours=time_h, prl_price_usdt=prl_price,
        arb_price_usd=arb_price, power_watts=power,
        electricity_price_kwh=elec_price, usdt_to_arb_fee=usdt_arb_fee,
        coinbase_fee_arb=coinbase_fee, arb_to_usd_pct=arb_pct,
        arb_to_usd_flat=arb_flat,
    )
    r.electricity_cost = (power * time_h / 1000.0) * elec_price
    r.gross_usdt = coins * prl_price
    r.net_usdt = max(r.gross_usdt - usdt_arb_fee, 0.0)
    r.arb_amount = r.net_usdt / arb_price if arb_price > 0 else 0.0
    r.arb_after_withdrawal = max(r.arb_amount - coinbase_fee, 0.0)
    r.gross_usd_from_arb = r.arb_after_withdrawal * arb_price
    r.arb_pct_fee = r.gross_usd_from_arb * (arb_pct / 100.0)
    r.arb_conv_fee_total = arb_flat + r.arb_pct_fee
    r.final_usd = r.gross_usd_from_arb - r.arb_conv_fee_total
    r.net_profit = r.final_usd - r.electricity_cost
    r.profit_per_hour = r.net_profit / time_h if time_h > 0 else 0.0
    r.profit_per_coin = r.net_profit / coins if coins > 0 else 0.0
    r.total_fees_usd = (
        r.electricity_cost + usdt_arb_fee
        + (coinbase_fee * arb_price) + r.arb_conv_fee_total
    )
    r.cost_ratio = (r.total_fees_usd / r.gross_usdt * 100.0) if r.gross_usdt > 0 else 0.0
    return r


def run_breakeven(
    coins: float, time_h: float, power: float,
    elec_price: float, arb_price: float,
    usdt_arb_fee: float = DEFAULT_USDT_TO_ARB_FEE,
    coinbase_fee: float = DEFAULT_COINBASE_FEE_ARB,
    arb_pct: float = DEFAULT_ARB_TO_USD_PCT,
    arb_flat: float = DEFAULT_ARB_TO_USD_FLAT,
) -> BreakevenResult:
    b = BreakevenResult(
        coins_mined=coins, time_hours=time_h, power_watts=power,
        electricity_price_kwh=elec_price, arb_price_usd=arb_price,
    )
    b.electricity_cost = (power * time_h / 1000.0) * elec_price
    fee_factor = 1.0 - (arb_pct / 100.0)
    b.breakeven_price = (
        ((b.electricity_cost + arb_flat) / fee_factor)
        + usdt_arb_fee
        + (coinbase_fee * arb_price)
    ) / coins
    b.total_cost_usd = (
        b.electricity_cost
        + (usdt_arb_fee * fee_factor)
        + (coinbase_fee * arb_price * fee_factor)
        + arb_flat
    )
    return b


# ── Daily mining (tab 3) — coins + electricity only ──────────

@dataclass
class DailyMiningEntry:
    date: str
    coins_mined: float
    prl_price: float
    power: float
    elec_price: float
    time_hours: float
    gross_revenue: float = 0.0
    electricity_cost: float = 0.0
    net_profit: float = 0.0

    def to_dict(self) -> dict:
        return {
            "date": self.date, "coins_mined": self.coins_mined,
            "prl_price": self.prl_price, "power": self.power,
            "elec_price": self.elec_price, "time_hours": self.time_hours,
            "gross_revenue": self.gross_revenue,
            "electricity_cost": self.electricity_cost,
            "net_profit": self.net_profit,
        }

    @staticmethod
    def from_dict(d: dict) -> "DailyMiningEntry":
        return DailyMiningEntry(**d)


def calc_daily_mining(coins: float, prl_price: float, power: float,
                      elec_price: float, time_h: float) -> DailyMiningEntry:
    elec_cost = (power * time_h / 1000.0) * elec_price
    gross = coins * prl_price
    return DailyMiningEntry(
        date="", coins_mined=coins, prl_price=prl_price,
        power=power, elec_price=elec_price, time_hours=time_h,
        gross_revenue=gross, electricity_cost=elec_cost,
        net_profit=gross - elec_cost,
    )


# ── Sales log (tab 4) — supports drafts / partial entries ────

@dataclass
class SalesEntry:
    date: str = ""
    prl_amount: float = 0.0
    prl_price: float = 0.0
    usdt_received: float = 0.0
    arb_received: float = 0.0
    arb_price: float = 0.0
    usd_received: float = 0.0
    # Computed
    gross_expected_usd: float = 0.0
    total_conversion_fees: float = 0.0
    effective_rate: float = 0.0
    total_drag_pct: float = 0.0
    # State
    draft: bool = True

    def to_dict(self) -> dict:
        return {
            "date": self.date, "prl_amount": self.prl_amount,
            "prl_price": self.prl_price, "usdt_received": self.usdt_received,
            "arb_received": self.arb_received, "arb_price": self.arb_price,
            "usd_received": self.usd_received,
            "gross_expected_usd": self.gross_expected_usd,
            "total_conversion_fees": self.total_conversion_fees,
            "effective_rate": self.effective_rate,
            "total_drag_pct": self.total_drag_pct,
            "draft": self.draft,
        }

    @staticmethod
    def from_dict(d: dict) -> "SalesEntry":
        return SalesEntry(**d)

    @property
    def is_complete(self) -> bool:
        """A sale is 'complete' when USD received has been entered."""
        return self.usd_received > 0 and self.prl_amount > 0

    @property
    def is_draft(self) -> bool:
        return not self.is_complete or self.draft


def analyze_sale(
    prl_amount: float = 0.0,
    prl_price: float = 0.0,
    usdt_received: float = 0.0,
    arb_received: float = 0.0,
    arb_price: float = 0.0,
    usd_received: float = 0.0,
    draft: bool = True,
) -> SalesEntry:
    e = SalesEntry(
        prl_amount=prl_amount, prl_price=prl_price,
        usdt_received=usdt_received, arb_received=arb_received,
        arb_price=arb_price, usd_received=usd_received,
        draft=draft,
    )

    # Gross expected (PRL * price)
    if prl_amount > 0 and prl_price > 0:
        e.gross_expected_usd = prl_amount * prl_price

    # Drag / fees / effective rate — only if we have USD received
    if e.gross_expected_usd > 0 and usd_received > 0:
        e.total_conversion_fees = e.gross_expected_usd - usd_received
        e.total_drag_pct = (e.total_conversion_fees / e.gross_expected_usd) * 100.0
        e.effective_rate = usd_received / prl_amount if prl_amount > 0 else 0.0
        e.draft = False  # auto-finalize when USD received is set

    return e


# ── Drag analysis (tab 5) ────────────────────────────────────

@dataclass
class DragResult:
    prl_to_usdt_drag_pct: float = 0.0
    usdt_to_arb_drag_pct: float = 0.0
    arb_to_usd_drag_pct: float = 0.0
    total_drag_pct: float = 0.0
    effective_rate: float = 0.0


def analyze_drag(
    prl_amount: float, prl_price_usdt: float,
    usdt_received: float, arb_received: float,
    arb_price_usd: float, usd_received: float,
) -> DragResult:
    d = DragResult()
    if prl_amount <= 0 or prl_price_usdt <= 0:
        return d
    expected_usdt = prl_amount * prl_price_usdt
    if expected_usdt > 0:
        d.prl_to_usdt_drag_pct = ((expected_usdt - usdt_received) / expected_usdt) * 100.0
    if arb_price_usd > 0 and usdt_received > 0:
        expected_arb = usdt_received / arb_price_usd
        if expected_arb > 0:
            d.usdt_to_arb_drag_pct = ((expected_arb - arb_received) / expected_arb) * 100.0
    if arb_received > 0 and arb_price_usd > 0:
        expected_usd = arb_received * arb_price_usd
        if expected_usd > 0:
            d.arb_to_usd_drag_pct = ((expected_usd - usd_received) / expected_usd) * 100.0
    initial_value = prl_amount * prl_price_usdt
    if initial_value > 0:
        d.total_drag_pct = ((initial_value - usd_received) / initial_value) * 100.0
        d.effective_rate = usd_received / prl_amount
    return d


# ── Persistence ──────────────────────────────────────────────

def load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def save_json(path: Path, data: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_daily_log() -> list[DailyMiningEntry]:
    return [DailyMiningEntry.from_dict(e) for e in load_json(DAILY_LOG)]


def save_daily_log(entries: list[DailyMiningEntry]) -> None:
    save_json(DAILY_LOG, [e.to_dict() for e in entries])


def load_sales_log() -> list[SalesEntry]:
    return [SalesEntry.from_dict(e) for e in load_json(SALES_LOG)]


def save_sales_log(entries: list[SalesEntry]) -> None:
    save_json(SALES_LOG, [e.to_dict() for e in entries])


# ══════════════════════════════════════════════════════════════
# CURSES UI HELPERS
# ══════════════════════════════════════════════════════════════

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)       # section titles
    curses.init_pair(2, curses.COLOR_GREEN, -1)      # labels / positive
    curses.init_pair(3, curses.COLOR_RED, -1)        # negative / errors
    curses.init_pair(4, curses.COLOR_YELLOW, -1)     # highlight / active
    curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)  # header bg
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)    # secondary info
    curses.init_pair(7, curses.COLOR_WHITE, -1)      # normal
    curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_YELLOW)  # draft badge


def draw_header(stdscr, title: str, tabs: list[str], active_tab: int):
    h, w = stdscr.getmaxyx()
    try:
        stdscr.addstr(0, 0, " " * w, curses.color_pair(5) | curses.A_BOLD)
        stdscr.addstr(0, 2, title, curses.color_pair(5) | curses.A_BOLD)
    except curses.error:
        pass
    tab_y = 1
    try:
        stdscr.addstr(tab_y, 0, " " * w, curses.color_pair(7))
        tx = 2
        for i, tab_name in enumerate(tabs):
            label = f" {i+1}:{tab_name} "
            if i == active_tab:
                stdscr.addstr(tab_y, tx, label, curses.color_pair(4) | curses.A_BOLD | curses.A_REVERSE)
            else:
                stdscr.addstr(tab_y, tx, label, curses.color_pair(7))
            tx += len(label) + 1
    except curses.error:
        pass
    try:
        stdscr.addstr(2, 0, "─" * w, curses.color_pair(4))
    except curses.error:
        pass


def safe_addstr(stdscr, y: int, x: int, text: str, attr=0):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0:
        return
    max_len = w - x
    if max_len <= 0:
        return
    try:
        stdscr.addstr(y, x, text[:max_len], attr)
    except curses.error:
        pass


class InputForm:
    def __init__(self, fields: list[tuple[str, str, str]]):
        self.fields_def = fields  # (key, label, default)
        self.values: dict[str, str] = {k: v for k, _, v in fields}
        self.cursor = 0

    def draw(self, stdscr, start_y: int, start_x: int, labels: dict[str, str] | None = None):
        for i, (key, default_label, _) in enumerate(self.fields_def):
            label = labels.get(key, default_label) if labels else default_label
            val = self.values.get(key, "")
            if i == self.cursor:
                safe_addstr(stdscr, start_y + i, start_x,
                            f" > {label}: {val}",
                            curses.color_pair(4) | curses.A_REVERSE)
            else:
                safe_addstr(stdscr, start_y + i, start_x,
                            f"   {label}: {val}",
                            curses.color_pair(2))

    def get_float(self, key: str) -> float:
        try:
            return float(self.values.get(key, "0") or "0")
        except ValueError:
            return 0.0

    def handle_key(self, key: int) -> bool:
        if key == curses.KEY_UP:
            self.cursor = max(0, self.cursor - 1)
            return True
        elif key == curses.KEY_DOWN or key == ord('\t'):
            self.cursor = min(len(self.fields_def) - 1, self.cursor + 1)
            return True
        elif key == curses.KEY_BACKSPACE or key == 127:
            k = self.fields_def[self.cursor][0]
            self.values[k] = self.values[k][:-1]
            return True
        elif key == curses.KEY_DC:  # delete key
            k = self.fields_def[self.cursor][0]
            self.values[k] = ""
            return True
        elif 32 <= key <= 126:
            k = self.fields_def[self.cursor][0]
            self.values[k] += chr(key)
            return True
        return False

    def load_from_entry(self, entry):
        """Populate form from a SalesEntry for editing."""
        self.values["date"] = str(getattr(entry, "date", ""))
        self.values["prl_amount"] = str(getattr(entry, "prl_amount", ""))
        self.values["prl_price"] = str(getattr(entry, "prl_price", ""))
        self.values["usdt_received"] = str(getattr(entry, "usdt_received", ""))
        self.values["arb_received"] = str(getattr(entry, "arb_received", ""))
        self.values["arb_price"] = str(getattr(entry, "arb_price", ""))
        self.values["usd_received"] = str(getattr(entry, "usd_received", ""))


# ══════════════════════════════════════════════════════════════
# SALES FORM FIELD DEFS (shared between add & edit modes)
# ══════════════════════════════════════════════════════════════

SALES_FIELDS = [
    ("date",          "Date (YYYY-MM-DD)",        ""),
    ("prl_amount",    "PRL amount",                ""),
    ("prl_price",     "PRL price (USDT)",          ""),
    ("usdt_received", "USDT received (0 if pending)", ""),
    ("arb_received",  "ARB received (0 if pending)",  ""),
    ("arb_price",     "ARB price (USD)",           ""),
    ("usd_received",  "USD received (0 if pending)",  ""),
]


# ══════════════════════════════════════════════════════════════
# TAB SCREENS
# ══════════════════════════════════════════════════════════════

def tab_profitability(stdscr):
    """Tab 1: Full profitability (PRL -> USDT -> ARB -> USD)."""
    fields = InputForm([
        ("coins",      "Coins mined (PRL)",    "100"),
        ("time",       "Mining time (hours)",   "24"),
        ("power",      "Power (watts)",         "800"),
        ("elec_price", "Electricity (USD/kWh)", "0.10"),
        ("prl_price",  "PRL price (USDT)",      "0.05"),
        ("arb_price",  "ARB price (USD)",       "0.30"),
    ])
    result: MiningResult | None = None
    message = "'c'=calculate | Tab=switch | 'q'=quit"

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.clear()
        draw_header(stdscr, "PRL Mining Toolkit",
                    ["Profit", "Breakeven", "Daily", "Sales", "Drag"], 0)

        safe_addstr(stdscr, 4, 2, "FULL CONVERSION PROFITABILITY", curses.color_pair(1) | curses.A_BOLD)
        fields.draw(stdscr, 5, 4)

        if result:
            ry, rx = 5, 44
            safe_addstr(stdscr, ry, rx, "RESULTS", curses.color_pair(1) | curses.A_BOLD)
            ry += 1
            lines = [
                f"  Gross USDT:         {result.gross_usdt:.4f}",
                f"  Net USDT:           {result.net_usdt:.4f}",
                f"  ARB amount:         {result.arb_amount:.4f}",
                f"  ARB after fee:      {result.arb_after_withdrawal:.4f}",
                f"  Gross USD from ARB: {result.gross_usd_from_arb:.4f}",
                f"  Final USD:          {result.final_usd:.4f}",
                "",
                f"  Electricity:        ${result.electricity_cost:.4f}",
                f"  USDT->ARB fee:      ${result.usdt_to_arb_fee:.2f}",
                f"  Coinbase fee:       ${result.coinbase_fee_arb * result.arb_price_usd:.4f}",
                f"  ARB->USD fee:       ${result.arb_conv_fee_total:.4f}",
                f"  Total costs:        ${result.total_fees_usd:.4f}",
                "",
                f"  Net profit:         ${result.net_profit:.4f}",
                f"  Profit/coin:        ${result.profit_per_coin:.6f}",
                f"  Profit/hour:        ${result.profit_per_hour:.4f}",
                f"  Cost ratio:         {result.cost_ratio:.1f}%",
            ]
            for line in lines:
                c = (curses.color_pair(2) if "Net profit" in line and result.net_profit >= 0
                     else curses.color_pair(3) if "Net profit" in line
                     else curses.color_pair(7))
                safe_addstr(stdscr, ry, rx, line, c)
                ry += 1
            status = "PROFITABLE" if result.net_profit > 0 else "UNPROFITABLE"
            sc = curses.color_pair(2) | curses.A_BOLD if result.net_profit > 0 else curses.color_pair(3) | curses.A_BOLD
            safe_addstr(stdscr, ry + 1, rx, f"  >>> {status} <<<", sc)

        safe_addstr(stdscr, h - 2, 2, message, curses.color_pair(6))
        stdscr.refresh()
        key = stdscr.getch()

        if key == ord('q') or key == ord('Q'):
            return
        elif key == 9:  # Tab
            return "next_tab"
        elif key == ord('c') or key == ord('C'):
            result = run_calculation(
                coins=fields.get_float("coins"), time_h=fields.get_float("time"),
                power=fields.get_float("power"), elec_price=fields.get_float("elec_price"),
                prl_price=fields.get_float("prl_price"), arb_price=fields.get_float("arb_price"),
            )
            message = f"Net: ${result.net_profit:.4f} | 'c'=recalculate | Tab=switch"
        else:
            fields.handle_key(key)


def tab_breakeven(stdscr):
    """Tab 2: Breakeven calculator."""
    fields = InputForm([
        ("coins",      "Coins mined (PRL)",    "100"),
        ("time",       "Mining time (hours)",   "24"),
        ("power",      "Power (watts)",         "800"),
        ("elec_price", "Electricity (USD/kWh)", "0.10"),
        ("arb_price",  "ARB price (USD)",       "0.30"),
    ])
    result: BreakevenResult | None = None
    message = "'c'=calculate | Tab=switch | 'q'=quit"

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.clear()
        draw_header(stdscr, "PRL Mining Toolkit",
                    ["Profit", "Breakeven", "Daily", "Sales", "Drag"], 1)

        safe_addstr(stdscr, 4, 2, "BREAK EVEN CALCULATOR", curses.color_pair(1) | curses.A_BOLD)
        fields.draw(stdscr, 5, 4)

        if result:
            ry, rx = 5, 44
            safe_addstr(stdscr, ry, rx, "COST BREAKDOWN", curses.color_pair(1) | curses.A_BOLD)
            ry += 1
            ff = 1.0 - DEFAULT_ARB_TO_USD_PCT / 100.0
            lines = [
                f"  Electricity:             ${result.electricity_cost:.4f}",
                f"  USDT->ARB fee (equiv):   ${DEFAULT_USDT_TO_ARB_FEE * ff:.4f}",
                f"  Coinbase fee (equiv):    ${DEFAULT_COINBASE_FEE_ARB * result.arb_price_usd * ff:.4f}",
                f"  ARB->USD flat fee:       ${DEFAULT_ARB_TO_USD_FLAT:.2f}",
                f"  Total to recover:        ${result.total_cost_usd:.4f}",
            ]
            for line in lines:
                safe_addstr(stdscr, ry, rx, line, curses.color_pair(7))
                ry += 1
            safe_addstr(stdscr, ry + 1, rx, "══════════════════════════════════", curses.color_pair(4))
            safe_addstr(stdscr, ry + 2, rx, "  BREAK EVEN:", curses.color_pair(4) | curses.A_BOLD)
            safe_addstr(stdscr, ry + 3, rx,
                        f"  ${result.breakeven_price:.6f} USDT/PRL",
                        curses.color_pair(2) | curses.A_BOLD | curses.A_REVERSE)
            v = run_calculation(
                coins=fields.get_float("coins"), time_h=fields.get_float("time"),
                power=fields.get_float("power"), elec_price=fields.get_float("elec_price"),
                prl_price=result.breakeven_price, arb_price=fields.get_float("arb_price"),
            )
            safe_addstr(stdscr, ry + 5, rx, f"  Verify: net = ${v.net_profit:.6f}", curses.color_pair(6))

        safe_addstr(stdscr, h - 2, 2, message, curses.color_pair(6))
        stdscr.refresh()
        key = stdscr.getch()

        if key == ord('q') or key == ord('Q'):
            return
        elif key == 9:
            return "next_tab"
        elif key == ord('c') or key == ord('C'):
            result = run_breakeven(
                coins=fields.get_float("coins"), time_h=fields.get_float("time"),
                power=fields.get_float("power"), elec_price=fields.get_float("elec_price"),
                arb_price=fields.get_float("arb_price"),
            )
            message = f"Breakeven: ${result.breakeven_price:.6f} USDT/PRL"
        else:
            fields.handle_key(key)


def tab_daily(stdscr):
    """Tab 3: Daily mining tracker — coins + electricity only."""
    entries = load_daily_log()
    fields = InputForm([
        ("date",       "Date (YYYY-MM-DD)",     date.today().isoformat()),
        ("coins",      "Coins mined (PRL)",      ""),
        ("prl_price",  "PRL spot price (USDT)",  ""),
        ("power",      "Power (watts)",          "800"),
        ("elec_price", "Electricity (USD/kWh)",  "0.10"),
        ("time",       "Mining time (hours)",    "24"),
    ])
    message = "'a'=add | 'd'=delete | '`'=focus list/form | Tab=switch | 'q'=quit"
    sel = 0
    scroll = 0
    focus = 0  # 0 = form, 1 = list

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.clear()
        draw_header(stdscr, "PRL Mining Toolkit",
                    ["Profit", "Breakeven", "Daily", "Sales", "Drag"], 2)

        # Left panel: form
        form_label = "DAILY MINING ENTRY" + (" ◄" if focus == 0 else "")
        form_title_attr = curses.color_pair(1) | curses.A_BOLD | curses.A_REVERSE if focus == 0 else curses.color_pair(1) | curses.A_BOLD
        safe_addstr(stdscr, 4, 2, form_label, form_title_attr)
        fields.draw(stdscr, 5, 4)

        # Live preview
        coins = fields.get_float("coins")
        if coins > 0:
            prev = calc_daily_mining(
                coins=coins, prl_price=fields.get_float("prl_price"),
                power=fields.get_float("power"), elec_price=fields.get_float("elec_price"),
                time_h=fields.get_float("time"),
            )
            py = 12
            safe_addstr(stdscr, py, 4, "── Preview ──", curses.color_pair(4))
            safe_addstr(stdscr, py + 1, 4, f"  Gross:    ${prev.gross_revenue:.4f}", curses.color_pair(7))
            safe_addstr(stdscr, py + 2, 4, f"  Electric: ${prev.electricity_cost:.4f}", curses.color_pair(7))
            pc = curses.color_pair(2) if prev.net_profit >= 0 else curses.color_pair(3)
            safe_addstr(stdscr, py + 3, 4, f"  Net:      ${prev.net_profit:.4f}", pc | curses.A_BOLD)

        # Right panel: list
        lx = 42
        list_label = "MINING LOG" + (" ◄" if focus == 1 else "")
        list_title_attr = curses.color_pair(1) | curses.A_BOLD | curses.A_REVERSE if focus == 1 else curses.color_pair(1) | curses.A_BOLD
        safe_addstr(stdscr, 4, lx, list_label, list_title_attr)
        total_coins = sum(e.coins_mined for e in entries)
        total_net = sum(e.net_profit for e in entries)
        avg = total_net / len(entries) if entries else 0.0
        sy = 5
        safe_addstr(stdscr, sy, lx, f"  Days:   {len(entries)}", curses.color_pair(7))
        safe_addstr(stdscr, sy + 1, lx, f"  Coins:  {total_coins:.4f}", curses.color_pair(7))
        tc = curses.color_pair(2) if total_net >= 0 else curses.color_pair(3)
        safe_addstr(stdscr, sy + 2, lx, f"  Net:    ${total_net:.4f}", tc | curses.color_pair(7))
        ac = curses.color_pair(2) if avg >= 0 else curses.color_pair(3)
        safe_addstr(stdscr, sy + 3, lx, f"  Avg/day:${avg:.4f}", ac)

        lsy = sy + 5
        max_vis = h - lsy - 3
        if sel < scroll:
            scroll = sel
        elif sel >= scroll + max_vis:
            scroll = sel - max_vis + 1
        for i in range(max_vis):
            idx = scroll + i
            if idx >= len(entries):
                break
            e = entries[idx]
            pc = curses.color_pair(2) if e.net_profit >= 0 else curses.color_pair(3)
            is_active_sel = (idx == sel and focus == 1)
            attr = curses.color_pair(4) | curses.A_REVERSE if is_active_sel else pc
            marker = ">" if is_active_sel else (" " if idx != sel or focus != 1 else "·")
            line = f"{marker} {e.date}  {e.coins_mined:>10.4f}  ${e.net_profit:>8.4f}"
            safe_addstr(stdscr, lsy + i, lx, line, attr)

        # Focus hint
        focus_hint = "FORM" if focus == 0 else "LIST"
        safe_addstr(stdscr, h - 3, 2,
                     f" Focus: [{focus_hint}]  (` to toggle)",
                     curses.color_pair(4))

        safe_addstr(stdscr, h - 2, 2, message, curses.color_pair(6))
        stdscr.refresh()
        key = stdscr.getch()

        if key == ord('q') or key == ord('Q'):
            return
        elif key == 9:  # Tab
            return "next_tab"
        elif key == ord('`'):
            focus = 1 - focus
            if focus == 1 and not entries:
                focus = 0
                message = "No entries to select — add one first"
        elif focus == 1:
            # List navigation
            if key == curses.KEY_UP and entries:
                sel = max(0, sel - 1)
            elif key == curses.KEY_DOWN and entries:
                sel = min(len(entries) - 1, sel + 1)
            elif key == ord('d') or key == 'D':
                if 0 <= sel < len(entries):
                    removed = entries.pop(sel)
                    save_daily_log(entries)
                    message = f"Deleted {removed.date}"
                    sel = min(sel, max(0, len(entries) - 1))
                    if not entries:
                        focus = 0
            else:
                # Still allow typing in form even when list is focused
                fields.handle_key(key)
        else:
            # Form navigation
            if key == curses.KEY_UP:
                fields.cursor = max(0, fields.cursor - 1)
            elif key == curses.KEY_DOWN:
                fields.cursor = min(len(fields.fields_def) - 1, fields.cursor + 1)
            elif key == ord('a') or key == 'A':
                if fields.get_float("coins") <= 0:
                    message = "Error: coins must be > 0"
                else:
                    entry = calc_daily_mining(
                        coins=fields.get_float("coins"),
                        prl_price=fields.get_float("prl_price"),
                        power=fields.get_float("power"),
                        elec_price=fields.get_float("elec_price"),
                        time_h=fields.get_float("time"),
                    )
                    entry.date = fields.values.get("date", date.today().isoformat())
                    entries.append(entry)
                    save_daily_log(entries)
                    message = f"Added {entry.date}: ${entry.net_profit:.4f} net"
            else:
                fields.handle_key(key)


def _save_sale_entry(form, entries, editing, edit_idx, save_fn):
    """Shared save logic for the Sales tab. Returns a status message."""
    pa = form.get_float("prl_amount")
    if pa <= 0:
        return "Error: PRL amount must be > 0"
    draft = form.get_float("usd_received") <= 0
    entry = analyze_sale(
        prl_amount=pa, prl_price=form.get_float("prl_price"),
        usdt_received=form.get_float("usdt_received"),
        arb_received=form.get_float("arb_received"),
        arb_price=form.get_float("arb_price"),
        usd_received=form.get_float("usd_received"),
        draft=draft,
    )
    entry.date = form.values.get("date", date.today().isoformat())
    if editing and 0 <= edit_idx < len(entries):
        entries[edit_idx] = entry
        msg = f"Updated sale {entry.date}"
    else:
        entries.append(entry)
        status = "draft saved" if draft else "sale recorded"
        msg = f"{status}: {entry.date} {pa:.4f} PRL"
    save_fn(entries)
    return msg


def tab_sales(stdscr):
    """Tab 4: Sales tracker — supports drafts / multi-hour edit cycles."""
    entries = load_sales_log()
    form = InputForm(list(SALES_FIELDS))
    message = "'a'=save | 'e'=edit | 'd'=delete | '`'=focus | Tab=switch | 'q'=quit"
    sel = 0
    scroll = 0
    focus = 0  # 0 = form, 1 = list
    editing = False  # True when editing an existing entry
    edit_idx = -1
    preview: SalesEntry | None = None

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.clear()
        draw_header(stdscr, "PRL Mining Toolkit",
                    ["Profit", "Breakeven", "Daily", "Sales", "Drag"], 3)

        # Left: form
        mode_label = ("EDIT SALE" if editing else "NEW SALE") + (" ◄" if focus == 0 else "")
        mode_attr = curses.color_pair(1) | curses.A_BOLD
        if focus == 0:
            mode_attr |= curses.A_REVERSE
        if editing:
            mode_attr |= curses.A_BLINK
        safe_addstr(stdscr, 4, 2, mode_label, mode_attr)
        form.draw(stdscr, 5, 4)

        # Live preview
        pa = form.get_float("prl_amount")
        pp = form.get_float("prl_price")
        if pa > 0 and pp > 0:
            preview = analyze_sale(
                prl_amount=pa, prl_price=pp,
                usdt_received=form.get_float("usdt_received"),
                arb_received=form.get_float("arb_received"),
                arb_price=form.get_float("arb_price"),
                usd_received=form.get_float("usd_received"),
            )
            py = 13
            safe_addstr(stdscr, py, 4, "── Preview ──", curses.color_pair(4))
            safe_addstr(stdscr, py + 1, 4,
                        f"  Expected (gross): ${preview.gross_expected_usd:.4f}",
                        curses.color_pair(7))
            safe_addstr(stdscr, py + 2, 4,
                        f"  USD received:    ${preview.usd_received:.4f}",
                        curses.color_pair(7))
            if preview.usd_received > 0:
                safe_addstr(stdscr, py + 3, 4,
                            f"  Conversion fees: ${preview.total_conversion_fees:.4f}",
                            curses.color_pair(3))
                safe_addstr(stdscr, py + 4, 4,
                            f"  Total drag:      {preview.total_drag_pct:.2f}%",
                            curses.color_pair(3))
                safe_addstr(stdscr, py + 5, 4,
                            f"  Effective rate:  ${preview.effective_rate:.6f}/PRL",
                            curses.color_pair(2) | curses.A_BOLD)
            else:
                safe_addstr(stdscr, py + 3, 4,
                            "  [DRAFT — fill in as conversion progresses]",
                            curses.color_pair(8) | curses.A_BOLD)

        # Right: list
        lx = 48
        list_label = "SALES LOG" + (" ◄" if focus == 1 else "")
        list_attr = curses.color_pair(1) | curses.A_BOLD | (curses.A_REVERSE if focus == 1 else 0)
        safe_addstr(stdscr, 4, lx, list_label, list_attr)

        total_prl = sum(e.prl_amount for e in entries)
        total_usd = sum(e.usd_received for e in entries)
        total_fees = sum(e.total_conversion_fees for e in entries)
        completed = [e for e in entries if e.is_complete]
        avg_drag = (sum(e.total_drag_pct for e in completed) / len(completed)) if completed else 0.0
        avg_eff = (sum(e.effective_rate for e in completed) / len(completed)) if completed else 0.0
        drafts = sum(1 for e in entries if e.is_draft)

        sy = 5
        safe_addstr(stdscr, sy, lx, f"  Sales:     {len(entries)} ({drafts} drafts)", curses.color_pair(7))
        safe_addstr(stdscr, sy + 1, lx, f"  Total PRL: {total_prl:.4f}", curses.color_pair(7))
        safe_addstr(stdscr, sy + 2, lx, f"  USD recv:  ${total_usd:.4f}", curses.color_pair(2) | curses.A_BOLD)
        safe_addstr(stdscr, sy + 3, lx, f"  Conv fees: ${total_fees:.4f}", curses.color_pair(3))
        safe_addstr(stdscr, sy + 4, lx, f"  Avg drag:  {avg_drag:.2f}%", curses.color_pair(6))
        safe_addstr(stdscr, sy + 5, lx, f"  Avg eff:   ${avg_eff:.6f}/PRL", curses.color_pair(6))

        # Scrollable list
        lsy = sy + 7
        max_vis = h - lsy - 3
        if sel < scroll:
            scroll = sel
        elif sel >= scroll + max_vis:
            scroll = sel - max_vis + 1
        for i in range(max_vis):
            idx = scroll + i
            if idx >= len(entries):
                break
            e = entries[idx]
            is_active_sel = (idx == sel and focus == 1)
            badge = "[DRAFT]" if e.is_draft else " [DONE]"
            badge_attr = curses.color_pair(8) | curses.A_BOLD if e.is_draft else curses.color_pair(2)
            marker = ">" if is_active_sel else ("·" if idx == sel and focus == 0 else " ")
            line = (f"{marker} {e.date}  {e.prl_amount:>9.4f} PRL  "
                    f"${e.usd_received:>8.4f}")
            row_attr = curses.color_pair(4) | curses.A_REVERSE if is_active_sel else curses.color_pair(7)
            safe_addstr(stdscr, lsy + i, lx, line, row_attr)
            safe_addstr(stdscr, lsy + i, lx + len(line) + 1, badge, badge_attr)

        # Focus hint
        focus_hint = "FORM" if focus == 0 else "LIST"
        safe_addstr(stdscr, h - 3, 2,
                     f" Focus: [{focus_hint}]  (` to toggle)",
                     curses.color_pair(4))

        safe_addstr(stdscr, h - 2, 2, message, curses.color_pair(6))
        stdscr.refresh()
        key = stdscr.getch()

        if key == ord('q') or key == ord('Q'):
            return
        elif key == 9:  # Tab between main tabs
            return "next_tab"
        elif key == ord('`'):
            focus = 1 - focus
            if focus == 1 and not entries:
                focus = 0
                message = "No entries to select — add one first"
        elif focus == 1:
            # List navigation
            if key == curses.KEY_UP and entries:
                sel = max(0, sel - 1)
            elif key == curses.KEY_DOWN and entries:
                sel = min(len(entries) - 1, sel + 1)
            elif key == ord('e') or key == 'E':
                if 0 <= sel < len(entries):
                    form = InputForm(list(SALES_FIELDS))
                    form.load_from_entry(entries[sel])
                    editing = True
                    edit_idx = sel
                    focus = 0
                    message = f"Editing entry {sel+1} — Tab fields, 'a' to save"
                else:
                    message = "No entry selected"
            elif key == ord('d') or key == 'D':
                if 0 <= sel < len(entries):
                    removed = entries.pop(sel)
                    save_sales_log(entries)
                    message = f"Deleted {removed.date}"
                    sel = min(sel, max(0, len(entries) - 1))
                    if not entries:
                        focus = 0
                    if editing and edit_idx >= len(entries):
                        editing = False
                        edit_idx = -1
            elif key == ord('a') or key == 'A':
                # Save new from list focus too
                _save_sale_entry(form, entries, editing, edit_idx, save_sales_log)
                form = InputForm(list(SALES_FIELDS))
                editing = False
                edit_idx = -1
            else:
                form.handle_key(key)
        else:
            # Form navigation
            if key == curses.KEY_UP:
                form.cursor = max(0, form.cursor - 1)
            elif key == curses.KEY_DOWN:
                form.cursor = min(len(form.fields_def) - 1, form.cursor + 1)
            elif key == ord('a') or key == 'A' or key == ord('s') or key == 'S':
                msg = _save_sale_entry(form, entries, editing, edit_idx, save_sales_log)
                message = msg
                form = InputForm(list(SALES_FIELDS))
                editing = False
                edit_idx = -1
            elif key == ord('e') or key == 'E':
                if 0 <= sel < len(entries):
                    form = InputForm(list(SALES_FIELDS))
                    form.load_from_entry(entries[sel])
                    editing = True
                    edit_idx = sel
                    message = f"Editing entry {sel+1} — Tab fields, 'a' to save"
                else:
                    message = "No entry selected in list"
            else:
                form.handle_key(key)


def tab_drag(stdscr):
    """Tab 5: Drag analysis — back-calculate per-step drag."""
    fields = InputForm([
        ("prl_amount",    "PRL amount sent",        ""),
        ("prl_price",     "PRL price (USDT)",       ""),
        ("usdt_received", "USDT actually received", ""),
        ("arb_received",  "ARB actually received",  ""),
        ("arb_price",     "ARB price (USD)",        ""),
        ("usd_received",  "USD actually received",  ""),
    ])
    result: DragResult | None = None
    message = "'c'=analyze | Tab=switch | 'q'=quit"

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.clear()
        draw_header(stdscr, "PRL Mining Toolkit",
                    ["Profit", "Breakeven", "Daily", "Sales", "Drag"], 4)

        safe_addstr(stdscr, 4, 2, "DRAG ANALYSIS (enter actual results)", curses.color_pair(1) | curses.A_BOLD)
        fields.draw(stdscr, 5, 4)

        if result:
            ry, rx = 5, 48
            safe_addstr(stdscr, ry, rx, "PER-STEP DRAG", curses.color_pair(1) | curses.A_BOLD)
            ry += 1

            pa = fields.get_float("prl_amount")
            pp = fields.get_float("prl_price")
            ur = fields.get_float("usdt_received")
            ar = fields.get_float("arb_received")
            ap = fields.get_float("arb_price")
            udr = fields.get_float("usd_received")

            exp_usdt = pa * pp
            exp_arb = ur / ap if ap > 0 else 0
            exp_usd = ar * ap if ap > 0 else 0

            lines = [
                "",
                "  Step 1: PRL -> USDT",
                f"    Expected:  {exp_usdt:.4f} USDT",
                f"    Received:  {ur:.4f} USDT",
                f"    Drag:      {result.prl_to_usdt_drag_pct:.3f}%  (${exp_usdt - ur:.4f})",
                "",
                "  Step 2: USDT -> ARB",
                f"    Expected:  {exp_arb:.4f} ARB",
                f"    Received:  {ar:.4f} ARB",
                f"    Drag:      {result.usdt_to_arb_drag_pct:.3f}%  ({exp_arb - ar:.4f} ARB)",
                "",
                "  Step 3: ARB -> USD",
                f"    Expected:  ${exp_usd:.4f} USD",
                f"    Received:  ${udr:.4f} USD",
                f"    Drag:      {result.arb_to_usd_drag_pct:.3f}%  (${exp_usd - udr:.4f})",
                "",
                "  ────────────────────────────────────",
                f"  TOTAL DRAG:  {result.total_drag_pct:.3f}%",
                f"  Effective:   ${result.effective_rate:.6f} USD/PRL",
                f"  (raw price:  ${pp:.6f} USDT/PRL)",
            ]
            for line in lines:
                if "TOTAL DRAG" in line:
                    attr = curses.color_pair(4) | curses.A_BOLD
                elif "Step" in line:
                    attr = curses.color_pair(1) | curses.A_BOLD
                elif "Drag:" in line:
                    attr = curses.color_pair(3)
                elif "Effective:" in line:
                    attr = curses.color_pair(2) | curses.A_BOLD
                else:
                    attr = curses.color_pair(7)
                safe_addstr(stdscr, ry, rx, line, attr)
                ry += 1

        safe_addstr(stdscr, h - 2, 2, message, curses.color_pair(6))
        stdscr.refresh()
        key = stdscr.getch()

        if key == ord('q') or key == ord('Q'):
            return
        elif key == 9:
            return "next_tab"
        elif key == ord('c') or key == 'C':
            result = analyze_drag(
                prl_amount=fields.get_float("prl_amount"),
                prl_price_usdt=fields.get_float("prl_price"),
                usdt_received=fields.get_float("usdt_received"),
                arb_received=fields.get_float("arb_received"),
                arb_price_usd=fields.get_float("arb_price"),
                usd_received=fields.get_float("usd_received"),
            )
            message = f"Total drag: {result.total_drag_pct:.3f}% | Eff: ${result.effective_rate:.6f}/PRL"
        else:
            fields.handle_key(key)


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

TABS = [tab_profitability, tab_breakeven, tab_daily, tab_sales, tab_drag]


def main(stdscr):
    curses.curs_set(0)
    init_colors()
    stdscr.keypad(True)
    stdscr.timeout(100)
    active_tab = 0
    while True:
        result = TABS[active_tab](stdscr)
        if result == "next_tab":
            active_tab = (active_tab + 1) % len(TABS)
        else:
            break


if __name__ == "__main__":
    curses.wrapper(main)
