"""`rufur` command line: an Ollama-style local daemon plus helpers.

    rufur serve            run the daemon in the foreground (REST + MCP + UI on :7437)
    rufur start / stop     run it in the background (pid + log file in ~/.rufur)
    rufur status           daemon, Ollama and model health
    rufur open             open the brain UI in a browser
    rufur mcp              MCP over stdio (proxies to the daemon if it's running)
    rufur agent add NAME   issue an API key for an agent
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx
import typer

from ..config import load_config

app = typer.Typer(add_completion=False, no_args_is_help=True, help="RUFUR knowledge brain daemon")
agent_app = typer.Typer(help="Manage agent identities and API keys")
app.add_typer(agent_app, name="agent")


def _health(cfg) -> dict | None:
    try:
        r = httpx.get(f"{cfg.base_url}/v1/health", timeout=1.5)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def _read_pid(cfg) -> int | None:
    try:
        pid = int(cfg.pid_path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


@app.command()
def serve(port: int = typer.Option(None, help="Port (default 7437)"), host: str = typer.Option(None)):
    """Run the daemon in the foreground."""
    import uvicorn

    from ..app import build_app

    overrides = {k: v for k, v in {"port": port, "host": host}.items() if v is not None}
    cfg = load_config(**overrides)
    cfg.ensure_dirs()
    cfg.pid_path.write_text(str(os.getpid()))
    try:
        api = build_app(cfg)
        service = api.state.service
        from ..scheduler import Scheduler

        Scheduler(service).start()  # STM sweeper + nightly consolidation
        service.warm()
        filled = service.reconcile_embeddings()
        if filled:
            typer.echo(f"embedded {filled} memories written while the embedder was down")
        typer.echo(f"RUFUR listening on {cfg.base_url}  (REST /v1, MCP /mcp, UI /)")
        uvicorn.run(api, host=cfg.host, port=cfg.port, log_level="warning")
    finally:
        if cfg.pid_path.exists() and cfg.pid_path.read_text().strip() == str(os.getpid()):
            cfg.pid_path.unlink()


@app.command()
def start():
    """Start the daemon in the background."""
    cfg = load_config()
    cfg.ensure_dirs()
    if _health(cfg):
        typer.echo(f"already running on {cfg.base_url}")
        return
    log = open(cfg.logs_dir / "daemon.log", "a")
    proc = subprocess.Popen(
        [sys.executable, "-m", "rufur.daemon.cli", "serve"],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
    )  # fmt: skip
    for _ in range(60):
        if _health(cfg):
            typer.echo(f"RUFUR started (pid {proc.pid}) on {cfg.base_url}")
            return
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    typer.echo(f"daemon failed to start; see {cfg.logs_dir / 'daemon.log'}", err=True)
    raise typer.Exit(1)


@app.command()
def stop():
    """Stop the background daemon."""
    cfg = load_config()
    pid = _read_pid(cfg)
    if pid is None:
        typer.echo("not running")
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if _read_pid(cfg) is None:
            break
        time.sleep(0.25)
    typer.echo(f"stopped (pid {pid})")


@app.command()
def reset(yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation"),
          backup: bool = typer.Option(True, help="Move the old data to ~/.rufur/backups/<time> instead of deleting it")):  # fmt: skip
    """Wipe all memory (database, procedure files, hook sessions). Config and the receipt key are kept."""
    cfg = load_config()
    targets = [p for p in (cfg.db_path, Path(f"{cfg.db_path}-wal"), Path(f"{cfg.db_path}-shm"), cfg.files_dir,
                           cfg.home / "hook_sessions.json") if p.exists()]  # fmt: skip
    if not targets:
        typer.echo("nothing to reset")
        return
    if not yes:
        where = "moved to ~/.rufur/backups" if backup else "deleted"
        typer.confirm(f"Wipe all RUFUR memory in {cfg.home}? It will be {where}.", abort=True)
    was_running = _read_pid(cfg) is not None
    if was_running:
        stop()
    import shutil

    dest = cfg.home / "backups" / time.strftime("%Y%m%d-%H%M%S")
    if backup:
        dest.mkdir(parents=True)
    for p in targets:
        if backup:
            shutil.move(str(p), dest / p.name)
        elif p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    typer.echo(f"memory wiped{f'; old data in {dest}' if backup else ''}")
    if was_running:
        start()


@app.command()
def status():
    """Show daemon, Ollama and model status."""
    cfg = load_config()
    h = _health(cfg)
    if not h:
        typer.echo(f"daemon: not running ({cfg.base_url})")
        raise typer.Exit(1)
    typer.echo(f"daemon: running on {cfg.base_url}  pid={_read_pid(cfg)}  memories={h['memories']}  home={h['home']}")
    typer.echo(f"embedder: {h['embedder']}{'  (offline mode)' if h['offline'] else ''}")
    oll = h["ollama"]
    typer.echo(f"ollama: {'reachable' if oll['reachable'] else 'NOT reachable'} at {cfg.ollama_url}")
    if oll["reachable"]:
        for role, chain in cfg.roles.items():
            hops = []
            for hop in chain:
                ok = hop.model in oll["models"] if hop.provider == "ollama" else bool(os.environ.get(hop.api_key_env or ""))
                hops.append(f"{hop.model}{'' if ok else ' (missing)'}")
            typer.echo(f"  {role:<9} {' → '.join(hops)}")


@app.command()
def logs(lines: int = 50):
    """Print the tail of the daemon log."""
    cfg = load_config()
    path = cfg.logs_dir / "daemon.log"
    if path.exists():
        typer.echo("".join(path.read_text().splitlines(keepends=True)[-lines:]))


@app.command("open")
def open_ui():
    """Open the brain UI in the browser."""
    cfg = load_config()
    if not _health(cfg):
        typer.echo("daemon not running; start it with `rufur start`", err=True)
        raise typer.Exit(1)
    webbrowser.open(cfg.base_url)


@app.command()
def mcp():
    """MCP over stdio. Proxies to the daemon when it's running, otherwise runs embedded."""
    from ..api.mcp_server import LocalBackend, RemoteBackend, build_mcp, stdio_agent

    cfg = load_config()
    agent, key = stdio_agent()
    if _health(cfg):
        server = build_mcp(RemoteBackend(cfg.base_url, key), default_agent=agent)
    else:
        from ..service import MemoryService

        service = MemoryService(cfg)
        if key:
            agent = service.agent_for_key(key) or agent
        server = build_mcp(LocalBackend(service), default_agent=agent)
    server.run()


