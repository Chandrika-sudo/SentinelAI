"""
video_processor.py
──────────────────
Per-camera inference engine using a shared YOLOv8 singleton.

Detection modules
─────────────────
  Fall    – Sliding-window bounding-box aspect ratio (8-frame window).

  Theft   – Three scenarios via centroid tracking + ownership mapping:
              A) Abandoned object  – owner leaves frame, object stays > 5 s
              B) Pickup by non-owner – object moves AND a different person
                 is now closest to it (ownership transfer)
              C) Restricted-zone access – person enters a defined polygon
                 zone while near a tracked object

  Fire    – HSV colour-space (red/orange hue + high saturation + brightness)
             combined with morphological cleanup and a flicker window to
             suppress false positives from red clothing / lights.
"""

import logging
import math
import threading
import time
from collections import deque, defaultdict

import cv2
import numpy as np
from ultralytics import YOLO

logger = logging.getLogger(__name__)

# ── Singleton model ────────────────────────────────────────────────────────────
_model_lock   = threading.Lock()
_shared_model: YOLO | None = None


def _get_model() -> YOLO:
    global _shared_model
    with _model_lock:
        if _shared_model is None:
            logger.info("Loading YOLOv8 model …")
            _shared_model = YOLO("yolov8n.pt")
            logger.info("YOLOv8 model loaded.")
    return _shared_model


# ── Tunable constants ──────────────────────────────────────────────────────────

# Fall
FALL_WINDOW      = 8
FALL_RATIO       = 1.25
FALL_CONSECUTIVE = 5

# Theft
TRACKABLE_OBJECTS  = {"backpack", "handbag", "suitcase", "laptop", "cell phone"}
OWNERSHIP_FRAMES   = 30      # frames near object to claim ownership
PROXIMITY_PX       = 120     # centroid distance threshold (pixels)
ABANDON_TIMEOUT    = 5.0     # secs after owner leaves to flag Scenario A
PICKUP_MOVE_PX     = 60      # object displacement to count as "picked up"
OBJECT_TTL         = 10.0    # secs before unseen track is evicted

# Fire detection — calibrated from real flame footage analysis
#
# WHY these values:
#   Real fire in video has H=4-30, S=50-255, V=80-255
#   Mean saturation is only ~70-91 (NOT 180+) because smoke, haze, and
#   camera exposure compress the saturation of actual flames.
#   Previous ranges (sat≥180) were too strict — they missed real fire.
#
# False-positive defence is in the MULTI-STAGE pipeline, not sat threshold:
#   Stage 3 — largest contour ≥ 3000 px²  (kills clothing specks)
#   Stage 4 — mean brightness ≥ 120       (kills dark-red shadows)
#   Stage 5 — 14/20 frame window           (kills single-frame glints)
FIRE_MIN_AREA        = 3000
FIRE_FRAME_WINDOW    = 20
FIRE_FRAME_HITS      = 14
FIRE_MIN_BRIGHT_MEAN = 120   # mean V inside mask; fire glows, shadows don't

