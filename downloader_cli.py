#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
downloader_cli.py — Versão headless do Site Downloader para CI/CD

Uso:
    python downloader_cli.py --url https://example.com [opções]

Opções:
    --url URL           URL do site a baixar (obrigatório)
    --dest DIR          Pasta de destino (padrão: ./snapshots)
    --depth N           Profundidade de recursão do wget (padrão: 2)
    --wait SECS         Espera entre requests em segundos (padrão: 1.0)
    --user-agent UA     User-Agent (padrão: SiteDownloader/2.4)
    --no-offline        Não salva versão offline navegável
    --no-warc           Não gera arquivo WARC/CDX
    --full-hash         Calcula SHA256 de todos os arquivos offline
    --timeout SECS      Timeout do processo wget em segundos (padrão: 600)

Exit codes:
    0  Download concluído (ou concluído com erros parciais aceitáveis)
    1  Falha fatal (URL inválida, wget não encontrado, etc.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

APP_VERSION = "2.4.0"
DEFAULT_USER_AGENT = "SiteDownloader/2.4 (+offline+warc)"


# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

@dataclass
class AppConfig:
    file_hash_buffer_size: int = 8 * 1024 * 1024  # 8MB
    io_retry_attempts: int = 3
    io_retry_delay: float = 1.0
    io_retry_backoff: float = 2.0
    max_url_length: int = 2048
    max_user_agent_length: int = 200
    allowed_url_schemes: list = field(default_factory=lambda: ["http", "https"])
    dangerous_system_dirs: list = field(default_factory=lambda: [
        '/etc', '/usr', '/bin', '/sbin', '/var', '/root', '/sys', '/proc',
        'C:\\Windows', 'C:\\Program Files', 'C:\\Program Files (x86)',
        'C:\\ProgramData\\Microsoft'
    ])


CONFIG = AppConfig()


# ============================================================================
# VALIDAÇÃO (reusa lógica do script original)
# ============================================================================

def validate_and_sanitize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL não pode estar vazia")
    if len(url) > CONFIG.max_url_length:
        raise ValueError(f"URL muito longa (máx {CONFIG.max_url_length} chars)")

    try:
        _pre = urlparse(url)
    except Exception as e:
        raise ValueError(f"URL mal formada: {e}")

    if _pre.scheme and _pre.scheme not in CONFIG.allowed_url_schemes:
        raise ValueError(f"Esquema não permitido: '{_pre.scheme}'")

    if not _pre.scheme:
        url = "https://" + url

    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"URL mal formada: {e}")

    if parsed.scheme not in CONFIG.allowed_url_schemes:
        raise ValueError(f"Esquema não permitido. Use http ou https.")

    if not parsed.netloc:
        raise ValueError("URL deve conter um domínio válido")

    dangerous_chars = [';', '|', '`', '$', '<', '>', '\n', '\r', '\x00', '"', "'"]
    non_query = parsed.scheme + "://" + parsed.netloc + parsed.path
    for char in dangerous_chars:
        if char in non_query:
            raise ValueError(f"URL contém caractere não permitido: '{char}'")

    shell_inject = [';', '|', '`', '$', '<', '>', '\n', '\r', '\x00']
    query_frag = (parsed.query or "") + (parsed.fragment or "")
    for char in shell_inject:
        if char in query_frag:
            raise ValueError(f"URL contém caractere não permitido na query: '{char}'")

    if ' ' in url:
        raise ValueError("URL não pode conter espaços")

    return urlunparse(parsed)


def validate_destination_path(path_str: str) -> Path:
    if not path_str or not path_str.strip():
        raise ValueError("Caminho não pode estar vazio")
    try:
        path = Path(path_str).expanduser().resolve()
    except Exception as e:
        raise ValueError(f"Caminho inválido: {e}")

    path_str_norm = str(path).replace('\\', '/')
    for danger in CONFIG.dangerous_system_dirs:
        danger_norm = danger.replace('\\', '/')
        if path_str_norm.lower().startswith(danger_norm.lower()):
            raise ValueError(f"Não é permitido usar diretórios do sistema: {danger}")

    return path


def sanitize_user_agent(ua: str) -> str:
    if not ua:
        return DEFAULT_USER_AGENT
    ua = re.sub(r'[^\w\s\-\.\+/\(\):;,]', '', ua)
    ua = ua[:CONFIG.max_user_agent_length]
    return ua if ua else DEFAULT_USER_AGENT


# ============================================================================
# UTILITÁRIOS
# ============================================================================

