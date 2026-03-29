from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import mimetypes
import secrets
import subprocess
import tempfile
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import quote

import aiohttp
import typer
from loguru import logger
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bub.channels.base import Channel
from bub.channels.message import ChannelMessage, MediaItem
from bub.types import MessageHandler

DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_BOT_TYPE = "3"
DEFAULT_CHANNEL_VERSION = "0.3.1"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
DEFAULT_API_TIMEOUT_MS = 15_000
DEFAULT_TYPING_TIMEOUT_MS = 10_000
DEFAULT_TYPING_REFRESH_SECONDS = 4.0
UPLOAD_MEDIA_IMAGE = 1
UPLOAD_MEDIA_VIDEO = 2
UPLOAD_MEDIA_FILE = 3
UPLOAD_MEDIA_VOICE = 4
MESSAGE_ITEM_TEXT = 1
MESSAGE_ITEM_IMAGE = 2
MESSAGE_ITEM_VOICE = 3
MESSAGE_ITEM_FILE = 4
MESSAGE_ITEM_VIDEO = 5
MESSAGE_STATE_FINISH = 2
MESSAGE_TYPE_BOT = 2
TYPING_STATUS_TYPING = 1
TYPING_STATUS_CANCEL = 2

def _default_state_file() -> Path:
    return Path.home() / ".local" / "state" / "bub" / "wechat-session.json"


class WeChatSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUB_WECHAT_", extra="ignore", env_file=".env")

    state_file: Path = Field(default_factory=_default_state_file)
    allow_users: str | None = Field(default=None, description="Comma-separated allowed WeChat user IDs.")
    allow_chats: str | None = Field(default=None, description="Comma-separated allowed chat IDs.")
    poll_interval_seconds: float = Field(default=1.0, description="Idle sleep between empty long-poll loops.")
    api_base_url: str = Field(default=DEFAULT_API_BASE_URL)
    cdn_base_url: str = Field(default=DEFAULT_CDN_BASE_URL)
    bot_type: str = Field(default=DEFAULT_BOT_TYPE)
    login_timeout_seconds: float = Field(default=480.0)
    temp_dir: Path = Field(default_factory=lambda: Path(tempfile.gettempdir()) / "bub" / "wechat")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "login": None, "sync": {"get_updates_buf": ""}, "contacts": {}}
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _client_version(version: str) -> int:
    parts = [int(piece) for piece in version.split(".")]
    major = parts[0] if len(parts) > 0 else 0
    minor = parts[1] if len(parts) > 1 else 0
    patch = parts[2] if len(parts) > 2 else 0
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _random_wechat_uin() -> str:
    return base64.b64encode(str(secrets.randbits(32)).encode("utf-8")).decode("utf-8")


def _ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


def _mime_from_filename(filename: str | None) -> str:
    if not filename:
        return "application/octet-stream"
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"


def _extension_from_mime(mime_type: str) -> str:
    extension = mimetypes.guess_extension(mime_type.split(";")[0].strip().lower())
    return extension or ".bin"


def _openssl_ecb(data: bytes, key_hex: str, *, decrypt: bool) -> bytes:
    command = ["openssl", "enc", "-aes-128-ecb", "-K", key_hex, "-nosalt"]
    if decrypt:
        command.append("-d")
    completed = subprocess.run(command, input=data, capture_output=True, check=False)
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip() or "(no stderr)"
        raise RuntimeError(f"openssl aes-128-ecb failed: {stderr}")
    return completed.stdout


