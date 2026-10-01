"""Optional local SOCKS5 proxy built from a vless:// link, used only for the schedule API."""
import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

log = logging.getLogger(__name__)

SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 10808
SOCKS_URL = f"socks5://{SOCKS_HOST}:{SOCKS_PORT}"
STARTUP_TIMEOUT = 15
EXIT_CHECK_URL = "https://www.cloudflare.com/cdn-cgi/trace"


def build_config(vless_url: str, socks_port: int = SOCKS_PORT) -> dict:
    """Xray config with a local SOCKS inbound and a single VLESS (TCP + REALITY/TLS) outbound."""
    link = urlsplit(vless_url.strip().strip("\"'").strip())
    if link.scheme != "vless":
        raise ValueError(
            f"VLESS_URL должен начинаться с vless://, а начинается с «{link.scheme or 'пусто'}://». "
            "Ссылку на подписку нужно сначала превратить в ссылку сервера (tools/get_vless.py)"
        )
    missing = [
        name for name, value in (("UUID", link.username), ("хост", link.hostname), ("порт", link.port)) if not value
    ]
    if missing:
        raise ValueError(f"В VLESS_URL не хватает: {', '.join(missing)}. Нужен вид vless://UUID@хост:порт?параметры")
    params = {key: values[0] for key, values in parse_qs(link.query).items()}
    network = params.get("type", "tcp")
    if network != "tcp":
        raise ValueError(f"Транспорт «{network}» не поддерживается, нужен type=tcp")

    user = {"id": unquote(link.username), "encryption": params.get("encryption", "none")}
    if params.get("flow"):
        user["flow"] = params["flow"]

    stream: dict = {"network": "tcp", "security": params.get("security", "none")}
    if stream["security"] == "reality":
        stream["realitySettings"] = {
            "serverName": params.get("sni", ""),
            "fingerprint": params.get("fp", "chrome"),
            "publicKey": params.get("pbk", ""),
            "shortId": params.get("sid", ""),
            "spiderX": unquote(params.get("spx", "")),
        }
    elif stream["security"] == "tls":
        stream["tlsSettings"] = {"serverName": params.get("sni", link.hostname), "fingerprint": params.get("fp", "chrome")}

    return {
        "log": {"loglevel": "warning", "access": "none"},
        "inbounds": [{
            "listen": SOCKS_HOST, "port": socks_port, "protocol": "socks",
            "settings": {"udp": False, "auth": "noauth"},
        }],
        "outbounds": [{
            "protocol": "vless",
            "settings": {"vnext": [{"address": link.hostname, "port": link.port, "users": [user]}]},
            "streamSettings": stream,
        }],
    }


class XrayProxy:
    def __init__(self, process: asyncio.subprocess.Process, config_dir: str, url: str):
        self.url = url
        self._process = process
        self._config_dir = config_dir
        self._log_task: asyncio.Task | None = None

    def forward_logs(self) -> None:
        """Copy xray's own warnings/errors (failed dials, handshake errors) into the bot log."""
        async def pump() -> None:
            async for line in self._process.stdout:
                if text := line.decode(errors="replace").rstrip():
                    log.warning("xray: %s", text)

        self._log_task = asyncio.create_task(pump())

    async def stop(self) -> None:
        if self._log_task:
            self._log_task.cancel()
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 5)
            except asyncio.TimeoutError:
                self._process.kill()
        shutil.rmtree(self._config_dir, ignore_errors=True)


async def _port_open(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection(SOCKS_HOST, port)
    except OSError:
        return False
    writer.close()
    return True


async def start_xray(vless_url: str, socks_port: int = SOCKS_PORT) -> XrayProxy:
    binary = os.getenv("XRAY_BIN") or shutil.which("xray")
    if not binary:
        raise RuntimeError("Не найден исполняемый файл xray (задайте XRAY_BIN или установите xray в PATH)")
    config_dir = tempfile.mkdtemp(prefix="xray-")
    config_path = Path(config_dir) / "config.json"
    config_path.write_text(json.dumps(build_config(vless_url, socks_port)), encoding="utf-8")

    process = await asyncio.create_subprocess_exec(
        binary, "run", "-config", str(config_path),
        # xray writes its log to stdout; merge stderr so nothing is lost
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    proxy = XrayProxy(process, config_dir, f"socks5://{SOCKS_HOST}:{socks_port}")
    for _ in range(STARTUP_TIMEOUT * 5):
        if process.returncode is not None:
            error = (await process.stdout.read()).decode(errors="replace").strip()
            await proxy.stop()
            raise RuntimeError(f"xray завершился при запуске: {error[-500:]}")
        if await _port_open(socks_port):
            log.info("Schedule proxy (xray) is listening on %s", proxy.url)
            proxy.forward_logs()
            return proxy
        await asyncio.sleep(0.2)
    await proxy.stop()
    raise RuntimeError("xray не открыл SOCKS-порт за отведённое время")


async def check_exit(proxy_url: str = SOCKS_URL, url: str = EXIT_CHECK_URL) -> dict[str, str]:
    """Where the proxy actually exits to the internet: {'ip': ..., 'loc': <country code>}."""
    async with httpx.AsyncClient(proxy=proxy_url, timeout=20) as client:
        response = await client.get(url)
        response.raise_for_status()
    fields = dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)
    return {"ip": fields.get("ip", "?"), "loc": fields.get("loc", "?")}


async def log_exit(proxy_url: str = SOCKS_URL) -> None:
    try:
        exit_info = await check_exit(proxy_url)
    except Exception as exc:
        log.error("Schedule proxy does not work: %s: %s", type(exc).__name__, exc)
        return
    if exit_info["loc"] == "RU":
        log.info("Schedule proxy exit: %s (RU)", exit_info["ip"])
    else:
        log.warning(
            "Schedule proxy exits in %s (%s), but schedule-of.mirea.ru accepts only Russian IPs; "
            "pick a VPN server with a Russian exit.", exit_info["loc"], exit_info["ip"],
        )
