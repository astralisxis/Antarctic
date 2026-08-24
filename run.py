"""Single-command launcher for the bot, admin panel, storefront and tunnel.

Usage:
    python run.py
    python run.py --no-tunnel
    python run.py --migrate
"""

from __future__ import annotations

import argparse
import atexit
import asyncio
import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import platform
import stat
import threading
import urllib.request
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parent
TUNNEL_CONFIG = PROJECT_ROOT / ".cloudflared" / "dabbackwood-config.yml"
TUNNEL_ID = "9a5036eb-ca98-483c-aca6-ab6d5dc8a3c5"


@dataclass
class Service:
    name: str
    command: list[str]
    critical: bool = True
    process: subprocess.Popen[bytes] | None = None


def load_environment() -> None:
    """Load .env before run.py selects local or hosted mode."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        path = PROJECT_ROOT / ".env"
        if not path.exists():
            return
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def ensure_dependencies(python: str, *, hosted: bool) -> None:
    """Install requirements once on hosts that do not run a build step."""
    required = ("uvicorn", "fastapi", "aiogram", "sqlalchemy", "pydantic_settings")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if not missing:
        return
    auto = os.getenv("AUTO_INSTALL_DEPENDENCIES", "true" if hosted else "false").strip().lower()
    if auto not in {"1", "true", "yes", "on"}:
        raise SystemExit(
            "Не хватает Python-пакетов: "
            + ", ".join(missing)
            + ". Выполните: python -m pip install -r requirements.txt"
        )
    requirements = PROJECT_ROOT / "requirements.txt"
    print(f"[setup] Устанавливаю зависимости из {requirements.name}...", flush=True)
    result = subprocess.run([python, "-m", "pip", "install", "-r", str(requirements)], cwd=PROJECT_ROOT)
    if result.returncode:
        raise SystemExit(f"Не удалось установить зависимости, код {result.returncode}.")


def hostname(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.hostname.lower() if parsed.hostname else None


def host_aliases(*values: str | None) -> set[str]:
    result: set[str] = set()
    for value in values:
        name = hostname(value)
        if not name:
            continue
        result.add(name)
        result.add(name[4:] if name.startswith("www.") else f"www.{name}")
    return result


class HostDispatchApp:
    """Serve the store and admin app from one public hosted port."""

    def __init__(
        self,
        web_app,
        admin_app,
        admin_hosts: set[str],
        services: list[Service] | None = None,
    ):
        self.web_app = web_app
        self.admin_app = admin_app
        self.admin_hosts = admin_hosts
        self.services = services or []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            try:
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(
                        self.admin_app.router.lifespan_context(self.admin_app)
                    )
                    await stack.enter_async_context(
                        self.web_app.router.lifespan_context(self.web_app)
                    )
                    for service in self.services:
                        start(service)
                    supervisor = asyncio.create_task(
                        supervise_services(self.services), name="service-supervisor"
                    )
                    await send({"type": "lifespan.startup.complete"})
                    try:
                        while True:
                            message = await receive()
                            if message.get("type") == "lifespan.shutdown":
                                break
                    finally:
                        supervisor.cancel()
                        with suppress(asyncio.CancelledError):
                            await supervisor
                await send({"type": "lifespan.shutdown.complete"})
            except Exception as exc:
                await send({"type": "lifespan.startup.failed", "message": str(exc)})
            finally:
                for service in reversed(self.services):
                    stop(service)
            return

        headers = dict(scope.get("headers", []))
        raw_host = ""
        for key in (b"x-forwarded-host", b"x-original-host", b"host"):
            value = headers.get(key, b"").decode("latin1").split(",", 1)[0]
            value = value.split(":", 1)[0].strip().lower()
            if value:
                raw_host = value
                break
        target = self.admin_app if raw_host in self.admin_hosts else self.web_app
        await target(scope, receive, send)


def env_enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def find_virtualenv_python() -> Path | None:
    executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    for directory in (".venv", "venv"):
        candidate = PROJECT_ROOT / directory / executable
        if candidate.exists():
            return candidate
    return None


def ensure_virtualenv() -> None:
    venv_python = find_virtualenv_python()
    if venv_python is None:
        return
    expected_prefix = venv_python.parent.parent
    # On Linux .venv/bin/python is often a symlink to the system binary. The
    # executable path can therefore match even when sys.prefix is still the
    # system installation; prefix is the reliable virtualenv check.
    if Path(sys.prefix).absolute() == expected_prefix.absolute():
        return
    command = [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]]
    if os.name == "nt":
        raise SystemExit(subprocess.call(command, cwd=PROJECT_ROOT))
    os.execv(str(venv_python), command)


def env_port(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} должен быть числом, получено: {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"{name} должен быть от 1 до 65535, получено: {port}")
    return port


def port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def find_cloudflared() -> Path | None:
    bundled = PROJECT_ROOT / "tools" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if bundled.exists():
        return bundled
    found = shutil.which("cloudflared")
    return Path(found) if found else None


def find_or_install_cloudflared() -> Path | None:
    current = find_cloudflared()
    if current is not None or os.name == "nt":
        return current
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower()
    )
    if arch is None:
        return None
    target_dir = PROJECT_ROOT / "data" / "bin"
    target = target_dir / "cloudflared"
    if target.exists():
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return target
    target_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".download")
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
    print(f"[setup] Скачиваю cloudflared для Linux {arch}...", flush=True)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Antarctic-Shop/1.0"})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.chmod(temporary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        temporary.replace(target)
        return target
    except (OSError, TimeoutError) as exc:
        temporary.unlink(missing_ok=True)
        print(f"[warning] cloudflared не скачан: {exc}", flush=True)
        return None


def cloudflared_is_running() -> bool:
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            return "cloudflared.exe" in result.stdout.lower()
        result = subprocess.run(
            ["pgrep", "-f", "cloudflared.*tunnel"],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def tunnel_is_connected(cloudflared: Path) -> bool:
    """A cloudflared process can exist while all edge connections are down."""
    if not cloudflared_is_running():
        return False
    try:
        result = subprocess.run(
            [str(cloudflared), "tunnel", "info", TUNNEL_ID],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    output = f"{result.stdout}\n{result.stderr}".lower()
    if "does not have any active connection" in output or "no active connection" in output:
        return False
    return any(marker in output for marker in ("connector id", "connection", "registered"))


def start(service: Service) -> None:
    if service.process is not None and service.process.poll() is None:
        return
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    service.process = subprocess.Popen(
        service.command,
        cwd=PROJECT_ROOT,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    print(f"[start] {service.name} (pid {service.process.pid})", flush=True)


async def supervise_services(services: list[Service], interval: float = 2.0) -> None:
    """Restart hosted child services after an unexpected exit."""
    while True:
        await asyncio.sleep(interval)
        for service in services:
            process = service.process
            if process is None or process.poll() is None:
                continue
            code = process.returncode
            print(
                f"[restart] {service.name} завершился с кодом {code}; перезапускаю",
                flush=True,
            )
            service.process = None
            start(service)


def stop(service: Service) -> None:
    process = service.process
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=8)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Запуск всех частей Antarctic Shop одной командой.")
    parser.add_argument("--no-bot", action="store_true", help="не запускать Telegram-бота")
    parser.add_argument("--no-support", action="store_true", help="не запускать бота поддержки")
    parser.add_argument("--no-admin", action="store_true", help="не запускать админ-панель")
    parser.add_argument("--no-web", action="store_true", help="не запускать сайт и Mini App")
    parser.add_argument("--no-tunnel", action="store_true", help="не подключать Cloudflare Tunnel")
    parser.add_argument("--migrate", action="store_true", help="перед запуском выполнить alembic upgrade head")
    return parser.parse_args()


def hosted_services(python: str, *, allow_tunnel: bool = True) -> list[Service]:
    services: list[Service] = []
    if env_enabled("RUN_BOT", True) and os.getenv("BOT_TOKEN", "").strip():
        services.append(Service("Telegram-бот", [python, "-m", "app.bot.main"], critical=False))
    if env_enabled("RUN_SUPPORT_BOT", True) and os.getenv("SUPPORT_BOT_TOKEN", "").strip():
        services.append(
            Service("Бот поддержки", [python, "-m", "app.bot.support_main"], critical=False)
        )
    tunnel_token = os.getenv("CLOUDFLARED_TUNNEL_TOKEN", "").strip()
    if allow_tunnel and tunnel_token:
        cloudflared = find_or_install_cloudflared()
        if cloudflared is None:
            print("[warning] CLOUDFLARED_TUNNEL_TOKEN задан, но cloudflared недоступен.", flush=True)
        else:
            services.append(
                Service(
                    "Cloudflare Tunnel",
                    [str(cloudflared), "tunnel", "run", "--token", tunnel_token],
                    critical=False,
                )
            )
    return services


def create_hosted_app(*, services: list[Service] | None = None):
    """Application exported as run:app for Gunicorn/Uvicorn hosting panels."""
    load_environment()
    from app.admin.main import app as admin_app
    from app.web.main import app as web_app

    admin_hosts = host_aliases(
        os.getenv("ADMIN_BASE_URL"),
        os.getenv("ADMIN_DOMAIN"),
        "dabbackwood.cfd",
    )
    return HostDispatchApp(web_app, admin_app, admin_hosts, services=services)


class LazyHostedApp:
    """Lazy ASGI app with a fallback adapter for sync Gunicorn workers.

    Most panels use the project's ``gunicorn.conf.py`` and select the Uvicorn
    worker. A few hard-code ``gunicorn run:app`` and force the default sync
    worker. The latter calls an application as WSGI, so this object supports
    both call conventions without duplicating the web or admin applications.
    """

    def __init__(self) -> None:
        self.delegate = None
        self.services: list[Service] | None = None
        self._wsgi_loop: asyncio.AbstractEventLoop | None = None
        self._wsgi_thread: threading.Thread | None = None
        self._wsgi_ready = threading.Event()
        self._wsgi_error: BaseException | None = None
        self._wsgi_stack: AsyncExitStack | None = None
        self._wsgi_supervisor: asyncio.Task | None = None
        atexit.register(self._stop_wsgi_runtime)
        if is_gunicorn_runtime():
            load_environment()
            self.services = hosted_services(str(Path(sys.executable).absolute()))
            # Child processes must start after the hosted app lifespans have
            # initialized the database schema. Starting them during import
            # races SQLite initialization and can crash the support bot.

    def __call__(self, first, second=None, third=None):
        # Uvicorn classifies callable objects with a synchronous __call__ as
        # ASGI2 and first invokes ``app(scope)``. Support that convention as
        # well as direct ASGI3 calls made by test clients and other servers.
        if isinstance(first, dict) and "type" in first:
            if second is None:
                async def asgi2_instance(receive, send):
                    await self._asgi(first, receive, send)

                return asgi2_instance
            return self._asgi(first, second, third)
        # Gunicorn's sync worker calls ``app(environ, start_response)``.
        return self._wsgi(first, second)

    async def _asgi(self, scope, receive, send):
        if self.delegate is None:
            load_environment()
            self.delegate = create_hosted_app(services=self.services)
        await self.delegate(scope, receive, send)

    def _wsgi(self, environ, start_response):
        self._ensure_wsgi_runtime()
        if self._wsgi_loop is None:
            raise RuntimeError("WSGI runtime не запустился")
        future = asyncio.run_coroutine_threadsafe(
            self._handle_wsgi_request(environ), self._wsgi_loop
        )
        status, headers, body = future.result(timeout=180)
        start_response(status, headers)
        return [body]

    def _ensure_wsgi_runtime(self) -> None:
        if self._wsgi_thread is not None:
            self._wsgi_ready.wait(timeout=30)
            if self._wsgi_error is not None:
                raise RuntimeError("Не удалось запустить приложение", self._wsgi_error)
            return
        self._wsgi_thread = threading.Thread(
            target=self._run_wsgi_loop,
            name="hosted-asgi-loop",
            daemon=True,
        )
        self._wsgi_thread.start()
        self._wsgi_ready.wait(timeout=30)
        if self._wsgi_error is not None:
            raise RuntimeError("Не удалось запустить приложение", self._wsgi_error)
        if self._wsgi_loop is None:
            raise RuntimeError("WSGI runtime не запустился за 30 секунд")

    def _run_wsgi_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._wsgi_loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._start_wsgi_runtime())
            self._wsgi_ready.set()
            loop.run_forever()
        except BaseException as exc:  # surfaced to the requesting worker
            self._wsgi_error = exc
            self._wsgi_ready.set()
        finally:
            if not loop.is_closed():
                loop.close()

    async def _start_wsgi_runtime(self) -> None:
        load_environment()
        if self.services is None:
            self.services = hosted_services(str(Path(sys.executable).absolute()))
        self.delegate = create_hosted_app(services=self.services)
        self._wsgi_stack = AsyncExitStack()
        await self._wsgi_stack.enter_async_context(
            self.delegate.admin_app.router.lifespan_context(self.delegate.admin_app)
        )
        await self._wsgi_stack.enter_async_context(
            self.delegate.web_app.router.lifespan_context(self.delegate.web_app)
        )
        for service in self.delegate.services:
            start(service)
        self._wsgi_supervisor = asyncio.create_task(
            supervise_services(self.delegate.services), name="service-supervisor"
        )

    async def _handle_wsgi_request(self, environ):
        body_length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(body_length) if body_length else b""
        raw_path = environ.get("PATH_INFO") or "/"
        query = (environ.get("QUERY_STRING") or "").encode("latin1")
        headers = []
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                header_name = key[5:].replace("_", "-").lower().encode("latin1")
                headers.append((header_name, str(value).encode("latin1")))
        if environ.get("CONTENT_TYPE"):
            headers.append((b"content-type", str(environ["CONTENT_TYPE"]).encode("latin1")))
        if environ.get("CONTENT_LENGTH"):
            headers.append((b"content-length", str(environ["CONTENT_LENGTH"]).encode("latin1")))
        server_name = environ.get("SERVER_NAME", "127.0.0.1")
        try:
            server_port = int(environ.get("SERVER_PORT") or 80)
        except (TypeError, ValueError):
            server_port = 80
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": environ.get("REQUEST_METHOD", "GET"),
            "scheme": environ.get("wsgi.url_scheme", "http"),
            "path": raw_path,
            "raw_path": raw_path.encode("latin1"),
            "query_string": query,
            "root_path": "",
            "headers": headers,
            "client": (environ.get("REMOTE_ADDR", "127.0.0.1"), 0),
            "server": (server_name, server_port),
        }
        sent = {"status": "500 Internal Server Error", "headers": [], "body": bytearray()}
        received = False

        async def receive():
            nonlocal received
            if received:
                await asyncio.sleep(0)
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                sent["status"] = f"{status_code} {status_text(status_code)}"
                sent["headers"] = [
                    (key.decode("latin1"), value.decode("latin1"))
                    for key, value in message.get("headers", [])
                ]
            elif message["type"] == "http.response.body":
                sent["body"].extend(message.get("body", b""))

        await self.delegate(scope, receive, send)
        return sent["status"], sent["headers"], bytes(sent["body"])

    def _stop_wsgi_runtime(self) -> None:
        loop = self._wsgi_loop
        if loop is None or loop.is_closed():
            return
        if self._wsgi_stack is not None:
            future = asyncio.run_coroutine_threadsafe(self._wsgi_stack.aclose(), loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
        if self._wsgi_supervisor is not None:
            self._wsgi_supervisor.cancel()
        for service in reversed(getattr(self.delegate, "services", [])):
            stop(service)
        loop.call_soon_threadsafe(loop.stop)


def status_text(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Unknown"


def is_gunicorn_runtime() -> bool:
    return "gunicorn" in sys.modules or "gunicorn" in Path(sys.argv[0]).name.lower()


# Several hosting panels hard-code `gunicorn run:app`. Export a valid app so the
# project works even when the panel ignores `python run.py`; in Gunicorn mode
# background services start from the hosted lifespan after DB initialization.
app = LazyHostedApp()


def main() -> int:
    ensure_virtualenv()
    load_environment()
    args = parse_args()
    # Do not resolve a Linux venv symlink: the resolved system interpreter may
    # not see packages installed into the venv.
    python = str(Path(sys.executable).absolute())

    hosted_flag = os.getenv("HOSTED_MODE", "").strip().lower()
    hosted = bool(os.getenv("PORT")) or hosted_flag in {"1", "true", "yes"}
    # Some hosts expose neither PORT nor a mode flag. ENV=prod on Linux is a
    # useful conservative fallback; HOSTED_MODE=false explicitly disables it.
    if (
        not hosted
        and os.name != "nt"
        and os.getenv("ENV", "").strip().lower() in {"prod", "production"}
        and hosted_flag not in {"0", "false", "no"}
    ):
        hosted = True

    ensure_dependencies(python, hosted=hosted)
    bind_host = "0.0.0.0" if hosted else os.getenv("HOST", "127.0.0.1")
    admin_port = env_port("ADMIN_PORT", 8080)
    web_port = env_port("PORT", 8081) if hosted else env_port("WEB_PORT", 8081)

    if args.migrate:
        print("[setup] Обновление структуры базы данных...", flush=True)
        result = subprocess.run([python, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT)
        if result.returncode:
            return result.returncode

    if hosted:
        services = hosted_services(python, allow_tunnel=not args.no_tunnel)
        if args.no_bot:
            services = [service for service in services if service.name != "Telegram-бот"]
        if args.no_support:
            services = [service for service in services if service.name != "Бот поддержки"]
        tunnel_token = os.getenv("CLOUDFLARED_TUNNEL_TOKEN", "").strip()

        admin_hosts = host_aliases(
            os.getenv("ADMIN_BASE_URL"),
            os.getenv("ADMIN_DOMAIN"),
            "dabbackwood.cfd",
        )
        print(f"\nЕдиный сервер: {bind_host}:{web_port}", flush=True)
        print("Магазин:   https://antarctic.cfd", flush=True)
        print("Админка:   https://dabbackwood.cfd", flush=True)
        print(
            "Cloudflare Tunnel: запускается по токену"
            if tunnel_token and not args.no_tunnel
            else "Cloudflare Tunnel: не нужен при подключении Custom Domain хостинга",
            flush=True,
        )
        try:
            import uvicorn

            uvicorn.run(
                create_hosted_app(services=services),
                host=bind_host,
                port=web_port,
                reload=False,
                log_config=None,
            )
            return 0
        finally:
            pass

    services = []
    if not args.no_bot:
        services.append(Service("Telegram-бот", [python, "-m", "app.bot.main"], critical=False))
    if not args.no_support:
        if os.getenv("SUPPORT_BOT_TOKEN", "").strip():
            services.append(
                Service("Бот поддержки", [python, "-m", "app.bot.support_main"], critical=False)
            )
        else:
            print("[warning] SUPPORT_BOT_TOKEN не задан — бот поддержки пропущен.", flush=True)

    if not args.no_admin:
        if port_is_open(admin_port):
            print(f"[ready] Админ-панель уже работает: http://127.0.0.1:{admin_port}", flush=True)
        else:
            services.append(
                Service(
                    "админ-панель",
                    [python, "-m", "uvicorn", "app.admin.main:app", "--host", bind_host, "--port", str(admin_port)],
                )
            )

    if not args.no_web:
        if port_is_open(web_port):
            print(f"[ready] Сайт уже работает: http://127.0.0.1:{web_port}", flush=True)
        else:
            services.append(
                Service(
                    "сайт и Mini App",
                    [python, "-m", "uvicorn", "app.web.main:app", "--host", bind_host, "--port", str(web_port)],
                )
            )

    if not args.no_tunnel:
        cloudflared = find_cloudflared()
        if cloudflared is not None and cloudflared_is_running() and tunnel_is_connected(cloudflared):
            print("[ready] Cloudflare Tunnel подключён к edge", flush=True)
        elif cloudflared is not None and cloudflared_is_running():
            print("[warning] Процесс cloudflared есть, но активного соединения с edge нет. Запускаю новый коннектор.", flush=True)
            services.append(
                Service(
                    "Cloudflare Tunnel",
                    [str(cloudflared), "tunnel", "--protocol", "http2", "--config", str(TUNNEL_CONFIG), "run", TUNNEL_ID],
                )
            )
        elif cloudflared is None:
            print("[warning] cloudflared не найден. Публичные домены не будут доступны.", flush=True)
        elif not TUNNEL_CONFIG.exists():
            print("[warning] Нет .cloudflared/dabbackwood-config.yml. Туннель пропущен.", flush=True)
        else:
            services.append(
                Service(
                    "Cloudflare Tunnel",
                    [
                        str(cloudflared),
                        "tunnel",
                        "--protocol",
                        "http2",
                        "--config",
                        str(TUNNEL_CONFIG),
                        "run",
                        TUNNEL_ID,
                    ],
                )
            )

    if not services:
        print("Все выбранные службы уже работают или отключены.", flush=True)
        return 0

    for service in services:
        start(service)

    print(f"\nЛокально:  http://127.0.0.1:{web_port}", flush=True)
    print("Магазин:   https://antarctic.cfd", flush=True)
    print("Админка:   https://dabbackwood.cfd", flush=True)
    print("Остановить всё: Ctrl+C\n", flush=True)

    exit_code = 0
    try:
        while True:
            for service in services:
                if service.process is None:
                    continue
                code = service.process.poll()
                if code is not None:
                    print(f"[stopped] {service.name}, код {code}", flush=True)
                    if service.critical:
                        exit_code = code or 1
                        return exit_code
                    service.process = None
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nОстанавливаю службы...", flush=True)
        return 0
    finally:
        for service in reversed(services):
            stop(service)


if __name__ == "__main__":
    raise SystemExit(main())