def _parse_aes_key(aes_key_base64: str) -> bytes:
    decoded = base64.b64decode(aes_key_base64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            text = decoded.decode("ascii")
        except UnicodeDecodeError as exc:  # pragma: no cover - defensive
            raise ValueError("invalid ASCII-hex AES key") from exc
        if all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key payload length={len(decoded)}")


def _download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    return f"{cdn_base_url}/download?encrypted_query_param={quote(encrypted_query_param)}"


def _upload_url(cdn_base_url: str, upload_param: str, file_key: str) -> str:
    return f"{cdn_base_url}/upload?encrypted_query_param={quote(upload_param)}&filekey={quote(file_key)}"


async def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


@dataclass(slots=True)
class UploadedFile:
    download_param: str
    aes_key_hex: str
    file_size: int
    ciphertext_size: int


class WeChatApiClient:
    def __init__(self, settings: WeChatSettings) -> None:
        self._settings = settings
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:  # pragma: no cover - guarded by start()
            raise RuntimeError("WeChatApiClient is not started")
        return self._session

    def _headers(self, body: bytes | None = None, token: str | None = None) -> dict[str, str]:
        headers = {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str(_client_version(DEFAULT_CHANNEL_VERSION)),
            "X-WECHAT-UIN": _random_wechat_uin(),
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
            headers["AuthorizationType"] = "ilink_bot_token"
        if token:
            headers["Authorization"] = f"Bearer {token.strip()}"
        return headers

    async def _get(self, base_url: str, endpoint: str, *, timeout_ms: int) -> dict[str, Any]:
        url = f"{_ensure_trailing_slash(base_url)}{endpoint}"
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
        async with self.session.get(url, headers=self._headers(), timeout=timeout) as response:
            text = await response.text()
            response.raise_for_status()
            return cast("dict[str, Any]", json.loads(text))

    async def _post(
        self,
        base_url: str,
        endpoint: str,
        payload: dict[str, Any],
        *,
        token: str | None,
        timeout_ms: int,
    ) -> dict[str, Any]:
        body = json.dumps({**payload, "base_info": {"channel_version": DEFAULT_CHANNEL_VERSION}}).encode("utf-8")
        url = f"{_ensure_trailing_slash(base_url)}{endpoint}"
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
        async with self.session.post(url, data=body, headers=self._headers(body, token), timeout=timeout) as response:
            text = await response.text()
            response.raise_for_status()
            return cast("dict[str, Any]", json.loads(text))

    async def get_bot_qrcode(self, *, bot_type: str) -> dict[str, Any]:
        return await self._get(
            self._settings.api_base_url,
            f"ilink/bot/get_bot_qrcode?bot_type={quote(bot_type)}",
            timeout_ms=5_000,
        )

    async def get_qrcode_status(self, *, qrcode: str, base_url: str) -> dict[str, Any]:
        try:
            return await self._get(
                base_url,
                f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode)}",
                timeout_ms=DEFAULT_LONG_POLL_TIMEOUT_MS,
            )
        except TimeoutError:
            return {"status": "wait"}

    async def get_updates(self, *, base_url: str, token: str, cursor: str) -> dict[str, Any]:
        try:
            return await self._post(
                base_url,
                "ilink/bot/getupdates",
                {"get_updates_buf": cursor},
                token=token,
                timeout_ms=DEFAULT_LONG_POLL_TIMEOUT_MS,
            )
        except TimeoutError:
            return {"ret": 0, "msgs": [], "get_updates_buf": cursor}

    async def get_upload_url(
        self,
        *,
        base_url: str,
        token: str,
        file_key: str,
        media_type: int,
        to_user_id: str,
        raw_size: int,
        raw_md5: str,
        ciphertext_size: int,
        aes_key_hex: str,
    ) -> dict[str, Any]:
        return await self._post(
            base_url,
            "ilink/bot/getuploadurl",
            {
                "filekey": file_key,
                "media_type": media_type,
                "to_user_id": to_user_id,
                "rawsize": raw_size,
                "rawfilemd5": raw_md5,
                "filesize": ciphertext_size,
                "no_need_thumb": True,
                "aeskey": aes_key_hex,
            },
            token=token,
            timeout_ms=DEFAULT_API_TIMEOUT_MS,
        )

    async def send_message(self, *, base_url: str, token: str, payload: dict[str, Any]) -> None:
        await self._post(
            base_url,
            "ilink/bot/sendmessage",
            payload,
            token=token,
            timeout_ms=DEFAULT_API_TIMEOUT_MS,
        )

    async def get_config(
        self,
        *,
        base_url: str,
        token: str,
        ilink_user_id: str,
        context_token: str | None,
    ) -> dict[str, Any]:
        return await self._post(
            base_url,
            "ilink/bot/getconfig",
            {"ilink_user_id": ilink_user_id, "context_token": context_token},
            token=token,
            timeout_ms=DEFAULT_TYPING_TIMEOUT_MS,
        )

    async def send_typing(
        self, *, base_url: str, token: str, ilink_user_id: str, typing_ticket: str, status: int
    ) -> None:
        await self._post(
            base_url,
            "ilink/bot/sendtyping",
            {"ilink_user_id": ilink_user_id, "typing_ticket": typing_ticket, "status": status},
            token=token,
            timeout_ms=DEFAULT_TYPING_TIMEOUT_MS,
        )

    async def upload_media(
        self,
        *,
        base_url: str,
        cdn_base_url: str,
        token: str,
        to_user_id: str,
        file_path: Path,
        media_type: int,
    ) -> UploadedFile:
        plaintext = file_path.read_bytes()
        file_key = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        aes_key_hex = aes_key.hex()
        ciphertext = _openssl_ecb(plaintext, aes_key_hex, decrypt=False)
        upload_meta = await self.get_upload_url(
            base_url=base_url,
            token=token,
            file_key=file_key,
            media_type=media_type,
            to_user_id=to_user_id,
            raw_size=len(plaintext),
            raw_md5=hashlib.md5(plaintext, usedforsecurity=False).hexdigest(),
            ciphertext_size=len(ciphertext),
            aes_key_hex=aes_key_hex,
        )
        upload_full_url = str(upload_meta.get("upload_full_url") or "").strip()
        upload_param = str(upload_meta.get("upload_param") or "").strip()
        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = _upload_url(cdn_base_url, upload_param, file_key)
        else:
            raise RuntimeError("WeChat upload metadata is missing upload url fields")
        async with self.session.post(
            upload_url,
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=aiohttp.ClientTimeout(total=DEFAULT_API_TIMEOUT_MS / 1000),
        ) as response:
            response.raise_for_status()
            download_param = response.headers.get("x-encrypted-param")
            if not download_param:
                raise RuntimeError("WeChat CDN response is missing x-encrypted-param")
        return UploadedFile(
            download_param=download_param,
            aes_key_hex=aes_key_hex,
            file_size=len(plaintext),
            ciphertext_size=len(ciphertext),
        )

    async def download_media(
        self,
        *,
        cdn_base_url: str,
        encrypt_query_param: str,
        aes_key_base64: str | None,
        full_url: str | None,
    ) -> bytes:
        url = full_url or _download_url(cdn_base_url, encrypt_query_param)
        async with self.session.get(
            url, timeout=aiohttp.ClientTimeout(total=DEFAULT_API_TIMEOUT_MS / 1000)
        ) as response:
            response.raise_for_status()
            payload = await response.read()
        if not aes_key_base64:
            return payload
        return _openssl_ecb(payload, _parse_aes_key(aes_key_base64).hex(), decrypt=True)


