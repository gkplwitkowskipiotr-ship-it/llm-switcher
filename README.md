# LLM Switcher

A lightweight web panel for switching between local LLM / GPU workloads (llama.cpp, ComfyUI, Whisper, …) running in Docker on a single machine.

It scans a directory for executable `*.sh` start scripts, shows them as cards in a browser UI, and lets you launch / reload them with one click. It also monitors hardware resources and running containers. Before a new workload starts, the previous GPU containers can be stopped automatically so that VRAM and ports are freed.

> **Note:** The UI is available in **English and Polish** (PL/EN switch in the header). The code, comments and documentation are in English.

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
├── llm-switcher.service   # systemd unit template (filled in by install-service.sh)
└── install-service.sh     # Renders the unit for the current user, installs and starts it
```

## Quick start

```bash
python3 server.py
# open http://<host-ip>:3001
```

### Configuration (environment variables)

| Variable      | Default                         | Description                               |
|---------------|---------------------------------|-------------------------------------------|
| `PORT`        | `3001`                          | HTTP port                                 |
| `HOST`        | `0.0.0.0`                       | Bind address                              |
| `SCRIPTS_DIR` | home directory of the user running the server | Directory scanned for executable `*.sh` |

The home directory is read from the system user database (`/etc/passwd`) for the user the process runs as, not from `$HOME`. Scripts are started with `HOME` set to that directory.

### Install as a systemd service

Run as a regular user (the script calls `sudo` itself):

```bash
./install-service.sh                       # port 3001, scripts from your home directory
./install-service.sh 3005                  # custom port
./install-service.sh 3001 /opt/llm-scripts # custom port and scripts directory
```

`llm-switcher.service` is a template. The installer fills in the current user, the user's home directory, the scripts directory, the project location and the `python3` path, then writes the result to `/etc/systemd/system/llm-switcher.service`. The project can be cloned anywhere.

Logs: `journalctl -u llm-switcher -f`

## Writing start scripts

Any executable `*.sh` file in `SCRIPTS_DIR` is picked up automatically. The server extracts metadata from the script:

- **Description**: the first comment block after the shebang.
- **Variables**: `CONTAINER_NAME`, `MODEL_FILE`, `PORT`, `CTX_SIZE` (also `COMFY_PORT`, `TEXT_PORT`, `WEBUI_PORT`).
- **Served ports**: comments in the form `# 8099 - Description of the service`.
- **Argument presets**: detected from `"$1" == "value"` comparisons.

Scripts run with `SCRIPTS_DIR` as the working directory. `bin/` is prepended to `PATH`, so `sudo docker …` works without a TTY.

## Translations (i18n)

The UI supports English (`en`) and Polish (`pl`).

- **Language selection**: the PL/EN switch in the header. The choice is saved in `localStorage`. On the first visit the browser language is used (`pl*` → Polish, anything else → English).
- **Frontend strings** are in the `I18N` dictionary in `index.html`. Static elements are marked with `data-i18n` / `data-i18n-title`.
- **Server strings** (app/port labels, presets, job log lines, API errors) are in the `MESSAGES` dictionary in `server.py`. The server picks the language from `?lang=` (GET) or the `"lang"` field of the JSON body (POST). It falls back to English.
- **Adding a language**: add a new key (e.g. `de`) to both `I18N` and `MESSAGES`, and add a button `<button data-lang="de">DE</button>` to the `.lang-switch` group.

> **Note:** Job log lines are written once, in the language of the request that started the job. Switching the language does not re-translate an existing log. Script descriptions come from the scripts' own comments and are not translated.

## REST API

| Method | Endpoint                    | Description                                                          |
|--------|-----------------------------|----------------------------------------------------------------------|
| GET    | `/api/status?lang=en`       | Scripts, containers, GPU and host metrics, current job and its log   |
| GET    | `/api/logs?container=NAME`  | Last 80 lines of a container's log                                   |
| POST   | `/api/run`                  | Body: `{"script": "x.sh", "args": ["3"], "pre_clean": true, "lang": "en"}` |
| POST   | `/api/stop-all`             | Body (optional): `{"lang": "en"}`. Stops the running job and removes managed containers |

## Security

> **Warning:** The panel has **no authentication** and listens on all interfaces by default. Anyone who can reach the port can run any executable script in `SCRIPTS_DIR`. Because the service runs in the `docker` group, that is effectively root access.

Use it only on a trusted network. Alternatively, set `HOST=127.0.0.1` and access the panel through an SSH tunnel or an authenticating reverse proxy.
