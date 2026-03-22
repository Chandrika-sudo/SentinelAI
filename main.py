import os
import queue
import threading
import logging
import time
from datetime import datetime

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from video_processor import VideoProcessor

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── App setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", template_folder="templates")

NUM_CAMERAS = 4
QUEUE_SIZE   = 10
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")

# ── Shared state ───────────────────────────────────────────────────────────────
frame_queues: dict[int, queue.Queue] = {
    i: queue.Queue(maxsize=QUEUE_SIZE) for i in range(1, NUM_CAMERAS + 1)
}

video_processors: dict[int, VideoProcessor] = {}

_default_status = lambda: {"type": "offline", "url": None, "count": 0, "fall": False, "theft": False, "fire": False}
camera_status: dict[int, dict] = {i: _default_status() for i in range(1, NUM_CAMERAS + 1)}

status_lock = threading.Lock()

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_processor(camera_id: int) -> VideoProcessor:
    """Return (and lazily create) the VideoProcessor for a given camera."""
    if camera_id not in video_processors:
        video_processors[camera_id] = VideoProcessor(camera_id, frame_queues[camera_id])
    return video_processors[camera_id]


def _blank_frame(camera_id: int) -> bytes:
    """Return a JPEG-encoded placeholder frame."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:] = (18, 20, 26)
    cv2.putText(
        frame, f"Camera {camera_id}  —  No Feed",
        (80, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (80, 80, 90), 2,
    )
    _, buf = cv2.imencode(".jpg", frame)
    return buf.tobytes()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/status")
def get_status():
    with status_lock:
        # Merge live detection flags from processors
        merged = {}
        for cam_id, info in camera_status.items():
            proc = video_processors.get(cam_id)
            live = dict(info)
            if proc:
                live["fall"]  = proc.fall_detected
                live["theft"] = proc.theft_detected
                live["fire"]  = proc.fire_detected
                live["count"] = proc.person_count
            merged[cam_id] = live
        return jsonify(merged)


@app.route("/set_ip", methods=["POST"])
def set_ip():
    data      = request.get_json(force=True)
    camera_id = int(data.get("camera_id", 0))
    ip        = (data.get("ip") or "").strip()

    if camera_id not in range(1, NUM_CAMERAS + 1):
        return jsonify({"error": "Invalid camera_id"}), 400
    if not ip:
        return jsonify({"error": "IP address required"}), 400

    with status_lock:
        camera_status[camera_id].update({"type": "ip_camera", "url": ip, "count": 0,
                                          "fall": False, "theft": False, "fire": False,
                                          "timestamp": datetime.now().isoformat()})

    get_processor(camera_id).start_ip_camera(ip)
    logger.info("Camera %d → IP camera %s", camera_id, ip)
    return jsonify({"success": True, "message": f"IP camera set for Camera {camera_id}"})


@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file      = request.files["file"]
    camera_id = int(request.form.get("camera_id", 1))

    if camera_id not in range(1, NUM_CAMERAS + 1):
        return jsonify({"error": "Invalid camera_id"}), 400
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = f"cam{camera_id}_{timestamp}_{file.filename}"
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)

    with status_lock:
        camera_status[camera_id].update({"type": "video_file", "url": filename, "count": 0,
                                          "fall": False, "theft": False, "fire": False,
                                          "timestamp": datetime.now().isoformat()})

    get_processor(camera_id).start_video_file(file_path)
    logger.info("Camera %d → video file %s", camera_id, filename)
    return jsonify({"success": True, "filename": filename, "camera_id": camera_id})


@app.route("/close_camera/<int:camera_id>", methods=["POST"])
def close_camera(camera_id: int):
    if camera_id not in range(1, NUM_CAMERAS + 1):
        return jsonify({"error": "Invalid camera_id"}), 400

    if camera_id in video_processors:
        video_processors[camera_id].stop()
        del video_processors[camera_id]

    with status_lock:
        camera_status[camera_id] = _default_status()

    logger.info("Camera %d closed", camera_id)
    return jsonify({"success": True})


@app.route("/reset_alert/<int:camera_id>", methods=["POST"])
def reset_alert(camera_id: int):
    """Clear fall / theft flags for a camera."""
    if camera_id not in range(1, NUM_CAMERAS + 1):
        return jsonify({"error": "Invalid camera_id"}), 400

    proc = video_processors.get(camera_id)
    if proc:
        proc.reset_alerts()

    with status_lock:
        camera_status[camera_id]["fall"]  = False
        camera_status[camera_id]["theft"] = False
        camera_status[camera_id]["fire"]  = False

    logger.info("Alerts reset for Camera %d", camera_id)
    return jsonify({"success": True})


@app.route("/feed/<int:camera_id>")
def video_feed(camera_id: int):
    if camera_id not in range(1, NUM_CAMERAS + 1):
        return "Invalid camera", 404

    def generate():
        while True:
            try:
                frame = frame_queues[camera_id].get(timeout=1.0)
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")
                time.sleep(0.033)
            except queue.Empty:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + _blank_frame(camera_id) + b"\r\n")
                time.sleep(1)
            except Exception as e:
                logger.error("Feed error camera %d: %s", camera_id, e)
                break

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    host  = os.environ.get("HOST", "0.0.0.0")
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"

    logger.info("Starting Fall Detection System on http://%s:%d  (debug=%s)", host, port, debug)
    app.run(host=host, port=port, debug=debug)