class WeChatChannel(Channel):
    name = "wechat"
    _POLL_SLEEP_SECONDS: ClassVar[float] = 0.1

    def __init__(self, on_receive: MessageHandler, *, settings: WeChatSettings | None = None) -> None:
        self._on_receive = on_receive
        self._settings = settings or WeChatSettings()
        self._state = _load_json(self._settings.state_file)
        self._client = WeChatApiClient(self._settings)
        self._allow_users = {
            token.strip() for token in (self._settings.allow_users or "").split(",") if token.strip()
        }
        self._allow_chats = {
            token.strip() for token in (self._settings.allow_chats or "").split(",") if token.strip()
        }
        self._poll_task: asyncio.Task[None] | None = None
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}
        self._typing_tickets: dict[str, str] = {}

    @property
    def needs_debounce(self) -> bool:
        return True

    async def start(self, stop_event: asyncio.Event) -> None:
        await self._client.start()
        login = self._state.get("login") or {}
        if not login.get("bot_token"):
            logger.info("wechat.start skipped: no logged-in session in {}", self._settings.state_file)
            return
        self._poll_task = asyncio.create_task(self._poll_loop(stop_event))
        logger.info("wechat.start polling state_file={}", self._settings.state_file)

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        for task in self._typing_tasks.values():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._typing_tasks.clear()
        await self._client.stop()

    async def send(self, message: ChannelMessage) -> None:
        self._refresh_state_from_disk()
        login = self._require_login()
        user_id = message.chat_id
        context_token = self._contact(user_id).get("context_token")
        text = message.content.strip()
        media = list(message.media)
        if not text and not media:
            return
        if not media:
            await self._send_text(login=login, to_user_id=user_id, text=text, context_token=context_token)
            return

        caption = text
        for index, item in enumerate(media):
            await self._send_media(
                login=login,
                to_user_id=user_id,
                item=item,
                text=caption if index == 0 else "",
                context_token=context_token,
            )

    @contextlib.asynccontextmanager
    async def start_typing(self, user_id: str, context_token: str | None) -> AsyncGenerator[None, None]:
        if user_id in self._typing_tasks:
            yield
            return
        login = self._require_login()
        typing_ticket = await self._ensure_typing_ticket(
            login=login, user_id=user_id, context_token=context_token
        )
        if not typing_ticket:
            yield
            return
        task = asyncio.create_task(self._typing_loop(login=login, user_id=user_id, typing_ticket=typing_ticket))
        self._typing_tasks[user_id] = task
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._typing_tasks.pop(user_id, None)
            with contextlib.suppress(Exception):
                await self._client.send_typing(
                    base_url=login["base_url"],
                    token=login["bot_token"],
                    ilink_user_id=user_id,
                    typing_ticket=typing_ticket,
                    status=TYPING_STATUS_CANCEL,
                )

    async def _typing_loop(self, *, login: dict[str, Any], user_id: str, typing_ticket: str) -> None:
        while True:
            try:
                await self._client.send_typing(
                    base_url=login["base_url"],
                    token=login["bot_token"],
                    ilink_user_id=user_id,
                    typing_ticket=typing_ticket,
                    status=TYPING_STATUS_TYPING,
                )
                await asyncio.sleep(DEFAULT_TYPING_REFRESH_SECONDS)
            except Exception as exc:
                logger.warning("wechat.typing_loop user_id={} error={}", user_id, exc)
                return

    async def _poll_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            login = self._require_login()
            response = await self._client.get_updates(
                base_url=login["base_url"],
                token=login["bot_token"],
                cursor=str((self._state.get("sync") or {}).get("get_updates_buf") or ""),
            )
            sync = self._state.setdefault("sync", {})
            if response.get("get_updates_buf") is not None:
                sync["get_updates_buf"] = response["get_updates_buf"]
            messages = response.get("msgs") or []
            for raw_message in messages:
                await self._handle_inbound(raw_message)
            _save_json(self._settings.state_file, self._state)
            if not messages:
                await asyncio.sleep(max(self._settings.poll_interval_seconds, self._POLL_SLEEP_SECONDS))

    async def _handle_inbound(self, raw_message: dict[str, Any]) -> None:
        user_id = str(raw_message.get("from_user_id") or raw_message.get("to_user_id") or "").strip()
        if not user_id:
            return
        if self._allow_users and user_id not in self._allow_users:
            return
        if self._allow_chats and user_id not in self._allow_chats:
            return

        context_token = raw_message.get("context_token")
        if context_token:
            self._contact(user_id)["context_token"] = context_token
        self._contact(user_id)["last_message_id"] = raw_message.get("message_id")
        self._contact(user_id)["last_seen_at"] = time.time()
        self._contact(user_id)["session_id"] = raw_message.get("session_id")

        media = await self._extract_media(raw_message.get("item_list") or [])
        content = _message_body(raw_message.get("item_list") or [])
        if not content and media:
            content = "[WeChat media message]"
        message = ChannelMessage(
            session_id=f"{self.name}:{user_id}",
            channel=self.name,
            chat_id=user_id,
            content=content,
            is_active=True,
            context={
                "sender_id": user_id,
                "message_id": raw_message.get("message_id"),
                "context_token": context_token or "",
                "item_types": ",".join(str(item.get("type")) for item in raw_message.get("item_list") or []),
            },
            media=media,
            lifespan=self.start_typing(user_id, context_token),
        )
        await self._on_receive(message)

    async def _extract_media(self, item_list: list[dict[str, Any]]) -> list[MediaItem]:
        results: list[MediaItem] = []
        for item in item_list:
            item_type = item.get("type")
            if item_type == MESSAGE_ITEM_IMAGE:
                image_item = item.get("image_item") or {}
                media_ref = image_item.get("media") or {}
                attachment = await self._download_attachment(
                    media_ref=media_ref,
                    mime_type="image/jpeg",
                    filename=f"wechat-image-{item.get('msg_id') or secrets.token_hex(4)}.jpg",
                    media_type="image",
                )
                if attachment:
                    results.append(attachment)
            elif item_type == MESSAGE_ITEM_VIDEO:
                video_item = item.get("video_item") or {}
                media_ref = video_item.get("media") or {}
                attachment = await self._download_attachment(
                    media_ref=media_ref,
                    mime_type="video/mp4",
                    filename=f"wechat-video-{item.get('msg_id') or secrets.token_hex(4)}.mp4",
                    media_type="video",
                )
                if attachment:
                    results.append(attachment)
            elif item_type == MESSAGE_ITEM_FILE:
                file_item = item.get("file_item") or {}
                filename = str(file_item.get("file_name") or f"wechat-file-{secrets.token_hex(4)}")
                attachment = await self._download_attachment(
                    media_ref=file_item.get("media") or {},
                    mime_type=_mime_from_filename(filename),
                    filename=filename,
                    media_type="document",
                )
                if attachment:
                    results.append(attachment)
            elif item_type == MESSAGE_ITEM_VOICE:
                voice_item = item.get("voice_item") or {}
                attachment = await self._download_attachment(
                    media_ref=voice_item.get("media") or {},
                    mime_type="audio/silk",
                    filename=f"wechat-voice-{item.get('msg_id') or secrets.token_hex(4)}.silk",
                    media_type="audio",
                )
                if attachment:
                    results.append(attachment)
        return results

    async def _download_attachment(
        self,
        *,
        media_ref: dict[str, Any],
        mime_type: str,
        filename: str,
        media_type: str,
    ) -> MediaItem | None:
        encrypt_query_param = str(media_ref.get("encrypt_query_param") or "").strip()
        full_url = str(media_ref.get("full_url") or "").strip() or None
        aes_key = media_ref.get("aes_key")
        if not encrypt_query_param and not full_url:
            return None
        try:
            payload = await self._client.download_media(
                cdn_base_url=self._settings.cdn_base_url,
                encrypt_query_param=encrypt_query_param,
                aes_key_base64=str(aes_key) if aes_key else None,
                full_url=full_url,
            )
        except Exception as exc:
            logger.warning("wechat.download_attachment filename={} error={}", filename, exc)
            return None
        target_dir = self._settings.temp_dir / "inbound"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename
        target_path.write_bytes(payload)
        return MediaItem(
            type=media_type,  # type: ignore[arg-type]
            mime_type=mime_type,
            filename=filename,
            data_fetcher=partial(_read_bytes, target_path),
        )

    def _contact(self, user_id: str) -> dict[str, Any]:
        contacts = cast("dict[str, dict[str, Any]]", self._state.setdefault("contacts", {}))
        return contacts.setdefault(user_id, {})

    def _refresh_state_from_disk(self) -> None:
        latest = _load_json(self._settings.state_file)
        if not latest:
            return
        if isinstance(latest.get("login"), dict):
            self._state["login"] = latest["login"]
        if isinstance(latest.get("sync"), dict):
            self._state["sync"] = latest["sync"]
        latest_contacts = latest.get("contacts")
        if isinstance(latest_contacts, dict):
            merged_contacts = self._state.setdefault("contacts", {})
            for user_id, payload in latest_contacts.items():
                if isinstance(payload, dict):
                    merged_contacts[user_id] = {**merged_contacts.get(user_id, {}), **payload}

    def _require_login(self) -> dict[str, Any]:
        login = self._state.get("login") or {}
        if not login.get("bot_token") or not login.get("base_url"):
            raise RuntimeError(
                f"WeChat is not logged in yet. Run `bub login wechat --state-file {self._settings.state_file}` first."
            )
        return login

    async def _ensure_typing_ticket(
        self, *, login: dict[str, Any], user_id: str, context_token: str | None
    ) -> str | None:
        if ticket := self._typing_tickets.get(user_id):
            return ticket
        try:
            response = await self._client.get_config(
                base_url=login["base_url"],
                token=login["bot_token"],
                ilink_user_id=user_id,
                context_token=context_token,
            )
        except Exception as exc:
            logger.warning("wechat.get_config user_id={} error={}", user_id, exc)
            return None
        ticket = str(response.get("typing_ticket") or "").strip()
        if ticket:
            self._typing_tickets[user_id] = ticket
        return ticket or None

    async def _send_text(
        self, *, login: dict[str, Any], to_user_id: str, text: str, context_token: str | None
    ) -> None:
        if not text.strip():
            return
        await self._client.send_message(
            base_url=login["base_url"],
            token=login["bot_token"],
            payload={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": f"bub-wechat-{secrets.token_hex(8)}",
                    "message_type": MESSAGE_TYPE_BOT,
                    "message_state": MESSAGE_STATE_FINISH,
                    "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": text}}],
                    "context_token": context_token or None,
                }
            },
        )

    async def _send_media(
        self,
        *,
        login: dict[str, Any],
        to_user_id: str,
        item: MediaItem,
        text: str,
        context_token: str | None,
    ) -> None:
        file_path = await self._materialize_media(item)
        mime_type = item.mime_type or _mime_from_filename(item.filename)
        item_payload: dict[str, Any]
        if item.type == "image" or mime_type.startswith("image/"):
            upload_kind = UPLOAD_MEDIA_IMAGE
            item_payload = {
                "type": MESSAGE_ITEM_IMAGE,
                "image_item": {
                    "media": {
                        "encrypt_query_param": "",
                    }
                },
            }
        elif item.type == "video" or mime_type.startswith("video/"):
            upload_kind = UPLOAD_MEDIA_VIDEO
            item_payload = {
                "type": MESSAGE_ITEM_VIDEO,
                "video_item": {
                    "media": {
                        "encrypt_query_param": "",
                    }
                },
            }
        elif item.type == "audio":
            upload_kind = UPLOAD_MEDIA_VOICE
            item_payload = {
                "type": MESSAGE_ITEM_FILE,
                "file_item": {
                    "media": {
                        "encrypt_query_param": "",
                    },
                    "file_name": file_path.name,
                    "len": "",
                },
            }
        else:
            upload_kind = UPLOAD_MEDIA_FILE
            item_payload = {
                "type": MESSAGE_ITEM_FILE,
                "file_item": {
                    "media": {
                        "encrypt_query_param": "",
                    },
                    "file_name": file_path.name,
                    "len": "",
                },
            }

        uploaded = await self._client.upload_media(
            base_url=login["base_url"],
            cdn_base_url=str(login.get("cdn_base_url") or self._settings.cdn_base_url),
            token=login["bot_token"],
            to_user_id=to_user_id,
            file_path=file_path,
            media_type=upload_kind,
        )

        media_ref = {
            "encrypt_query_param": uploaded.download_param,
            "aes_key": base64.b64encode(uploaded.aes_key_hex.encode("ascii")).decode("utf-8"),
            "encrypt_type": 1,
        }
        if "image_item" in item_payload:
            item_payload["image_item"]["media"] = media_ref
            item_payload["image_item"]["mid_size"] = uploaded.ciphertext_size
        elif "video_item" in item_payload:
            item_payload["video_item"]["media"] = media_ref
            item_payload["video_item"]["video_size"] = uploaded.ciphertext_size
        else:
            item_payload["file_item"]["media"] = media_ref
            item_payload["file_item"]["len"] = str(uploaded.file_size)

        messages: list[dict[str, Any]] = []
        if text.strip():
            messages.append({"type": MESSAGE_ITEM_TEXT, "text_item": {"text": text}})
        messages.append(item_payload)
        for payload_item in messages:
            await self._client.send_message(
                base_url=login["base_url"],
                token=login["bot_token"],
                payload={
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": to_user_id,
                        "client_id": f"bub-wechat-{secrets.token_hex(8)}",
                        "message_type": MESSAGE_TYPE_BOT,
                        "message_state": MESSAGE_STATE_FINISH,
                        "item_list": [payload_item],
                        "context_token": context_token or None,
                    }
                },
            )

    async def _materialize_media(self, item: MediaItem) -> Path:
        target_dir = self._settings.temp_dir / "outbound"
        target_dir.mkdir(parents=True, exist_ok=True)
        if item.url and item.url.startswith("file://"):
            return Path(item.url.removeprefix("file://"))
        if item.url and item.url.startswith("data:"):
            header, encoded = item.url.split(",", 1)
            is_base64 = header.endswith(";base64")
            raw = base64.b64decode(encoded) if is_base64 else encoded.encode("utf-8")
            extension = _extension_from_mime(item.mime_type)
            target_path = target_dir / (
                item.filename or f"wechat-media-{secrets.token_hex(4)}{extension}"
            )
            target_path.write_bytes(raw)
            return target_path
        if item.url and (item.url.startswith("http://") or item.url.startswith("https://")):
            async with self._client.session.get(
                item.url, timeout=aiohttp.ClientTimeout(total=DEFAULT_API_TIMEOUT_MS / 1000)
            ) as response:
                response.raise_for_status()
                raw = await response.read()
            extension = _extension_from_mime(item.mime_type)
            target_path = target_dir / (
                item.filename or f"wechat-media-{secrets.token_hex(4)}{extension}"
            )
            target_path.write_bytes(raw)
            return target_path
        if item.data_fetcher is not None:
            raw = await item.data_fetcher()
            extension = Path(item.filename or "").suffix or _extension_from_mime(item.mime_type)
            target_path = target_dir / (
                item.filename or f"wechat-media-{secrets.token_hex(4)}{extension}"
            )
            target_path.write_bytes(raw)
            return target_path
        raise RuntimeError(f"media item {item.filename or item.mime_type} has no readable source")


