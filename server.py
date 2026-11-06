#!/usr/bin/env python3
"""
LLM / GPU Script Switcher Web Server (port 3001)
Scans /home/pwit for executable *.sh scripts and lets the user launch/switch them via Web UI.
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", "/home/pwit")).resolve()
APP_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "3001"))
HOST = os.environ.get("HOST", "0.0.0.0")

# Container name patterns managed by the scripts (excluding open-webui)
MANAGED_CONTAINER_REGEX = re.compile(r"^(llama-.*|comfyui|photo-restore)$")


class JobState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.script = None
        self.args = []
        self.started_at = None
        self.finished_at = None
        self.returncode = None
        self.output_lines = []
        self.proc = None

    def to_dict(self):
        with self.lock:
            return {
                "running": self.running,
                "script": self.script,
                "args": self.args,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "returncode": self.returncode,
                "output": "".join(self.output_lines[-400:]),
            }


JOB = JobState()


def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "localhost"


def docker_cmd() -> list[str]:
    """Return ['docker'] if accessible directly, otherwise ['sudo', '-n', 'docker']."""
    if os.geteuid() == 0 or os.access("/var/run/docker.sock", os.R_OK | os.W_OK):
        return ["docker"]
    return ["sudo", "-n", "docker"]


def parse_script_metadata(path: Path) -> dict:
    """Extract description, model, port, container name, and supported arguments from a .sh file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        text = ""

    lines = text.splitlines()
    comments = []
    for line in lines[1:20]:
        s = line.strip()
        if not s:
            if comments:
                break
            continue
        if s.startswith("set -"):
            continue
        if s.startswith("#"):
            c = s.lstrip("#").strip()
            if c and not c.startswith("==="):
                comments.append(c)
        else:
            break

    def find_var(name: str) -> str | None:
        m = re.search(rf'^{name}="([^"\n]+)"', text, re.M)
        if m:
            val = m.group(1)
            # Clean up bash parameter expansion like ${CTX_SIZE:-16384}
            m2 = re.match(r"^\$\{[A-Za-z0-9_]+:-([^}]+)\}$", val)
            return m2.group(1) if m2 else val
        m = re.search(rf"^{name}=([0-9]+)", text, re.M)
        return m.group(1) if m else None

    container = find_var("CONTAINER_NAME")
    model_file = find_var("MODEL_FILE")
    port = find_var("PORT")
    comfy_port = find_var("COMFY_PORT")
    text_port = find_var("TEXT_PORT")
    webui_port = find_var("WEBUI_PORT")
    ctx_size = find_var("CTX_SIZE")

    # Build list of ports & applications served by this script
    services = []
    if path.name == "start-photo-restore.sh":
        if webui_port:
            services.append({"port": int(webui_port), "app": "OpenWebUI (Czat)", "path": "/"})
        if text_port:
            services.append({"port": int(text_port), "app": f"llama.cpp CPU ({model_file or 'LLM'})", "path": "/v1"})
        if port:
            services.append({"port": int(port), "app": "OpenAI Images API (Qwen-Image)", "path": "/v1"})
        if comfy_port:
            services.append({"port": int(comfy_port), "app": "ComfyUI (Workflow UI)", "path": "/"})
    elif path.name == "start-comfyui-qwen-image.sh":
        if port:
            services.append({"port": int(port), "app": "OpenAI Images API (Qwen-Image)", "path": "/v1/images/generations"})
        if comfy_port:
            services.append({"port": int(comfy_port), "app": "ComfyUI (Web UI)", "path": "/"})
    elif path.name == "start-photo-restore-pipeline.sh":
        if port:
            services.append({"port": int(port), "app": "Photo Restore (Web UI + API)", "path": "/"})
    elif path.name == "start-whisper.sh":
        if port:
            services.append({"port": int(port), "app": "Whisper large-v3 (Web UI + API)", "path": "/"})
    elif path.name.startswith("start-llama-"):
        p = int(port) if port and port.isdigit() else 8080
        lbl = model_file or path.stem.replace("start-llama-", "")
        services.append({"port": p, "app": f"llama.cpp ({lbl}) – UI & OpenAI API", "path": "/"})
    else:
        # Generic detection from comments like "# 8099 - opis" or PORT variable
        for m_c in re.finditer(r"^#\s*(\d{2,5})\s*-\s*(.+)$", text, re.M):
            services.append({"port": int(m_c.group(1)), "app": m_c.group(2).strip(), "path": "/"})
        if not services and port and port.isdigit():
            services.append({"port": int(port), "app": container or path.stem, "path": "/"})

    # Detect presets/arguments for scripts that take $1
    presets = []
    if path.name == "start-llama-qwen3.sh":
        presets = [
            {"label": "Q3_K_M (kontekst 128K)", "args": ["3"], "default": True},
            {"label": "Q4_K_M (kontekst 95K)", "args": ["4"], "default": False},
        ]
    elif path.name == "start-llama-qwen35-vision.sh":
        presets = [
            {"label": "Q3_K_M + Vision (domyślny)", "args": ["3"], "default": True},
            {"label": "Q4_K_M + Vision", "args": ["4"], "default": False},
        ]
    elif path.name == "start-whisper.sh":
        presets = [
            {"label": "Standardowy start", "args": [], "default": True},
            {"label": "Przebuduj obraz (--rebuild)", "args": ["--rebuild"], "default": False},
        ]
    elif "$1" in text:
        # Generic fallback if a new script checks $1
        m_opts = re.findall(r'\"\$1\"\s*==?\s*\"([^\"]+)\"', text)
        seen = []
        for opt in m_opts:
            if opt not in seen:
                seen.append(opt)
                presets.append({"label": f"Parametr: {opt}", "args": [opt], "default": len(presets) == 0})

    return {
        "name": path.name,
        "mtime": int(path.stat().st_mtime),
        "size": path.stat().st_size,
        "description": " ".join(comments[:3]) if comments else "",
        "container": container,
        "model": model_file,
        "port": int(port) if port and port.isdigit() else None,
        "services": services,
        "ctx_size": ctx_size,
        "presets": presets,
    }


