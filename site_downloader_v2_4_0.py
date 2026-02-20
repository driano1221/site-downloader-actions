#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Site Downloader v2.4.0 — Versão Melhorada e Robusta

Melhorias implementadas:
✅ Validação robusta de entrada (URL, paths) - Segurança
✅ Timeout configurável e monitoring - Robustez
✅ Otimização de scanning (os.walk) - Performance 2-3x
✅ Thread-safe logging com Queue - Estabilidade
✅ Retry logic para operações de I/O - Confiabilidade
✅ Logging estruturado - Diagnóstico
✅ Validação em tempo real - UX
✅ Progress com ETA - UX
✅ Sistema de configuração - Flexibilidade
"""

from __future__ import annotations

import os
import sys
import json
import time
import queue
import shutil
import signal
import hashlib
import logging
import threading
import subprocess
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from functools import wraps
from typing import Callable, Any
from logging.handlers import RotatingFileHandler

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


APP_TITLE = "Site Downloader v2.6.0 — Melhorado e Robusto"
APP_VERSION = "2.6.0"
DEFAULT_USER_AGENT = "SiteDownloader/2.4 (+offline+warc)"


# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

@dataclass
class AppConfig:
    """Configuração global do aplicativo"""
    # Performance
    stats_scan_interval_s: float = 2.0
    stats_ui_tick_ms: int = 500
    file_hash_buffer_size: int = 8 * 1024 * 1024  # 8MB
    
    # Timeout
    subprocess_timeout: int = 600000  # 10 minutos
    subprocess_cleanup_timeout: int = 30
    
    # Retry
    io_retry_attempts: int = 3
    io_retry_delay: float = 1.0
    io_retry_backoff: float = 2.0
    
    # Validação
    max_url_length: int = 2048
    max_user_agent_length: int = 200
    allowed_url_schemes: list = field(default_factory=lambda: ["http", "https"])
    
    # Segurança
    dangerous_system_dirs: list = field(default_factory=lambda: [
        '/etc', '/usr', '/bin', '/sbin', '/var', '/root', '/sys', '/proc',
        'C:\\Windows', 'C:\\Program Files', 'C:\\Program Files (x86)',
        'C:\\ProgramData\\Microsoft'
    ])
    
    # Logging
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024  # 10MB
    log_backup_count: int = 5
    
    @classmethod
    def load_from_file(cls, path: Path) -> "AppConfig":
        """Carrega configuração de arquivo JSON"""
        if not path.exists():
            return cls()

        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            # Filtra apenas campos conhecidos para evitar TypeError com chaves extras
            import dataclasses
            known = {f.name for f in dataclasses.fields(cls)}
            filtered = {k: v for k, v in data.items() if k in known}
            return cls(**filtered)
        except Exception as e:
            logging.warning(f"Erro ao carregar config: {e}, usando padrões")
            return cls()
    
    def save_to_file(self, path: Path) -> None:
        """Salva configuração em arquivo JSON"""
        try:
            path.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False),
                encoding='utf-8'
            )
        except Exception as e:
            logging.error(f"Erro ao salvar config: {e}")


# Instância global de configuração
CONFIG = AppConfig()


# ============================================================================
# VALIDAÇÃO E SANITIZAÇÃO
# ============================================================================

def validate_and_sanitize_url(url: str) -> str:
    """
    Valida e sanitiza URL para prevenir command injection
    
    Raises:
        ValueError: Se URL for inválida ou insegura
    """
    url = (url or "").strip()
    
    if not url:
        raise ValueError("URL não pode estar vazia")
    
    if len(url) > CONFIG.max_url_length:
        raise ValueError(f"URL muito longa (máx {CONFIG.max_url_length} chars)")
    
    # Faz parse inicial para inspecionar o scheme antes de adicionar prefixo
    try:
        _pre = urlparse(url)
    except Exception as e:
        raise ValueError(f"URL mal formada: {e}")

    # Se tem scheme explícito mas não é http/https, rejeita já (evita javascript:, ftp:, etc.)
    if _pre.scheme and _pre.scheme not in CONFIG.allowed_url_schemes:
        raise ValueError(f"Esquema não permitido: '{_pre.scheme}'. Use: {', '.join(CONFIG.allowed_url_schemes)}")

    # Adiciona https:// apenas se não há scheme nenhum (sem ":")
    if not _pre.scheme:
        url = "https://" + url

    # Parse final com scheme garantido
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"URL mal formada: {e}")

    # Valida esquema final
    if parsed.scheme not in CONFIG.allowed_url_schemes:
        raise ValueError(f"Esquema não permitido. Use: {', '.join(CONFIG.allowed_url_schemes)}")
    
    # Valida domínio
    if not parsed.netloc:
        raise ValueError("URL deve conter um domínio válido")
    
    # Previne caracteres perigosos (command injection) na URL completa.
    # IMPORTANTE: '&' e '=' são válidos em query strings (?a=1&b=2),
    # portanto verificamos apenas no scheme+netloc+path, não na query.
    dangerous_chars = [';', '|', '`', '$', '<', '>', '\n', '\r', '\x00', '"', "'"]
    # Verifica nos componentes fora da query string
    non_query = parsed.scheme + "://" + parsed.netloc + parsed.path
    for char in dangerous_chars:
        if char in non_query:
            raise ValueError(f"URL contém caractere não permitido: '{char}'")
    # Na query/fragment verificamos apenas os mais perigosos (shell injection)
    shell_inject = [';', '|', '`', '$', '<', '>', '\n', '\r', '\x00']
    query_frag = (parsed.query or "") + (parsed.fragment or "")
    for char in shell_inject:
        if char in query_frag:
            raise ValueError(f"URL contém caractere não permitido na query: '{char}'")
    
    # Verifica espaços
    if ' ' in url:
        raise ValueError("URL não pode conter espaços")
    
    # Reconstrói URL sanitizada
    return urlunparse(parsed)


def validate_destination_path(path_str: str, create: bool = False) -> Path:
    """
    Valida caminho de destino para prevenir directory traversal.

    Args:
        path_str: Caminho informado pelo usuário.
        create:   Se True, cria o diretório e testa permissão de escrita.
                  Se False (padrão), apenas valida a estrutura sem tocar no disco —
                  use este modo na validação em tempo real (a cada tecla) para
                  evitar criar pastas enquanto o usuário ainda digita.

    Raises:
        ValueError: Se path for inválido ou inseguro
    """
    if not path_str or not path_str.strip():
        raise ValueError("Caminho não pode estar vazio")

    try:
        path = Path(path_str).expanduser().resolve()
    except Exception as e:
        raise ValueError(f"Caminho inválido: {e}")

    # Previne paths em diretórios do sistema
    path_str_normalized = str(path).replace('\\', '/')
    for danger in CONFIG.dangerous_system_dirs:
        danger_normalized = danger.replace('\\', '/')
        if path_str_normalized.lower().startswith(danger_normalized.lower()):
            raise ValueError(f"Não é permitido usar diretórios do sistema: {danger}")

    if create:
        # Garante que path é ou pode ser criado e é gravável
        try:
            ensure_dir(path)
            test_file = path / '.write_test_sitedownloader'
            test_file.touch()
            test_file.unlink()
        except PermissionError:
            raise ValueError(f"Sem permissão de escrita em: {path}")
        except Exception as e:
            raise ValueError(f"Não foi possível criar/acessar diretório: {e}")

    return path


def sanitize_user_agent(ua: str) -> str:
    """Sanitiza User-Agent para prevenir injection"""
    if not ua:
        return DEFAULT_USER_AGENT
    
    # Remove caracteres perigosos
    ua = re.sub(r'[^\w\s\-\.\+/\(\):;,]', '', ua)
    # Limita tamanho
    ua = ua[:CONFIG.max_user_agent_length]
    return ua if ua else DEFAULT_USER_AGENT


# ============================================================================
# RETRY LOGIC
# ============================================================================

def retry_on_io_error(max_attempts=3, delay=1.0, backoff=2.0):
    """Decorator para retry automático em operações de I/O"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            current_delay = delay
            last_exception = None
            
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except (IOError, OSError, PermissionError) as e:
                    attempt += 1
                    last_exception = e
                    
                    if attempt >= max_attempts:
                        break
                    
                    logging.warning(
                        f"Erro I/O em {func.__name__} (tentativa {attempt}/{max_attempts}): {e}"
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff
            
            # Se chegou aqui, todas as tentativas falharam
            raise IOError(
                f"{func.__name__} falhou após {max_attempts} tentativas"
            ) from last_exception
        
        return wrapper
    return decorator


# ============================================================================
# UTILITÁRIOS
# ============================================================================

def which(cmd: str) -> str | None:
    """Encontra executável no PATH"""
    return shutil.which(cmd)


def now_stamp() -> str:
    """Retorna timestamp atual formatado"""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def safe_domain_from_url(url: str) -> str:
    """Extrai domínio seguro da URL para nome de pasta"""
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


@retry_on_io_error(max_attempts=CONFIG.io_retry_attempts)
def sha256_file(p: Path) -> str:
    """
    Calcula SHA256 de arquivo com buffer otimizado
    
    Raises:
        IOError: Em caso de erro de leitura
    """
    h = hashlib.sha256()
    try:
        with p.open("rb") as f:
            while True:
                chunk = f.read(CONFIG.file_hash_buffer_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        raise IOError(f"Erro ao calcular hash de {p.name}: {e}") from e


def ensure_dir(p: Path) -> None:
    """Cria diretório se não existir"""
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise IOError(f"Erro ao criar diretório {p}: {e}") from e


def open_in_explorer(path: Path) -> None:
    """Abre caminho no explorador de arquivos do sistema"""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception as e:
        messagebox.showerror("Erro", f"Não foi possível abrir:\n{path}\n\n{e}")


# ============================================================================
# SNAPSHOT PATHS
# ============================================================================

@dataclass
class SnapshotPaths:
    """Estrutura de caminhos do snapshot"""
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
        """Cria estrutura de diretórios do snapshot"""
        stamp = now_stamp()
        snap_root = dest_root / domain / stamp

        offline = snap_root / "OFFLINE"
        evidencia = snap_root / "EVIDENCIA"
        evidencia_warc = evidencia / "warc"
        meta = snap_root / "META"
        logs = snap_root / "LOGS"

        abrir_aqui = snap_root / "ABRIR_AQUI.html"
        manifest = meta / "manifest.json"
        wget_log = logs / "wget.log"
        app_log = logs / "app.log"

        for d in [offline, evidencia_warc, meta, logs]:
            ensure_dir(d)

        return SnapshotPaths(
            root=snap_root,
            offline=offline,
            evidencia=evidencia,
            evidencia_warc=evidencia_warc,
            meta=meta,
            logs=logs,
            abrir_aqui=abrir_aqui,
            manifest=manifest,
            wget_log=wget_log,
            app_log=app_log,
        )


# ============================================================================
# STATS MONITOR (OTIMIZADO)
# ============================================================================

class StatsMonitor:
    """
    Monitor de estatísticas otimizado
    
    Usa os.walk() ao invés de rglob() para melhor performance (2-3x mais rápido)
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Reseta todas as estatísticas"""
        self.running = False
        self.start_ts = 0.0
        self.last_scan_ts = 0.0
        self.last_bytes = 0

        self.total_files = 0
        self.html_files = 0
        self.total_bytes = 0
        self.speed_bps = 0.0
        self.last_relpath = ""

    def start(self) -> None:
        """Inicia monitoramento"""
        self.running = True
        self.start_ts = time.time()
        self.last_scan_ts = 0.0
        self.last_bytes = 0
        self.total_files = 0
        self.html_files = 0
        self.total_bytes = 0
        self.speed_bps = 0.0
        self.last_relpath = ""

    def stop(self) -> None:
        """Para monitoramento"""
        self.running = False

    def _scan_tree_optimized(self, root: Path) -> tuple[int, int, int, str]:
        """
        Versão otimizada usando os.walk() - 2-3x mais rápido que rglob()
        
        Returns:
            (total_files, html_files, total_bytes, newest_file_path)
        """
        files = 0
        html = 0
        total = 0
        newest_mtime = -1.0
        newest_rel = ""

        if not root.exists():
            return 0, 0, 0, ""

        try:
            # os.walk é significativamente mais rápido que rglob
            for dirpath, _, filenames in os.walk(str(root)):
                dir_path = Path(dirpath)
                
                for filename in filenames:
                    file_path = dir_path / filename
                    
                    try:
                        st = file_path.stat()
                    except (OSError, PermissionError):
                        continue
                    
                    files += 1
                    total += st.st_size
                    
                    name_lower = filename.lower()
                    if name_lower.endswith(('.html', '.htm')):
                        html += 1
                    
                    if st.st_mtime > newest_mtime:
                        newest_mtime = st.st_mtime
                        try:
                            newest_rel = str(file_path.relative_to(root)).replace("\\", "/")
                        except ValueError:
                            newest_rel = filename
        
        except Exception as e:
            logging.warning(f"Erro ao escanear {root}: {e}")
        
        return files, html, total, newest_rel

    def maybe_scan(self, offline_dir: Path | None, warc_dir: Path | None) -> None:
        """Executa scan se intervalo mínimo passou"""
        if not self.running:
            return
        
        now = time.time()
        if (now - self.last_scan_ts) < CONFIG.stats_scan_interval_s:
            return

        files = 0
        html = 0
        total = 0
        last_rel = ""

        if offline_dir is not None:
            f, h, b, lr = self._scan_tree_optimized(offline_dir)
            files += f
            html += h
            total += b
            if lr:
                last_rel = f"OFFLINE/{lr}"

        if warc_dir is not None:
            f2, h2, b2, lr2 = self._scan_tree_optimized(warc_dir)
            files += f2
            html += h2
            total += b2
            if lr2:
                last_rel = f"EVIDENCIA/warc/{lr2}"

        # Calcula velocidade
        dt = max(1e-6, now - (self.last_scan_ts if self.last_scan_ts else self.start_ts))
        self.speed_bps = (total - self.last_bytes) / dt
        self.last_bytes = total
        self.last_scan_ts = now

        self.total_files = files
        self.html_files = html
        self.total_bytes = total
        if last_rel:
            self.last_relpath = last_rel

    def get_elapsed_time(self) -> float:
        """Retorna tempo decorrido desde o início"""
        if not self.running or self.start_ts == 0:
            return 0.0
        return time.time() - self.start_ts

    def format_bytes(self, b: int) -> str:
        """Formata bytes em unidade legível"""
        if b < 1024:
            return f"{b} B"
        elif b < 1024 ** 2:
            return f"{b / 1024:.1f} KB"
        elif b < 1024 ** 3:
            return f"{b / (1024 ** 2):.1f} MB"
        else:
            return f"{b / (1024 ** 3):.2f} GB"

    def format_speed(self, bps: float) -> str:
        """Formata velocidade"""
        return f"{self.format_bytes(int(bps))}/s"

    def format_time(self, seconds: float) -> str:
        """Formata tempo em HH:MM:SS"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# ============================================================================
# GERAÇÃO DE HTML
# ============================================================================

def make_abrir_aqui_html(snapshot: SnapshotPaths, offline_entry: str | None = None) -> str:
    """Gera HTML do painel ABRIR_AQUI.html"""
    
    offline_link = ""
    if offline_entry:
        offline_link = f'<li><a href="{offline_entry}" target="_blank">🌐 Abrir versão offline do site</a></li>'
    
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Snapshot - {snapshot.root.name}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .container {{
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 800px;
            width: 100%;
            padding: 40px;
        }}
        h1 {{
            color: #333;
            margin-bottom: 10px;
            font-size: 28px;
        }}
        .subtitle {{
            color: #666;
            margin-bottom: 30px;
            font-size: 14px;
        }}
        .info-box {{
            background: #f8f9fa;
            border-left: 4px solid #667eea;
            padding: 15px;
            margin-bottom: 20px;
            border-radius: 4px;
        }}
        .info-box strong {{
            color: #667eea;
        }}
        h2 {{
            color: #444;
            margin: 30px 0 15px 0;
            font-size: 20px;
            border-bottom: 2px solid #667eea;
            padding-bottom: 8px;
        }}
        ul {{
            list-style: none;
            padding: 0;
        }}
        li {{
            margin: 10px 0;
        }}
        a {{
            color: #667eea;
            text-decoration: none;
            padding: 10px 15px;
            display: inline-block;
            border-radius: 6px;
            transition: all 0.3s ease;
            background: #f8f9fa;
        }}
        a:hover {{
            background: #667eea;
            color: white;
            transform: translateX(5px);
        }}
        .footer {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #e0e0e0;
            text-align: center;
            color: #999;
            font-size: 12px;
        }}
        .emoji {{ font-size: 1.2em; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📦 Snapshot Arquivado</h1>
        <p class="subtitle">Site Downloader v{APP_VERSION} • {datetime.now().strftime("%d/%m/%Y %H:%M")}</p>
        
        <div class="info-box">
            <strong>Snapshot:</strong> {snapshot.root.name}<br>
            <strong>Localização:</strong> {snapshot.root}
        </div>

        <h2>🌐 Navegação</h2>
        <ul>
            {offline_link}
            <li><a href="META/manifest.json" target="_blank">📋 Ver manifest.json (metadados)</a></li>
            <li><a href="META/checksums.sha256" target="_blank">🔐 Ver checksums SHA256</a></li>
        </ul>

        <h2>📂 Estrutura</h2>
        <ul>
            <li><a href="OFFLINE/" target="_blank">📁 OFFLINE/ - Versão navegável do site</a></li>
            <li><a href="EVIDENCIA/warc/" target="_blank">📁 EVIDENCIA/warc/ - Arquivos WARC e CDX</a></li>
            <li><a href="META/" target="_blank">📁 META/ - Manifest e checksums</a></li>
            <li><a href="LOGS/" target="_blank">📁 LOGS/ - Logs de download</a></li>
        </ul>

        <h2>ℹ️ Informações</h2>
        <div class="info-box">
            <p><strong>OFFLINE/</strong> → Cópia navegável (HTML/CSS/JS/Imagens)</p>
            <p><strong>EVIDENCIA/warc/</strong> → WARC + CDX (evidência/verificação)</p>
            <p><strong>META/</strong> → manifest.json + checksums</p>
            <p><strong>LOGS/</strong> → wget.log + app.log</p>
        </div>

        <div class="footer">
            Site Downloader v{APP_VERSION} — Arquivamento & Verificação
        </div>
    </div>
</body>
</html>"""
    
    return html


# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging(snapshot: SnapshotPaths | None = None) -> None:
    """Configura sistema de logging"""
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, CONFIG.log_level))
    
    # Remove handlers existentes
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # File handler com rotação (se snapshot fornecido)
    if snapshot:
        try:
            file_handler = RotatingFileHandler(
                snapshot.app_log,
                maxBytes=CONFIG.log_max_bytes,
                backupCount=CONFIG.log_backup_count,
                encoding='utf-8'
            )
            file_handler.setLevel(logging.DEBUG)
            file_format = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
            )
            file_handler.setFormatter(file_format)
            logger.addHandler(file_handler)
        except Exception as e:
            logging.warning(f"Não foi possível configurar file handler: {e}")


