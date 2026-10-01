"""VLESS link parsing, subscription parsing and an end-to-end run of the real xray binary.

The end-to-end tests need xray: set XRAY_BIN or put xray on PATH (the Docker image has it).
"""
import asyncio
import base64
import json
import os
import shutil
import socket
import uuid

import httpx
import pytest

from src import xray
from tools.get_vless import parse_servers

REALITY_LINK = (
    "vless://11111111-2222-3333-4444-555555555555@201.34.139.15:443"
    "?encryption=none&flow=xtls-rprx-vision&type=tcp&security=reality"
    "&sni=ru.example.com&fp=qq&pbk=PUBLICKEY&sid=ab12&spx=%2F#%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F"
)


# --- build_config ----------------------------------------------------------------------


def test_reality_link_to_config():
    config = xray.build_config(REALITY_LINK, socks_port=12345)
    [inbound], [outbound] = config["inbounds"], config["outbounds"]

    assert inbound["listen"] == "127.0.0.1" and inbound["port"] == 12345 and inbound["protocol"] == "socks"
    server = outbound["settings"]["vnext"][0]
    assert (server["address"], server["port"]) == ("201.34.139.15", 443)
    assert server["users"] == [{
        "id": "11111111-2222-3333-4444-555555555555", "encryption": "none", "flow": "xtls-rprx-vision",
    }]
    assert outbound["streamSettings"] == {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
            "serverName": "ru.example.com", "fingerprint": "qq", "publicKey": "PUBLICKEY",
            "shortId": "ab12", "spiderX": "/",
        },
    }


def test_link_with_quotes_and_spaces_is_accepted():
    assert xray.build_config(f'  "{REALITY_LINK}"\n')["outbounds"][0]["protocol"] == "vless"


@pytest.mark.parametrize(
    ("link", "message"),
    [
        ("https://key.example.com/sub/abc", "начинается с «https://»"),
        ("", "пусто"),
        ("vless://host:443?type=tcp", "UUID"),
        ("vless://id@host?type=tcp", "порт"),
        ("vless://id@host:443?type=ws", "не поддерживается"),
    ],
)
def test_bad_links_are_explained(link, message):
    with pytest.raises(ValueError, match=message):
        xray.build_config(link)


def test_error_message_does_not_leak_the_link():
    with pytest.raises(ValueError) as error:
        xray.build_config("vless://secret-uuid@host:443?type=ws&pbk=SECRETKEY")
    assert "secret-uuid" not in str(error.value) and "SECRETKEY" not in str(error.value)


# --- subscription parsing (tools/get_vless.py) -------------------------------------------------


def test_subscription_with_json_configs():
    outbound = xray.build_config(REALITY_LINK)["outbounds"][0]
    body = json.dumps([{"remarks": "Россия", "outbounds": [outbound, {"protocol": "freedom"}]},
                       {"remarks": "Польша", "outbounds": [{"protocol": "freedom"}]}])
    [(name, link)] = parse_servers(body)

    assert name == "Россия"
    assert xray.build_config(link)["outbounds"][0] == outbound  # round trip keeps every setting


def test_subscription_with_base64_links():
    body = base64.b64encode(f"{REALITY_LINK}\ntrojan://ignored@h:1#X\n".encode()).decode()
    assert parse_servers(body) == [("Россия", REALITY_LINK)]


# --- end to end with the real binary ----------------------------------------------------------

XRAY_BIN = os.getenv("XRAY_BIN") or shutil.which("xray")
needs_xray = pytest.mark.skipif(not XRAY_BIN, reason="xray binary not found (set XRAY_BIN)")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_port(port: int, timeout: float = 10) -> None:
    for _ in range(int(timeout * 10)):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise TimeoutError(port)


@pytest.fixture
async def trace_server():
    """Plain HTTP server that answers like cloudflare's /cdn-cgi/trace."""
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = b"ip=203.0.113.7\nloc=RU\n"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/cdn-cgi/trace"
    server.close()


@pytest.fixture
async def vless_server(tmp_path):
    """A local xray VLESS server (no TLS) that forwards traffic straight to the internet/localhost."""
    client_id, port = str(uuid.uuid4()), free_port()
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "listen": "127.0.0.1", "port": port, "protocol": "vless",
            "settings": {"clients": [{"id": client_id}], "decryption": "none"},
            "streamSettings": {"network": "tcp", "security": "none"},
        }],
        "outbounds": [{"protocol": "freedom"}],
    }
    path = tmp_path / "server.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    process = await asyncio.create_subprocess_exec(
        XRAY_BIN, "run", "-config", str(path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await wait_port(port)
    yield f"vless://{client_id}@127.0.0.1:{port}?type=tcp&security=none&encryption=none#test"
    process.terminate()
    await process.wait()


@needs_xray
async def test_proxy_chain_end_to_end(vless_server, trace_server, caplog, monkeypatch):
    monkeypatch.setenv("XRAY_BIN", XRAY_BIN)
    proxy = await xray.start_xray(vless_server, socks_port=free_port())
    try:
        assert await xray.check_exit(proxy.url, trace_server) == {"ip": "203.0.113.7", "loc": "RU"}

        async with httpx.AsyncClient(proxy=proxy.url, timeout=10) as client:
            assert (await client.get(trace_server)).status_code == 200
        await asyncio.sleep(0.2)
        assert "xray:" in caplog.text  # xray's own log reaches the bot log
    finally:
        await proxy.stop()


@needs_xray
async def test_dead_vless_server_is_reported(trace_server, caplog, monkeypatch):
    monkeypatch.setenv("XRAY_BIN", XRAY_BIN)
    dead = f"vless://{uuid.uuid4()}@127.0.0.1:{free_port()}?type=tcp&security=none&encryption=none"
    proxy = await xray.start_xray(dead, socks_port=free_port())
    try:
        await xray.log_exit(proxy.url)
        assert "Schedule proxy does not work" in caplog.text
    finally:
        await proxy.stop()


@needs_xray
async def test_foreign_exit_is_warned(vless_server, caplog, monkeypatch):
    monkeypatch.setenv("XRAY_BIN", XRAY_BIN)

    async def nl_exit(*_):
        return {"ip": "198.51.100.1", "loc": "NL"}

    monkeypatch.setattr(xray, "check_exit", nl_exit)
    proxy = await xray.start_xray(vless_server, socks_port=free_port())
    try:
        await xray.log_exit(proxy.url)
        assert "exits in NL" in caplog.text
    finally:
        await proxy.stop()


async def test_missing_binary_is_reported(monkeypatch):
    monkeypatch.setenv("XRAY_BIN", "")
    monkeypatch.setattr(xray.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="Не найден"):
        await xray.start_xray(REALITY_LINK)