def list_executable_scripts() -> list[dict]:
    scripts = []
    if not SCRIPTS_DIR.exists():
        return scripts
    for entry in sorted(SCRIPTS_DIR.iterdir(), key=lambda p: p.name):
        if entry.is_file() and entry.suffix == ".sh" and os.access(entry, os.X_OK):
            scripts.append(parse_script_metadata(entry))
    return scripts


def describe_active_port(host_port: int, container_name: str, image: str, command: str) -> dict:
    """Identify what application and model are served on a given active Docker host port."""
    model_match = re.search(r"-m\s+/models/([^\s\"']+)", command)
    model_name = model_match.group(1) if model_match else None

    if "open-webui" in image or "open-webui" in container_name or host_port == 3000:
        return {"port": host_port, "app": "OpenWebUI (Interfejs czatu)", "kind": "Web UI", "api": None}
    if "whisper" in image or host_port in (3002, 8300):
        return {"port": host_port, "app": "Whisper large-v3 (Transkrypcja audio/wideo)", "kind": "Web UI + API", "api": f"/api/status"}
    if "photo-restore" in image or host_port == 8097:
        return {"port": host_port, "app": "Photo Restore Pipeline (Renowacja zdjęć)", "kind": "Web UI + API", "api": "/restore"}
    if host_port == 8188:
        return {"port": host_port, "app": "ComfyUI (Interfejs graficzny Workflow)", "kind": "Web UI", "api": None}
    if host_port == 8099 or "comfyui" in image:
        return {"port": host_port, "app": "Qwen-Image 2.1 (OpenAI Images API)", "kind": "OpenAI API", "api": "/v1/images/generations"}
    if "llama.cpp" in image or container_name.startswith("llama-"):
        mode = "CPU" if ("--device none" in command or container_name == "llama-cpu") else "GPU"
        vision = " + Vision" if "--mmproj" in command else ""
        label = f"llama.cpp {mode}{vision}"
        if model_name:
            label += f" — {model_name}"
        return {"port": host_port, "app": label, "kind": "Web UI + OpenAI API", "api": "/v1/chat/completions"}
    return {"port": host_port, "app": f"{container_name} ({image})", "kind": "HTTP", "api": None}


