"""「版本」分頁與更新：查詢 GitHub Releases、篩選 0.5.4 以後、下載與檢查碼驗證、安裝流程（全部模擬，不連網、不安裝）。"""

import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_scribe import updater
from meeting_scribe.config import load_config

REPO = "donny2582/meeting-scribe"
DL = f"https://github.com/{REPO}/releases/download"
PAYLOAD = b"fake installer" * 1000
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def _release(version: str, prerelease: bool, checksum: bool = True, url: str | None = None) -> dict[str, Any]:
    name = f"MeetingScribe_Setup_{version}.exe"
    assets = [{"name": name, "size": len(PAYLOAD), "browser_download_url": url or f"{DL}/v{version}/{name}"}]
    if checksum:
        assets.append({"name": name + ".sha256", "size": 80, "browser_download_url": f"{DL}/v{version}/{name}.sha256"})
    return {"tag_name": f"v{version}", "name": f"MeetingScribe {version}", "prerelease": prerelease, "draft": False,
            "published_at": "2026-11-26T07:29:25Z", "body": f"## {version} 更新內容", "assets": assets,
            "html_url": f"https://github.com/{REPO}/releases/tag/v{version}"}


RELEASES = [_release("0.5.5", True), _release("0.5.4", False), _release("0.5.3", True),
            _release("0.5.6", False, url="https://evil.example.com/MeetingScribe_Setup_0.5.6.exe"),
            _release("0.5.10", False, checksum=False)]


def _client(routes: dict[str, Any]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        reply = routes.get(url)
        if reply is None:
            return httpx.Response(404)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, bytes):
            return httpx.Response(200, content=reply, headers={"content-length": str(len(reply))})
        return httpx.Response(200, json=reply) if not isinstance(reply, str) else httpx.Response(200, text=reply)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


API_URL = f"https://api.github.com/repos/{REPO}/releases"


def test_parse_version_ordering() -> None:
    assert updater.parse_version("v0.5.10") > updater.parse_version("0.5.9") > updater.parse_version("0.5.4")
    assert updater.parse_version("abc") == ()


def test_list_releases_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    rels = updater.list_releases(cfg, True, _client({API_URL: RELEASES}))
    assert [r.version for r in rels] == ["0.5.10", "0.5.5", "0.5.4"]  # 0.5.3 太舊、0.5.6 不是官方網址 → 不列出
    assert rels[0].checksum_url is None and rels[2].prerelease is False
    assert isinstance(rels[1].published, datetime)
    stable = updater.list_releases(cfg, False, _client({API_URL: RELEASES}))
    assert [r.version for r in stable] == ["0.5.10", "0.5.4"]  # 不顯示預覽版
    monkeypatch.setattr(updater, "current_version", lambda: "0.5.4")
    assert updater.newer_than_current(rels, include_prerelease=False).version == "0.5.10"  # type: ignore[union-attr]
    monkeypatch.setattr(updater, "current_version", lambda: "0.5.10")
    assert updater.newer_than_current(rels, include_prerelease=True) is None


def test_list_releases_network_errors() -> None:
    cfg = load_config()
    with pytest.raises(updater.UpdateError, match="無法連線到 GitHub"):
        updater.list_releases(cfg, True, _client({API_URL: httpx.ConnectError("blocked")}))
    rate = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    with pytest.raises(updater.UpdateError, match="60 次"):
        updater.list_releases(cfg, True, rate)


def test_download_verifies_checksum(tmp_path: Path) -> None:
    rel = updater.list_releases(load_config(), True, _client({API_URL: RELEASES}))[1]  # 0.5.5
    routes = {rel.installer_url: PAYLOAD, rel.checksum_url: f"{SHA}  {rel.installer_name}\n"}
    seen: list[str] = []
    path = updater.download(rel, lambda p, m: seen.append(m), None, _client(routes), tmp_path)
    assert path.read_bytes() == PAYLOAD and seen and "MB" in seen[-1]
    bad = {rel.installer_url: PAYLOAD + b"x", rel.checksum_url: SHA}
    with pytest.raises(updater.UpdateError, match="不一致"):
        updater.download(rel, None, None, _client(bad), tmp_path / "bad")
    assert not list((tmp_path / "bad").glob("*.exe")) and not list((tmp_path / "bad").glob("*.part"))


def test_download_refuses_without_checksum(tmp_path: Path) -> None:
    rel = updater.list_releases(load_config(), True, _client({API_URL: RELEASES}))[0]  # 0.5.10 沒有檢查碼
    with pytest.raises(updater.UpdateError, match="沒有附檢查碼"):
        updater.download(rel, dest=tmp_path)


def test_should_check_today() -> None:
    cfg = {"update": {"check_on_startup": True, "last_check": "2026-11-26"}}
    assert not updater.should_check_today(cfg, datetime(2026, 11, 26, 9))
    assert updater.should_check_today(cfg, datetime(2026, 11, 27, 9))
    assert not updater.should_check_today({"update": {"check_on_startup": False}})


