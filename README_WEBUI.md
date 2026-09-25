# WeBan WebUI

WeBan WebUI is an optional local web interface for the WeBan runtime. It
provides an Apple-inspired light interface with Chinese and English language
switching.

## Features

- Start and stop the real `main.py` process from the dashboard.
- Stream task output through a WebSocket log panel.
- Add, list, and delete accounts without changing unrelated TOML sections.
- Load the official school list and filter it by typing a few characters.
- Select the complete school name from autocomplete suggestions.
- Edit the study, exam, concurrency, timing, answer, and video settings.
- Use the interface on desktop or mobile layouts.

## Requirements

The WeBan runtime targets Python 3.12:

```text
Python >= 3.12, < 3.13
```

Install the runtime and WebUI dependencies with:

```powershell
py -3.12 -m pip install -r requirements-webui.txt
```

Alternatively, use the optional dependency group:

```powershell
uv sync --extra webui
```

Verify the important packages:

```powershell
py -3.12 -c "import fastapi, uvicorn, tomli_w, requests, pyaes, nodriver, cv2; print('dependencies ok')"
```

## Start

On Windows, double-click `启动WebUI.bat` or run:

```powershell
py -3.12 webui.py
```

Open `http://127.0.0.1:8080`.

The WebUI and `main.py` must remain in the same directory. The task
controller starts the local `main.py` with `--data-dir` and
`--non-interactive`, so the WebUI and runtime use the same configuration and
log directory.

## Account Configuration

The account form writes the format expected by WeBan:

```toml
[[account]]
tenant_name = "The complete school name shown by WeBan"
username = "Your student number"
password = "Your password"
```

The school name is required. Type part of the name, then select the complete
name returned by the official WeBan school list. The runtime uses the exact
tenant name to find the school code. Multiple accounts, including accounts
from different schools, remain separate entries and are processed by the
original runtime.

The existing `config.toml` is not overwritten during startup. Saving an
account or setting uses an atomic temporary file and keeps unrelated TOML
sections. Passwords and tokens remain in the local configuration file and are
never returned by the account API.

## Custom Data Directory

Set `WB_DATA_DIR` when configuration and logs should live outside the source
directory:

```powershell
$env:WB_DATA_DIR = "D:\WeBanData"
py -3.12 webui.py
```

The WebUI still starts the local `main.py`; only `config.toml` and `logs` move
to the selected data directory.

## Host and Port

The defaults are localhost and port 8080. Override them with:

```powershell
$env:WEBUI_HOST = "127.0.0.1"
$env:WEBUI_PORT = "8081"
py -3.12 webui.py
```

The default localhost binding is intentional because passwords are stored in
`config.toml` as plain text and the WebUI has no built-in authentication.

## API

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Serve the WebUI |
| `/api/task/status` | GET | Read task status |
| `/api/task/start` | POST | Start the real `main.py` |
| `/api/task/stop` | POST | Stop the task process |
| `/api/accounts` | GET | List configured accounts without secrets |
| `/api/accounts` | POST | Add an account |
| `/api/accounts/{username}` | DELETE | Delete an account |
| `/api/schools` | GET | Load the official autocomplete list |
| `/api/settings` | GET | Read global settings |
| `/api/settings` | POST | Validate and save editable settings |
| `/api/logs` | GET | List historical log files |
| `/ws/logs` | WebSocket | Stream task output |

## Troubleshooting

### The WebUI does not start

Use Python 3.12 explicitly. Python 3.14 is not supported by the project
metadata and may fail when importing `nodriver`.

```powershell
py -3.12 webui.py
```

### The page looks unchanged

Refresh `http://127.0.0.1:8080/` after restarting the server. A running
Python process does not reload `webui.py` automatically.

### The school list is unavailable

The WebUI uses the official school endpoint and falls back to school names
already present in the local configuration if the endpoint is temporarily
unavailable.

### The task refuses to start

Save at least one account with a complete school name, username, and password
or a supported token account. The WebUI refuses to start `main.py` when no
valid account is configured.

## Development Checks

```powershell
py -3.12 -m compileall -q webui.py
py -3.12 main.py --help
git diff --check
```