def get_running_containers() -> list[dict]:
    cmd = docker_cmd() + [
        "ps",
        "-a",
        "--no-trunc",
        "--format",
        "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.State}}\t{{.Ports}}\t{{.Command}}",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
        if res.returncode != 0:
            return []
        containers = []
        for line in res.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                name, image, status, state = parts[0], parts[1], parts[2], parts[3]
                ports = parts[4] if len(parts) > 4 else ""
                command = parts[5].strip('"') if len(parts) > 5 else ""

                host_ports = []
                for m in re.finditer(r"(?:0\.0\.0\.0|\[::\]):(\d+)->(\d+)/tcp", ports):
                    hp = int(m.group(1))
                    if hp not in host_ports:
                        host_ports.append(hp)

                endpoints = [describe_active_port(hp, name, image, command) for hp in host_ports]

                containers.append(
                    {
                        "name": name,
                        "image": image,
                        "status": status,
                        "state": state,
                        "ports": ports,
                        "endpoints": endpoints,
                        "managed": bool(MANAGED_CONTAINER_REGEX.match(name)),
                    }
                )
        return containers
    except Exception:
        return []


_CPU_PREV = [0, 0]  # [total, idle]


def _read_cpu_times() -> tuple[int, int]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle
    except Exception:
        return 0, 0


_CPU_PREV[0], _CPU_PREV[1] = _read_cpu_times()


def get_host_metrics() -> dict:
    # CPU %
    total, idle = _read_cpu_times()
    dt = total - _CPU_PREV[0]
    di = idle - _CPU_PREV[1]
    _CPU_PREV[0], _CPU_PREV[1] = total, idle
    cpu_pct = round(max(0.0, min(100.0, (1.0 - di / dt) * 100.0))) if dt > 0 else 0

    # RAM
    ram_total_gb = 0.0
    ram_used_gb = 0.0
    ram_pct = 0
    try:
        mem = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.strip().split()[0])
        tot_kb = mem.get("MemTotal", 0)
        avail_kb = mem.get("MemAvailable", 0)
        used_kb = max(0, tot_kb - avail_kb)
        if tot_kb > 0:
            ram_total_gb = round(tot_kb / (1024 * 1024), 1)
            ram_used_gb = round(used_kb / (1024 * 1024), 1)
            ram_pct = round((used_kb / tot_kb) * 100)
    except Exception:
        pass

    # Disk
    disk_total_gb = 0.0
    disk_used_gb = 0.0
    disk_free_gb = 0.0
    disk_pct = 0
    try:
        du = shutil.disk_usage(SCRIPTS_DIR)
        disk_total_gb = round(du.total / (1024**3), 1)
        disk_used_gb = round(du.used / (1024**3), 1)
        disk_free_gb = round(du.free / (1024**3), 1)
        disk_pct = round((du.used / du.total) * 100) if du.total else 0
    except Exception:
        pass

    return {
        "cpu_pct": cpu_pct,
        "cpu_threads": os.cpu_count() or 1,
        "ram_used_gb": ram_used_gb,
        "ram_total_gb": ram_total_gb,
        "ram_pct": ram_pct,
        "disk_used_gb": disk_used_gb,
        "disk_free_gb": disk_free_gb,
        "disk_total_gb": disk_total_gb,
        "disk_pct": disk_pct,
    }


def get_gpu_status() -> dict | None:
    try:
        res = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        line = res.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            return {
                "name": parts[0],
                "mem_used_mb": int(parts[1]),
                "mem_total_mb": int(parts[2]),
                "util_pct": int(parts[3]),
                "temp_c": int(parts[4]),
            }
    except Exception:
        pass
    return None