def test_install_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "local" / "Programs" / "MeetingScribe" / "a.exe"))
    assert updater.install_scope_args() == ["/CURRENTUSER"]
    monkeypatch.setattr(updater.sys, "executable", r"C:\Program Files\MeetingScribe\MeetingScribe.exe")
    assert updater.install_scope_args() == ["/ALLUSERS"]


# ---- 介面 ----
@pytest.fixture
def win(monkeypatch: pytest.MonkeyPatch) -> Any:
    from PySide6.QtWidgets import QApplication

    from meeting_scribe.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    w = MainWindow(secret_store={"get_secret": lambda n: None, "set_secret": lambda n, v: None,
                                 "delete_secret": lambda n: None})
    yield w
    w.close()
    del app


def _releases() -> list[updater.ReleaseInfo]:
    return updater.list_releases(load_config(), True, _client({API_URL: RELEASES}))


def test_version_tab_is_last_and_lists(win: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert win.tabs.tabText(win.tabs.count() - 1) == "版本" and win.tabs.widget(win.tabs.count() - 1) is win.version
    monkeypatch.setattr(updater, "current_version", lambda: "0.5.5")
    monkeypatch.setattr(updater, "is_installed", lambda: True)
    page = win.version
    page.show_releases(_releases())
    states = [page.table.item(r, 3).text() for r in range(page.table.rowCount())]
    assert states == ["較新，可以更新", "目前版本", "較舊，可以退回"]
    assert [page.table.item(r, 1).text() for r in range(3)] == ["穩定版", "預覽版", "穩定版"]
    assert "有新版本：0.5.10" in page.status.text() and win.tabs.tabText(win.tabs.count() - 1) == "版本（有新版）"
    page.table.selectRow(0)
    assert page.install_button.text() == "更新到 0.5.10" and page.install_button.isEnabled()
    assert "0.5.10 更新內容" in page.notes.toPlainText()
    page.table.selectRow(1)
    assert page.install_button.text() == "目前版本" and not page.install_button.isEnabled()
    page.table.selectRow(2)
    assert page.install_button.text() == "退回到 0.5.4"


def test_dev_edition_cannot_install(win: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "is_installed", lambda: False)
    page = win.version
    page.show_releases(_releases())
    page.table.selectRow(0)
    assert not page.install_button.isEnabled()


def test_install_flow_downloads_then_launches(win: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox

    monkeypatch.setattr(updater, "current_version", lambda: "0.5.5")
    monkeypatch.setattr(updater, "is_installed", lambda: True)
    asked: list[str] = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes)
    installer = tmp_path / "MeetingScribe_Setup_0.5.4.exe"
    installer.write_bytes(PAYLOAD)
    monkeypatch.setattr(updater, "download", lambda rel, progress=None, cancel=None: installer)
    launched: list[Path] = []
    monkeypatch.setattr(updater, "launch_installer", lambda p: launched.append(p))
    quits: list[bool] = []
    monkeypatch.setattr(QApplication.instance(), "quit", lambda: quits.append(True))
    page = win.version
    page.show_releases(_releases())
    page.table.selectRow(2)  # 退回 0.5.4
    page.install_selected()
    for _ in range(100):
        QTest.qWait(10)
        if not page.panel.running:
            break
    assert asked and "退回到 0.5.4" in asked[0] and "設定、API 金鑰與會議資料都會保留" in asked[0]
    assert "還不能在程式裡更新" in asked[0]  # 0.5.4 沒有更新功能 → 提醒之後要手動更新
    assert launched == [installer] and quits == [True]


def test_background_check_marks_new_version(win: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "current_version", lambda: "0.5.4")
    page = win.version
    page._background_checked(_releases())
    assert "版本（有新版）" in win.tabs.tabText(win.tabs.count() - 1)
    assert win.cfg["update"]["last_check"] == datetime.now().strftime("%Y-%m-%d")  # 今天不會再自動檢查
    page._background_checked(updater.UpdateError("連不上"))  # 失敗時不打擾使用者


def test_launch_installer_runner_actually_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """0.5.5 的問題：用 DETACHED_PROCESS＋-WindowStyle Hidden 啟動 PowerShell 會直接結束，安裝程式不會執行。"""
    import subprocess
    import sys
    import time

    calls: list[dict] = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kw: calls.append({"args": args, **kw}))
    updater.launch_installer(tmp_path / "setup.exe")
    flags, args = calls[0]["creationflags"], calls[0]["args"]
    assert not flags & 0x00000008 and "-WindowStyle" not in args  # 不可 DETACHED、不可 Hidden
    assert flags & updater.CREATE_NO_WINDOW
    if sys.platform != "win32":
        return
    mark = tmp_path / "ran.txt"  # 實際用同樣的方式啟動 PowerShell，確認真的會執行
    env = dict(calls[0]["env"], MS_SETUP="cmd.exe", MS_ARGS=f"/c|echo ok > \"{mark}\"", MS_APP="cmd.exe")
    monkeypatch.undo()
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                      updater._RUNNER.replace("Start-Process -FilePath $env:MS_APP", "")],
                     env=env, creationflags=flags)
    for _ in range(40):
        if mark.exists():
            break
        time.sleep(0.25)
    assert mark.exists()