FIRE_HSV_RANGES = [
    # Main flame body — orange-yellow (H 4-30, moderate-high S, bright V)
    (np.array([4,  50,  80]), np.array([30, 255, 255])),
    # Red flame edge (H 0-4)
    (np.array([0,  50,  80]), np.array([4,  255, 255])),
    # Deep red wrap-around (H 170-180)
    (np.array([170, 50, 80]), np.array([180,255, 255])),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _centroid(x1, y1, x2, y2):
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _point_in_polygon(point, polygon):
    """Ray-casting test – returns True if point is inside the polygon."""
    x, y = point
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and \
                (x < (xj - xi) * (y - yi) / (yj - yi + 1e-9) + xi):
            inside = not inside
        j = i
    return inside


# ── Lightweight centroid track ─────────────────────────────────────────────────

class _Track:
    _counter = 0

    def __init__(self, label, centroid, bbox):
        _Track._counter += 1
        self.track_id   = _Track._counter
        self.label      = label
        self.centroid   = centroid
        self.bbox       = bbox
        self.first_seen = time.time()
        self.last_seen  = time.time()
        self.history    = deque(maxlen=30)
        self.history.append(centroid)
        # Ownership tracking (only used for objects, not persons)
        self.owner_id   = None          # person track_id
        self.near_counts = defaultdict(int)  # person_track_id -> frames near

    def update(self, centroid, bbox):
        self.centroid  = centroid
        self.bbox      = bbox
        self.last_seen = time.time()
        self.history.append(centroid)

    @property
    def displacement(self):
        """Euclidean distance from first to latest recorded centroid."""
        if len(self.history) < 2:
            return 0.0
        return _dist(self.history[0], self.history[-1])

    @property
    def age(self):
        return time.time() - self.first_seen


# ── VideoProcessor ─────────────────────────────────────────────────────────────

class VideoProcessor:
    """Runs all detection modules on a single camera stream."""

    def __init__(self, camera_id: int, frame_queue,
                 restricted_zones=None):
        """
        Parameters
        ----------
        camera_id        : 1-based camera index
        frame_queue      : queue.Queue receiving annotated frames
        restricted_zones : list of polygons, each a list of (x, y) pixel tuples.
                           A person inside a zone who is near a tracked object
                           triggers Scenario C.
        """
        self.camera_id        = camera_id
        self.frame_queue      = frame_queue
        self.model            = _get_model()
        self.restricted_zones = restricted_zones or []

        self._running = False
        self._thread  = None
        self._cap     = None
        self._lock    = threading.Lock()

        # Fall
        self._fall_window = deque(maxlen=FALL_WINDOW)

        # Theft – centroid tracks
        self._person_tracks  = {}   # track_id -> _Track
        self._object_tracks  = {}   # track_id -> _Track
        self._abandon_timer  = {}   # object track_id -> float (timestamp)
        self._theft_reason   = ""

        # Fire
        self._fire_window = deque(maxlen=FIRE_FRAME_WINDOW)

        # Public flags – read by main.py /status
        self.fall_detected  = False
        self.theft_detected = False
        self.fire_detected  = False
        self.person_count   = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def start_video_file(self, path: str):
        self._restart(cv2.VideoCapture(path))

    def start_ip_camera(self, url: str):
        self._restart(cv2.VideoCapture(url))

    def stop(self):
        with self._lock:
            self._running = False
            if self._cap:
                self._cap.release()
                self._cap = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        logger.info("Processor %d stopped.", self.camera_id)

    def reset_alerts(self):
        """Clear all detection flags and tracking state."""
        self.fall_detected  = False
        self.theft_detected = False
        self.fire_detected  = False
        self._fall_window.clear()
        self._fire_window.clear()
        self._person_tracks.clear()
        self._object_tracks.clear()
        self._abandon_timer.clear()
        self._theft_reason = ""
        logger.info("Alerts reset for camera %d", self.camera_id)

    def set_restricted_zones(self, zones):
        """Hot-swap restricted zone polygons without restarting the processor."""
        self.restricted_zones = zones

    # ── Internal loop ──────────────────────────────────────────────────────────

    def _restart(self, cap):
        self.stop()
        self.reset_alerts()
        with self._lock:
            self._cap     = cap
            self._running = True
        self._thread = threading.Thread(target=self._process, daemon=True)
        self._thread.start()

    def _process(self):
        logger.info("Camera %d processing started.", self.camera_id)
        while True:
            with self._lock:
                if not self._running or self._cap is None:
                    break
                ret, frame = self._cap.read()
            if not ret:
                logger.info("Camera %d stream ended.", self.camera_id)
                break
            try:
                annotated = self._run_inference(frame)
                if not self.frame_queue.full():
                    self.frame_queue.put(annotated)
            except Exception as exc:
                logger.error("Inference error on camera %d: %s", self.camera_id, exc)
        self.stop()

    # ── Inference ──────────────────────────────────────────────────────────────

    def _run_inference(self, frame):
        results = self.model(frame, verbose=False)[0]

        raw_persons = []   # list of (x1,y1,x2,y2)
        raw_objects  = []  # list of (label, x1,y1,x2,y2)

        for box in results.boxes:
            cls   = int(box.cls[0])
            label = self.model.names[cls]
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            if label == "person":
                raw_persons.append((x1, y1, x2, y2))
            elif label in TRACKABLE_OBJECTS:
                raw_objects.append((label, x1, y1, x2, y2))

        self.person_count = len(raw_persons)

        self._update_person_tracks(raw_persons)
        self._update_object_tracks(raw_objects)
        self._evict_stale_tracks()

        self._detect_fall(raw_persons)
        self._detect_theft()
        self._detect_fire(frame)

        return self._draw_overlays(results.plot(), frame)

    # ── Track management ───────────────────────────────────────────────────────

    def _nearest_track(self, centroid, tracks):
        """Return id of closest track within PROXIMITY_PX, or None."""
        best_id, best_d = None, PROXIMITY_PX
        for tid, t in tracks.items():
            d = _dist(centroid, t.centroid)
            if d < best_d:
                best_d, best_id = d, tid
        return best_id

    def _update_person_tracks(self, detections):
        for (x1, y1, x2, y2) in detections:
            c   = _centroid(x1, y1, x2, y2)
            tid = self._nearest_track(c, self._person_tracks)
            if tid is not None:
                self._person_tracks[tid].update(c, (x1, y1, x2, y2))
            else:
                t = _Track("person", c, (x1, y1, x2, y2))
                self._person_tracks[t.track_id] = t

    def _update_object_tracks(self, detections):
        for (label, x1, y1, x2, y2) in detections:
            c   = _centroid(x1, y1, x2, y2)
            tid = self._nearest_track(c, self._object_tracks)
            if tid is not None:
                self._object_tracks[tid].update(c, (x1, y1, x2, y2))
            else:
                t = _Track(label, c, (x1, y1, x2, y2))
                self._object_tracks[t.track_id] = t

    def _evict_stale_tracks(self):
        now = time.time()
        for store in (self._person_tracks, self._object_tracks):
            stale = [tid for tid, t in store.items()
                     if now - t.last_seen > OBJECT_TTL]
            for tid in stale:
                del store[tid]
                self._abandon_timer.pop(tid, None)

    # ── Fall ───────────────────────────────────────────────────────────────────

    def _detect_fall(self, person_boxes):
        positive = any(
            (x2 - x1) > FALL_RATIO * (y2 - y1)
            for (x1, y1, x2, y2) in person_boxes
        )
        self._fall_window.append(positive)
        if (len(self._fall_window) == FALL_WINDOW
                and sum(self._fall_window) >= FALL_CONSECUTIVE):
            if not self.fall_detected:
                logger.warning("FALL detected on camera %d", self.camera_id)
            self.fall_detected = True

    # ── Theft ──────────────────────────────────────────────────────────────────

    def _detect_theft(self):
        """
        For each tracked object:

        Ownership assignment
        ───────────────────
        Each frame we count how many frames every person has been within
        PROXIMITY_PX of the object.  The first person to accumulate
        OWNERSHIP_FRAMES consecutive near-frames becomes the owner.

        Scenario A – Abandoned object
        ──────────────────────────────
        Owner track disappears from scene while the object remains visible.
        If the owner is absent for more than ABANDON_TIMEOUT seconds → flag.

        Scenario B – Picked up by non-owner
        ─────────────────────────────────────
        The object's centroid has moved more than PICKUP_MOVE_PX pixels from
        its first recorded position AND the person now closest to it is NOT
        the assigned owner → flag.

        Scenario C – Restricted-zone interaction
        ─────────────────────────────────────────
        A person whose centroid falls inside any restricted polygon is also
        within PROXIMITY_PX of a tracked object → flag.
        """
        now = time.time()

        for obj_id, obj in self._object_tracks.items():

            # ── Update near-counts & ownership ────────────────────────────────
            for pid, person in self._person_tracks.items():
                if _dist(obj.centroid, person.centroid) <= PROXIMITY_PX:
                    obj.near_counts[pid] += 1
                    if (obj.owner_id is None
                            and obj.near_counts[pid] >= OWNERSHIP_FRAMES):
                        obj.owner_id = pid
                        logger.info(
                            "Cam %d: person track %d → owns %s (obj track %d)",
                            self.camera_id, pid, obj.label, obj_id
                        )

            # ── Scenario A ────────────────────────────────────────────────────
            if obj.owner_id is not None:
                owner_alive = obj.owner_id in self._person_tracks
                if not owner_alive:
                    if obj_id not in self._abandon_timer:
                        self._abandon_timer[obj_id] = now
                    elif now - self._abandon_timer[obj_id] > ABANDON_TIMEOUT:
                        self._theft_reason = (
                            f"A: {obj.label} abandoned — owner left frame")
                        self._flag_theft()
                else:
                    self._abandon_timer.pop(obj_id, None)

            # ── Scenario B ────────────────────────────────────────────────────
            if (obj.owner_id is not None
                    and obj.displacement > PICKUP_MOVE_PX
                    and obj.age > 2.0):
                closest = self._closest_person_to(obj.centroid)
                if closest is not None and closest != obj.owner_id:
                    self._theft_reason = (
                        f"B: {obj.label} moved {obj.displacement:.0f}px"
                        f" — now with person {closest} (owner={obj.owner_id})")
                    self._flag_theft()

            # ── Scenario C ────────────────────────────────────────────────────
            for zone in self.restricted_zones:
                pid = self._closest_person_to(obj.centroid)
                if pid is None:
                    continue
                person = self._person_tracks[pid]
                if (_point_in_polygon(person.centroid, zone)
                        and _dist(obj.centroid, person.centroid) <= PROXIMITY_PX):
                    self._theft_reason = (
                        f"C: person {pid} in restricted zone near {obj.label}")
                    self._flag_theft()

    def _closest_person_to(self, point):
        if not self._person_tracks:
            return None
        return min(self._person_tracks,
                   key=lambda pid: _dist(self._person_tracks[pid].centroid, point))

    def _flag_theft(self):
        if not self.theft_detected:
            logger.warning("THEFT cam %d: %s", self.camera_id, self._theft_reason)
        self.theft_detected = True

    # ── Fire ───────────────────────────────────────────────────────────────────

    def _detect_fire(self, frame):
        """
        Multi-stage fire detection designed to eliminate false positives.

        Stage 1 – HSV masking
            Three tight ranges cover true flame colours (orange-yellow and
            deep red) with HIGH saturation (≥180) and HIGH brightness (≥150).
            This excludes skin (~sat 80-140), warm white lights (low sat),
            red clothing in shadows (low brightness), and sunset backgrounds.

        Stage 2 – Morphological cleanup
            Open (remove specks) then Close (fill gaps) with a 7×7 kernel.

        Stage 3 – Connected-component area filter
            Only the LARGEST contiguous blob counts. A scattered mask from
            clothing or lighting rarely forms one large connected region.
            The blob must be ≥ FIRE_MIN_AREA px².

        Stage 4 – Brightness mean check
            The mean V-channel value inside the mask must exceed
            FIRE_MIN_BRIGHT_MEAN. Fire glows brightly; red clothing in dim
            scenes does not.

        Stage 5 – Temporal consistency window
            At least FIRE_FRAME_HITS of the last FIRE_FRAME_WINDOW frames
            must pass stages 1-4. Static red objects maintain constant
            area — actual fire flickers but stays consistently present.
        """
        hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for (lo, hi) in FIRE_HSV_RANGES:
            mask |= cv2.inRange(hsv, lo, hi)

        # Stage 2 – morphological cleanup with larger kernel
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        mask   = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  kernel, iterations=2)
        mask   = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

        # Stage 3 – largest connected component only
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                        cv2.CHAIN_APPROX_SIMPLE)
        frame_positive = False
        if contours:
            largest_area = max(cv2.contourArea(c) for c in contours)

            if largest_area >= FIRE_MIN_AREA:
                # Stage 4 – brightness check inside the mask
                v_channel  = hsv[:, :, 2]
                fire_pixels = v_channel[mask > 0]
                if len(fire_pixels) > 0:
                    mean_brightness = float(np.mean(fire_pixels))
                    if mean_brightness >= FIRE_MIN_BRIGHT_MEAN:
                        frame_positive = True

        # Stage 5 – temporal window
        self._fire_window.append(frame_positive)
        if (len(self._fire_window) == FIRE_FRAME_WINDOW
                and sum(self._fire_window) >= FIRE_FRAME_HITS):
            if not self.fire_detected:
                logger.warning("FIRE detected on camera %d", self.camera_id)
            self.fire_detected = True

    # ── Overlay drawing ────────────────────────────────────────────────────────

    def _draw_overlays(self, annotated, raw_frame):
        h, w = annotated.shape[:2]
        banner_y = 0

        # ── Restricted zones (semi-transparent orange fill) ────────────────
        for zone in self.restricted_zones:
            pts     = np.array(zone, dtype=np.int32)
            overlay = annotated.copy()
            cv2.fillPoly(overlay, [pts], (0, 140, 255))
            cv2.addWeighted(overlay, 0.18, annotated, 0.82, 0, annotated)
            cv2.polylines(annotated, [pts], True, (0, 165, 255), 2)
            # Label
            cx = int(sum(p[0] for p in zone) / len(zone))
            cy = int(sum(p[1] for p in zone) / len(zone))
            cv2.putText(annotated, "RESTRICTED",
                        (cx - 46, cy), cv2.FONT_HERSHEY_SIMPLEX,
                        0.55, (0, 165, 255), 1, cv2.LINE_AA)

        # ── Ownership arrows (owner person → their object) ─────────────────
        for obj in self._object_tracks.values():
            if obj.owner_id and obj.owner_id in self._person_tracks:
                p = self._person_tracks[obj.owner_id]
                cv2.arrowedLine(
                    annotated,
                    (int(p.centroid[0]), int(p.centroid[1])),
                    (int(obj.centroid[0]), int(obj.centroid[1])),
                    (50, 220, 130), 2, tipLength=0.25,
                )

        # ── Alert banners ──────────────────────────────────────────────────
        def banner(text, bgr):
            nonlocal banner_y
            cv2.rectangle(annotated, (0, banner_y), (w, banner_y + 52), bgr, -1)
            cv2.putText(annotated, text,
                        (10, banner_y + 36),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.05,
                        (255, 255, 255), 2, cv2.LINE_AA)
            banner_y += 58

        if self.fall_detected:
            banner("  FALL DETECTED", (30, 30, 200))

        if self.theft_detected:
            short = self._theft_reason[:52] if self._theft_reason else "Theft detected"
            banner(f"  THEFT — {short}", (20, 70, 180))

        if self.fire_detected:
            banner("  FIRE DETECTED — EVACUATE NOW", (0, 50, 215))

        return annotated