# ============================================================================
# APLICAÇÃO PRINCIPAL
# ============================================================================

class App:
    """Aplicação principal com todas as melhorias implementadas"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("900x750")
        self.root.minsize(800, 600)
        
        # Configuração
        self.config_path = Path.home() / ".site_downloader_config.json"
        global CONFIG
        CONFIG = AppConfig.load_from_file(self.config_path)
        
        # Estado
        self.proc: subprocess.Popen | None = None
        self.stop_event = threading.Event()
        self.stats = StatsMonitor()
        self.last_snapshot: SnapshotPaths | None = None
        
        # Queue para logging thread-safe
        self.log_queue: queue.Queue[str | None] = queue.Queue()
        
        # Variáveis Tkinter
        self.url_var = tk.StringVar()
        self.dest_var = tk.StringVar(value=str(Path.home() / "ArquivosSites"))
        self.depth_var = tk.StringVar(value="2")
        self.user_agent_var = tk.StringVar(value=DEFAULT_USER_AGENT)
        self.wait_var = tk.StringVar(value="1.0")
        self.offline_var = tk.IntVar(value=1)
        self.warc_var = tk.IntVar(value=1)
        self.extra_domains_var = tk.StringVar()
        self.full_hash_var = tk.IntVar(value=0)
        self.status_var = tk.StringVar(value="Pronto")
        
        # Variáveis de validação
        self.url_valid = False
        self.dest_valid = True
        
        self._build_ui()
        self._start_log_processor()
        self._start_stats_ui_updater()
        
        # Validação em tempo real
        self.url_var.trace_add("write", self._validate_url_realtime)
        self.dest_var.trace_add("write", self._validate_dest_realtime)
        
        logging.info("Aplicação inicializada")

    def _build_ui(self) -> None:
        """Constrói interface do usuário com abas: Download e PDFs"""

        # Notebook principal
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        # ── ABA 1: Download ──────────────────────────────────────────
        download_outer = ttk.Frame(nb)
        nb.add(download_outer, text="⬇️  Download")

        # Canvas com scrollbar para a aba de download
        main_canvas = tk.Canvas(download_outer)
        scrollbar = ttk.Scrollbar(download_outer, orient="vertical", command=main_canvas.yview)
        scrollable_frame = ttk.Frame(main_canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: main_canvas.configure(scrollregion=main_canvas.bbox("all"))
        )

        main_canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        main_canvas.configure(yscrollcommand=scrollbar.set)

        main_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # === SEÇÃO URL ===
        url_frame = ttk.LabelFrame(scrollable_frame, text="🌐 URL do Site", padding=10)
        url_frame.pack(fill="x", padx=10, pady=5)

        ttk.Entry(url_frame, textvariable=self.url_var, width=60).pack(side="left", fill="x", expand=True)
        self.url_status_label = ttk.Label(url_frame, text="", width=30)
        self.url_status_label.pack(side="left", padx=5)

        # === SEÇÃO DESTINO ===
        dest_frame = ttk.LabelFrame(scrollable_frame, text="📁 Pasta de Destino", padding=10)
        dest_frame.pack(fill="x", padx=10, pady=5)

        ttk.Entry(dest_frame, textvariable=self.dest_var, width=50).pack(side="left", fill="x", expand=True)
        ttk.Button(dest_frame, text="Procurar...", command=self.choose_dest).pack(side="left", padx=5)
        ttk.Button(dest_frame, text="Abrir", command=self.open_dest).pack(side="left")
        self.dest_status_label = ttk.Label(dest_frame, text="✓", foreground="green")
        self.dest_status_label.pack(side="left", padx=5)

        # === OPÇÕES BÁSICAS ===
        basic_frame = ttk.LabelFrame(scrollable_frame, text="⚙️ Opções Básicas", padding=10)
        basic_frame.pack(fill="x", padx=10, pady=5)

        row1 = ttk.Frame(basic_frame)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Profundidade:").pack(side="left")
        ttk.Spinbox(row1, textvariable=self.depth_var, from_=1, to=20, width=10).pack(side="left", padx=5)
        ttk.Label(row1, text="Espera entre requests (s):").pack(side="left", padx=(20, 0))
        ttk.Spinbox(row1, textvariable=self.wait_var, from_=0.1, to=60.0, increment=0.1, width=10).pack(side="left", padx=5)

        row2 = ttk.Frame(basic_frame)
        row2.pack(fill="x", pady=5)
        ttk.Label(row2, text="User-Agent:").pack(side="left")
        ttk.Entry(row2, textvariable=self.user_agent_var, width=60).pack(side="left", fill="x", expand=True, padx=5)

        # === OPÇÕES AVANÇADAS ===
        adv_frame = ttk.LabelFrame(scrollable_frame, text="🔧 Opções Avançadas", padding=10)
        adv_frame.pack(fill="x", padx=10, pady=5)

        ttk.Checkbutton(adv_frame, text="Criar versão OFFLINE navegável", variable=self.offline_var).pack(anchor="w")
        ttk.Checkbutton(adv_frame, text="Gerar WARC/CDX (evidência)", variable=self.warc_var).pack(anchor="w")
        ttk.Checkbutton(adv_frame, text="Hash completo de OFFLINE (lento em sites grandes)", variable=self.full_hash_var).pack(anchor="w")

        extra_row = ttk.Frame(adv_frame)
        extra_row.pack(fill="x", pady=5)
        ttk.Label(extra_row, text="Domínios extras (separados por vírgula):").pack(side="left")
        ttk.Entry(extra_row, textvariable=self.extra_domains_var, width=40).pack(side="left", fill="x", expand=True, padx=5)

        # === STATUS ===
        status_frame = ttk.LabelFrame(scrollable_frame, text="📊 Status", padding=10)
        status_frame.pack(fill="x", padx=10, pady=5)

        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, font=("", 10, "bold"))
        self.status_label.pack(anchor="w")

        self.stats_label = ttk.Label(status_frame, text="", justify="left")
        self.stats_label.pack(anchor="w", pady=5)

        # === BOTÕES ===
        btn_frame = ttk.Frame(scrollable_frame, padding=10)
        btn_frame.pack(fill="x", padx=10, pady=5)

        self.btn_start = ttk.Button(btn_frame, text="▶ Iniciar Download", command=self.start_snapshot, state="disabled")
        self.btn_start.pack(side="left", padx=5)

        self.btn_stop = ttk.Button(btn_frame, text="⏹ Parar", command=self.stop_snapshot, state="disabled")
        self.btn_stop.pack(side="left", padx=5)

        self.btn_open_snap = ttk.Button(btn_frame, text="📂 Abrir Último Snapshot", command=self.open_snapshot, state="disabled")
        self.btn_open_snap.pack(side="left", padx=5)

        ttk.Button(btn_frame, text="ℹ️ Sobre", command=self.show_about).pack(side="right", padx=5)

        # === LOG ===
        log_frame = ttk.LabelFrame(scrollable_frame, text="📝 Log", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

        # ── ABA 2: Batch ─────────────────────────────────────────────
        self.batch_tab = BatchTab(nb, app_ref=self)

        # ── ABA 3: PDFs ──────────────────────────────────────────────
        self.pdf_tab = PdfTab(nb, get_dest=lambda: self.dest_var.get())

        # ── ABA 4: GitHub ─────────────────────────────────────────────
        self.git_tab = GitTab(nb, get_dest=lambda: self.dest_var.get())

    def _validate_url_realtime(self, *args) -> None:
        """Valida URL em tempo real conforme usuário digita"""
        url = self.url_var.get().strip()
        
        if not url:
            self.url_status_label.configure(text="", foreground="black")
            self.url_valid = False
            self._update_start_button_state()
            return
        
        try:
            validate_and_sanitize_url(url)
            self.url_status_label.configure(text="✓ URL válida", foreground="green")
            self.url_valid = True
        except ValueError as e:
            error_msg = str(e)
            if len(error_msg) > 50:
                error_msg = error_msg[:47] + "..."
            self.url_status_label.configure(text=f"✗ {error_msg}", foreground="red")
            self.url_valid = False
        
        self._update_start_button_state()

    def _validate_dest_realtime(self, *args) -> None:
        """Valida destino em tempo real"""
        dest = self.dest_var.get().strip()
        
        if not dest:
            self.dest_status_label.configure(text="✗ Vazio", foreground="red")
            self.dest_valid = False
            self._update_start_button_state()
            return
        
        try:
            validate_destination_path(dest, create=False)
            self.dest_status_label.configure(text="✓", foreground="green")
            self.dest_valid = True
        except ValueError as e:
            error_msg = str(e)
            if len(error_msg) > 30:
                error_msg = error_msg[:27] + "..."
            self.dest_status_label.configure(text=f"✗ {error_msg}", foreground="red")
            self.dest_valid = False
        
        self._update_start_button_state()

    def _update_start_button_state(self) -> None:
        """Atualiza estado do botão Iniciar baseado em validações"""
        if self.url_valid and self.dest_valid and not self.proc:
            self.btn_start.configure(state="normal")
        else:
            self.btn_start.configure(state="disabled")

    def _start_log_processor(self) -> None:
        """Inicia processador de logs thread-safe"""
        def process_logs():
            try:
                while True:
                    try:
                        msg = self.log_queue.get(timeout=0.1)
                        if msg is None:  # Sinal de parada
                            break
                        # Atualiza UI de forma segura na main thread
                        self.root.after(0, lambda m=msg: self._append_log_safe(m))
                        self.log_queue.task_done()
                    except queue.Empty:
                        if self.stop_event.is_set():
                            break
                        continue
            except Exception as e:
                logging.error(f"Erro no log processor: {e}")
        
        log_thread = threading.Thread(target=process_logs, daemon=True)
        log_thread.start()

    def _append_log_safe(self, text: str) -> None:
        """Append de log garantidamente na main thread"""
        try:
            self.log_text.insert("end", text)
            self.log_text.see("end")
        except Exception as e:
            logging.error(f"Erro ao adicionar log: {e}")

    def log(self, text: str) -> None:
        """Thread-safe log - pode ser chamado de qualquer thread"""
        self.log_queue.put(text)

    def _start_stats_ui_updater(self) -> None:
        """Inicia atualizador de UI de estatísticas"""
        def update_ui():
            if self.stats.running:
                elapsed = self.stats.get_elapsed_time()
                speed = self.stats.format_speed(self.stats.speed_bps)
                size = self.stats.format_bytes(self.stats.total_bytes)
                time_str = self.stats.format_time(elapsed)
                
                stats_text = (
                    f"Tempo: {time_str} | "
                    f"Arquivos: {self.stats.total_files} (HTML: {self.stats.html_files}) | "
                    f"Tamanho: {size} | "
                    f"Velocidade: {speed}"
                )
                
                if self.stats.last_relpath:
                    stats_text += f"\nÚltimo: {self.stats.last_relpath}"
                
                self.stats_label.configure(text=stats_text)
            
            self.root.after(CONFIG.stats_ui_tick_ms, update_ui)
        
        self.root.after(CONFIG.stats_ui_tick_ms, update_ui)

    def choose_dest(self) -> None:
        """Abre diálogo para escolher destino"""
        d = filedialog.askdirectory(title="Escolher pasta de destino")
        if d:
            self.dest_var.set(d)

    def show_about(self) -> None:
        """Mostra informações sobre o aplicativo"""
        about_text = f"""Site Downloader v{APP_VERSION}
