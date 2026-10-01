"""Optional local SOCKS5 proxy built from a vless:// link, used only for the schedule API."""
import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

log = logging.getLogger(__name__)

SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 10808
SOCKS_URL = f"socks5://{SOCKS_HOST}:{SOCKS_PORT}"
STARTUP_TIMEOUT = 15


def build_config(vless_url: str) -> dict:
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
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "listen": SOCKS_HOST, "port": SOCKS_PORT, "protocol": "socks",
            "settings": {"udp": False, "auth": "noauth"},
        }],
        "outbounds": [{
            "protocol": "vless",
            "settings": {"vnext": [{"address": link.hostname, "port": link.port, "users": [user]}]},
            "streamSettings": stream,
        }],
    }


class XrayProxy:
    def __init__(self, process: asyncio.subprocess.Process, config_dir: str):
        self._process = process
        self._config_dir = config_dir

    async def stop(self) -> None:
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 5)
            except asyncio.TimeoutError:
                self._process.kill()
        shutil.rmtree(self._config_dir, ignore_errors=True)


async def _port_open() -> bool:
    try:
        _, writer = await asyncio.open_connection(SOCKS_HOST, SOCKS_PORT)
    except OSError:
        return False
    writer.close()
    return True


async def start_xray(vless_url: str) -> XrayProxy:
    binary = os.getenv("XRAY_BIN") or shutil.which("xray")
    if not binary:
        raise RuntimeError("Не найден исполняемый файл xray (задайте XRAY_BIN или установите xray в PATH)")
    config_dir = tempfile.mkdtemp(prefix="xray-")
    config_path = Path(config_dir) / "config.json"
    config_path.write_text(json.dumps(build_config(vless_url)), encoding="utf-8")

    process = await asyncio.create_subprocess_exec(
        binary, "run", "-config", str(config_path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    proxy = XrayProxy(process, config_dir)
    for _ in range(STARTUP_TIMEOUT * 5):
        if process.returncode is not None:
            error = (await process.stderr.read()).decode(errors="replace").strip()
            await proxy.stop()
            raise RuntimeError(f"xray завершился при запуске: {error[-500:]}")
        if await _port_open():
            log.info("Schedule proxy (xray) is listening on %s", SOCKS_URL)
            return proxy
        await asyncio.sleep(0.2)
    await proxy.stop()
    raise RuntimeError("xray не открыл SOCKS-порт за отведённое время")
