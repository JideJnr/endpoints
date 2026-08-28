# PredictX Backend - Termux Setup Guide

Run the PredictX FastAPI backend on your Android device using Termux.

## Prerequisites

- Android device (7.0+)
- Termux app installed from F-Droid (recommended) or Google Play
- At least 2GB free storage
- Internet connection for initial setup

## Step 1: Install Termux

1. Download Termux from [F-Droid](https://f-droid.org/packages/com.termux/) (recommended) or Google Play Store
2. Open Termux and grant storage permission:
   ```bash
   termux-setup-storage
   ```
3. Update packages first:
   ```bash
   pkg update -y && pkg upgrade -y
   ```

## Step 2: Transfer Project Files

Copy the `predictx` folder to your device. You can use:

**Option A: Git (if you have the repo)**
```bash
cd ~/storage/shared
git clone <your-repo-url>
cd football/predictx
```

**Option B: File transfer app**
- Use a file manager or app like "Send Files to TV" to copy the `predictx` folder to `~/storage/shared/football/`

**Option C: ADB (if you have USB debugging enabled)**
```bash
adb push predictx /sdcard/football/
```

## Step 3: Run Setup Script

```bash
cd ~/storage/shared/football/predictx
bash termux_setup.sh
```

This will:
1. Update Termux packages
2. Install system dependencies (Python, compilers, libraries)
3. Create a Python virtual environment
4. Install all Python dependencies

**Note:** The setup may take 10-30 minutes depending on your device and internet speed. Some packages (numpy, scipy) need to be compiled.

## Step 4: Configure Environment

Edit the `.env` file if needed:

```bash
nano .env
```

Key settings for mobile:
- `PREDICTX_CORS_ORIGINS` - Add your Ionic app's origin (e.g., `capacitor://localhost`)
- `OPENROUTER_API_KEY` - Add your OpenRouter API key for AI features
- `PREDICTX_DB_PATH` - Database path (defaults to `data/predictx_memory.sqlite3`)

## Step 5: Run the Backend

```bash
bash termux_run.sh
```

Or manually:
```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8002
```

## Step 6: Access the API

The backend will be available at:

- **API Docs:** `http://127.0.0.1:8002/docs`
- **Health Check:** `http://127.0.0.1:8002/health`
- **From other devices on same network:** `http://<your-device-ip>:8002/docs`

To find your device's IP:
```bash
ifconfig | grep "inet "
```

## Step 7: Keep it Running

To keep the backend running when Termux is closed:

1. Install `termux:api` app from F-Droid
2. Enable battery optimization exemption for Termux
3. Use a terminal multiplexer like `tmux`:
   ```bash
   pkg install tmux
   tmux new -s predictx
   bash termux_run.sh
   # Press Ctrl+B then D to detach
   ```

## Troubleshooting

### "No space left on device"
- Free up storage or use an SD card
- Move the project to SD card and symlink

### "pip install fails with compilation error"
- Ensure you ran `termux_setup.sh` which installs clang and libopenblas
- Try: `pip install --no-build-isolation <package>`

### "metadata generation failed while generating scipy"
This is a known issue on Termux when building scipy from source. The setup script now handles this automatically, but if you encounter it manually:

1. Set the legacy build flag: `export SCIPY_USE_LEGACY_BUILD=1`
2. Install numpy first: `pip install --no-build-isolation numpy`
3. Then install scipy: `pip install --no-build-isolation scipy`
4. If compilation fails due to missing gfortran, try installing a pre-built wheel or use `--only-binary=:all:` flag
5. If memory is an issue, try: `pip install --no-build-isolation scipy --no-cache-dir`

### "Permission denied" on scripts
```bash
chmod +x termux_setup.sh termux_run.sh
```

### Backend crashes on startup
- Check logs for missing dependencies
- Ensure `data/` directory exists: `mkdir -p data`
- Verify `.env` file is present

### Can't connect from Ionic app
- Ensure both app and backend are on the same network
- Add the app's origin to `PREDICTX_CORS_ORIGINS` in `.env`
- For Capacitor apps, add `capacitor://localhost` to CORS origins

## API Endpoints

Key endpoints for your Ionic app:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/matches/today` | GET | Today's matches |
| `/matches/live` | GET | Live matches |
| `/matches/{id}` | GET | Match details |
| `/predictions/refresh` | POST | Refresh predictions |
| `/ws/live` | WS | Live match updates |

See full API docs at `http://127.0.0.1:8002/docs` when running.