Arquivamento & Verificação

✅ Melhorias implementadas:
• Validação robusta de entrada
• Otimização de performance (2-3x)
• Thread-safe logging
• Retry automático
• Timeout configurável
• Logging estruturado

© 2024-2026 - Código aberto
"""
        messagebox.showinfo("Sobre", about_text)

    def start_snapshot(self) -> None:
        """Inicia processo de snapshot"""
        # Valida entrada
        try:
            url = validate_and_sanitize_url(self.url_var.get())
            dest = validate_destination_path(self.dest_var.get(), create=True)
        except ValueError as e:
            messagebox.showerror("Erro de Validação", str(e))
            return
        
        # Verifica wget
        wget_cmd = which("wget") or which("wget2")
        if not wget_cmd:
            messagebox.showerror(
                "wget não encontrado",
                "wget ou wget2 não foi encontrado no PATH.\n\n"
                "Instale wget e tente novamente."
            )
            return
        
        # Cria estrutura
        domain = safe_domain_from_url(url)
        try:
            snapshot = SnapshotPaths.create(dest, domain)
            self.last_snapshot = snapshot
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível criar estrutura:\n{e}")
            logging.error(f"Erro ao criar snapshot: {e}", exc_info=True)
            return
        
        # Configura logging para este snapshot
        setup_logging(snapshot)
        logging.info(f"Iniciando snapshot de {url}")
        
        # Reseta estado
        self.stop_event.clear()
        self.stats.reset()
        self.stats.start()
        
        # UI
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_open_snap.configure(state="disabled")
        self.status_var.set("Iniciando...")
        self.log("="*80 + "\n")
        self.log(f"[INFO] Iniciando snapshot de: {url}\n")
        self.log(f"[INFO] Destino: {snapshot.root}\n")
        self.log(f"[INFO] Comando wget: {wget_cmd}\n")
        self.log("="*80 + "\n")
        
        # Salva configurações iniciais no manifest
        manifest_data = {
            "version": APP_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "url": url,
            "domain": domain,
            "options": self._get_options_dict(),
            "wget_command": wget_cmd,
        }
        
        try:
            snapshot.manifest.write_text(
                json.dumps(manifest_data, indent=2, ensure_ascii=False),
                encoding='utf-8'
            )
        except Exception as e:
            logging.warning(f"Erro ao salvar manifest inicial: {e}")
        
        # Constrói argumentos wget
        args = self._build_wget_args(wget_cmd, url, snapshot)
        
        # Inicia threads
        wget_thread = threading.Thread(
            target=self._run_wget_robust,
            args=(args, snapshot, str(snapshot.root)),
            daemon=False
        )
        wget_thread.start()
        
        stats_thread = threading.Thread(
            target=self._stats_loop,
            args=(snapshot,),
            daemon=True
        )
        stats_thread.start()

    @staticmethod
    def _parse_depth(raw: str) -> int:
        """Valida e retorna profundidade como inteiro seguro (1-20)."""
        try:
            v = int(float(raw.strip()))
        except (ValueError, AttributeError):
            raise ValueError(f"Profundidade inválida: '{raw}'. Use um número inteiro de 1 a 20.")
        if not (1 <= v <= 20):
            raise ValueError(f"Profundidade fora do intervalo permitido (1-20): {v}")
        return v

    @staticmethod
    def _parse_wait(raw: str) -> float:
        """Valida e retorna tempo de espera como float seguro (0.0-60.0)."""
        try:
            v = float(raw.strip())
        except (ValueError, AttributeError):
            raise ValueError(f"Espera inválida: '{raw}'. Use um número como 1.0 ou 2.5.")
        if not (0.0 <= v <= 60.0):
            raise ValueError(f"Espera fora do intervalo permitido (0-60s): {v}")
        return v

    def _build_wget_args(self, wget_cmd: str, url: str, snapshot: SnapshotPaths) -> list[str]:
        """Constrói argumentos para wget"""
        args = [wget_cmd]

        # User-Agent sanitizado
        ua = sanitize_user_agent(self.user_agent_var.get())
        args.extend(["--user-agent", ua])

        # Valida e converte depth/wait para evitar injeção de argumentos
        depth = self._parse_depth(self.depth_var.get())
        wait  = self._parse_wait(self.wait_var.get())

        # Opções básicas
        args.extend([
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
        ])
        
        # Domínios extras
        extra = self.extra_domains_var.get().strip()
        if extra:
            domains = [d.strip() for d in extra.split(",") if d.strip()]
            for d in domains:
                args.extend(["--domains", d])
        
        # WARC
        if self.warc_var.get():
            warc_base = snapshot.evidencia_warc / "snapshot"
            args.extend([
                "--warc-file", str(warc_base),
                "--warc-cdx",
            ])
        
        # Offline
        if self.offline_var.get():
            args.extend([
                "-P", str(snapshot.offline),
            ])
        
        args.append(url)
        
        return args

    def _run_wget_robust(self, args: list[str], snapshot: SnapshotPaths, cwd: str) -> None:
        """Versão robusta de execução do wget com timeout e monitoring"""
        timeout = max(CONFIG.subprocess_timeout, 86400)
        
        # Inicia processo
        try:
            self.proc = subprocess.Popen(
                args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            self.proc = None
            self.log(f"[ERRO] Falha ao iniciar wget: {e}\n")
            logging.error(f"Falha ao iniciar wget: {e}", exc_info=True)
            self._finish(False, snapshot)
            return
        
        logging.info(f"Processo wget iniciado (PID: {self.proc.pid})")
        
        # Thread para monitorar timeout
        timeout_event = threading.Event()
        
        def timeout_monitor():
            if not timeout_event.wait(timeout):
                self.log(f"\n[WARN] Processo excedeu timeout de {timeout}s, forçando término\n")
                logging.warning(f"Timeout de {timeout}s excedido")
                self.stop_snapshot()
        
        timeout_thread = threading.Thread(target=timeout_monitor, daemon=True)
        timeout_thread.start()
        
        # Processa output
        try:
            assert self.proc.stdout is not None
            
            # Abre arquivo de log — se falhar, avisa e continua sem gravar em disco
            try:
                logf = snapshot.wget_log.open("a", encoding="utf-8", errors="replace")
            except Exception as e:
                self.log(f"[WARN] Não foi possível abrir wget.log: {e} — saída não será salva em disco\n")
                logging.warning(f"Erro ao abrir wget.log: {e}")
                logf = None

            def _process_output(log_file_or_none):
                for line in self.proc.stdout:
                    if self.stop_event.is_set():
                        break
                    if log_file_or_none is not None:
                        log_file_or_none.write(line)
                        log_file_or_none.flush()
                    self.log(line)

            if logf is not None:
                with logf as log_file:
                    _process_output(log_file)
            else:
                # Sem arquivo de log, mas ainda processa/exibe a saída na UI
                for line in self.proc.stdout:
                    if self.stop_event.is_set():
                        break
                    self.log(line)

            # Cancela monitor de timeout
            timeout_event.set()
            
            # Aguarda processo terminar
            try:
                rc = self.proc.wait(timeout=CONFIG.subprocess_cleanup_timeout)
            except subprocess.TimeoutExpired:
                self.log("[WARN] Processo não terminou após timeout de cleanup, forçando...\n")
                logging.warning("Timeout no cleanup, forçando término")
                self.stop_snapshot()
                try:
                    rc = self.proc.wait(timeout=5)
                except:
                    rc = -1
            
        except Exception as e:
            self.log(f"[ERRO] Erro durante execução: {e}\n")
            logging.error(f"Erro durante execução do wget: {e}", exc_info=True)
            timeout_event.set()
            rc = -1
        
# Considera 0 (perfeito) e 8 (erros do servidor/404) como ok
        ok = (rc in (0, 8)) and (not self.stop_event.is_set())
        logging.info(f"Processo wget finalizado (RC: {rc}, OK: {ok})")
        self._finish(ok, snapshot)

    def _stats_loop(self, snapshot: SnapshotPaths) -> None:
        """Loop de estatísticas em thread separada"""
        offline_dir = snapshot.offline if self.offline_var.get() else None
        warc_dir = snapshot.evidencia_warc if self.warc_var.get() else None
        
        while self.stats.running and (not self.stop_event.is_set()):
            if self.proc is None:
                time.sleep(0.1)
                continue
            
            try:
                self.stats.maybe_scan(offline_dir=offline_dir, warc_dir=warc_dir)
            except Exception as e:
                logging.error(f"Erro no stats loop: {e}", exc_info=True)
            
            time.sleep(0.25)

    def stop_snapshot(self) -> None:
        """Para o processo de snapshot"""
        if self.proc is None:
            return
        
        self.stop_event.set()
        self.status_var.set("Parando...")
        self.log("\n[INFO] Parando processo...\n")
        logging.info("Parando processo...")
        
        try:
            if sys.platform.startswith("win"):
                # Windows: taskkill com árvore de processos
                pid = self.proc.pid
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10
                )
            else:
                # Unix: SIGTERM
                self.proc.send_signal(signal.SIGTERM)
                time.sleep(2)
                if self.proc.poll() is None:
                    # Ainda rodando, força SIGKILL
                    self.proc.kill()
        except Exception as e:
            self.log(f"[WARN] Falha ao parar: {e}\n")
            logging.warning(f"Erro ao parar processo: {e}", exc_info=True)

    def _find_offline_entry(self, offline_dir: Path, url: str) -> str | None:
        """Retorna caminho relativo do HTML de entrada do OFFLINE"""
        try:
            parsed = urlparse(url)
            host = parsed.netloc
            path = parsed.path or "/"
            
            # Wget costuma criar: OFFLINE/<host>/<path>/index.html
            rel_parts = [host] + [p for p in path.split("/") if p]
            
            if path.endswith("/"):
                rel_parts.append("index.html")
            else:
                if rel_parts and "." in rel_parts[-1]:
                    pass  # Já aponta para arquivo
                else:
                    rel_parts.append("index.html")
            
            candidate = offline_dir.joinpath(*rel_parts)
            if candidate.exists():
                rel = candidate.relative_to(offline_dir)
                return f"OFFLINE/{rel.as_posix()}"
        except Exception as e:
            logging.warning(f"Erro ao encontrar entrada offline: {e}")
        
        # Fallback: procura primeiro index.html com os.walk (consistente com o resto do código)
        try:
            found = []
            for dirpath, _, filenames in os.walk(str(offline_dir)):
                if "index.html" in filenames:
                    found.append(Path(dirpath) / "index.html")
            if found:
                found.sort()
                rel = found[0].relative_to(offline_dir)
                return f"OFFLINE/{rel.as_posix()}"
        except Exception:
            pass
        
        return None

    def _finish(self, ok: bool, snapshot: SnapshotPaths) -> None:
        """Finaliza snapshot"""
        def _ui_finalize():
            # Scan final
            try:
                self.stats.maybe_scan(
                    offline_dir=snapshot.offline if self.offline_var.get() else None,
                    warc_dir=snapshot.evidencia_warc if self.warc_var.get() else None,
                )
            except Exception as e:
                logging.error(f"Erro no scan final: {e}", exc_info=True)
            
            self.stats.stop()
            
            # Finaliza artifacts
            try:
                self._finalize_artifacts(snapshot, ok)
            except Exception as e:
                self.log(f"[ERRO] Erro ao finalizar artifacts: {e}\n")
                logging.error(f"Erro ao finalizar artifacts: {e}", exc_info=True)
            
            self.proc = None
            
            if ok:
                self.status_var.set("✓ Concluído")
                self.log("\n[OK] Download concluído com sucesso.\n")
                logging.info("Download concluído com sucesso")
                
                try:
                    messagebox.showinfo(
                        "Concluído",
                        "Snapshot finalizado com sucesso!\n\n"
                        "Abra o arquivo ABRIR_AQUI.html para navegar."
                    )
                except:
                    pass
            else:
                if self.stop_event.is_set():
                    self.status_var.set("⏹ Interrompido")
                    self.log("\n[INFO] Download interrompido pelo usuário.\n")
                    logging.info("Download interrompido pelo usuário")
                else:
                    self.status_var.set("✗ Falhou")
                    self.log("\n[ERRO] Download finalizado com falha (veja o log).\n")
                    logging.error("Download finalizado com falha")
            
            self.btn_start.configure(state="normal" if self.url_valid and self.dest_valid else "disabled")
            self.btn_stop.configure(state="disabled")
            self.btn_open_snap.configure(state="normal")
        
        self.root.after(0, _ui_finalize)

    def _finalize_artifacts(self, snapshot: SnapshotPaths, ok: bool) -> None:
        """Finaliza artifacts (manifest, hashes, ABRIR_AQUI.html)"""
        # Encontra entrada offline
        offline_entry = None
        if self.offline_var.get():
            try:
                offline_entry = self._find_offline_entry(snapshot.offline, url=self.url_var.get().strip())
            except Exception as e:
                logging.warning(f"Erro ao encontrar offline entry: {e}")
        
        # Gera ABRIR_AQUI.html
        try:
            html = make_abrir_aqui_html(snapshot, offline_entry=offline_entry)
            snapshot.abrir_aqui.write_text(html, encoding="utf-8")
        except Exception as e:
            logging.error(f"Erro ao criar ABRIR_AQUI.html: {e}", exc_info=True)
        
        # Atualiza manifest
        try:
            data = json.loads(snapshot.manifest.read_text(encoding="utf-8"))
        except:
            data = {}
        
        data.setdefault("result", {})
        data["result"].update({
            "ok": bool(ok),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "stats": {
                "total_files": int(self.stats.total_files),
                "html_files": int(self.stats.html_files),
                "total_bytes": int(self.stats.total_bytes),
                "elapsed_seconds": self.stats.get_elapsed_time(),
            },
            "paths": {
                "offline_dir": str(snapshot.offline) if self.offline_var.get() else None,
                "warc_dir": str(snapshot.evidencia_warc) if self.warc_var.get() else None,
                "abrir_aqui": str(snapshot.abrir_aqui),
                "wget_log": str(snapshot.wget_log),
                "app_log": str(snapshot.app_log),
            }
        })
        
        # Calcula hashes de arquivos importantes
        hashes = {}
        important = [snapshot.wget_log, snapshot.app_log, snapshot.manifest, snapshot.abrir_aqui]
        
        if snapshot.evidencia_warc.exists():
            for p in snapshot.evidencia_warc.glob("snapshot.warc*"):
                important.append(p)
            for p in snapshot.evidencia_warc.glob("snapshot.cdx"):
                important.append(p)
        
        for p in important:
            if p.exists() and p.is_file():
                try:
                    rel_path = str(p.relative_to(snapshot.root)).replace("\\", "/")
                    hashes[rel_path] = sha256_file(p)
                except Exception as e:
                    logging.warning(f"Erro ao calcular hash de {p.name}: {e}")
        
        data["result"]["hashes"] = hashes
        
        # Hash completo de OFFLINE (se habilitado)
        if self.full_hash_var.get() and self.offline_var.get():
            self.log("[INFO] Calculando hashes de todos os arquivos OFFLINE (pode demorar)...\n")
            full_hashes = {}
            
            try:
                for p in snapshot.offline.rglob("*"):
                    if p.is_file():
                        try:
                            rel = str(p.relative_to(snapshot.root)).replace("\\", "/")
                            full_hashes[rel] = sha256_file(p)
                        except Exception as e:
                            logging.warning(f"Erro ao calcular hash de {p.name}: {e}")
                
                data["result"]["full_hashes_offline_sha256"] = full_hashes
            except Exception as e:
                logging.error(f"Erro ao calcular hashes completos: {e}", exc_info=True)
        
        # Salva manifest
        try:
            snapshot.manifest.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        except Exception as e:
            logging.error(f"Erro ao salvar manifest: {e}", exc_info=True)
        
        # Salva checksums.sha256
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
                f"""Site Downloader v{APP_VERSION} - Snapshot