def _call(cfg, method: str, path: str, body: dict | None = None):
    """Use the running daemon if there is one (single writer), otherwise open the database directly."""
    if _health(cfg):
        r = httpx.request(method, f"{cfg.base_url}{path}", json=body, headers={"X-Rufur-Agent": "ui"}, timeout=600)
        r.raise_for_status()
        return r.json()
    return None


@app.command()
def consolidate(journal: bool = typer.Option(False, help="Also write today's journal digest")):
    """File short-term memories into long-term storage now."""
    cfg = load_config()
    out = _call(cfg, "POST", "/v1/consolidate", {"journal": journal})
    if out is None:
        from ..service import MemoryService

        out = MemoryService(cfg).consolidate("ui", trigger="manual", journal=journal)
    typer.echo(json.dumps(out, indent=2))


@app.command()
def sweep():
    """Run the STM sweeper now (expire, archive, purge, close stale subagent pools)."""
    cfg = load_config()
    out = _call(cfg, "POST", "/v1/maintenance/sweep")
    if out is None:
        from ..service import MemoryService

        out = MemoryService(cfg).sweep()
    typer.echo(json.dumps(out, indent=2))


@app.command("verify-forgetting")
def verify_forgetting():
    """Re-check every deletion receipt: chain, signatures, ids still gone, nothing resurfaced."""
    cfg = load_config()
    out = _call(cfg, "POST", "/v1/forget/verify")
    if out is None:
        from ..service import MemoryService

        out = MemoryService(cfg).verify_forgetting("ui")
    typer.echo(json.dumps(out, indent=2))
    if not out["valid"]:
        raise typer.Exit(1)


@app.command("tune-stm")
def tune_stm():
    """Suggest the short-term TTL from how old STM items are when they get reused."""
    cfg = load_config()
    out = _call(cfg, "GET", "/v1/stm/stats")
    if out is None:
        from ..service import MemoryService

        out = MemoryService(cfg).stm_stats()
    typer.echo(json.dumps(out, indent=2))
    if out.get("suggested_h"):
        typer.echo(f"\nset stm_base_ttl_h = {out['suggested_h']} in {cfg.home / 'config.toml'} to apply")


@app.command()
def doctor():
    """Check that every memory row, vector and lineage link is consistent."""
    cfg = load_config()
    out = _call(cfg, "GET", "/v1/doctor")
    if out is None:
        from ..service import MemoryService

        out = MemoryService(cfg).doctor()
    typer.echo(json.dumps(out, indent=2))
    if any(out.values()):
        raise typer.Exit(1)


@agent_app.command("add")
def agent_add(name: str, trust: str = "medium_low", clearance: str = "confidential"):
    """Create an agent identity and print its API key (shown once)."""
    cfg = load_config()
    if _health(cfg):
        r = httpx.post(f"{cfg.base_url}/v1/agents", json={"name": name, "trust": trust, "clearance": clearance})
        r.raise_for_status()
        out = r.json()
    else:
        from ..service import MemoryService

        out = MemoryService(cfg).create_agent(name, trust=trust, clearance=clearance)
    typer.echo(json.dumps(out, indent=2))


@agent_app.command("list")
def agent_list():
    cfg = load_config()
    from ..service import MemoryService

    if _health(cfg):
        typer.echo(json.dumps(httpx.get(f"{cfg.base_url}/v1/agents").json(), indent=2))
    else:
        typer.echo(json.dumps(MemoryService(cfg).list_agents(), indent=2))


@app.command("install-service")
def install_service(load: bool = typer.Option(False, help="Also load it with launchctl now")):
    """Write a launchd agent so the daemon starts at login (macOS), like Ollama.app."""
    cfg = load_config()
    plist = Path.home() / "Library/LaunchAgents/ai.rufur.daemon.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.rufur.daemon</string>
  <key>ProgramArguments</key><array><string>{sys.executable}</string><string>-m</string><string>rufur.daemon.cli</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict><key>RUFUR_HOME</key><string>{cfg.home}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{cfg.logs_dir / 'daemon.log'}</string>
  <key>StandardErrorPath</key><string>{cfg.logs_dir / 'daemon.log'}</string>
</dict></plist>
""")
    typer.echo(f"wrote {plist}")
    if load:
        subprocess.run(["launchctl", "load", "-w", str(plist)], check=False)
        typer.echo("loaded")
    else:
        typer.echo(f"load it with: launchctl load -w {plist}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
