# LLM Switcher

A lightweight web panel for switching between local LLM / GPU workloads (llama.cpp, ComfyUI, Whisper, …) running in Docker on a single machine.

It scans a directory for executable `*.sh` start scripts, shows them as cards in a browser UI, and lets you launch / reload them with one click. It also monitors hardware resources and running containers. Before a new workload starts, the previous GPU containers can be stopped automatically so that VRAM and ports are freed.

> **Note:** The UI is in Polish. The code, comments and documentation are in English.

## Features

- **Script launcher**: lists every executable `*.sh` in `SCRIPTS_DIR` with description, model, context size, served ports and argument presets parsed from the script.
- **Auto GPU cleanup**: optionally stops and removes previously managed containers (`llama-*`, `comfyui`, `photo-restore`) before starting a new script.
- **Live job log**: streams the stdout/stderr of the running script.
- **Hardware monitor**: GPU (VRAM, GPU utilization, temperature via `nvidia-smi`), CPU load, RAM and disk usage.
- **Docker overview**: running containers with clickable endpoints, a description of the app on each port, and container logs.
- **Zero dependencies**: a single Python file using only the standard library, plus one static HTML page.

## Requirements

- Linux with **Python ≥ 3.10**
- **Docker**. The service user must be in the `docker` group (or have password-less `sudo docker`)
- **NVIDIA driver** with `nvidia-smi` (optional; without it the GPU box shows "no data")
- `systemd` (optional, for running as a service)

## Project layout

```
llm-switcher/
├── server.py              # HTTP server + REST API (stdlib only)
├── index.html             # Single-page web UI (vanilla JS, no build step)
├── bin/sudo               # Shim: turns `sudo docker …` into plain `docker …` when the socket is accessible
├── llm-switcher.service   # systemd unit
└── install-service.sh     # Installs and starts the systemd unit
```

## Quick start

```bash
python3 server.py
# open http://<host-ip>:3001
```

### Configuration (environment variables)

| Variable      | Default      | Description                               |
|---------------|--------------|-------------------------------------------|
| `PORT`        | `3001`       | HTTP port                                 |
| `HOST`        | `0.0.0.0`    | Bind address                              |
| `SCRIPTS_DIR` | `/home/pwit` | Directory scanned for executable `*.sh`   |

### Install as a systemd service

```bash
./install-service.sh          # default port 3001
./install-service.sh 3005     # custom port
```

Logs: `journalctl -u llm-switcher -f`

## Writing start scripts

Any executable `*.sh` file in `SCRIPTS_DIR` is picked up automatically. The server extracts metadata from the script:

- **Description**: the first comment block after the shebang.
- **Variables**: `CONTAINER_NAME`, `MODEL_FILE`, `PORT`, `CTX_SIZE` (also `COMFY_PORT`, `TEXT_PORT`, `WEBUI_PORT`).
- **Served ports**: comments in the form `# 8099 - Description of the service`.
- **Argument presets**: detected from `"$1" == "value"` comparisons.

Scripts run with `SCRIPTS_DIR` as the working directory. `bin/` is prepended to `PATH`, so `sudo docker …` works without a TTY.

## REST API

| Method | Endpoint                    | Description                                                          |
|--------|-----------------------------|----------------------------------------------------------------------|
| GET    | `/api/status`               | Scripts, containers, GPU and host metrics, current job and its log   |
| GET    | `/api/logs?container=NAME`  | Last 80 lines of a container's log                                   |
| POST   | `/api/run`                  | Body: `{"script": "x.sh", "args": ["3"], "pre_clean": true}`          |
| POST   | `/api/stop-all`             | Stops the running job and removes managed containers                 |

## Security

> **Warning:** The panel has **no authentication** and listens on all interfaces by default. Anyone who can reach the port can run any executable script in `SCRIPTS_DIR`. Because the service runs in the `docker` group, that is effectively root access.

Use it only on a trusted network. Alternatively, set `HOST=127.0.0.1` and access the panel through an SSH tunnel or an authenticating reverse proxy.
