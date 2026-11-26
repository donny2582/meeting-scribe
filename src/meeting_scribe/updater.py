"""從 GitHub Releases 更新或切換版本（「版本」分頁使用）。

- 只查詢設定檔 ``[update] repo`` 指定的官方儲存庫（公開儲存庫，不需要金鑰，不送出任何使用者資料）。
- 只列出 ``min_version``（0.5.4）以後、有安裝檔的版本；更早的版本沒有這個功能，不提供切換。
- 下載後比對 Release 附的 SHA-256 檢查碼，一致才安裝；只接受該儲存庫 Releases 的下載網址。
- 安裝：結束程式 → 以靜默模式執行安裝檔（沿用原本的安裝位置與安裝範圍）→ 安裝完成後自動重新開啟。
  設定、金鑰與會議資料不受影響。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from meeting_scribe import __version__
from meeting_scribe.config import localappdata_dir

log = logging.getLogger(__name__)

API = "https://api.github.com"
USER_AGENT = "MeetingScribe-updater"  # 不帶任何使用者資訊
INSTALLER_RE = re.compile(r"^MeetingScribe_Setup_(\d+(?:\.\d+)*)\.exe$")
ProgressFn = Callable[[float, str], None]


class UpdateError(Exception):
    """查詢、下載或驗證失敗（訊息給使用者看）。"""


def parse_version(text: str) -> tuple[int, ...]:
    """「v0.5.4」「0.5.10」→ (0, 5, 4)；無法解析時回傳空 tuple。"""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def current_version() -> str:
    return __version__


def is_installed() -> bool:
    """打包安裝的版本才能透過這裡更新；開發版請用 git。"""
    return bool(getattr(sys, "frozen", False))


@dataclass
class ReleaseInfo:
    """一個可以安裝的版本。"""

    version: str
    tag: str
    title: str
    published: datetime | None
    prerelease: bool
    notes: str
    installer_url: str
    installer_name: str
    installer_size: int
    checksum_url: str | None
    page_url: str

    @property
    def key(self) -> tuple[int, ...]:
        return parse_version(self.version)


def _settings(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("update", {})


def repo_of(cfg: dict[str, Any]) -> str:
    return str(_settings(cfg).get("repo", "donny2582/meeting-scribe"))


def releases_page(cfg: dict[str, Any]) -> str:
    return f"https://github.com/{repo_of(cfg)}/releases"


def _client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0), follow_redirects=True,
                        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})


def _official(url: str, repo: str) -> bool:
    """只接受官方儲存庫 Releases 的下載網址。"""
    return url.startswith(f"https://github.com/{repo}/releases/download/")


def list_releases(cfg: dict[str, Any], include_prerelease: bool = True,
                  client: httpx.Client | None = None) -> list[ReleaseInfo]:
    """查詢 GitHub Releases，回傳 min_version 以後、有安裝檔的版本（新到舊）。"""
    repo = repo_of(cfg)
    minimum = parse_version(str(_settings(cfg).get("min_version", "0.5.4")))
    own = client is None
    client = client or _client()
    log.debug("查詢版本：%s（含預覽版=%s、最低版本=%s）", repo, include_prerelease, ".".join(map(str, minimum)))
    try:
        resp = client.get(f"{API}/repos/{repo}/releases", params={"per_page": 50})
    except httpx.HTTPError as exc:
        raise UpdateError("無法連線到 GitHub（可能是網路中斷，或單位網路擋住 GitHub）。"
                          "可以改按「開啟 Releases 網頁」手動下載。") from exc
    finally:
        if own:
            client.close()
    if resp.status_code == 403:
        raise UpdateError("GitHub 暫時限制查詢次數（每小時 60 次），請稍後再試。")
    if resp.status_code >= 400:
        raise UpdateError(f"GitHub 回應錯誤（HTTP {resp.status_code}）。")
    out: list[ReleaseInfo] = []
    for rel in resp.json():
        if rel.get("draft") or (rel.get("prerelease") and not include_prerelease):
            continue
        assets = {a.get("name", ""): a for a in rel.get("assets", [])}
        installer = next((a for name, a in assets.items() if INSTALLER_RE.match(name)), None)
        if installer is None or not _official(str(installer.get("browser_download_url", "")), repo):
            continue
        version = INSTALLER_RE.match(installer["name"]).group(1)  # type: ignore[union-attr]
        if parse_version(version) < minimum:
            continue
        checksum = assets.get(installer["name"] + ".sha256")
        checksum_url = str(checksum["browser_download_url"]) if checksum else None
        if checksum_url and not _official(checksum_url, repo):
            checksum_url = None
        published = rel.get("published_at")
        out.append(ReleaseInfo(
            version=version, tag=str(rel.get("tag_name", "")), title=str(rel.get("name") or rel.get("tag_name", "")),
            published=datetime.fromisoformat(published) if published else None,
            prerelease=bool(rel.get("prerelease")), notes=str(rel.get("body") or ""),
            installer_url=str(installer["browser_download_url"]), installer_name=str(installer["name"]),
            installer_size=int(installer.get("size") or 0), checksum_url=checksum_url,
            page_url=str(rel.get("html_url") or releases_page(cfg)),
        ))
    out.sort(key=lambda r: r.key, reverse=True)
    log.debug("可安裝的版本：%s", "、".join(f"{r.version}{'（預覽版）' if r.prerelease else ''}"
                                          f"{'' if r.checksum_url else '（沒有檢查碼）'}" for r in out) or "無")
    return out


def newer_than_current(releases: list[ReleaseInfo], include_prerelease: bool) -> ReleaseInfo | None:
    """比目前版本新的最新版本（沒有時回傳 None）。"""
    current = parse_version(current_version())
    for rel in releases:
        if rel.key > current and (include_prerelease or not rel.prerelease):
            return rel
    return None


def updates_dir() -> Path:
    return localappdata_dir() / "updates"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(release: ReleaseInfo, progress: ProgressFn | None = None, cancel: Callable[[], bool] | None = None,
             client: httpx.Client | None = None, dest: Path | None = None) -> Path:
    """下載安裝檔並比對 SHA-256，回傳安裝檔路徑。沒有檢查碼或不一致時拋出 UpdateError，不會安裝。"""
    if not release.checksum_url:
        raise UpdateError(f"{release.version} 沒有附檢查碼，無法驗證檔案是否完整，為了安全不提供安裝。")
    folder = dest or updates_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / release.installer_name
    tmp = target.with_suffix(".part")
    own = client is None
    client = client or _client(timeout=120.0)
    try:
        resp = client.get(release.checksum_url)
        if resp.status_code >= 400:
            raise UpdateError(f"無法下載檢查碼（HTTP {resp.status_code}）。")
        expected = resp.text.strip().split()[0].lower() if resp.text.strip() else ""
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise UpdateError("檢查碼格式不正確，為了安全不安裝。")
        with client.stream("GET", release.installer_url) as stream:
            if stream.status_code >= 400:
                raise UpdateError(f"無法下載安裝檔（HTTP {stream.status_code}）。")
            total = int(stream.headers.get("content-length") or release.installer_size or 0)
            done = 0
            with open(tmp, "wb") as f:
                for chunk in stream.iter_bytes(1024 * 256):
                    if cancel and cancel():
                        raise UpdateError("已取消下載。")
                    f.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(100.0 * done / total, f"下載 {release.installer_name}：{done / 1048576:.0f} / "
                                                       f"{total / 1048576:.0f} MB")
    except httpx.HTTPError as exc:
        raise UpdateError("下載中斷（網路問題），請稍後再試。") from exc
    finally:
        if own:
            client.close()
    actual = sha256_of(tmp)
    if actual != expected:
        tmp.unlink(missing_ok=True)
        raise UpdateError("下載的安裝檔與檢查碼不一致（檔案可能不完整或被竄改），已刪除，不會安裝。")
    tmp.replace(target)
    log.info("已下載並驗證 %s", target.name)
    return target


def install_scope_args() -> list[str]:
    """沿用目前的安裝範圍：裝在使用者資料夾 → 只給目前使用者；否則所有使用者。"""
    exe = Path(sys.executable).resolve()
    local = Path(os.environ.get("LOCALAPPDATA", "")).resolve() if os.environ.get("LOCALAPPDATA") else None
    if local and str(exe).lower().startswith(str(local).lower()):
        return ["/CURRENTUSER"]
    return ["/ALLUSERS"]


_RUNNER = (
    "$p = Start-Process -FilePath $env:MS_SETUP -ArgumentList $env:MS_ARGS.Split('|') -Wait -PassThru; "
    "if ($p.ExitCode -eq 0) { Start-Process -FilePath $env:MS_APP }"
)


def launch_installer(installer: Path) -> None:
    """在背景啟動安裝（程式要隨後自行結束）：等安裝完成後自動重新開啟程式。"""
    args = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", *install_scope_args()]
    env = dict(os.environ, MS_SETUP=str(installer), MS_ARGS="|".join(args), MS_APP=str(Path(sys.executable)))
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _RUNNER],
        env=env, creationflags=0x08000000 | 0x00000008,  # CREATE_NO_WINDOW | DETACHED_PROCESS
    )
    log.info("啟動安裝：%s %s", installer.name, " ".join(args))


def should_check_today(cfg: dict[str, Any], now: datetime | None = None) -> bool:
    """開啟程式時是否要自動檢查（設定開啟、而且今天還沒檢查過）。"""
    settings = _settings(cfg)
    if not settings.get("check_on_startup", True):
        return False
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    return str(settings.get("last_check", "")) != today
