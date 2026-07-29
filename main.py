from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import customtkinter as ctk
import psutil
import pystray
from PIL import Image

try:
    from embedded_config import (
        EMBEDDED_DISCORD_CLIENT_ID,
        EMBEDDED_LARGE_IMAGE,
        EMBEDDED_LARGE_TEXT,
    )
except Exception:
    EMBEDDED_DISCORD_CLIENT_ID = ""
    EMBEDDED_LARGE_IMAGE = "telegram_music"
    EMBEDDED_LARGE_TEXT = "Музыка из Telegram"


APP_TITLE = "Telegram RPC | By Apdeit"
APP_SLUG = "TelegramRPC_By_Apdeit"
APP_VERSION = "1.0.0"

NAVY = "#08263D"
NAVY_LIGHT = "#103B56"
SEA = "#087F9B"
SEA_HOVER = "#066A82"
AQUA = "#45C7C5"
PALE_AQUA = "#DDF6F5"
WHITE = "#FFFFFF"
OFF_WHITE = "#F3F8FA"
TEXT = "#102E3E"
MUTED = "#65808D"
SUCCESS = "#159A74"
WARNING = "#D58B25"
DANGER = "#D34E5B"
BORDER = "#D9E7EC"

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    path = Path(base) / APP_SLUG if base else Path.home() / f".{APP_SLUG}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / relative


def executable_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" --minimized'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{runner}" "{Path(__file__).resolve()}" --minimized'


def is_valid_client_id(value: str) -> bool:
    value = value.strip()
    return value.isdigit() and 15 <= len(value) <= 25


def shorten(text: str, limit: int = 54) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


@dataclass
class AppSettings:
    discord_client_id: str = ""
    large_image: str = "telegram_music"
    large_text: str = "Музыка из Telegram"
    show_when_paused: bool = True
    start_with_windows: bool = False
    start_rpc_on_launch: bool = False
    minimize_to_tray: bool = True
    telegram_session_match: list[str] | None = None

    def __post_init__(self) -> None:
        if self.telegram_session_match is None:
            self.telegram_session_match = ["telegram"]


class SettingsStore:
    def __init__(self) -> None:
        self.path = app_data_dir() / "config.json"

    def load(self) -> AppSettings:
        defaults = AppSettings(
            discord_client_id=str(EMBEDDED_DISCORD_CLIENT_ID or "").strip(),
            large_image=str(EMBEDDED_LARGE_IMAGE or "telegram_music").strip(),
            large_text=str(EMBEDDED_LARGE_TEXT or "Музыка из Telegram").strip(),
        )
        if not self.path.exists():
            return defaults

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for key in asdict(defaults):
                if key in data:
                    setattr(defaults, key, data[key])
            defaults.__post_init__()
            return defaults
        except Exception:
            return defaults

    def save(self, settings: AppSettings) -> None:
        self.path.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class StartupManager:
    VALUE_NAME = APP_SLUG

    @staticmethod
    def set_enabled(enabled: bool) -> None:
        if os.name != "nt":
            return
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            key_path,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    StartupManager.VALUE_NAME,
                    0,
                    winreg.REG_SZ,
                    executable_command(),
                )
            else:
                try:
                    winreg.DeleteValue(key, StartupManager.VALUE_NAME)
                except FileNotFoundError:
                    pass


