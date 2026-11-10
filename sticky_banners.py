"""Repost two independent Discord Components V2 banner messages.

Originals are edited with Discohook in a separate private source channel.
Only Discord webhook URLs and message IDs kept in GitHub Actions secrets.
No message content privileged intent is required.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib import error, parse, request

API = "https://discord.com/api/v10"
STATE = Path(__file__).with_name("state.json")
USER_AGENT = "SGA-Banner-Sticky/1.0 (GitHub Actions)"
V2_FLAG = 1 << 15
MAX_MEDIA = 10


class SafeError(Exception):
    """Exception that never includes a webhook URL or token."""


def discord(method: str, url: str, *, token: str = "", payload=None, missing_ok=False):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bot " + token
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(3):
        req = request.Request(url, method=method, data=body, headers=headers)
        try:
            with request.urlopen(req, timeout=20) as response:
                result = response.read().decode("utf-8")
                return json.loads(result) if result else None
        except error.HTTPError as ex:
            if ex.code == 404 and missing_ok:
                return None
            try:
                info = json.loads(ex.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                info = {}
            if ex.code == 429 and attempt < 2:
                time.sleep(min(12, max(1, float(info.get("retry_after", 2)))))
                continue
            raise SafeError(
                f"Discord API HTTP {ex.code} (code {info.get('code', 'unknown')}): "
                f"{str(info.get('message', 'Request failed'))[:160]}"
            ) from None
        except (error.URLError, TimeoutError):
            raise SafeError("Discord network request failed") from None
    raise SafeError("Discord API retry limit reached")


def webhook_url(value: str) -> str:
    u = parse.urlsplit(value.strip())
    if u.scheme != "https" or u.hostname not in ("discord.com", "discordapp.com"):
        raise SafeError("Webhook must be an official HTTPS Discord webhook")
    if not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+", u.path):
        raise SafeError("Webhook URL format invalid")
    return "https://discord.com" + u.path


def stable_media_url(raw: str, files: dict[str, str]) -> str:
    if raw.startswith("attachment://"):
        filename = parse.unquote(raw[len("attachment://"):])
        if filename not in files:
            raise SafeError("Source media attachment not found; repost original using Discohook")
        raw = files[filename]
    u = parse.urlsplit(raw)
    if u.scheme != "https" or not u.netloc:
        raise SafeError("Source banner image must use an HTTPS URL")
    # Discord accepts its CDN attachment URL without expiring signature params.
    if u.hostname in ("cdn.discordapp.com", "media.discordapp.net"):
        return parse.urlunsplit((u.scheme, u.netloc, u.path, "", ""))
    return raw


def clean_component(item: dict, files: dict[str, str]) -> dict:
    kind = item.get("type")
    if kind == 1:  # Link-button action row
        buttons = []
        for button in item.get("components", []):
            if button.get("type") != 2 or button.get("style") != 5:
                raise SafeError("Only external-link buttons are supported")
            link = button.get("url", "")
            if not isinstance(link, str) or not link.startswith("https://"):
                raise SafeError("Link button has no valid HTTPS destination")
            b = {"type": 2, "style": 5, "url": link}
            for key in ("label", "emoji", "disabled"):
                if key in button:
                    b[key] = button[key]
            buttons.append(b)
        return {"type": 1, "components": buttons}
    if kind == 10:  # Text display
        return {"type": 10, "content": item.get("content", "")}
    if kind == 11:  # Thumbnail
        media = (item.get("media") or {}).get("url", "")
        return {"type": 11, "media": {"url": stable_media_url(media, files)}}
    if kind == 12:  # Media gallery
        items = []
        for src in item.get("items", []):
            media = (src.get("media") or {}).get("url", "")
            m = {"media": {"url": stable_media_url(media, files)}}
            for key in ("description", "spoiler"):
                if key in src:
                    m[key] = src[key]
            items.append(m)
        return {"type": 12, "items": items}
    if kind == 14:  # Separator
        result = {"type": 14}
        for key in ("divider", "spacing"):
            if key in item:
                result[key] = item[key]
        return result
    if kind == 9:  # Section
        result = {"type": 9, "components": [
            clean_component(x, files) for x in item.get("components", [])
        ]}
        if item.get("accessory"):
            result["accessory"] = clean_component(item["accessory"], files)
        return result
    if kind == 17:  # Container
        result = {"type": 17, "components": [
            clean_component(x, files) for x in item.get("components", [])
        ]}
        for key in ("accent_color", "spoiler"):
            if key in item:
                result[key] = item[key]
        return result
    raise SafeError(f"Unsupported Discord component type: {kind}")


def traverse(items):
    for item in items:
        yield item
        if "components" in item:
            yield from traverse(item["components"])
        if "accessory" in item:
            yield from traverse([item["accessory"]])


def source_payload(source: dict) -> dict:
    attachments = source.get("attachments") or []
    file_urls = {
        a["filename"]: a["url"]
        for a in attachments
        if a.get("filename") and a.get("url")
    }
    components = [clean_component(x, file_urls) for x in source.get("components") or []]

    # A Discohook "Standard Message" may place the image in Attachments
    # rather than a Media Gallery. Convert those attached images to gallery.
    existing_urls = {
        item["media"]["url"]
        for comp in traverse(components)
        if comp["type"] == 12
        for item in comp.get("items", [])
    }
    for comp in traverse(components):
        if comp["type"] == 11:
            existing_urls.add(comp["media"]["url"])

    new_images = []
    for a in attachments:
        name = (a.get("filename") or "").lower()
        if not name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            continue
        url = stable_media_url(a["url"], file_urls)
        if url not in existing_urls:
            new_images.append({"media": {"url": url}})
    if new_images:
        if len(new_images) > MAX_MEDIA:
            raise SafeError("Too many banner images (max 10)")
        components.insert(0, {"type": 12, "items": new_images})

    # Non-V2 Discohook content can still be presented as V2 Text Displays.
    if source.get("content"):
        components.insert(0, {"type": 10, "content": source["content"]})
    if source.get("embeds"):
        raise SafeError("Source has legacy embeds; upload banner as an image and link button")

    flattened = list(traverse(components))
    links = [b for b in flattened if b.get("type") == 1]
    galleries = [g for g in flattened if g.get("type") == 12]
    if not links or not any(b.get("components") for b in links):
        raise SafeError("Source banner has no link button")
    if not galleries:
        raise SafeError("Source banner image not found")
    if len(flattened) > 40:
        raise SafeError("Too many message components")
    return {
        "flags": V2_FLAG,
        "components": components,
        "allowed_mentions": {"parse": []},
    }


def jsonhash(data: dict) -> str:
    canon = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def load_state() -> dict:
    if not STATE.exists():
        return {}
    value = json.loads(STATE.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def save_state(value: dict) -> None:
    STATE.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SafeError("Missing DISCORD_BOT_TOKEN secret")

    # Each source webhook can read ONLY messages it originally posted, even
    # when both webhooks point to the very same private source channel.
    configs = [
        ("oneday", "ONEDAY_SOURCE_WEBHOOK_URL", "ONEDAY_SOURCE_MESSAGE_ID", "ONEDAY_TARGET_WEBHOOK_URL"),
        ("career", "CAREER_SOURCE_WEBHOOK_URL", "CAREER_SOURCE_MESSAGE_ID", "CAREER_TARGET_WEBHOOK_URL"),
    ]
    state = load_state()
    target_channels = set()
    for key, source_url_var, source_id_var, target_var in configs:
        source_raw = os.environ.get(source_url_var, "").strip()
        source_id = os.environ.get(source_id_var, "").strip()
        target_url = os.environ.get(target_var, "").strip()
        if not source_raw or not re.fullmatch(r"\d{16,22}", source_id) or not target_url:
            raise SafeError(f"Missing or invalid secret: {source_url_var} / {source_id_var} / {target_var}")
        source_url = webhook_url(source_raw)
        source_webhook = discord("GET", source_url)
        if not isinstance(source_webhook, dict):
            raise SafeError(f"{key}: source webhook inaccessible")
        source_channel = str(source_webhook.get("channel_id") or "")
        if not source_channel:
            raise SafeError(f"{key}: source webhook has no channel ID")
        target_url = webhook_url(target_url)

        target_webhook = discord("GET", target_url)
        if not isinstance(target_webhook, dict):
            raise SafeError(f"{key}: target webhook inaccessible")
        channel_id = str(target_webhook.get("channel_id") or "")
        if not channel_id or channel_id == source_channel or channel_id in target_channels:
            raise SafeError(f"{key}: source and target must be separate channels")
        target_channels.add(channel_id)

        original = discord("GET", f"{source_url}/messages/{source_id}")
        if not isinstance(original, dict):
            raise SafeError(f"{key}: source message not found; check source message ID")
        if str(original.get("channel_id")) != source_channel:
            raise SafeError(f"{key}: original not in the source channel")
        payload = source_payload(original)
        digest = jsonhash(payload)

        channel = discord("GET", f"{API}/channels/{channel_id}", token=token)
        if not isinstance(channel, dict):
            raise SafeError(f"{key}: bot cannot view target channel")
        last_id = str(channel.get("last_message_id") or "")
        prev = state.get(key) or {}
        managed_id = str(prev.get("message_id") or "")
        same_content = digest == prev.get("hash")
        has_new_chat = not managed_id or (last_id and int(last_id) > int(managed_id))

        if managed_id and same_content and not has_new_chat:
            # If deleted manually, restore on next run.
            msg = discord(
                "GET", f"{API}/channels/{channel_id}/messages/{managed_id}",
                token=token, missing_ok=True,
            )
            if msg is not None:
                print(f"{key}: unchanged")
                continue

        # with_components=true is mandatory for link buttons on normal webhooks.
        created = discord(
            "POST", target_url + "?wait=true&with_components=true", payload=payload,
        )
        if not isinstance(created, dict) or not created.get("id"):
            raise SafeError(f"{key}: Discord did not return new message ID")
        new_id = str(created["id"])
        returned_components = list(traverse(created.get("components") or []))
        if not any(c.get("type") == 1 for c in returned_components):
            # Do not delete old sticky if the link-button component was stripped.
            raise SafeError(f"{key}: Discord stripped the link button; old sticky preserved")

        # Persist each channel independently, even if the next channel errors.
        state[key] = {"message_id": new_id, "hash": digest}
        save_state(state)
        if managed_id:
            try:
                discord("DELETE", f"{target_url}/messages/{managed_id}", missing_ok=True)
            except SafeError:
                print(f"{key}: warning: previous managed sticky could not be removed")
        print(f"{key}: banner with link button posted")


if __name__ == "__main__":
    try:
        run()
    except (SafeError, ValueError, KeyError, json.JSONDecodeError) as e:
        # Prevent details of secret URLs from appearing in logs.
        print(f"SGA banner sticky error: {e}", file=sys.stderr)
        sys.exit(1)