def _message_body(item_list: list[dict[str, Any]]) -> str:
    for item in item_list:
        item_type = item.get("type")
        if item_type == MESSAGE_ITEM_TEXT:
            text_item = item.get("text_item") or {}
            text = str(text_item.get("text") or "").strip()
            if text:
                return text
        if item_type == MESSAGE_ITEM_VOICE:
            voice_item = item.get("voice_item") or {}
            text = str(voice_item.get("text") or "").strip()
            if text:
                return text
    return ""


async def _login_wechat_async(settings: WeChatSettings) -> dict[str, Any]:
    client = WeChatApiClient(settings)
    await client.start()
    try:
        qr = await client.get_bot_qrcode(bot_type=settings.bot_type)
        qrcode = str(qr.get("qrcode") or "").strip()
        qrcode_url = str(qr.get("qrcode_img_content") or "").strip()
        if not qrcode or not qrcode_url:
            raise RuntimeError(f"WeChat QR login did not return a usable QR payload: {qr}")
        typer.echo("wechat login: scan this QR in WeChat")
        typer.echo(qrcode_url)
        typer.echo("wechat login: the QR page itself may not redirect; watch this terminal for status updates")
        deadline = time.monotonic() + settings.login_timeout_seconds
        current_base_url = settings.api_base_url
        last_state = ""
        while time.monotonic() < deadline:
            status = await client.get_qrcode_status(qrcode=qrcode, base_url=current_base_url)
            state = str(status.get("status") or "").strip()
            if state and state != last_state and state not in {"wait", "confirmed"}:
                if state == "scanned":
                    typer.echo("wechat login: QR scanned, confirm the login in WeChat")
                else:
                    typer.echo(f"wechat login: status={state}")
                last_state = state
            if state == "confirmed" and status.get("bot_token") and status.get("baseurl"):
                login = {
                    "bot_token": status["bot_token"],
                    "base_url": status["baseurl"],
                    "ilink_bot_id": status.get("ilink_bot_id"),
                    "ilink_user_id": status.get("ilink_user_id"),
                    "cdn_base_url": settings.cdn_base_url,
                    "linked_at": time.time(),
                }
                data = _load_json(settings.state_file)
                data["login"] = login
                data.setdefault("sync", {})["get_updates_buf"] = ""
                _save_json(settings.state_file, data)
                typer.echo("wechat login: ok")
                typer.echo(f"state_file: {settings.state_file}")
                return login
            if state == "expired":
                raise RuntimeError("WeChat QR code expired before confirmation")
            await asyncio.sleep(1.0)
        raise RuntimeError("WeChat login timed out")
    finally:
        await client.stop()


def login_command(
    state_file: Path | None = typer.Option(None, "--state-file", help="Override the WeChat state file"),  # noqa: B008
) -> None:
    settings = WeChatSettings(state_file=state_file or _default_state_file())
    asyncio.run(_login_wechat_async(settings))
