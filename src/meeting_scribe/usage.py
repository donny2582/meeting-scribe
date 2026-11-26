"""API 使用量與費用估算、每月費用上限。

- 每次呼叫雲端服務後記一筆到 ``%LOCALAPPDATA%\\MeetingScribe\\usage.csv``（UTF-8 含 BOM，可用 Excel 開啟）。
- 費用為本程式估算：語音轉錄＝錄音時數 × 設定的每小時單價；文字整理＝tokens × 模型單價
  （assets/model_catalog.toml）。沒有單價的服務或模型只記用量、不計費用。
- 一般 API 金鑰無法查詢實際帳單（需要管理員金鑰），實際金額以各服務的帳單為準。
- 不記錄任何逐字稿內容。
"""

from __future__ import annotations

import csv
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from meeting_scribe.config import localappdata_dir

log = logging.getLogger(__name__)

COLUMNS = ["時間", "類別", "服務", "模型", "工作", "音訊秒數", "輸入tokens", "輸出tokens", "估計費用USD"]
KIND_STT = "語音轉錄"
KIND_LLM = "文字整理"
SERVICE_NAMES = {"azure": "Azure", "openai": "OpenAI", "soniox": "Soniox", "anthropic": "Anthropic"}
TASK_NAMES = {"localize": "臺灣在地化", "filler": "刪除贅字", "correct": "語意校正與講者角色", "translate": "外語翻譯",
              "summary": "整理結論"}

_lock = threading.Lock()


class BudgetExceeded(Exception):
    """本月費用已達上限。"""


def ledger_path() -> Path:
    return localappdata_dir() / "usage.csv"


@dataclass
class UsageRecord:
    """一筆使用紀錄。"""

    time: datetime
    kind: str
    service: str
    model: str
    task: str = ""
    audio_sec: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None


def record(rec: UsageRecord) -> None:
    """寫入一筆紀錄（多執行緒安全；寫入失敗只記日誌，不影響工作）。"""
    path = ledger_path()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(COLUMNS)
                w.writerow([
                    rec.time.strftime("%Y-%m-%d %H:%M:%S"), rec.kind, rec.service, rec.model, rec.task,
                    f"{rec.audio_sec:.1f}" if rec.audio_sec else "", rec.input_tokens or "", rec.output_tokens or "",
                    "" if rec.cost is None else f"{rec.cost:.6f}",
                ])
    except OSError as exc:
        log.warning("無法寫入使用紀錄：%s", exc)


def record_stt(service: str, model: str, audio_sec: float, price_per_hour: float) -> UsageRecord:
    """記錄一次語音轉錄；單價為 0 表示未設定，只記用量。"""
    cost = audio_sec / 3600 * price_per_hour if price_per_hour > 0 else None
    rec = UsageRecord(datetime.now(), KIND_STT, service, model, audio_sec=audio_sec, cost=cost)
    record(rec)
    return rec


def record_llm(service: str, model: str, task: str, input_tokens: int, output_tokens: int) -> UsageRecord:
    """記錄一次文字整理呼叫；模型單價來自 model_catalog.toml，查不到時只記用量。"""
    from meeting_scribe.models_catalog import token_price

    price = token_price(model)
    cost = (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000 if price else None
    rec = UsageRecord(datetime.now(), KIND_LLM, service, model, task, input_tokens=input_tokens,
                      output_tokens=output_tokens, cost=cost)
    record(rec)
    return rec


def load_records(path: Path | None = None) -> list[UsageRecord]:
    """讀取全部紀錄（格式錯誤的列略過）。"""
    path = path or ledger_path()
    if not path.exists():
        return []
    records: list[UsageRecord] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                records.append(UsageRecord(
                    time=datetime.strptime(row["時間"], "%Y-%m-%d %H:%M:%S"),
                    kind=row["類別"], service=row["服務"], model=row["模型"], task=row.get("工作", ""),
                    audio_sec=float(row["音訊秒數"] or 0), input_tokens=int(row["輸入tokens"] or 0),
                    output_tokens=int(row["輸出tokens"] or 0),
                    cost=float(row["估計費用USD"]) if row["估計費用USD"] else None,
                ))
            except (KeyError, ValueError):
                continue
    return records


@dataclass
class ServiceUsage:
    """某類別＋服務的合計。"""

    kind: str
    service: str
    cost: float = 0.0
    audio_sec: float = 0.0
    tokens: int = 0
    calls: int = 0
    unpriced: int = 0


@dataclass
class UsageSummary:
    """本月與累計的費用。"""

    month: str
    month_cost: float = 0.0
    total_cost: float = 0.0
    by_service: list[ServiceUsage] = field(default_factory=list)
    unpriced_month: int = 0


def summarize(records: list[UsageRecord], now: datetime | None = None) -> UsageSummary:
    """彙總本月（依電腦時間）與全部紀錄。"""
    now = now or datetime.now()
    month = now.strftime("%Y-%m")
    summary = UsageSummary(month=month)
    groups: dict[tuple[str, str], ServiceUsage] = defaultdict(lambda: ServiceUsage("", ""))
    for r in records:
        summary.total_cost += r.cost or 0.0
        if r.time.strftime("%Y-%m") != month:
            continue
        summary.month_cost += r.cost or 0.0
        g = groups[(r.kind, r.service)]
        g.kind, g.service = r.kind, r.service
        g.cost += r.cost or 0.0
        g.audio_sec += r.audio_sec
        g.tokens += r.input_tokens + r.output_tokens
        g.calls += 1
        if r.cost is None:
            g.unpriced += 1
            summary.unpriced_month += 1
    summary.by_service = sorted(groups.values(), key=lambda g: (g.kind != KIND_STT, -g.cost))
    return summary


@dataclass
class BudgetStatus:
    """每月上限的狀態。"""

    limit: float
    used: float
    warn_ratio: float = 0.8

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def ratio(self) -> float:
        return self.used / self.limit if self.enabled else 0.0

    @property
    def exceeded(self) -> bool:
        return self.enabled and self.used >= self.limit

    @property
    def warning(self) -> bool:
        return self.enabled and not self.exceeded and self.ratio >= self.warn_ratio


def budget_status(cfg: dict[str, Any], now: datetime | None = None) -> BudgetStatus:
    """依設定 [budget] 與本月紀錄計算上限狀態。"""
    b = cfg.get("budget", {})
    used = summarize(load_records(), now).month_cost
    return BudgetStatus(float(b.get("monthly_limit_usd", 0) or 0), used, float(b.get("warn_ratio", 0.8)))


def check_budget(cfg: dict[str, Any]) -> BudgetStatus:
    """開始新工作前檢查；已達上限時拋出 BudgetExceeded。"""
    status = budget_status(cfg)
    if status.exceeded:
        raise BudgetExceeded(
            f"本月估計費用 US$ {status.used:.2f} 已達上限 US$ {status.limit:.2f}，"
            "已停止新的雲端工作；如需繼續，請到「API 金鑰」分頁提高上限。"
        )
    return status
