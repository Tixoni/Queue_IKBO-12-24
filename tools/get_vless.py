"""Достаёт ссылку vless:// одного сервера из ссылки-подписки (Happ, v2rayN и т.п.).

Запускать локально, на своём компьютере:
    python tools/get_vless.py "<ссылка-подписка>" Россия

Подписку никуда не отправляйте и в репозиторий не добавляйте: это ваш ключ доступа.
"""
import base64
import json
import sys
from urllib.parse import quote, unquote, urlencode, urlsplit

import httpx


def _decode_body(text: str) -> str:
    text = text.strip()
    if "://" in text or text[:1] in "[{":
        return text
    try:
        padded = text + "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8")
    except Exception:
        return text


def _link_from_outbound(outbound: dict, name: str) -> str | None:
    if outbound.get("protocol") != "vless":
        return None
    vnext = outbound["settings"]["vnext"][0]
    user = vnext["users"][0]
    stream = outbound.get("streamSettings", {})
    security = stream.get("security", "none")
    params = {"type": stream.get("network", "tcp"), "security": security,
              "encryption": user.get("encryption", "none")}
    if user.get("flow"):
        params["flow"] = user["flow"]
    reality = stream.get("realitySettings")
    if security == "reality" and reality:
        params.update(sni=reality.get("serverName", ""), fp=reality.get("fingerprint", "chrome"),
                      pbk=reality.get("publicKey", ""), sid=reality.get("shortId", ""))
        if reality.get("spiderX"):
            params["spx"] = reality["spiderX"]
    elif security == "tls" and stream.get("tlsSettings"):
        params.update(sni=stream["tlsSettings"].get("serverName", ""), fp=stream["tlsSettings"].get("fingerprint", "chrome"))
    return f"vless://{user['id']}@{vnext['address']}:{vnext['port']}?{urlencode(params)}#{quote(name)}"


def parse_servers(body: str) -> list[tuple[str, str]]:
    """Список (название, ссылка vless://) из тела подписки: JSON-конфиги или обычные ссылки."""
    body = _decode_body(body)
    servers: list[tuple[str, str]] = []
    if body[:1] in "[{":
        data = json.loads(body)
        for config in data if isinstance(data, list) else [data]:
            name = str(config.get("remarks") or config.get("ps") or "без названия")
            for outbound in config.get("outbounds", []):
                link = _link_from_outbound(outbound, name)
                if link:
                    servers.append((name, link))
                    break
        return servers
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("vless://"):
            servers.append((unquote(urlsplit(line).fragment) or "без названия", line))
    return servers


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    url, wanted = sys.argv[1], sys.argv[2].casefold()
    response = httpx.get(url, headers={"User-Agent": "Happ/4.3.0"}, follow_redirects=True, timeout=30)
    response.raise_for_status()
    servers = parse_servers(response.text)
    matches = [(name, link) for name, link in servers if wanted in name.casefold()]
    if not matches:
        print("Сервер не найден. В подписке есть:")
        for name, _ in servers:
            print(" -", name)
        sys.exit(1)
    for name, link in matches:
        print(f"\n{name}:\n{link}")


if __name__ == "__main__":
    main()