def cleanup_managed_containers(log_fn):
    """Stop and remove existing GPU/LLM containers so port 8080 and VRAM are freed before launching."""
    dcmd = docker_cmd()
    res = subprocess.run(
        dcmd + ["ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if res.returncode != 0:
        return
    names = [n.strip() for n in res.stdout.splitlines() if n.strip()]
    to_remove = [n for n in names if MANAGED_CONTAINER_REGEX.match(n)]
    if to_remove:
        log_fn(f"[pre-clean] Zatrzymywanie i usuwanie poprzednich kontenerów: {', '.join(to_remove)}\n")
        subprocess.run(dcmd + ["rm", "-f"] + to_remove, capture_output=True, text=True, timeout=30)


def build_exec_env() -> dict:
    """Prepare environment variables so scripts work whether service runs as root or user."""
    env = os.environ.copy()
    env["HOME"] = str(SCRIPTS_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    # Prepend our bin shim directory so `sudo docker` works seamlessly even without a TTY
    shim_dir = str(APP_DIR / "bin")
    env["PATH"] = f"{shim_dir}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return env


def run_script_worker(script_name: str, args: list[str], pre_clean: bool):
    script_path = (SCRIPTS_DIR / script_name).resolve()

    def append_log(text: str):
        with JOB.lock:
            JOB.output_lines.append(text)

    try:
        append_log(f"=== Uruchamianie: ./{script_name} {' '.join(args)} ===\n")
        if pre_clean:
            cleanup_managed_containers(append_log)

        proc = subprocess.Popen(
            [str(script_path)] + args,
            cwd=str(SCRIPTS_DIR),
            env=build_exec_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        with JOB.lock:
            JOB.proc = proc

        assert proc.stdout is not None
        for line in proc.stdout:
            append_log(line)

        proc.wait()
        with JOB.lock:
            JOB.returncode = proc.returncode
            JOB.finished_at = time.time()
            JOB.running = False
            JOB.proc = None
        append_log(f"\n=== Zakończono z kodem: {proc.returncode} ===\n")
        if proc.returncode == 0:
            ip = get_lan_ip()
            running = [c for c in get_running_containers() if c.get("state") == "running"]
            if running:
                append_log("\n--- Aktywne aplikacje i porty ---\n")
                for c in running:
                    for ep in c.get("endpoints", []):
                        append_log(f" • Port :{ep['port']} -> {ep['app']}  [ http://{ip}:{ep['port']} ]\n")
    except Exception as e:
        with JOB.lock:
            JOB.returncode = -1
            JOB.finished_at = time.time()
            JOB.running = False
            JOB.proc = None
        append_log(f"\n[BŁĄD] {e}\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data: dict, status: int = 200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            html_path = APP_DIR / "index.html"
            raw = html_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if parsed.path == "/api/status":
            self._send_json(
                {
                    "ip": get_lan_ip(),
                    "scripts_dir": str(SCRIPTS_DIR),
                    "scripts": list_executable_scripts(),
                    "containers": get_running_containers(),
                    "gpu": get_gpu_status(),
                    "host": get_host_metrics(),
                    "job": JOB.to_dict(),
                }
            )
            return

        if parsed.path == "/api/logs":
            qs = parse_qs(parsed.query)
            container = qs.get("container", [""])[0]
            if not container or not re.match(r"^[a-zA-Z0-9_.-]+$", container):
                self._send_json({"error": "Nieprawidłowa nazwa kontenera"}, 400)
                return
            try:
                res = subprocess.run(
                    docker_cmd() + ["logs", "--tail", "80", container],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                out = (res.stdout or "") + (res.stderr or "")
                self._send_json({"container": container, "logs": out})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {}

        if parsed.path == "/api/run":
            script_name = str(payload.get("script", "")).strip()
            args = [str(a) for a in payload.get("args", []) if str(a).strip()]
            pre_clean = bool(payload.get("pre_clean", True))

            if not script_name or "/" in script_name or "\\" in script_name or not script_name.endswith(".sh"):
                self._send_json({"error": "Nieprawidłowa nazwa skryptu"}, 400)
                return

            script_path = (SCRIPTS_DIR / script_name).resolve()
            if script_path.parent != SCRIPTS_DIR or not script_path.is_file() or not os.access(script_path, os.X_OK):
                self._send_json({"error": "Skrypt nie istnieje lub nie ma uprawnień +x"}, 404)
                return

            with JOB.lock:
                if JOB.running:
                    self._send_json({"error": f"Trwa już wykonywanie skryptu {JOB.script}"}, 409)
                    return
                JOB.running = True
                JOB.script = script_name
                JOB.args = args
                JOB.started_at = time.time()
                JOB.finished_at = None
                JOB.returncode = None
                JOB.output_lines = []

            t = threading.Thread(
                target=run_script_worker,
                args=(script_name, args, pre_clean),
                daemon=True,
            )
            t.start()
            self._send_json({"ok": True, "job": JOB.to_dict()})
            return

        if parsed.path == "/api/stop-all":
            with JOB.lock:
                if JOB.running and JOB.proc is not None:
                    try:
                        os.killpg(os.getpgid(JOB.proc.pid), signal.SIGTERM)
                    except Exception:
                        pass
                JOB.output_lines.append("\n=== Zatrzymywanie kontenerów na żądanie użytkownika ===\n")
            cleanup_managed_containers(lambda msg: JOB.output_lines.append(msg))
            self._send_json({"ok": True})
            return

        self._send_json({"error": "Not found"}, 404)


def main():
    print(f"LLM Switcher Web UI: http://{get_lan_ip()}:{PORT} (katalog skryptów: {SCRIPTS_DIR})")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
