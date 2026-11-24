"""模型清單（assets/model_catalog.toml）與使用者新增的模型。

下拉選單的選項＝內建清單＋使用者按「＋」新增、存在使用者設定檔 ``[models.custom.<類別>]`` 的模型。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from meeting_scribe.config import assets_dir

#: 文字整理處理的工作代碼
TASKS = ("localize", "filler", "translate", "summary")


@dataclass(frozen=True)
class ModelOption:
    """下拉選單的一個選項。"""

    id: str
    label: str
    note: str = ""
    custom: bool = False


@dataclass(frozen=True)
class TaskInfo:
    """文字整理處理的一項工作。"""

    code: str
    label: str
    reason: str
    cost: str
    recommend: dict[str, str]


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    with open(assets_dir() / "model_catalog.toml", "rb") as f:
        return tomllib.load(f)


def token_price(model_id: str) -> tuple[float, float] | None:
    """模型的 token 單價（美元／百萬 tokens：輸入, 輸出）；清單裡沒有的模型回傳 None（費用未知）。"""
    for kind in ("anthropic", "openai"):
        for m in _catalog().get(kind, []):
            if m.get("id") == model_id and "price_in" in m and "price_out" in m:
                return float(m["price_in"]), float(m["price_out"])
    return None


def builtin_options(kind: str) -> list[ModelOption]:
    """內建選項（kind：anthropic、openai、openai_stt、soniox）。"""
    return [ModelOption(m["id"], m.get("label", m["id"]), m.get("note", "")) for m in _catalog().get(kind, [])]


def options(kind: str, cfg: dict[str, Any]) -> list[ModelOption]:
    """內建選項＋從服務取得的模型＋使用者新增的模型（不重複）。"""
    result = builtin_options(kind)
    known = {o.id for o in result}
    for item in cfg.get("models", {}).get("fetched", {}).get(kind, []):
        model_id = item.get("id", "") if isinstance(item, dict) else str(item)
        if model_id and model_id not in known:
            label = item.get("label", model_id) if isinstance(item, dict) else model_id
            result.append(ModelOption(model_id, label, "從服務取得"))
            known.add(model_id)
    for model_id in cfg.get("models", {}).get("custom", {}).get(kind, []):
        if model_id and model_id not in known:
            result.append(ModelOption(model_id, model_id, "自行新增", custom=True))
            known.add(model_id)
    return result


#: 可以「從服務更新模型清單」的類別 → 使用的金鑰
FETCHABLE = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "openai_stt": "openai_api_key",
    "soniox": "soniox_api_key",
}
_OPENAI_NON_TEXT = ("transcribe", "tts", "audio", "realtime", "image", "embedding", "search", "moderation",
                    "whisper", "dall-e", "sora", "codex")


class FetchError(Exception):
    """無法從服務取得模型清單。"""


def fetch_models(kind: str, key: str, cfg: dict[str, Any]) -> list[ModelOption]:
    """用官方「列出模型」API 取得目前可用的模型（比抓網頁穩定，廠商改版網頁也不受影響）。"""
    import httpx

    try:
        if kind == "anthropic":
            import anthropic

            client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=30)
            return [ModelOption(m.id, getattr(m, "display_name", None) or m.id, "從 Anthropic 取得")
                    for m in client.models.list()]
        if kind in ("openai", "openai_stt"):
            base = cfg.get("llm", {}).get("openai", {}).get("base_url", "https://api.openai.com/v1").rstrip("/")
            resp = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
            _check(resp, "OpenAI")
            ids = sorted(m["id"] for m in resp.json().get("data", []))
            if kind == "openai_stt":
                ids = [i for i in ids if "transcribe" in i]
            else:
                ids = [i for i in ids if i.startswith(("gpt-", "o")) and not any(w in i for w in _OPENAI_NON_TEXT)]
            return [ModelOption(i, i, "從 OpenAI 取得") for i in ids]
        if kind == "soniox":
            base = cfg.get("stt", {}).get("soniox", {}).get("base_url", "https://api.soniox.com/v1").rstrip("/")
            resp = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
            _check(resp, "Soniox")
            return [ModelOption(m["id"], m.get("name") or m["id"], "從 Soniox 取得")
                    for m in resp.json().get("models", []) if m.get("transcription_mode") == "async"]
    except FetchError:
        raise
    except Exception as exc:
        if type(exc).__name__ == "AuthenticationError":
            raise FetchError("金鑰無效") from exc
        raise FetchError(f"無法取得模型清單：{type(exc).__name__}") from exc
    raise FetchError(f"「{kind}」不支援更新模型清單")


def _check(resp: Any, what: str) -> None:
    if resp.status_code in (401, 403):
        raise FetchError(f"{what} 金鑰無效或沒有權限")
    if resp.status_code >= 400:
        raise FetchError(f"{what} 回應 HTTP {resp.status_code}")


def tasks() -> list[TaskInfo]:
    """文字整理處理的工作清單（依 order 排序）。"""
    data = _catalog().get("tasks", {})
    items = sorted(data.items(), key=lambda kv: kv[1].get("order", 99))
    return [TaskInfo(code, t.get("label", code), t.get("reason", ""), t.get("cost", ""), dict(t.get("recommend", {})))
            for code, t in items]