class DiscordManager:
    PROCESS_NAMES = {"discord.exe", "discordcanary.exe", "discordptb.exe"}

    @classmethod
    def is_running(cls) -> bool:
        try:
            for process in psutil.process_iter(["name"]):
                name = (process.info.get("name") or "").casefold()
                if name in cls.PROCESS_NAMES:
                    return True
        except (psutil.Error, OSError):
            pass
        return False

    @staticmethod
    def launch() -> bool:
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        variants = [
            ("Discord", "Discord.exe"),
            ("DiscordCanary", "DiscordCanary.exe"),
            ("DiscordPTB", "DiscordPTB.exe"),
        ]
        for folder, process_name in variants:
            updater = local / folder / "Update.exe"
            if updater.exists():
                try:
                    subprocess.Popen(
                        [str(updater), "--processStart", process_name],
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    return True
                except OSError:
                    continue

        try:
            os.startfile("discord://-/")  # type: ignore[attr-defined]
            return True
        except OSError:
            return False


@dataclass(frozen=True)
class Track:
    source_app_id: str
    title: str
    artist: str
    album: str
    status: str
    position_seconds: float
    duration_seconds: float

    @property
    def key(self) -> tuple[str, str, str]:
        return (
            self.source_app_id.casefold(),
            self.title.casefold(),
            self.artist.casefold(),
        )


class TrackClock:
    def __init__(self) -> None:
        self.track_key: tuple[str, str, str] | None = None
        self.started_at: float | None = None
        self.paused_position = 0.0
        self.was_paused = False

    def reset(self) -> None:
        self.track_key = None
        self.started_at = None
        self.paused_position = 0.0
        self.was_paused = False

    def update(self, track: Track) -> int | None:
        now = time.time()
        actual = max(0.0, track.position_seconds)

        if track.key != self.track_key:
            self.track_key = track.key
            self.started_at = now - actual if actual > 0 else now
            self.paused_position = actual
            self.was_paused = track.status == "paused"
            return None if self.was_paused else int(self.started_at * 1000)

        if self.started_at is None:
            self.started_at = now - actual if actual > 0 else now

        if track.status == "paused":
            if not self.was_paused:
                self.paused_position = actual if actual > 0 else max(0.0, now - self.started_at)
                self.was_paused = True
            return None

        if self.was_paused:
            position = actual if actual > 0 else self.paused_position
            self.started_at = now - position
            self.was_paused = False
        elif actual > 0:
            expected = max(0.0, now - self.started_at)
            if abs(expected - actual) > 3.0:
                self.started_at = now - actual

        return int(self.started_at * 1000)


class RpcWorker(threading.Thread):
    def __init__(self, settings: AppSettings, events: queue.Queue[dict[str, Any]]) -> None:
        super().__init__(daemon=True, name="TelegramRPCWorker")
        self.settings = settings
        self.events = events
        self.stop_event = threading.Event()
        self._rpc: Any = None
        self._last_payload: dict[str, Any] | None = None

    def emit(self, event: str, **payload: Any) -> None:
        self.events.put({"event": event, **payload})

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:
            self.emit(
                "fatal",
                message=f"Критическая ошибка RPC: {exc}",
                details=traceback.format_exc(),
            )
        finally:
            self.emit("worker_stopped")

    @staticmethod
    def _timespan_seconds(value: Any) -> float:
        if value is None:
            return 0.0
        total_seconds = getattr(value, "total_seconds", None)
        if callable(total_seconds):
            try:
                return max(0.0, float(total_seconds()))
            except Exception:
                return 0.0
        duration = getattr(value, "duration", None)
        if duration is not None:
            try:
                return max(0.0, float(duration) / 10_000_000.0)
            except Exception:
                return 0.0
        try:
            return max(0.0, float(value))
        except Exception:
            return 0.0

    @staticmethod
    def _split_artist(title: str, artist: str) -> tuple[str, str]:
        if artist:
            return title, artist
        for separator in (" — ", " – ", " - "):
            if separator in title:
                left, right = title.split(separator, 1)
                if left.strip() and right.strip():
                    return right.strip(), left.strip()
        return title, artist

    async def _dispose_rpc(self) -> None:
        rpc, self._rpc = self._rpc, None
        self._last_payload = None
        if rpc is None:
            return
        writer = getattr(rpc, "sock_writer", None)
        if writer is not None:
            try:
                writer.close()
                wait_closed = getattr(writer, "wait_closed", None)
                if callable(wait_closed):
                    await wait_closed()
            except Exception:
                pass

    async def _ensure_rpc(self, AioPresence: Any) -> bool:
        if self._rpc is not None:
            return True
        try:
            rpc = AioPresence(self.settings.discord_client_id)
            await rpc.connect()
            self._rpc = rpc
            self._last_payload = None
            self.emit("discord_rpc", connected=True)
            self.emit("log", level="success", message="Discord RPC подключён")
            return True
        except Exception as exc:
            self.emit("discord_rpc", connected=False)
            self.emit("log", level="warning", message=f"Discord RPC недоступен: {exc}")
            await self._dispose_rpc()
            return False

    async def _clear_rpc(self) -> None:
        if self._rpc is None:
            self._last_payload = None
            return
        try:
            await self._rpc.clear()
        except Exception:
            await self._dispose_rpc()
        finally:
            self._last_payload = None

    async def _read_track(
        self,
        manager: Any,
        PlaybackStatus: Any,
    ) -> Track | None:
        sessions = list(manager.get_sessions())
        terms = [x.casefold() for x in (self.settings.telegram_session_match or ["telegram"])]
        candidates: list[tuple[int, Any, str, str]] = []

        for session in sessions:
            source_id = str(session.source_app_user_model_id or "")
            if not any(term in source_id.casefold() for term in terms):
                continue
            try:
                raw_status = session.get_playback_info().playback_status
                if raw_status == PlaybackStatus.PLAYING:
                    status = "playing"
                elif raw_status == PlaybackStatus.PAUSED:
                    status = "paused"
                else:
                    status = "inactive"
            except Exception:
                status = "inactive"
            priority = {"playing": 0, "paused": 1, "inactive": 5}[status]
            candidates.append((priority, session, source_id, status))

        candidates.sort(key=lambda item: item[0])
        for _, session, source_id, status in candidates:
            if status not in {"playing", "paused"}:
                continue
            try:
                props = await session.try_get_media_properties_async()
                if props is None:
                    continue
                title = str(props.title or "").strip()
                artist = str(props.artist or props.album_artist or "").strip()
                album = str(props.album_title or "").strip()
                if not title:
                    continue
                title, artist = self._split_artist(title, artist)
                timeline = session.get_timeline_properties()
                return Track(
                    source_app_id=source_id,
                    title=title,
                    artist=artist,
                    album=album,
                    status=status,
                    position_seconds=self._timespan_seconds(getattr(timeline, "position", None)),
                    duration_seconds=self._timespan_seconds(getattr(timeline, "end_time", None)),
                )
            except Exception:
                continue
        return None

    async def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stop_event.is_set() and time.monotonic() < end:
            await asyncio.sleep(min(0.15, max(0.0, end - time.monotonic())))

    async def _main(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Поддерживаются только Windows 10 и Windows 11")

        from pypresence import AioPresence
        from pypresence.types import ActivityType, StatusDisplayType
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
        )

        manager = await MediaManager.request_async()
        clock = TrackClock()
        last_track_state: tuple[Any, ...] | None = None
        no_track_since: float | None = None
        presence_visible = False
        last_discord_process_state: bool | None = None
        last_connect_attempt = 0.0

        self.emit("engine_ready")
        self.emit("log", level="info", message="Windows Media Control подключён")

        try:
            while not self.stop_event.is_set():
                discord_running = DiscordManager.is_running()
                if discord_running != last_discord_process_state:
                    self.emit("discord_process", running=discord_running)
                    last_discord_process_state = discord_running

                if not discord_running:
                    if presence_visible:
                        await self._clear_rpc()
                        presence_visible = False
                    await self._dispose_rpc()
                    self.emit("discord_rpc", connected=False)
                    await self._sleep(1.0)
                    continue

                if self._rpc is None and time.monotonic() - last_connect_attempt >= 4.0:
                    last_connect_attempt = time.monotonic()
                    await self._ensure_rpc(AioPresence)

                try:
                    track = await self._read_track(manager, PlaybackStatus)
                except Exception as exc:
                    self.emit("log", level="warning", message=f"Перезапуск Media Control: {exc}")
                    manager = await MediaManager.request_async()
                    track = None

                if track is None:
                    clock.reset()
                    self.emit("telegram", active=False)
                    if no_track_since is None:
                        no_track_since = time.monotonic()
                    if presence_visible and time.monotonic() - no_track_since >= 2.5:
                        await self._clear_rpc()
                        presence_visible = False
                        last_track_state = None
                        self.emit("presence_cleared")
                    await self._sleep(0.8)
                    continue

                no_track_since = None
                self.emit("telegram", active=True)
                self.emit(
                    "track",
                    title=track.title,
                    artist=track.artist or "Неизвестный исполнитель",
                    status=track.status,
                    position=track.position_seconds,
                    duration=track.duration_seconds,
                )

                if track.status == "paused" and not self.settings.show_when_paused:
                    if presence_visible:
                        await self._clear_rpc()
                        presence_visible = False
                    await self._sleep(0.8)
                    continue

                start_timestamp = clock.update(track)
                payload: dict[str, Any] = {
                    "activity_type": ActivityType.LISTENING,
                    "status_display_type": StatusDisplayType.DETAILS,
                    "name": "Telegram Music",
                    "details": shorten(track.title, 128),
                    "state": shorten(
                        track.artist or "Неизвестный исполнитель",
                        128,
                    ) if track.status == "playing" else shorten(
                        f"⏸ {track.artist or 'Неизвестный исполнитель'}",
                        128,
                    ),
                }
                if start_timestamp is not None and track.status == "playing":
                    payload["start"] = start_timestamp
                if self.settings.large_image.strip():
                    payload["large_image"] = self.settings.large_image.strip()
                    payload["large_text"] = self.settings.large_text.strip() or "Музыка из Telegram"

                current_state = (track.key, track.status, start_timestamp)
                if self._rpc is not None and (payload != self._last_payload or current_state != last_track_state):
                    try:
                        await self._rpc.update(**payload)
                        self._last_payload = payload
                        last_track_state = current_state
                        presence_visible = True
                        self.emit("discord_rpc", connected=True)
                    except Exception as exc:
                        self.emit("log", level="warning", message=f"Ошибка обновления Discord: {exc}")
                        self.emit("discord_rpc", connected=False)
                        await self._dispose_rpc()

                await self._sleep(0.8)
        finally:
            try:
                await self._clear_rpc()
            finally:
                await self._dispose_rpc()


class SeaDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk,
        title: str,
        message: str,
        yes_text: str = "Да",
        no_text: str = "Нет",
        on_yes: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.on_yes = on_yes
        self.title(title)
        self.geometry("470x260")
        self.resizable(False, False)
        self.configure(fg_color=OFF_WHITE)
        self.transient(master)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)
        ctk.CTkFrame(self, height=8, fg_color=SEA, corner_radius=0).grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            self,
            text=title,
            text_color=TEXT,
            font=ctk.CTkFont("Segoe UI", 22, "bold"),
        ).grid(row=1, column=0, padx=28, pady=(26, 8), sticky="w")
        ctk.CTkLabel(
            self,
            text=message,
            text_color=MUTED,
            justify="left",
            wraplength=410,
            font=ctk.CTkFont("Segoe UI", 14),
        ).grid(row=2, column=0, padx=28, sticky="w")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, padx=28, pady=(28, 22), sticky="e")
        ctk.CTkButton(
            buttons,
            text=no_text,
            width=120,
            height=40,
            fg_color=WHITE,
            hover_color=PALE_AQUA,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            command=self.destroy,
        ).pack(side="left", padx=(0, 10))
        ctk.CTkButton(
            buttons,
            text=yes_text,
            width=150,
            height=40,
            fg_color=SEA,
            hover_color=SEA_HOVER,
            command=self._yes,
        ).pack(side="left")

        self.after(50, self._center)

    def _center(self) -> None:
        self.update_idletasks()
        master = self.master
        x = master.winfo_x() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_y() + (master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _yes(self) -> None:
        callback = self.on_yes
        self.destroy()
        if callback:
            callback()


class StatusBadge(ctk.CTkFrame):
    def __init__(self, master: Any, title: str) -> None:
        super().__init__(master, fg_color=WHITE, corner_radius=14, border_width=1, border_color=BORDER)
        self.grid_columnconfigure(1, weight=1)
        self.dot = ctk.CTkLabel(self, text="●", text_color=MUTED, font=ctk.CTkFont(size=18), width=25)
        self.dot.grid(row=0, column=0, padx=(14, 3), pady=12)
        self.title_label = ctk.CTkLabel(self, text=title, text_color=TEXT, font=ctk.CTkFont("Segoe UI", 13, "bold"))
        self.title_label.grid(row=0, column=1, padx=(0, 8), pady=12, sticky="w")
        self.value_label = ctk.CTkLabel(self, text="Проверка…", text_color=MUTED, font=ctk.CTkFont("Segoe UI", 12))
        self.value_label.grid(row=0, column=2, padx=(0, 14), pady=12, sticky="e")

    def set(self, value: str, state: str) -> None:
        color = {"ok": SUCCESS, "warn": WARNING, "bad": DANGER, "idle": MUTED}.get(state, MUTED)
        self.dot.configure(text_color=color)
        self.value_label.configure(text=value, text_color=color if state != "idle" else MUTED)


class TelegramRpcApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.store = SettingsStore()
        self.settings = self.store.load()
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker: RpcWorker | None = None
        self.rpc_running = False
        self.exiting = False
        self.log_lines: list[str] = []
        self.tray_icon: pystray.Icon | None = None
        self.current_page = "main"

        self.title(APP_TITLE)
        self.geometry("940x640")
        self.minsize(860, 590)
        self.configure(fg_color=OFF_WHITE)
        self.protocol("WM_DELETE_WINDOW", self.on_window_close)

        icon_path = resource_path("assets/app.ico")
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self._build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color=OFF_WHITE, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self.pages: dict[str, ctk.CTkFrame] = {}
        self._build_main_page()
        self._build_settings_page()
        self._build_logs_page()
        self._build_about_page()
        self.show_page("main")

        self._start_tray()
        self.after(120, self._poll_events)
        self.after(500, self._poll_system_status)

        if "--minimized" in sys.argv:
            self.after(200, self.withdraw)
        if self.settings.start_rpc_on_launch and is_valid_client_id(self.settings.discord_client_id):
            self.after(1000, self.request_start_rpc)
        elif not is_valid_client_id(self.settings.discord_client_id):
            self.after(700, self._show_first_run)

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, width=220, fg_color=NAVY, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(7, weight=1)

        logo = ctk.CTkFrame(sidebar, fg_color="transparent")
        logo.grid(row=0, column=0, padx=20, pady=(24, 30), sticky="ew")
        logo.grid_columnconfigure(1, weight=1)
        circle = ctk.CTkLabel(
            logo,
            text="➤",
            width=46,
            height=46,
            corner_radius=23,
            fg_color=AQUA,
            text_color=NAVY,
            font=ctk.CTkFont("Segoe UI", 24, "bold"),
        )
        circle.grid(row=0, column=0, rowspan=2, padx=(0, 11))
        ctk.CTkLabel(
            logo,
            text="Telegram RPC",
            text_color=WHITE,
            font=ctk.CTkFont("Segoe UI", 17, "bold"),
        ).grid(row=0, column=1, sticky="sw")
        ctk.CTkLabel(
            logo,
            text="By Apdeit",
            text_color="#9EC8D5",
            font=ctk.CTkFont("Segoe UI", 12),
        ).grid(row=1, column=1, sticky="nw")

        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("main", "⌂  Главная"),
            ("settings", "⚙  Настройки"),
            ("logs", "≡  Журнал"),
            ("about", "ⓘ  О программе"),
        ]
        for index, (key, text) in enumerate(nav_items, start=1):
            btn = ctk.CTkButton(
                sidebar,
                text=text,
                anchor="w",
                height=44,
                corner_radius=10,
                fg_color="transparent",
                hover_color=NAVY_LIGHT,
                text_color="#DDEEF3",
                font=ctk.CTkFont("Segoe UI", 14),
                command=lambda page=key: self.show_page(page),
            )
            btn.grid(row=index, column=0, padx=14, pady=4, sticky="ew")
            self.nav_buttons[key] = btn

        ctk.CTkLabel(
            sidebar,
            text=f"v{APP_VERSION}\nWindows 10 / 11",
            text_color="#7FA7B5",
            justify="left",
            font=ctk.CTkFont("Segoe UI", 11),
        ).grid(row=8, column=0, padx=22, pady=20, sticky="sw")

    def _new_page(self, name: str) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self.content, fg_color=OFF_WHITE, corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        self.pages[name] = page
        return page

    def _header(self, page: ctk.CTkFrame, title: str, subtitle: str) -> None:
        ctk.CTkLabel(page, text=title, text_color=TEXT, font=ctk.CTkFont("Segoe UI", 28, "bold")).grid(
            row=0, column=0, padx=34, pady=(28, 2), sticky="w"
        )
        ctk.CTkLabel(page, text=subtitle, text_color=MUTED, font=ctk.CTkFont("Segoe UI", 13)).grid(
            row=1, column=0, padx=34, pady=(0, 22), sticky="w"
        )

    def _build_main_page(self) -> None:
        page = self._new_page("main")
        page.grid_rowconfigure(5, weight=1)
        self._header(page, "Главная", "Музыка из Telegram в профиле Discord")

        hero = ctk.CTkFrame(page, fg_color=NAVY, corner_radius=22)
        hero.grid(row=2, column=0, padx=34, sticky="ew")
        hero.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            hero,
            text="♫",
            width=86,
            height=86,
            corner_radius=43,
            fg_color=AQUA,
            text_color=NAVY,
            font=ctk.CTkFont("Segoe UI", 40, "bold"),
        ).grid(row=0, column=0, rowspan=3, padx=26, pady=24)

        self.hero_status = ctk.CTkLabel(
            hero,
            text="RPC выключен",
            text_color="#93C0CD",
            font=ctk.CTkFont("Segoe UI", 12, "bold"),
        )
        self.hero_status.grid(row=0, column=1, padx=(0, 18), pady=(24, 2), sticky="sw")
        self.track_title = ctk.CTkLabel(
            hero,
            text="Включите музыку в Telegram",
            text_color=WHITE,
            font=ctk.CTkFont("Segoe UI", 22, "bold"),
        )
        self.track_title.grid(row=1, column=1, padx=(0, 18), sticky="w")
        self.track_artist = ctk.CTkLabel(
            hero,
            text="Название и исполнитель появятся здесь",
            text_color="#A9CFD9",
            font=ctk.CTkFont("Segoe UI", 14),
        )
        self.track_artist.grid(row=2, column=1, padx=(0, 18), pady=(2, 24), sticky="nw")

        status_grid = ctk.CTkFrame(page, fg_color="transparent")
        status_grid.grid(row=3, column=0, padx=34, pady=18, sticky="ew")
        for column in (0, 1, 2):
            status_grid.grid_columnconfigure(column, weight=1, uniform="status")
        self.discord_badge = StatusBadge(status_grid, "Discord")
        self.discord_badge.grid(row=0, column=0, padx=(0, 7), sticky="ew")
        self.telegram_badge = StatusBadge(status_grid, "Telegram")
        self.telegram_badge.grid(row=0, column=1, padx=7, sticky="ew")
        self.rpc_badge = StatusBadge(status_grid, "Rich Presence")
        self.rpc_badge.grid(row=0, column=2, padx=(7, 0), sticky="ew")

        control = ctk.CTkFrame(page, fg_color=WHITE, corner_radius=18, border_width=1, border_color=BORDER)
        control.grid(row=4, column=0, padx=34, sticky="ew")
        control.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            control,
            text="Включить Telegram RPC в Discord?",
            text_color=TEXT,
            font=ctk.CTkFont("Segoe UI", 17, "bold"),
        ).grid(row=0, column=0, padx=24, pady=(20, 3), sticky="w")
        self.control_hint = ctk.CTkLabel(
            control,
            text="Приложение будет работать в фоне и обновлять текущий трек.",
            text_color=MUTED,
            font=ctk.CTkFont("Segoe UI", 12),
        )
        self.control_hint.grid(row=1, column=0, padx=24, pady=(0, 20), sticky="w")

        self.main_button = ctk.CTkButton(
            control,
            text="Да, включить RPC",
            width=190,
            height=46,
            corner_radius=12,
            fg_color=SEA,
            hover_color=SEA_HOVER,
            font=ctk.CTkFont("Segoe UI", 14, "bold"),
            command=self.toggle_rpc,
        )
        self.main_button.grid(row=0, column=1, rowspan=2, padx=24, pady=18)

        self.progress = ctk.CTkProgressBar(page, mode="indeterminate", fg_color=BORDER, progress_color=AQUA, height=5)
        self.progress.grid(row=6, column=0, padx=34, pady=(12, 20), sticky="ew")
        self.progress.grid_remove()

    def _build_settings_page(self) -> None:
        page = self._new_page("settings")
        self._header(page, "Настройки", "Параметры Discord RPC и запуска приложения")

        form = ctk.CTkFrame(page, fg_color=WHITE, corner_radius=18, border_width=1, border_color=BORDER)
        form.grid(row=2, column=0, padx=34, sticky="ew")
        form.grid_columnconfigure(1, weight=1)

        labels = [
            ("Discord Application ID", "Application ID из Discord Developer Portal"),
            ("Ключ изображения", "Имя загруженного Rich Presence Asset"),
            ("Подсказка изображения", "Текст при наведении на логотип"),
        ]
        for row, (title, subtitle) in enumerate(labels):
            ctk.CTkLabel(form, text=title, text_color=TEXT, font=ctk.CTkFont("Segoe UI", 13, "bold")).grid(
                row=row, column=0, padx=(22, 18), pady=(18 if row == 0 else 12, 2), sticky="nw"
            )
            ctk.CTkLabel(form, text=subtitle, text_color=MUTED, font=ctk.CTkFont("Segoe UI", 10)).grid(
                row=row, column=0, padx=(22, 18), pady=(39 if row == 0 else 33, 0), sticky="nw"
            )

        self.client_id_entry = ctk.CTkEntry(form, height=40, placeholder_text="123456789012345678")
        self.client_id_entry.grid(row=0, column=1, padx=(0, 22), pady=18, sticky="ew")
        self.asset_entry = ctk.CTkEntry(form, height=40)
        self.asset_entry.grid(row=1, column=1, padx=(0, 22), pady=12, sticky="ew")
        self.asset_text_entry = ctk.CTkEntry(form, height=40)
        self.asset_text_entry.grid(row=2, column=1, padx=(0, 22), pady=12, sticky="ew")

        switches = ctk.CTkFrame(page, fg_color=WHITE, corner_radius=18, border_width=1, border_color=BORDER)
        switches.grid(row=3, column=0, padx=34, pady=16, sticky="ew")
        switches.grid_columnconfigure(0, weight=1)

        self.pause_switch = ctk.CTkSwitch(switches, text="Показывать трек на паузе", progress_color=SEA)
        self.pause_switch.grid(row=0, column=0, padx=22, pady=(18, 9), sticky="w")
        self.autorun_switch = ctk.CTkSwitch(switches, text="Запускать приложение вместе с Windows", progress_color=SEA)
        self.autorun_switch.grid(row=1, column=0, padx=22, pady=9, sticky="w")
        self.auto_rpc_switch = ctk.CTkSwitch(switches, text="Автоматически включать RPC при запуске", progress_color=SEA)
        self.auto_rpc_switch.grid(row=2, column=0, padx=22, pady=9, sticky="w")
        self.tray_switch = ctk.CTkSwitch(switches, text="При закрытии сворачивать в системный трей", progress_color=SEA)
        self.tray_switch.grid(row=3, column=0, padx=22, pady=(9, 18), sticky="w")

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.grid(row=4, column=0, padx=34, sticky="e")
        ctk.CTkButton(
            actions,
            text="Открыть Developer Portal",
            width=185,
            height=42,
            fg_color=WHITE,
            hover_color=PALE_AQUA,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            command=lambda: webbrowser.open("https://discord.com/developers/applications"),
        ).pack(side="left", padx=(0, 10))
        ctk.CTkButton(
            actions,
            text="Сохранить настройки",
            width=185,
            height=42,
            fg_color=SEA,
            hover_color=SEA_HOVER,
            command=self.save_settings,
        ).pack(side="left")

        self._load_settings_into_form()

    def _build_logs_page(self) -> None:
        page = self._new_page("logs")
        self._header(page, "Журнал", "Состояние Telegram, Discord и Rich Presence")
        self.log_box = ctk.CTkTextbox(
            page,
            fg_color=NAVY,
            text_color="#D7EEF4",
            corner_radius=18,
            border_width=0,
            font=ctk.CTkFont("Consolas", 12),
        )
        self.log_box.grid(row=2, column=0, padx=34, pady=(0, 12), sticky="nsew")
        page.grid_rowconfigure(2, weight=1)
        self.log_box.configure(state="disabled")
        ctk.CTkButton(
            page,
            text="Очистить журнал",
            width=150,
            height=38,
            fg_color=WHITE,
            hover_color=PALE_AQUA,
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            command=self.clear_logs,
        ).grid(row=3, column=0, padx=34, pady=(0, 22), sticky="e")

    def _build_about_page(self) -> None:
        page = self._new_page("about")
        self._header(page, "О программе", "Telegram RPC | By Apdeit")
        card = ctk.CTkFrame(page, fg_color=WHITE, corner_radius=20, border_width=1, border_color=BORDER)
        card.grid(row=2, column=0, padx=34, sticky="ew")
        ctk.CTkLabel(
            card,
            text="➤",
            width=72,
            height=72,
            corner_radius=36,
            fg_color=AQUA,
            text_color=NAVY,
            font=ctk.CTkFont("Segoe UI", 34, "bold"),
        ).grid(row=0, column=0, rowspan=3, padx=26, pady=26)
        ctk.CTkLabel(card, text=APP_TITLE, text_color=TEXT, font=ctk.CTkFont("Segoe UI", 20, "bold")).grid(
            row=0, column=1, padx=(0, 20), pady=(27, 2), sticky="sw"
        )
        ctk.CTkLabel(card, text=f"Версия {APP_VERSION}", text_color=SEA, font=ctk.CTkFont("Segoe UI", 12, "bold")).grid(
            row=1, column=1, padx=(0, 20), sticky="w"
        )
        ctk.CTkLabel(
            card,
            text=(
                "Передаёт название трека, исполнителя, статус воспроизведения "
                "и таймер из Telegram Desktop в Discord Rich Presence."
            ),
            wraplength=550,
            justify="left",
            text_color=MUTED,
            font=ctk.CTkFont("Segoe UI", 13),
        ).grid(row=2, column=1, padx=(0, 26), pady=(4, 26), sticky="nw")

        info = ctk.CTkFrame(page, fg_color=PALE_AQUA, corner_radius=16)
        info.grid(row=3, column=0, padx=34, pady=18, sticky="ew")
        ctk.CTkLabel(
            info,
            text="✓ Python и зависимости включаются в готовый EXE\n✓ Другу не нужно запускать setup.bat\n✓ Настройки хранятся в профиле Windows",
            justify="left",
            text_color=TEXT,
            font=ctk.CTkFont("Segoe UI", 13),
        ).grid(row=0, column=0, padx=22, pady=18, sticky="w")

    def _load_settings_into_form(self) -> None:
        self.client_id_entry.delete(0, "end")
        self.client_id_entry.insert(0, self.settings.discord_client_id)
        self.asset_entry.delete(0, "end")
        self.asset_entry.insert(0, self.settings.large_image)
        self.asset_text_entry.delete(0, "end")
        self.asset_text_entry.insert(0, self.settings.large_text)
        for switch, enabled in [
            (self.pause_switch, self.settings.show_when_paused),
            (self.autorun_switch, self.settings.start_with_windows),
            (self.auto_rpc_switch, self.settings.start_rpc_on_launch),
            (self.tray_switch, self.settings.minimize_to_tray),
        ]:
            switch.select() if enabled else switch.deselect()

    def show_page(self, name: str) -> None:
        self.current_page = name
        self.pages[name].tkraise()
        for key, button in self.nav_buttons.items():
            button.configure(fg_color=NAVY_LIGHT if key == name else "transparent")

    def _show_first_run(self) -> None:
        SeaDialog(
            self,
            "Первый запуск",
            "Зависимости уже встроены в приложение. Осталось указать Discord Application ID в настройках.",
            yes_text="Открыть настройки",
            no_text="Позже",
            on_yes=lambda: self.show_page("settings"),
        )

    def save_settings(self) -> None:
        client_id = self.client_id_entry.get().strip()
        if not is_valid_client_id(client_id):
            SeaDialog(
                self,
                "Неверный Application ID",
                "ID должен быть длинным числом из Discord Developer Portal. Не используйте Client Secret или токен.",
                yes_text="Понятно",
                no_text="Закрыть",
            )
            return

        old_autorun = self.settings.start_with_windows
        self.settings.discord_client_id = client_id
        self.settings.large_image = self.asset_entry.get().strip() or "telegram_music"
        self.settings.large_text = self.asset_text_entry.get().strip() or "Музыка из Telegram"
        self.settings.show_when_paused = bool(self.pause_switch.get())
        self.settings.start_with_windows = bool(self.autorun_switch.get())
        self.settings.start_rpc_on_launch = bool(self.auto_rpc_switch.get())
        self.settings.minimize_to_tray = bool(self.tray_switch.get())
        self.store.save(self.settings)

        if old_autorun != self.settings.start_with_windows:
            try:
                StartupManager.set_enabled(self.settings.start_with_windows)
            except Exception as exc:
                self.add_log("warning", f"Не удалось изменить автозапуск: {exc}")

        self.add_log("success", "Настройки сохранены")
        self.control_hint.configure(text="Настройки сохранены. Можно включать Telegram RPC.")
        self.show_page("main")

    def toggle_rpc(self) -> None:
        if self.rpc_running or (self.worker and self.worker.is_alive()):
            self.stop_rpc()
        else:
            self.request_start_rpc()

    def request_start_rpc(self) -> None:
        if not is_valid_client_id(self.settings.discord_client_id):
            self.show_page("settings")
            self.control_hint.configure(text="Сначала укажите Discord Application ID в настройках.")
            return
        if DiscordManager.is_running():
            self.start_rpc()
            return
        SeaDialog(
            self,
            "Discord не запущен",
            "Для Rich Presence нужен Discord Desktop. Открыть Discord сейчас и продолжить запуск RPC?",
            yes_text="Да, открыть Discord",
            no_text="Нет",
            on_yes=self._launch_discord_then_start,
        )

    def _launch_discord_then_start(self) -> None:
        self.set_busy(True, "Открываем Discord…")

        def task() -> None:
            launched = DiscordManager.launch()
            if not launched:
                self.events.put({"event": "discord_launch_result", "ok": False})
                return
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if DiscordManager.is_running():
                    self.events.put({"event": "discord_launch_result", "ok": True})
                    return
                time.sleep(0.5)
            self.events.put({"event": "discord_launch_result", "ok": False})

        threading.Thread(target=task, daemon=True).start()

    def start_rpc(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.rpc_running = True
        self.main_button.configure(text="Выключить RPC", fg_color=DANGER, hover_color="#B83F4B")
        self.hero_status.configure(text="RPC запускается…", text_color="#A9CFD9")
        self.rpc_badge.set("Запуск…", "warn")
        self.set_busy(True, "Подключаем Telegram и Discord…")
        self.worker = RpcWorker(self.settings, self.events)
        self.worker.start()
        self.add_log("info", "Запуск Telegram RPC")

    def stop_rpc(self) -> None:
        if self.worker:
            self.worker.stop()
        self.rpc_running = False
        self.main_button.configure(text="Да, включить RPC", fg_color=SEA, hover_color=SEA_HOVER)
        self.hero_status.configure(text="RPC выключается…", text_color="#A9CFD9")
        self.set_busy(True, "Очищаем активность Discord…")
        self.add_log("info", "Остановка Telegram RPC")

    def set_busy(self, busy: bool, text: str = "") -> None:
        if text:
            self.control_hint.configure(text=text)
        if busy:
            self.progress.grid()
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.grid_remove()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        if not self.exiting:
            self.after(120, self._poll_events)

    def _handle_event(self, data: dict[str, Any]) -> None:
        event = data.get("event")
        if event == "discord_launch_result":
            self.set_busy(False)
            if data.get("ok"):
                self.start_rpc()
            else:
                self.control_hint.configure(text="Не удалось открыть Discord. Запустите его вручную.")
                SeaDialog(
                    self,
                    "Discord не найден",
                    "Discord не запустился за 25 секунд. Откройте Discord Desktop вручную и повторите.",
                    yes_text="Понятно",
                    no_text="Закрыть",
                )
        elif event == "engine_ready":
            self.set_busy(False)
            self.hero_status.configure(text="RPC работает", text_color=AQUA)
            self.control_hint.configure(text="Ожидаем воспроизведение музыки в Telegram Desktop.")
            self.rpc_badge.set("Работает", "ok")
        elif event == "discord_process":
            running = bool(data.get("running"))
            self.discord_badge.set("Запущен" if running else "Не запущен", "ok" if running else "bad")
        elif event == "discord_rpc":
            connected = bool(data.get("connected"))
            self.rpc_badge.set("Подключён" if connected else "Ожидание", "ok" if connected else "warn")
        elif event == "telegram":
            active = bool(data.get("active"))
            self.telegram_badge.set("Трек найден" if active else "Нет музыки", "ok" if active else "idle")
            if not active and self.rpc_running:
                self.track_title.configure(text="Включите музыку в Telegram")
                self.track_artist.configure(text="RPC работает и ожидает трек")
        elif event == "track":
            title = shorten(str(data.get("title", "Неизвестный трек")), 56)
            artist = shorten(str(data.get("artist", "Неизвестный исполнитель")), 62)
            paused = data.get("status") == "paused"
            self.track_title.configure(text=title)
            self.track_artist.configure(text=("Пауза • " if paused else "") + artist)
            self.hero_status.configure(text="Трек на паузе" if paused else "Сейчас играет", text_color=AQUA)
        elif event == "presence_cleared":
            self.rpc_badge.set("Ожидает трек", "warn")
        elif event == "log":
            self.add_log(str(data.get("level", "info")), str(data.get("message", "")))
        elif event == "fatal":
            self.rpc_running = False
            self.set_busy(False)
            self.main_button.configure(text="Да, включить RPC", fg_color=SEA, hover_color=SEA_HOVER)
            self.rpc_badge.set("Ошибка", "bad")
            self.add_log("error", str(data.get("message", "Неизвестная ошибка")))
            details = str(data.get("details", ""))
            if details:
                self.add_log("error", details)
            SeaDialog(
                self,
                "Ошибка Telegram RPC",
                str(data.get("message", "Произошла неизвестная ошибка.")),
                yes_text="Открыть журнал",
                no_text="Закрыть",
                on_yes=lambda: self.show_page("logs"),
            )
        elif event == "worker_stopped":
            self.rpc_running = False
            self.worker = None
            self.set_busy(False)
            self.main_button.configure(text="Да, включить RPC", fg_color=SEA, hover_color=SEA_HOVER)
            self.hero_status.configure(text="RPC выключен", text_color="#93C0CD")
            self.track_title.configure(text="Включите музыку в Telegram")
            self.track_artist.configure(text="Название и исполнитель появятся здесь")
            self.rpc_badge.set("Выключен", "idle")
            self.telegram_badge.set("Ожидание", "idle")
            self.control_hint.configure(text="Приложение готово к следующему запуску.")

    def _poll_system_status(self) -> None:
        running = DiscordManager.is_running()
        self.discord_badge.set("Запущен" if running else "Не запущен", "ok" if running else "bad")
        if not self.exiting:
            self.after(1500, self._poll_system_status)

    def add_log(self, level: str, message: str) -> None:
        if not message:
            return
        icons = {"success": "OK", "warning": "WARN", "error": "ERR", "info": "INFO"}
        timestamp = time.strftime("%H:%M:%S")
        line = f"{timestamp}  {icons.get(level, 'INFO'):>4}  {message}"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-500:]
        if hasattr(self, "log_box"):
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

    def clear_logs(self) -> None:
        self.log_lines.clear()
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _start_tray(self) -> None:
        icon_path = resource_path("assets/icon.png")
        if not icon_path.exists():
            return
        try:
            image = Image.open(icon_path)
            menu = pystray.Menu(
                pystray.MenuItem("Открыть приложение", lambda icon, item: self.after(0, self.restore_window), default=True),
                pystray.MenuItem("Включить / выключить RPC", lambda icon, item: self.after(0, self.toggle_rpc)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda icon, item: self.after(0, self.exit_app)),
            )
            self.tray_icon = pystray.Icon(APP_SLUG, image, APP_TITLE, menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception as exc:
            self.add_log("warning", f"Системный трей недоступен: {exc}")

    def restore_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def on_window_close(self) -> None:
        if self.settings.minimize_to_tray and self.tray_icon is not None:
            self.withdraw()
            try:
                self.tray_icon.notify("Приложение продолжает работать в фоне", APP_TITLE)
            except Exception:
                pass
        else:
            self.exit_app()

    def exit_app(self) -> None:
        if self.exiting:
            return
        self.exiting = True
        if self.worker:
            self.worker.stop()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.after(350, self.destroy)


def main() -> None:
    app = TelegramRpcApp()
    app.mainloop()


if __name__ == "__main__":
    main()