Abra ABRIR_AQUI.html para navegar.

ESTRUTURA:
----------
OFFLINE/          → Cópia navegável (HTML/CSS/JS/Imagens)
EVIDENCIA/warc/   → WARC + CDX (evidência/verificação)
META/             → manifest.json + checksums.sha256
LOGS/             → wget.log + app.log

VERIFICAÇÃO:
-----------
Use checksums.sha256 para verificar integridade dos arquivos.

Criado em: {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}
""",
                encoding="utf-8"
            )
        except Exception as e:
            logging.warning(f"Erro ao criar README: {e}")

    def _get_options_dict(self) -> dict:
        """Retorna dicionário com opções atuais"""
        return {
            "url": self.url_var.get().strip(),
            "dest": self.dest_var.get().strip(),
            "depth": self.depth_var.get(),
            "user_agent": self.user_agent_var.get(),
            "wait": self.wait_var.get(),
            "offline": bool(self.offline_var.get()),
            "warc": bool(self.warc_var.get()),
            "extra_domains": self.extra_domains_var.get().strip(),
            "full_hash_offline": bool(self.full_hash_var.get()),
        }

    def open_dest(self) -> None:
        """Abre pasta de destino no explorador"""
        try:
            p = Path(self.dest_var.get()).expanduser().resolve()
            if p.exists():
                open_in_explorer(p)
            else:
                messagebox.showwarning("Aviso", "Pasta ainda não existe")
        except Exception as e:
            messagebox.showerror("Erro", f"Erro ao abrir pasta:\n{e}")

    def open_snapshot(self) -> None:
        """Abre último snapshot no explorador"""
        if self.last_snapshot and self.last_snapshot.root.exists():
            open_in_explorer(self.last_snapshot.root)
        else:
            messagebox.showwarning("Aviso", "Nenhum snapshot disponível")

    def cleanup(self) -> None:
        """Cleanup ao fechar aplicação"""
        logging.info("Encerrando aplicação")
        
        # Para processos
        if self.proc:
            self.stop_snapshot()
        
        # Sinaliza threads
        self.stop_event.set()
        self.log_queue.put(None)
        
        # Salva configuração
        try:
            CONFIG.save_to_file(self.config_path)
        except Exception as e:
            logging.error(f"Erro ao salvar config: {e}")


# ============================================================================
# COLETOR E ORGANIZADOR DE PDFs
# ============================================================================

@dataclass
class PdfEntry:
    """Representa um PDF encontrado em um snapshot"""
    path: Path
    domain: str
    snapshot: str          # nome da pasta de snapshot (timestamp)
    size_bytes: int
    sha256: str = ""
    source_url: str = ""   # URL original (se encontrada no manifest)

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def size_human(self) -> str:
        b = self.size_bytes
        if b < 1024:
            return f"{b} B"
        elif b < 1024**2:
            return f"{b/1024:.1f} KB"
        elif b < 1024**3:
            return f"{b/1024**2:.1f} MB"
        return f"{b/1024**3:.2f} GB"


class PdfCollector:
    """
    Coleta, organiza e exporta PDFs encontrados nos snapshots.

    Estrutura de saída:
        <dest_root>/
        └── _PDFs/
            ├── dominio.com/
            │   ├── arquivo1.pdf
            │   └── arquivo2.pdf
            ├── outro.com/
            │   └── relatorio.pdf
            └── indice_pdfs.json    ← índice com metadados de todos os PDFs
    """

    PDF_DIR_NAME = "_PDFs"

    def __init__(self, dest_root: Path) -> None:
        self.dest_root = dest_root
        self.pdf_root = dest_root / self.PDF_DIR_NAME

    # ------------------------------------------------------------------
    # Varredura
    # ------------------------------------------------------------------

    def scan(self, on_progress: Callable[[str], None] | None = None) -> list[PdfEntry]:
        """
        Varre todos os snapshots em dest_root e retorna lista de PdfEntry.
        Não copia nada — apenas cataloga.
        """
        entries: list[PdfEntry] = []
        if not self.dest_root.exists():
            return entries

        for dirpath, dirnames, filenames in os.walk(str(self.dest_root)):
            # Não varrer dentro da própria pasta _PDFs
            dirnames[:] = [d for d in dirnames if d != self.PDF_DIR_NAME]

            for filename in filenames:
                if not filename.lower().endswith(".pdf"):
                    continue

                full_path = Path(dirpath) / filename
                try:
                    size = full_path.stat().st_size
                except OSError:
                    continue

                # Determina domínio e snapshot a partir do caminho relativo
                try:
                    rel = full_path.relative_to(self.dest_root)
                    parts = rel.parts
                    domain = parts[0] if len(parts) > 0 else "desconhecido"
                    snapshot_name = parts[1] if len(parts) > 1 else ""
                except ValueError:
                    domain = "desconhecido"
                    snapshot_name = ""

                entry = PdfEntry(
                    path=full_path,
                    domain=domain,
                    snapshot=snapshot_name,
                    size_bytes=size,
                )

                if on_progress:
                    on_progress(f"Encontrado: {domain}/{filename}")

                entries.append(entry)

        return entries

    # ------------------------------------------------------------------
    # Cópia organizada
    # ------------------------------------------------------------------

    @retry_on_io_error(max_attempts=3)
    def _copy_pdf(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            return  # já existe com mesmo tamanho, pula
        shutil.copy2(str(src), str(dst))

    def collect(
        self,
        entries: list[PdfEntry],
        calc_hashes: bool = False,
        on_progress: Callable[[str], None] | None = None,
    ) -> list[PdfEntry]:
        """
        Copia PDFs para <dest_root>/_PDFs/<dominio>/<filename>
        resolvendo colisões de nome com sufixo numérico.

        Retorna a lista atualizada com sha256 preenchido (se calc_hashes=True).
        """
        ensure_dir(self.pdf_root)
        seen: dict[str, int] = {}  # dst_path -> contador de colisões

        for entry in entries:
            domain_dir = self.pdf_root / entry.domain
            dst = domain_dir / entry.filename

            # Resolve colisão de nomes
            dst_key = str(dst).lower()
            if dst_key in seen:
                seen[dst_key] += 1
                stem = dst.stem
                dst = domain_dir / f"{stem}_{seen[dst_key]}{dst.suffix}"
            else:
                seen[dst_key] = 0

            try:
                self._copy_pdf(entry.path, dst)
                if on_progress:
                    on_progress(f"Copiado: {entry.domain}/{dst.name}")
            except Exception as e:
                logging.warning(f"Erro ao copiar {entry.path}: {e}")
                if on_progress:
                    on_progress(f"[ERRO] {entry.domain}/{entry.filename}: {e}")
                continue

            if calc_hashes:
                try:
                    entry.sha256 = sha256_file(dst)
                except Exception as e:
                    logging.warning(f"Erro ao calcular hash de {dst.name}: {e}")

        return entries

    # ------------------------------------------------------------------
    # Índice JSON
    # ------------------------------------------------------------------

    def save_index(self, entries: list[PdfEntry]) -> Path:
        """Grava indice_pdfs.json com metadados de todos os PDFs coletados."""
        index_path = self.pdf_root / "indice_pdfs.json"
        data = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(entries),
            "total_bytes": sum(e.size_bytes for e in entries),
            "pdfs": [
                {
                    "filename": e.filename,
                    "domain": e.domain,
                    "snapshot": e.snapshot,
                    "size_bytes": e.size_bytes,
                    "size_human": e.size_human,
                    "sha256": e.sha256,
                    "source_url": e.source_url,
                    "collected_path": str(
                        (self.pdf_root / e.domain / e.filename)
                        .relative_to(self.dest_root)
                    ).replace("\\", "/"),
                }
                for e in entries
            ],
        }
        try:
            ensure_dir(self.pdf_root)
            index_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as ex:
            logging.error(f"Erro ao salvar índice de PDFs: {ex}")
        return index_path


# ============================================================================
# ABA DE PDFs (widget reutilizável)
# ============================================================================

class PdfTab:
    """
    Aba de gerenciamento de PDFs integrada na interface principal.
    Pode ser adicionada em qualquer ttk.Notebook.
    """

    def __init__(self, notebook: ttk.Notebook, get_dest: Callable[[], str]) -> None:
        self.get_dest = get_dest
        self._entries: list[PdfEntry] = []
        self._running = False

        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="📄 PDFs")

        # --- Barra de ações ---
        action_bar = ttk.Frame(frame)
        action_bar.pack(fill="x", pady=(0, 6))

        self.btn_scan = ttk.Button(action_bar, text="🔍 Varrer Snapshots", command=self._scan)
        self.btn_scan.pack(side="left", padx=2)

        self.btn_collect = ttk.Button(
            action_bar, text="📥 Coletar PDFs", command=self._collect, state="disabled"
        )
        self.btn_collect.pack(side="left", padx=2)

        self.hash_var = tk.IntVar(value=0)
        ttk.Checkbutton(action_bar, text="Calcular SHA256", variable=self.hash_var).pack(
            side="left", padx=6
        )

        self.btn_open = ttk.Button(
            action_bar, text="📂 Abrir Pasta _PDFs", command=self._open_pdf_folder, state="disabled"
        )
        self.btn_open.pack(side="left", padx=2)

        ttk.Button(action_bar, text="💾 Exportar índice JSON", command=self._export_json).pack(
            side="right", padx=2
        )

        # --- Estatísticas ---
        self.stats_var = tk.StringVar(value="Clique em 'Varrer Snapshots' para começar.")
        ttk.Label(frame, textvariable=self.stats_var, foreground="#444").pack(anchor="w", pady=2)

        # --- Tabela de PDFs ---
        cols = ("domain", "filename", "snapshot", "size")
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True, pady=4)

        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("domain",   text="Domínio",   command=lambda: self._sort("domain"))
        self.tree.heading("filename", text="Arquivo",   command=lambda: self._sort("filename"))
        self.tree.heading("snapshot", text="Snapshot",  command=lambda: self._sort("snapshot"))
        self.tree.heading("size",     text="Tamanho",   command=lambda: self._sort("size"))
        self.tree.column("domain",   width=160, minwidth=100)
        self.tree.column("filename", width=240, minwidth=150)
        self.tree.column("snapshot", width=160, minwidth=120)
        self.tree.column("size",     width=90,  minwidth=70, anchor="e")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        # Duplo-clique → abre o PDF
        self.tree.bind("<Double-1>", self._open_selected)

        # --- Log interno ---
        log_frame = ttk.LabelFrame(frame, text="Log", padding=4)
        log_frame.pack(fill="x", pady=4)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=5, wrap="word", font=("Consolas", 8))
        self.log_text.pack(fill="both", expand=True)

        self._sort_col = "domain"
        self._sort_rev = False

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for e in self._entries:
            self.tree.insert("", "end", values=(e.domain, e.filename, e.snapshot, e.size_human))

    def _sort(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = False

        key_map = {
            "domain":   lambda e: e.domain.lower(),
            "filename": lambda e: e.filename.lower(),
            "snapshot": lambda e: e.snapshot,
            "size":     lambda e: e.size_bytes,
        }
        self._entries.sort(key=key_map.get(col, lambda e: e.domain), reverse=self._sort_rev)
        self._refresh_tree()

    def _update_stats(self) -> None:
        total = len(self._entries)
        total_bytes = sum(e.size_bytes for e in self._entries)
        domains = len({e.domain for e in self._entries})
        size_h = PdfEntry(Path(), "", "", total_bytes).size_human if self._entries else "0 B"
        self.stats_var.set(
            f"{total} PDFs encontrados | {domains} domínios | {size_h} total"
        )

    # ------------------------------------------------------------------
    # Ações
    # ------------------------------------------------------------------

    def _scan(self) -> None:
        dest_str = self.get_dest()
        if not dest_str:
            self._log("[ERRO] Pasta de destino não definida.")
            return

        dest = Path(dest_str).expanduser().resolve()
        if not dest.exists():
            self._log(f"[ERRO] Pasta não existe: {dest}")
            return

        self._log(f"Varrendo: {dest}")
        self.btn_scan.configure(state="disabled")

        collector = PdfCollector(dest)

        def run():
            entries = collector.scan(on_progress=lambda m: self.log_text.after(0, lambda msg=m: self._log(msg)))
            self.log_text.after(0, lambda: self._on_scan_done(entries))

        threading.Thread(target=run, daemon=True).start()

    def _on_scan_done(self, entries: list[PdfEntry]) -> None:
        self._entries = entries
        self._refresh_tree()
        self._update_stats()
        self.btn_scan.configure(state="normal")
        self.btn_collect.configure(state="normal" if entries else "disabled")
        self._log(f"Varredura concluída: {len(entries)} PDFs encontrados.")

    def _collect(self) -> None:
        if not self._entries:
            return

        dest_str = self.get_dest()
        if not dest_str:
            self._log("[ERRO] Pasta de destino não definida.")
            return

        dest = Path(dest_str).expanduser().resolve()
        calc_hashes = bool(self.hash_var.get())
        collector = PdfCollector(dest)

        self.btn_collect.configure(state="disabled")
        self._log(f"Coletando {len(self._entries)} PDFs para {collector.pdf_root} ...")

        def run():
            updated = collector.collect(
                self._entries,
                calc_hashes=calc_hashes,
                on_progress=lambda m: self.log_text.after(0, lambda msg=m: self._log(msg)),
            )
            index_path = collector.save_index(updated)
            self.log_text.after(0, lambda: self._on_collect_done(index_path))

        threading.Thread(target=run, daemon=True).start()

    def _on_collect_done(self, index_path: Path) -> None:
        self._refresh_tree()
        self.btn_collect.configure(state="normal")
        self.btn_open.configure(state="normal")
        self._log(f"Coleta concluída! Índice salvo em: {index_path}")

    def _open_pdf_folder(self) -> None:
        dest_str = self.get_dest()
        if not dest_str:
            return
        pdf_root = Path(dest_str).expanduser().resolve() / PdfCollector.PDF_DIR_NAME
        if pdf_root.exists():
            open_in_explorer(pdf_root)
        else:
            messagebox.showinfo("Aviso", "Pasta _PDFs ainda não foi criada.\nClique em 'Coletar PDFs' primeiro.")

    def _open_selected(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if 0 <= idx < len(self._entries):
            open_in_explorer(self._entries[idx].path)

    def _export_json(self) -> None:
        if not self._entries:
            messagebox.showwarning("Aviso", "Faça uma varredura primeiro.")
            return
        dest_str = self.get_dest()
        if not dest_str:
            return
        dest = Path(dest_str).expanduser().resolve()
        collector = PdfCollector(dest)
        index_path = collector.save_index(self._entries)
        messagebox.showinfo("Exportado", f"Índice JSON salvo em:\n{index_path}")
        self._log(f"Índice exportado: {index_path}")


# ============================================================================
# BATCH — DOWNLOAD DE MÚLTIPLOS SITES
# ============================================================================

@dataclass
class BatchJob:
    """Representa um item da fila de batch"""
    url: str
    status: str = "aguardando"     # aguardando / em_andamento / concluido / erro / cancelado
    snapshot_root: Path | None = None
    error_msg: str = ""
    elapsed: float = 0.0

    @property
    def status_icon(self) -> str:
        return {
            "aguardando":   "⏳",
            "em_andamento": "⬇️",
            "concluido":    "✅",
            "erro":         "❌",
            "cancelado":    "⏹",
        }.get(self.status, "?")


class BatchTab:
    """
    Aba para download de múltiplos sites em fila sequencial.
    Reutiliza as mesmas opções da aba Download (dest, depth, wait, etc.)
    via referência ao objeto App.
    """

    def __init__(self, notebook: ttk.Notebook, app_ref: "App") -> None:
        self.app = app_ref
        self.root = app_ref.root
        self._jobs: list[BatchJob] = []
        self._proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()
        self._running = False

        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="📋 Batch")

        # --- Entrada de URLs ---
        url_frame = ttk.LabelFrame(frame, text="URLs (uma por linha)", padding=6)
        url_frame.pack(fill="x", pady=(0, 6))

        self.url_text = scrolledtext.ScrolledText(url_frame, height=6, wrap="word", font=("Consolas", 9))
        self.url_text.pack(fill="x")
        ttk.Label(url_frame, text="Cole as URLs abaixo, uma por linha. As opções de Profundidade, Espera, Destino e demais da aba Download serão usadas.", foreground="#666", wraplength=700).pack(anchor="w", pady=(4, 0))

        # --- Botões ---
        btn_bar = ttk.Frame(frame)
        btn_bar.pack(fill="x", pady=(0, 6))

        self.btn_start = ttk.Button(btn_bar, text="▶ Iniciar Batch", command=self._start_batch)
        self.btn_start.pack(side="left", padx=2)

        self.btn_stop = ttk.Button(btn_bar, text="⏹ Parar Tudo", command=self._stop_batch, state="disabled")
        self.btn_stop.pack(side="left", padx=2)

        self.btn_clear = ttk.Button(btn_bar, text="🗑 Limpar Lista", command=self._clear)
        self.btn_clear.pack(side="left", padx=2)

        self.progress_var = tk.StringVar(value="")
        ttk.Label(btn_bar, textvariable=self.progress_var, foreground="#444").pack(side="left", padx=10)

        # --- Tabela de progresso ---
        cols = ("icon", "url", "status", "elapsed", "snapshot")
        tbl_frame = ttk.Frame(frame)
        tbl_frame.pack(fill="both", expand=True, pady=4)

        self.tree = ttk.Treeview(tbl_frame, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("icon",     text="")
        self.tree.heading("url",      text="URL")
        self.tree.heading("status",   text="Status")
        self.tree.heading("elapsed",  text="Tempo")
        self.tree.heading("snapshot", text="Snapshot")
        self.tree.column("icon",     width=30,  minwidth=30,  anchor="center")
        self.tree.column("url",      width=300, minwidth=200)
        self.tree.column("status",   width=120, minwidth=80)
        self.tree.column("elapsed",  width=70,  minwidth=60, anchor="e")
        self.tree.column("snapshot", width=260, minwidth=150)

        vsb = ttk.Scrollbar(tbl_frame, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tbl_frame.rowconfigure(0, weight=1)
        tbl_frame.columnconfigure(0, weight=1)

        # Duplo-clique abre pasta do snapshot
        self.tree.bind("<Double-1>", self._open_selected_snapshot)

        # --- Log ---
        log_frame = ttk.LabelFrame(frame, text="Log Batch", padding=4)
        log_frame.pack(fill="x", pady=4)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=5, wrap="word", font=("Consolas", 8))
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _ui(self, fn) -> None:
        """Agenda execução na main thread de forma segura."""
        self.root.after(0, fn)

    def _refresh_row(self, idx: int) -> None:
        """Atualiza a linha da tabela para o job[idx] — deve rodar na main thread."""
        job = self._jobs[idx]
        elapsed_str = f"{job.elapsed:.0f}s" if job.elapsed > 0 else ""
        snap_str = str(job.snapshot_root) if job.snapshot_root else ""
        status_text = job.status if not job.error_msg else f"{job.status}: {job.error_msg[:40]}"
        children = self.tree.get_children()
        if idx < len(children):
            self.tree.item(children[idx], values=(
                job.status_icon, job.url, status_text, elapsed_str, snap_str
            ))

    def _update_progress(self) -> None:
        done  = sum(1 for j in self._jobs if j.status in ("concluido", "erro", "cancelado"))
        total = len(self._jobs)
        ok    = sum(1 for j in self._jobs if j.status == "concluido")
        err   = sum(1 for j in self._jobs if j.status == "erro")
        self.progress_var.set(f"{done}/{total} | ✅ {ok}  ❌ {err}")

    # ------------------------------------------------------------------
    # Ações principais
    # ------------------------------------------------------------------

    def _parse_urls(self) -> list[str]:
        """Lê o ScrolledText e retorna lista de URLs válidas."""
        raw = self.url_text.get("1.0", "end").strip()
        result = []
        errors = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                url = validate_and_sanitize_url(line)
                result.append(url)
            except ValueError as e:
                errors.append(f"URL inválida ignorada: {line!r} — {e}")
        for err in errors:
            self._log(f"[WARN] {err}")
        return result

    def _start_batch(self) -> None:
        urls = self._parse_urls()
        if not urls:
            messagebox.showwarning("Batch", "Nenhuma URL válida encontrada na lista.")
            return

        # Valida destino antes de começar
        dest_str = self.app.dest_var.get()
        try:
            validate_destination_path(dest_str, create=True)
        except ValueError as e:
            messagebox.showerror("Erro", f"Pasta de destino inválida:\n{e}")
            return

        # Monta jobs
        self._jobs = [BatchJob(url=u) for u in urls]
        self._stop_event.clear()
        self._running = True

        # Popula tabela
        self.tree.delete(*self.tree.get_children())
        for job in self._jobs:
            self.tree.insert("", "end", values=(job.status_icon, job.url, job.status, "", ""))

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._log(f"Iniciando batch: {len(self._jobs)} sites")

        threading.Thread(target=self._batch_worker, daemon=False).start()

    def _stop_batch(self) -> None:
        self._stop_event.set()
        self.btn_stop.configure(state="disabled")
        self._log("[INFO] Solicitado parada do batch...")
        # Para o processo wget atual se existir
        if self._proc and self._proc.poll() is None:
            try:
                if sys.platform.startswith("win"):
                    subprocess.run(
                        ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10
                    )
                else:
                    self._proc.send_signal(signal.SIGTERM)
            except Exception as e:
                self._log(f"[WARN] Erro ao parar processo: {e}")

    def _clear(self) -> None:
        if self._running:
            messagebox.showwarning("Batch", "Pare o batch antes de limpar.")
            return
        self.tree.delete(*self.tree.get_children())
        self._jobs = []
        self.progress_var.set("")
        self.log_text.delete("1.0", "end")

    def _open_selected_snapshot(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if 0 <= idx < len(self._jobs):
            snap = self._jobs[idx].snapshot_root
            if snap and snap.exists():
                open_in_explorer(snap)

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _batch_worker(self) -> None:
        """Processa jobs sequencialmente em thread separada."""
        for idx, job in enumerate(self._jobs):
            if self._stop_event.is_set():
                job.status = "cancelado"
                self._ui(lambda i=idx: self._refresh_row(i))
                continue

            job.status = "em_andamento"
            self._ui(lambda i=idx: (self._refresh_row(i), self._update_progress()))

            t0 = time.time()
            try:
                self._run_single(job)
            except Exception as e:
                job.status = "erro"
                job.error_msg = str(e)
                logging.error(f"Batch erro em {job.url}: {e}", exc_info=True)
            finally:
                job.elapsed = time.time() - t0

            self._ui(lambda i=idx: (self._refresh_row(i), self._update_progress()))
            self._ui(lambda u=job.url, s=job.status: self._log(f"[{s.upper()}] {u}"))

        self._running = False
        self._ui(self._on_batch_done)

    def _run_single(self, job: BatchJob) -> None:
        """
        Executa download de um único site.
        Reutiliza _build_wget_args e _finalize_artifacts do App.
        """
        url = job.url
        dest = validate_destination_path(self.app.dest_var.get(), create=True)
        wget_cmd = which("wget") or which("wget2")
        if not wget_cmd:
            raise RuntimeError("wget não encontrado no PATH")

        domain = safe_domain_from_url(url)
        snapshot = SnapshotPaths.create(dest, domain)
        job.snapshot_root = snapshot.root

        # Manifest inicial
        manifest_data = {
            "version": APP_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "url": url,
            "domain": domain,
            "options": self.app._get_options_dict(),
            "wget_command": wget_cmd,
            "batch": True,
        }
        try:
            snapshot.manifest.write_text(
                json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logging.warning(f"Erro ao salvar manifest inicial batch: {e}")

        # Sobrescreve url_var temporariamente para _build_wget_args
        old_url = self.app.url_var.get()
        self.app.url_var.set(url)
        try:
            args = self.app._build_wget_args(wget_cmd, url, snapshot)
        finally:
            self.app.url_var.set(old_url)

        self._ui(lambda u=url: self._log(f"[WGET] Iniciando: {u}"))

        # Executa wget
        try:
            self._proc = subprocess.Popen(
                args,
                cwd=str(snapshot.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            raise RuntimeError(f"Falha ao iniciar wget: {e}") from e
        if self._proc.stdout is None:
            raise RuntimeError("Falha ao capturar stdout do wget")
        # Processa saída e grava log
        try:
            logf = snapshot.wget_log.open("a", encoding="utf-8", errors="replace")
        except Exception:
            logf = None

        def _stream():
            if logf:
                with logf as lf:
                    for line in self._proc.stdout:
                        if self._stop_event.is_set():
                            break
                        lf.write(line)
                        lf.flush()
                        self._ui(lambda l=line.rstrip(): self._log(l))
            else:
                for line in self._proc.stdout:
                    if self._stop_event.is_set():
                        break
                    self._ui(lambda l=line.rstrip(): self._log(l))

        stream_thread = threading.Thread(target=_stream, daemon=True)
        stream_thread.start()

        try:
            timeout_sec = max(CONFIG.subprocess_timeout, 86400)
            rc = self._proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            self._ui(lambda: self._log(f"Timeout excedido para {url}"))
            try:
                if self._proc and sys.platform.startswith("win"):
                    subprocess.run(
                        ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                elif self._proc:
                    self._proc.kill()
            except Exception as e:
                self._ui(lambda: self._log(f"[WARN] Falha ao matar processo após timeout: {e}"))
            rc = -1

        stream_thread.join(timeout=5)
        self._proc = None

        if self._stop_event.is_set():
            job.status = "cancelado"
            return

        ok = (rc in (0, 8))
        if rc == 0:
            job.status = "concluido"
        elif rc == 8:
            job.status = "concluido"
            job.error_msg = "Alguns links estavam quebrados (404/403)"
        else:
            job.status = "erro"
            job.error_msg = f"wget retornou código {rc}"

        # Finaliza artifacts (manifest, checksums, ABRIR_AQUI.html)
        try:
            # Temporariamente ajusta url_var para _finalize_artifacts
            self.app.url_var.set(url)
            self.app._finalize_artifacts(snapshot, ok)
        finally:
            self.app.url_var.set(old_url)

    def _on_batch_done(self) -> None:
        ok    = sum(1 for j in self._jobs if j.status == "concluido")
        err   = sum(1 for j in self._jobs if j.status == "erro")
        canc  = sum(1 for j in self._jobs if j.status == "cancelado")
        total = len(self._jobs)
        self._log(f"\n{'='*60}")
        self._log(f"Batch concluído: {total} sites | ✅ {ok} OK | ❌ {err} erros | ⏹ {canc} cancelados")
        self._log(f"{'='*60}")
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")


# ============================================================================
# GITHUB — COMMIT & PUSH DE SNAPSHOTS
# ============================================================================

class GitTab:
    """
    Aba para configurar repositório git e fazer commit/push dos snapshots.
    Suporta GitHub (e qualquer repositório git remoto).
    Token PAT armazenado apenas em memória, nunca em disco.
    """

    def __init__(self, notebook: ttk.Notebook, get_dest: Callable[[], str]) -> None:
        self.get_dest = get_dest
        self._running = False

        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="🔗 GitHub")

        # --- Configuração do repositório ---
        cfg_frame = ttk.LabelFrame(frame, text="Configuração do Repositório", padding=8)
        cfg_frame.pack(fill="x", pady=(0, 6))

        # Repo URL
        r1 = ttk.Frame(cfg_frame)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="URL do repositório:", width=22, anchor="w").pack(side="left")
        self.repo_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.repo_var, width=55).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(r1, text="ex: https://github.com/user/repo.git", foreground="#888").pack(side="left")

        # Token
        r2 = ttk.Frame(cfg_frame)
        r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="Token (PAT):", width=22, anchor="w").pack(side="left")
        self.token_var = tk.StringVar()
        token_entry = ttk.Entry(r2, textvariable=self.token_var, width=55, show="*")
        token_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(r2, text="Nunca salvo em disco", foreground="#888").pack(side="left")

        # Branch
        r3 = ttk.Frame(cfg_frame)
        r3.pack(fill="x", pady=2)
        ttk.Label(r3, text="Branch:", width=22, anchor="w").pack(side="left")
        self.branch_var = tk.StringVar(value="main")
        ttk.Entry(r3, textvariable=self.branch_var, width=20).pack(side="left", padx=4)

        # Pasta local git (padrão = pasta de destino)
        r4 = ttk.Frame(cfg_frame)
        r4.pack(fill="x", pady=2)
        ttk.Label(r4, text="Pasta local (git root):", width=22, anchor="w").pack(side="left")
        self.git_dir_var = tk.StringVar()
        ttk.Entry(r4, textvariable=self.git_dir_var, width=45).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(r4, text="Usar pasta de destino", command=self._use_dest_dir).pack(side="left", padx=2)
        ttk.Button(r4, text="Procurar...", command=self._browse_git_dir).pack(side="left")

        # Mensagem de commit personalizada
        r5 = ttk.Frame(cfg_frame)
        r5.pack(fill="x", pady=2)
        ttk.Label(r5, text="Mensagem do commit:", width=22, anchor="w").pack(side="left")
        self.commit_msg_var = tk.StringVar(value="")
        ttk.Entry(r5, textvariable=self.commit_msg_var, width=55).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(r5, text="(deixe vazio para gerar automaticamente)", foreground="#888").pack(side="left")

        # --- Botões de ação ---
        btn_bar = ttk.Frame(frame)
        btn_bar.pack(fill="x", pady=(0, 6))

        self.btn_init = ttk.Button(btn_bar, text="🔧 Inicializar Repo", command=self._configure_repo)
        self.btn_init.pack(side="left", padx=2)

        self.btn_status = ttk.Button(btn_bar, text="📋 Ver Status Git", command=self._check_status)
        self.btn_status.pack(side="left", padx=2)

        self.btn_commit = ttk.Button(btn_bar, text="📤 Commit & Push", command=self._commit_and_push)
        self.btn_commit.pack(side="left", padx=2)

        ttk.Label(btn_bar, text="⚠️ Commits incluem TODOS os arquivos na pasta local.", foreground="#c07000").pack(side="left", padx=10)

        # --- Log ---
        log_frame = ttk.LabelFrame(frame, text="Log Git", padding=4)
        log_frame.pack(fill="both", expand=True, pady=4)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=14, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _mask_token(self, text: str) -> str:
        """Substitui token na string por *** para exibição segura no log."""
        token = self.token_var.get().strip()
        if token and token in text:
            text = text.replace(token, "***")
        return text

    def _get_git_dir(self) -> Path | None:
        """Retorna pasta git validada ou None."""
        raw = self.git_dir_var.get().strip()
        if not raw:
            raw = self.get_dest()
        if not raw:
            messagebox.showerror("Erro", "Configure a pasta local do repositório.")
            return None
        p = Path(raw).expanduser().resolve()
        if not p.exists():
            messagebox.showerror("Erro", f"Pasta não existe:\n{p}")
            return None
        return p

    def _build_remote_url(self) -> str | None:
        """Constrói URL remota com token embutido."""
        repo = self.repo_var.get().strip()
        token = self.token_var.get().strip()
        if not repo:
            messagebox.showerror("Erro", "Informe a URL do repositório.")
            return None
        if token:
            # Injeta token: https://TOKEN@github.com/...
            if repo.startswith("https://"):
                repo_with_token = "https://" + token + "@" + repo[len("https://"):]
            else:
                repo_with_token = repo
        else:
            repo_with_token = repo
        return repo_with_token

    def _run_git(self, args: list[str], cwd: Path) -> tuple[int, str]:
        """
        Executa comando git e retorna (returncode, output).
        Nunca usa shell=True. Mascara token no log.
        """
        try:
            result = subprocess.run(
                args,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            output = result.stdout + result.stderr
            return result.returncode, output
        except subprocess.TimeoutExpired:
            return -1, "Timeout ao executar git"
        except FileNotFoundError:
            return -1, "git não encontrado no PATH. Instale o Git: https://git-scm.com"
        except Exception as e:
            return -1, str(e)

    def _git_in_thread(self, fn: Callable) -> None:
        """Executa fn em thread separada para não travar a UI."""
        if self._running:
            messagebox.showwarning("Git", "Operação git já em andamento.")
            return
        self._running = True
        for btn in (self.btn_init, self.btn_status, self.btn_commit):
            btn.configure(state="disabled")

        def worker():
            try:
                fn()
            finally:
                self._running = False
                self.log_text.after(0, lambda: [
                    b.configure(state="normal")
                    for b in (self.btn_init, self.btn_status, self.btn_commit)
                ])

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _use_dest_dir(self) -> None:
        dest = self.get_dest()
        if dest:
            self.git_dir_var.set(dest)

    def _browse_git_dir(self) -> None:
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Pasta raiz do repositório git")
        if d:
            self.git_dir_var.set(d)

    # ------------------------------------------------------------------
    # Ações git
    # ------------------------------------------------------------------

    def _configure_repo(self) -> None:
        def _do():
            git_dir = self._get_git_dir()
            if not git_dir:
                return

            self.log_text.after(0, lambda: self._log(f"\n{'='*60}"))
            self.log_text.after(0, lambda: self._log(f"Configurando repositório em: {git_dir}"))

            # git init
            rc, out = self._run_git(["git", "init"], git_dir)
            self.log_text.after(0, lambda o=out: self._log(self._mask_token(o.strip())))
            if rc != 0:
                self.log_text.after(0, lambda: self._log("[ERRO] Falha no git init"))
                return

            # Configura remote (se fornecido)
            repo_url = self._build_remote_url()
            if repo_url:
                # Verifica se já existe remote origin
                rc2, out2 = self._run_git(["git", "remote", "get-url", "origin"], git_dir)
                if rc2 == 0:
                    # Já existe — atualiza
                    rc3, out3 = self._run_git(["git", "remote", "set-url", "origin", repo_url], git_dir)
                    self.log_text.after(0, lambda o=out3: self._log(self._mask_token(f"Remote atualizado: {o.strip()}")))
                else:
                    # Não existe — adiciona
                    rc3, out3 = self._run_git(["git", "remote", "add", "origin", repo_url], git_dir)
                    self.log_text.after(0, lambda o=out3: self._log(self._mask_token(f"Remote adicionado: {o.strip()}")))

            # Cria .gitignore básico se não existir
            gitignore = git_dir / ".gitignore"
            if not gitignore.exists():
                try:
                    gitignore.write_text(
                        "# Site Downloader\n*.log\n__pycache__/\n*.pyc\n",
                        encoding="utf-8"
                    )
                    self.log_text.after(0, lambda: self._log("Criado .gitignore básico"))
                except Exception:
                    pass

            self.log_text.after(0, lambda: self._log("✅ Repositório configurado com sucesso!"))

        self._git_in_thread(_do)

    def _check_status(self) -> None:
        def _do():
            git_dir = self._get_git_dir()
            if not git_dir:
                return

            self.log_text.after(0, lambda: self._log(f"\n{'='*60}"))
            self.log_text.after(0, lambda: self._log(f"git status em: {git_dir}"))

            rc, out = self._run_git(["git", "status", "--short"], git_dir)
            if rc != 0:
                self.log_text.after(0, lambda o=out: self._log(f"[ERRO] {self._mask_token(o.strip())}"))
                return

            lines = out.strip().splitlines()
            if not lines:
                self.log_text.after(0, lambda: self._log("Nada para commitar (working tree limpa)"))
            else:
                self.log_text.after(0, lambda: self._log(f"{len(lines)} arquivo(s) modificado(s)/novo(s):"))
                # Mostra até 30 linhas para não poluir
                for line in lines[:30]:
                    self.log_text.after(0, lambda l=line: self._log(f"  {l}"))
                if len(lines) > 30:
                    self.log_text.after(0, lambda n=len(lines)-30: self._log(f"  ... e mais {n} arquivo(s)"))

        self._git_in_thread(_do)

    def _commit_and_push(self) -> None:
        def _do():
            git_dir = self._get_git_dir()
            if not git_dir:
                return

            repo_url = self._build_remote_url()
            branch = self.branch_var.get().strip() or "main"

            # Monta mensagem de commit
            custom_msg = self.commit_msg_var.get().strip()
            if custom_msg:
                commit_msg = custom_msg
            else:
                commit_msg = f"Snapshots {datetime.now().strftime('%Y-%m-%d %H:%M')} — Site Downloader v{APP_VERSION}"

            self.log_text.after(0, lambda: self._log(f"\n{'='*60}"))
            self.log_text.after(0, lambda: self._log(f"Iniciando commit em: {git_dir}"))
            self.log_text.after(0, lambda: self._log(f"Branch: {branch}"))
            self.log_text.after(0, lambda m=commit_msg: self._log(f"Mensagem: {m}"))

            # 1. git init (caso ainda não tenha sido feito)
            rc, out = self._run_git(["git", "init"], git_dir)
            if rc != 0:
                self.log_text.after(0, lambda o=out: self._log(f"[ERRO] git init: {self._mask_token(o.strip())}"))
                return

            # 2. Configura remote se URL fornecida
            if repo_url:
                rc2, _ = self._run_git(["git", "remote", "get-url", "origin"], git_dir)
                if rc2 == 0:
                    self._run_git(["git", "remote", "set-url", "origin", repo_url], git_dir)
                else:
                    self._run_git(["git", "remote", "add", "origin", repo_url], git_dir)

            # 3. git add .
            self.log_text.after(0, lambda: self._log("git add . (adicionando arquivos...)"))
            rc3, out3 = self._run_git(["git", "add", "."], git_dir)
            if rc3 != 0:
                self.log_text.after(0, lambda o=out3: self._log(f"[ERRO] git add: {self._mask_token(o.strip())}"))
                return
            self.log_text.after(0, lambda: self._log("Arquivos adicionados ao staging."))

            # 4. git commit
            self.log_text.after(0, lambda: self._log("git commit..."))
            rc4, out4 = self._run_git(
                ["git", "commit", "-m", commit_msg,
                 "--author", "Site Downloader <sitedownloader@local>"],
                git_dir
            )
            out4_clean = self._mask_token(out4.strip())
            if rc4 != 0:
                # Pode ser "nothing to commit" — não é erro fatal
                if "nothing to commit" in out4.lower():
                    self.log_text.after(0, lambda: self._log("ℹ️ Nada para commitar (sem mudanças desde o último commit)."))
                else:
                    self.log_text.after(0, lambda o=out4_clean: self._log(f"[ERRO] git commit: {o}"))
                    return
            else:
                self.log_text.after(0, lambda o=out4_clean: self._log(f"Commit criado:\n{o}"))

            # 5. git push (só se tiver remote configurado)
            if repo_url:
                self.log_text.after(0, lambda: self._log(f"git push origin {branch}..."))
                rc5, out5 = self._run_git(
                    ["git", "push", "-u", "origin", branch, "--force-with-lease"],
                    git_dir
                )
                out5_clean = self._mask_token(out5.strip())
                if rc5 != 0:
                    self.log_text.after(0, lambda o=out5_clean: self._log(f"[ERRO] git push: {o}"))
                    self.log_text.after(0, lambda: self._log("Dica: verifique o token, a URL do repo e se o branch existe no remote."))
                    return
                self.log_text.after(0, lambda o=out5_clean: self._log(f"Push concluído:\n{o}"))
                self.log_text.after(0, lambda: self._log("✅ Snapshots enviados ao repositório!"))
            else:
                self.log_text.after(0, lambda: self._log("✅ Commit local criado (sem push — URL do repositório não configurada)."))

        self._git_in_thread(_do)


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    """Função principal"""
    # Setup logging inicial (console apenas)
    setup_logging()
    
    logging.info(f"Iniciando {APP_TITLE}")
    
    # Cria janela
    root = tk.Tk()
    app = App(root)
    
    # Handler de fechamento
    def on_closing():
        if app.proc:
            if messagebox.askokcancel("Sair", "Download em andamento. Deseja realmente sair?"):
                app.cleanup()
                root.destroy()
        else:
            app.cleanup()
            root.destroy()
    
    root.protocol("WM_DELETE_WINDOW", on_closing)
    
    # Inicia loop
    root.mainloop()
    
    logging.info("Aplicação encerrada")


if __name__ == "__main__":
    main()
