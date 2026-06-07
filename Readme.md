# SentinelAI — Real-time AI Safety Monitoring System

> Multi-camera safety monitoring powered by **YOLOv8** and **Flask**.  
> Detects falls, theft, and fire in real time across up to 4 simultaneous camera feeds.

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.0-black?logo=flask)
![YOLOv8](https://img.shields.io/badge/YOLOv8-ultralytics-orange)
![OpenCV](https://img.shields.io/badge/OpenCV-4.10-green?logo=opencv)
![License](https://img.shields.io/badge/license-MIT-brightgreen)

---

## Features

| Module | How it works |
|---|---|
| **Fall detection** | Sliding-window bounding-box aspect ratio over 8 frames — 5 must be positive to confirm |
| **Theft detection** | Three scenarios: abandoned object, pickup by non-owner (ownership tracking), restricted-zone access |
| **Fire detection** | 5-stage HSV pipeline: colour masking → morphological cleanup → contour area → brightness check → 20-frame temporal window |
| **Multi-camera** | Up to 4 simultaneous MJPEG streams — MP4 uploads or RTSP/HTTP IP cameras |
| **Live dashboard** | Dark tactical UI with per-camera overlays, alert badges, event log, dismiss controls |
| **Shared model** | YOLOv8 loaded once, shared across all camera threads — saves ~400 MB RAM |

---

## Screenshots

> Upload an MP4 to any camera slot and see bounding boxes, ownership arrows, alert banners and the event log update in real time.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.10+, Flask 3, OpenCV 4, Ultralytics YOLOv8 |
| Frontend | Vanilla JS (no frameworks), CSS custom properties |
| Fonts | Share Tech Mono + DM Sans |
| Streaming | MJPEG via `multipart/x-mixed-replace` |
| Concurrency | `threading.Thread` per camera, `threading.Lock` for shared state |

---

## Quick Start

### Windows

```powershell
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/sentinelai.git
cd sentinelai

# 2. Create a fresh virtual environment
python -m venv venv
venv\Scripts\Activate.ps1

# If you get an execution policy error, run this once first:
# Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run
python main.py
```

### macOS / Linux

```bash
git clone https://github.com/YOUR_USERNAME/sentinelai.git
cd sentinelai
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

Then open **http://localhost:5000** in your browser.

---

## Project Structure

```
sentinelai/
├── main.py                 # Flask app — routes, MJPEG streaming, status API
├── video_processor.py      # YOLOv8 inference + fall / theft / fire detection
├── requirements.txt        # Pinned dependencies
├── .gitignore
├── templates/
│   └── index.html          # Dashboard UI (Jinja2 template)
├── static/
│   ├── css/style.css       # Dark monitoring theme
│   └── js/script.js        # Vanilla JS — polling, alerts, upload, modals
└── uploads/                # Auto-created at runtime; gitignored
```

---

## How Detection Works

### Fall Detection

Each person bounding box has a width and height. A standing person is taller than wide. When they fall, the box flips to landscape (`width > height × 1.25`). A **sliding window of 8 frames** requires at least 5 to be positive before the alert fires, preventing false triggers from people sitting down or bending over.

### Theft Detection

Three independent scenarios run every frame using lightweight centroid tracking:

**Scenario A — Abandoned object**  
A person stays near an object for 30 consecutive frames → they become the owner. If the owner's track disappears while the object stays visible for more than 5 seconds, an alert fires.

**Scenario B — Pickup by non-owner**  
If the object moves more than 60 pixels from its first known position AND a different person is now closest to it (not the registered owner), a theft is flagged.

**Scenario C — Restricted zone access**  
You can define polygon zones in pixel coordinates. Any person inside a zone who is within 120 pixels of a tracked object triggers an alert. Zones are drawn as semi-transparent orange overlays on the video feed.

### Fire Detection

A 5-stage pipeline designed to eliminate false positives from red clothing, warm lighting, and skin tone:

1. **HSV masking** — matches orange-red flame hues (H: 0–30) with moderate saturation (S ≥ 50) and brightness (V ≥ 80)
2. **Morphological cleanup** — 7×7 open+close removes scattered noise pixels
3. **Largest contour filter** — only the biggest connected blob counts; must be ≥ 3000 px²
4. **Brightness check** — mean V-channel inside the mask must be ≥ 120 (fire glows, shadows don't)
5. **Temporal window** — 14 of the last 20 frames must pass stages 1–4 before alert fires

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `5000` | Listen port |
| `DEBUG` | `false` | Flask debug mode — never set `true` in production |
| `UPLOAD_FOLDER` | `uploads` | Directory for saved video files |

Create a `.env` file in the project root (gitignored):

```env
HOST=0.0.0.0
PORT=5000
DEBUG=false
UPLOAD_FOLDER=uploads
```

---

## API Reference

| Method | Endpoint | Body | Description |
|---|---|---|---|
| `GET` | `/` | — | Serve dashboard UI |
| `GET` | `/status` | — | JSON status for all 4 cameras |
| `GET` | `/feed/<id>` | — | MJPEG stream for camera 1–4 |
| `POST` | `/upload` | `multipart/form-data` — `file`, `camera_id` | Upload and stream a video file |
| `POST` | `/set_ip` | `{ "camera_id": 1, "ip": "rtsp://..." }` | Connect an IP/RTSP camera |
| `POST` | `/close_camera/<id>` | — | Stop and disconnect a camera |
| `POST` | `/reset_alert/<id>` | — | Clear all alert flags for a camera |

### Example `/status` response

```json
{
  "1": { "type": "video_file", "count": 2, "fall": false, "theft": false, "fire": false },
  "2": { "type": "offline",    "count": 0, "fall": false, "theft": false, "fire": false },
  "3": { "type": "ip_camera",  "count": 1, "fall": true,  "theft": false, "fire": false },
  "4": { "type": "offline",    "count": 0, "fall": false, "theft": false, "fire": false }
}
```

---

## Deployment

### Render / Railway

Set the start command to:

```bash
gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

> **Important:** Use exactly **1 worker**. Multiple workers each load YOLOv8 into memory separately (~400 MB each) and cannot share the frame queues between processes.

### Environment on the platform

Set `HOST`, `PORT`, `DEBUG=false`, and `UPLOAD_FOLDER` in the platform's environment variable dashboard.

---

## Roadmap

- [ ] WebSocket push alerts (replace 2s polling)
- [ ] Pose estimation (MediaPipe) for improved fall accuracy
- [ ] SQLite alert history with export
- [ ] Email notifications on alert
- [ ] Configurable restricted zones via UI (click to draw polygons)
- [ ] Docker containarization setup

---

## License

MIT © 2024 Your Name — feel free to fork and build on this.