def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def safe_domain_from_url(url: str) -> str:
    try:
        p = urlparse(url)
        host = p.netloc or p.path
        host = host.strip().lower()
        if host.startswith("www."):
            host = host[4:]
        host = host.split(":")[0]
        host = "".join(ch if (ch.isalnum() or ch in ".-_") else "_" for ch in host)
        return host or "site"
    except Exception:
        return "site"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            chunk = f.read(CONFIG.file_hash_buffer_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


# ============================================================================
# SNAPSHOT PATHS (reusa estrutura do script original)
# ============================================================================

@dataclass
class SnapshotPaths:
    root: Path
    offline: Path
    evidencia: Path
    evidencia_warc: Path
    meta: Path
    logs: Path
    abrir_aqui: Path
    manifest: Path
    wget_log: Path
    app_log: Path

    @staticmethod
    def create(dest_root: Path, domain: str) -> "SnapshotPaths":
        stamp = now_stamp()
        snap_root = dest_root / domain / stamp

        offline = snap_root / "OFFLINE"
        evidencia = snap_root / "EVIDENCIA"
        evidencia_warc = evidencia / "warc"
        meta = snap_root / "META"
        logs = snap_root / "LOGS"

        for d in [offline, evidencia_warc, meta, logs]:
            ensure_dir(d)

        return SnapshotPaths(
            root=snap_root,
            offline=offline,
            evidencia=evidencia,
            evidencia_warc=evidencia_warc,
            meta=meta,
            logs=logs,
            abrir_aqui=snap_root / "ABRIR_AQUI.html",
            manifest=meta / "manifest.json",
            wget_log=logs / "wget.log",
            app_log=logs / "app.log",
        )


# ============================================================================
# GERAÇÃO DE HTML (reusa do script original)
# ============================================================================

def make_abrir_aqui_html(snapshot: SnapshotPaths, offline_entry: str | None = None) -> str:
    offline_link = ""
    if offline_entry:
        offline_link = f'<li><a href="{offline_entry}" target="_blank">🌐 Abrir versão offline do site</a></li>'

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <title>Snapshot - {snapshot.root.name}</title>
    <style>
        body {{ font-family: sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; }}
        a {{ color: #667eea; display: block; padding: 8px 0; }}
        h2 {{ border-bottom: 2px solid #667eea; padding-bottom: 4px; }}
        .info {{ background: #f8f9fa; border-left: 4px solid #667eea; padding: 12px; margin: 12px 0; }}
    </style>
</head>
<body>
    <h1>📦 Snapshot Arquivado</h1>
    <p>Site Downloader v{APP_VERSION} · {datetime.now().strftime("%d/%m/%Y %H:%M")}</p>
    <div class="info">
        <strong>Snapshot:</strong> {snapshot.root.name}<br>
        <strong>Localização:</strong> {snapshot.root}
    </div>
    <h2>🌐 Navegação</h2>
    <ul>
        {offline_link}
        <li><a href="META/manifest.json">📋 manifest.json</a></li>
        <li><a href="META/checksums.sha256">🔐 checksums.sha256</a></li>
    </ul>
    <h2>📂 Estrutura</h2>
    <ul>
        <li><a href="OFFLINE/">📁 OFFLINE/</a></li>
        <li><a href="EVIDENCIA/warc/">📁 EVIDENCIA/warc/</a></li>
        <li><a href="META/">📁 META/</a></li>
        <li><a href="LOGS/">📁 LOGS/</a></li>
    </ul>
    <div style="margin-top:40px;color:#999;font-size:12px;text-align:center">
        Site Downloader v{APP_VERSION}
    </div>
</body>
</html>"""


# ============================================================================
# WGET
# ============================================================================

def build_wget_args(
    wget_cmd: str,
    url: str,
    snapshot: SnapshotPaths,
    depth: int,
    wait: float,
    user_agent: str,
    offline: bool,
    warc: bool,
) -> list[str]:
    args = [
        wget_cmd,
        "--user-agent", sanitize_user_agent(user_agent),
        "--recursive",
        f"--level={depth}",
        "--page-requisites",
        "--adjust-extension",
        "--convert-links",
        "--backup-converted",
        f"--wait={wait:.2f}",
        "--random-wait",
        "--no-check-certificate",
        "--tries=3",
        "--timeout=30",
    ]

    if warc:
        warc_base = snapshot.evidencia_warc / "snapshot"
        args.extend(["--warc-file", str(warc_base), "--warc-cdx"])

    if offline:
        args.extend(["-P", str(snapshot.offline)])

    args.append(url)
    return args


def run_wget(args: list[str], snapshot: SnapshotPaths, timeout: int) -> bool:
    logging.info(f"Iniciando wget: {' '.join(args[:6])} ... {args[-1]}")

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(snapshot.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        logging.error("wget não encontrado. Instale com: sudo apt-get install wget")
        return False
    except Exception as e:
        logging.error(f"Falha ao iniciar wget: {e}")
        return False

    logging.info(f"wget PID={proc.pid}")

    try:
        with snapshot.wget_log.open("a", encoding="utf-8", errors="replace") as logf:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                logf.write(line)
    except Exception as e:
        logging.warning(f"Erro ao ler saída do wget: {e}")

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.warning(f"Timeout de {timeout}s excedido, encerrando wget")
        try:
            proc.send_signal(signal.SIGTERM)
            time.sleep(3)
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        rc = -1

    ok = rc in (0, 8)
    logging.info(f"wget encerrado (RC={rc}, ok={ok})")
    return ok


# ============================================================================
# ARTIFACTS
# ============================================================================

def find_offline_entry(offline_dir: Path, url: str) -> str | None:
    try:
        parsed = urlparse(url)
        host = parsed.netloc
        path = parsed.path or "/"
        rel_parts = [host] + [p for p in path.split("/") if p]
        if path.endswith("/") or "." not in (rel_parts[-1] if rel_parts else ""):
            rel_parts.append("index.html")
        candidate = offline_dir.joinpath(*rel_parts)
        if candidate.exists():
            return f"OFFLINE/{candidate.relative_to(offline_dir).as_posix()}"
    except Exception:
        pass

    try:
        found = []
        for dirpath, _, filenames in os.walk(str(offline_dir)):
            if "index.html" in filenames:
                found.append(Path(dirpath) / "index.html")
        if found:
            found.sort()
            return f"OFFLINE/{found[0].relative_to(offline_dir).as_posix()}"
    except Exception:
        pass

    return None


def finalize_artifacts(
    snapshot: SnapshotPaths,
    url: str,
    ok: bool,
    offline: bool,
    warc: bool,
    full_hash: bool,
    start_ts: float,
) -> None:
    # ABRIR_AQUI.html
    offline_entry = find_offline_entry(snapshot.offline, url) if offline else None
    try:
        snapshot.abrir_aqui.write_text(
            make_abrir_aqui_html(snapshot, offline_entry=offline_entry),
            encoding="utf-8",
        )
    except Exception as e:
        logging.warning(f"Erro ao criar ABRIR_AQUI.html: {e}")

    # Contagem de arquivos
    total_files = html_files = total_bytes = 0
    if offline and snapshot.offline.exists():
        for dirpath, _, filenames in os.walk(str(snapshot.offline)):
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    st = fp.stat()
                    total_files += 1
                    total_bytes += st.st_size
                    if fn.lower().endswith(('.html', '.htm')):
                        html_files += 1
                except Exception:
                    pass

    elapsed = time.time() - start_ts

    # Manifest
    data = {
        "url": url,
        "started_at": datetime.fromtimestamp(start_ts).isoformat(timespec="seconds"),
        "result": {
            "ok": ok,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "stats": {
                "total_files": total_files,
                "html_files": html_files,
                "total_bytes": total_bytes,
                "elapsed_seconds": round(elapsed, 1),
            },
        },
    }

    # Hashes de arquivos importantes
    hashes: dict[str, str] = {}
    important = [snapshot.wget_log, snapshot.app_log, snapshot.abrir_aqui]
    if warc and snapshot.evidencia_warc.exists():
        for p in snapshot.evidencia_warc.glob("snapshot.warc*"):
            important.append(p)
        for p in snapshot.evidencia_warc.glob("snapshot.cdx"):
            important.append(p)

    for p in important:
        if p.exists() and p.is_file():
            try:
                rel = str(p.relative_to(snapshot.root)).replace("\\", "/")
                hashes[rel] = sha256_file(p)
            except Exception as e:
                logging.warning(f"Hash falhou para {p.name}: {e}")

    # Hash completo de OFFLINE
    if full_hash and offline and snapshot.offline.exists():
        logging.info("Calculando hashes completos de OFFLINE (pode demorar)...")
        full_hashes: dict[str, str] = {}
        for dirpath, _, filenames in os.walk(str(snapshot.offline)):
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    rel = str(fp.relative_to(snapshot.root)).replace("\\", "/")
                    full_hashes[rel] = sha256_file(fp)
                except Exception as e:
                    logging.warning(f"Hash falhou: {e}")
        data["result"]["full_hashes_offline_sha256"] = full_hashes

    data["result"]["hashes"] = hashes

    try:
        snapshot.manifest.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logging.warning(f"Erro ao salvar manifest: {e}")

    # checksums.sha256
    checksums_path = snapshot.meta / "checksums.sha256"
    try:
        with checksums_path.open("w", encoding="utf-8") as f:
            for k, v in sorted(hashes.items()):
                f.write(f"{v}  {k}\n")
    except Exception as e:
        logging.warning(f"Erro ao salvar checksums: {e}")

    # README_SNAPSHOT.txt
    readme = snapshot.root / "README_SNAPSHOT.txt"
    try:
        readme.write_text(
            f"Site Downloader v{APP_VERSION} - Snapshot\n\n"
            f"URL: {url}\n"
            f"Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
            f"Status: {'OK' if ok else 'FALHOU'}\n\n"
            "Abra ABRIR_AQUI.html para navegar.\n",
            encoding="utf-8",
        )
    except Exception as e:
        logging.warning(f"Erro ao criar README_SNAPSHOT.txt: {e}")


# ============================================================================
# MAIN
# ============================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Site Downloader CLI - headless")
    p.add_argument("--url", required=True, help="URL do site a baixar")
    p.add_argument("--dest", default="./snapshots", help="Pasta de destino")
    p.add_argument("--depth", type=int, default=2, help="Profundidade de recursão")
    p.add_argument("--wait", type=float, default=1.0, help="Espera entre requests (s)")
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent")
    p.add_argument("--no-offline", action="store_true", help="Não salva versão offline")
    p.add_argument("--no-warc", action="store_true", help="Não gera WARC/CDX")
    p.add_argument("--full-hash", action="store_true", help="Hash completo de todos os arquivos")
    p.add_argument("--timeout", type=int, default=600, help="Timeout do wget em segundos")
    return p.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    args = parse_args()

    # Valida URL
    try:
        url = validate_and_sanitize_url(args.url)
    except ValueError as e:
        logging.error(f"URL inválida: {e}")
        return 1

    # Valida destino
    try:
        dest_root = validate_destination_path(args.dest)
    except ValueError as e:
        logging.error(f"Destino inválido: {e}")
        return 1

    ensure_dir(dest_root)

    # Verifica wget
    wget_cmd = shutil.which("wget") or shutil.which("wget2")
    if not wget_cmd:
        logging.error("wget não encontrado no PATH. Instale com: sudo apt-get install wget")
        return 1

    # Valida depth e wait
    if not (0 <= args.depth <= 20):
        logging.error(f"Profundidade inválida: {args.depth} (deve ser 0-20)")
        return 1
    if not (0.0 <= args.wait <= 60.0):
        logging.error(f"Wait inválido: {args.wait} (deve ser 0-60)")
        return 1

    offline = not args.no_offline
    warc = not args.no_warc
    domain = safe_domain_from_url(url)

    logging.info(f"=== Site Downloader CLI v{APP_VERSION} ===")
    logging.info(f"URL:       {url}")
    logging.info(f"Destino:   {dest_root}")
    logging.info(f"Domínio:   {domain}")
    logging.info(f"Offline:   {offline} | WARC: {warc} | Depth: {args.depth}")

    # Cria estrutura de snapshot
    snapshot = SnapshotPaths.create(dest_root, domain)
    logging.info(f"Snapshot:  {snapshot.root}")

    # Configura log em arquivo
    file_handler = logging.FileHandler(snapshot.app_log, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(funcName)s:%(lineno)d %(message)s"
    ))
    logging.getLogger().addHandler(file_handler)

    # Salva manifest inicial
    try:
        snapshot.manifest.write_text(
            json.dumps({
                "url": url,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "options": {
                    "depth": args.depth,
                    "wait": args.wait,
                    "offline": offline,
                    "warc": warc,
                    "user_agent": args.user_agent,
                },
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logging.warning(f"Erro ao salvar manifest inicial: {e}")

    # Executa wget
    wget_args = build_wget_args(
        wget_cmd=wget_cmd,
        url=url,
        snapshot=snapshot,
        depth=args.depth,
        wait=args.wait,
        user_agent=args.user_agent,
        offline=offline,
        warc=warc,
    )

    start_ts = time.time()
    ok = run_wget(wget_args, snapshot, timeout=args.timeout)

    # Finaliza artifacts
    finalize_artifacts(
        snapshot=snapshot,
        url=url,
        ok=ok,
        offline=offline,
        warc=warc,
        full_hash=args.full_hash,
        start_ts=start_ts,
    )

    logging.info(f"Snapshot salvo em: {snapshot.root}")
    # Imprime o caminho do snapshot para o workflow capturar
    print(f"SNAPSHOT_PATH={snapshot.root}")

    if ok:
        logging.info("Download concluído com sucesso.")
        return 0
    else:
        logging.error("Download finalizado com falha (veja LOGS/wget.log).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
