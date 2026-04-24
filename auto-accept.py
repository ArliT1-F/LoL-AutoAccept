import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from math import hypot
from typing import Dict, List, Optional, Tuple

import cv2
import keyboard
import numpy as np
import pyautogui
import tkinter as tk
from tkinter import filedialog, messagebox


logging.basicConfig(filename="app.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATE_IDLE = "idle"
STATE_SEARCHING = "searching"
STATE_LOCKED = "locked"
STATE_COOLDOWN = "cooldown"
STATE_STOPPED = "stopped"

Region = Tuple[int, int, int, int]

is_running = False
stop_event = threading.Event()
config = {}
CONFIG_PATH = ""
loop_state = STATE_IDLE

hotkey_handles = {"start": None, "stop": None}
template_cache: Dict[str, Dict[str, np.ndarray]] = {}

last_click_time = 0.0
last_click_pos: Optional[Tuple[int, int]] = None


@dataclass
class DetectionResult:
    found: bool
    confidence: float = 0.0
    top_left: Optional[Tuple[int, int]] = None
    size: Optional[Tuple[int, int]] = None
    click_point: Optional[Tuple[int, int]] = None
    scale: float = 1.0
    template_path: str = ""
    match_mode: str = ""
    region_used: Optional[Region] = None
    search_mode: str = STATE_SEARCHING


def get_runtime_base_dir():
    """Get the writable runtime directory for config and assets."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_default_config_path():
    """Pick runtime config path and fall back to bundled config if needed."""
    runtime_path = os.path.join(get_runtime_base_dir(), "config.json")
    if os.path.exists(runtime_path):
        return runtime_path
    bundled_base = getattr(sys, "_MEIPASS", get_runtime_base_dir())
    bundled_path = os.path.join(bundled_base, "config.json")
    if os.path.exists(bundled_path):
        return bundled_path
    return runtime_path


def clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


def parse_region(region_value):
    """Normalize region input into a 4-int tuple or None."""
    if not region_value:
        return None
    if isinstance(region_value, (list, tuple)) and len(region_value) == 4:
        x, y, w, h = [int(v) for v in region_value]
        if w > 0 and h > 0:
            return (x, y, w, h)
    if isinstance(region_value, str):
        parts = [p.strip() for p in region_value.split(",")]
        if len(parts) == 4:
            try:
                x, y, w, h = [int(v) for v in parts]
            except ValueError:
                return None
            if w > 0 and h > 0:
                return (x, y, w, h)
    return None


def resolve_path(path_value, base_dir):
    if not path_value:
        return ""
    if os.path.isabs(path_value):
        return os.path.normpath(path_value)
    return os.path.normpath(os.path.join(base_dir, path_value))


def serialize_path(path_value, base_dir):
    """Store paths relative to config for portability when possible."""
    if not path_value:
        return ""
    abs_path = os.path.abspath(path_value)
    try:
        common = os.path.commonpath([abs_path, os.path.abspath(base_dir)])
    except ValueError:
        return abs_path
    if common == os.path.abspath(base_dir):
        return os.path.relpath(abs_path, base_dir)
    return abs_path


def normalize_config(cfg, base_dir):
    """Normalize config keys, defaults, and relative paths."""
    normalized = dict(cfg)

    threshold = normalized.get("threshold", normalized.get("threshhold", 0.8))
    normalized["threshold"] = float(threshold)
    normalized.pop("threshhold", None)

    normalized["template_path"] = resolve_path(normalized.get("template_path", "accept_button.png"), base_dir)
    normalized["template_variants"] = [
        resolve_path(path_item, base_dir)
        for path_item in normalized.get("template_variants", [])
        if path_item
    ]

    normalized["retry_interval"] = max(0.1, float(normalized.get("retry_interval", 1.8)))
    normalized["max_retries"] = int(normalized.get("max_retries", 20))
    normalized["region"] = parse_region(normalized.get("region"))
    normalized["debug"] = bool(normalized.get("debug", False))

    normalized["enable_multiscale"] = bool(normalized.get("enable_multiscale", True))
    normalized["min_scale"] = clamp(float(normalized.get("min_scale", 0.45)), 0.2, 3.0)
    normalized["max_scale"] = clamp(float(normalized.get("max_scale", 2.2)), normalized["min_scale"], 4.0)
    normalized["scale_step"] = clamp(float(normalized.get("scale_step", 0.08)), 0.01, 1.0)
    normalized["scale_window"] = clamp(float(normalized.get("scale_window", 0.45)), 0.05, 2.0)
    normalized["edge_fallback"] = bool(normalized.get("edge_fallback", True))
    normalized["auto_template_variants"] = bool(normalized.get("auto_template_variants", True))

    source_res = normalized.get("template_source_resolution")
    if isinstance(source_res, (list, tuple)) and len(source_res) == 2:
        try:
            source_w, source_h = int(source_res[0]), int(source_res[1])
            if source_w > 0 and source_h > 0:
                normalized["template_source_resolution"] = [source_w, source_h]
            else:
                normalized["template_source_resolution"] = None
        except (TypeError, ValueError):
            normalized["template_source_resolution"] = None
    else:
        normalized["template_source_resolution"] = None

    normalized["roi_padding"] = max(20, int(normalized.get("roi_padding", 120)))
    normalized["reacquire_interval"] = max(1, int(normalized.get("reacquire_interval", 8)))
    normalized["lock_miss_tolerance"] = max(1, int(normalized.get("lock_miss_tolerance", 2)))

    normalized["click_cooldown"] = max(0.0, float(normalized.get("click_cooldown", 1.2)))
    normalized["duplicate_click_radius"] = max(0, int(normalized.get("duplicate_click_radius", 18)))
    normalized["duplicate_click_window"] = max(0.0, float(normalized.get("duplicate_click_window", 4.0)))
    normalized["stability_check"] = bool(normalized.get("stability_check", True))
    normalized["stability_delta"] = clamp(float(normalized.get("stability_delta", 0.05)), 0.0, 0.3)
    normalized["cooldown_after_click"] = max(0.0, float(normalized.get("cooldown_after_click", 0.7)))

    normalized["adaptive_backoff"] = clamp(float(normalized.get("adaptive_backoff", 1.25)), 1.0, 3.0)
    normalized["max_retry_wait"] = max(normalized["retry_interval"], float(normalized.get("max_retry_wait", 8.0)))

    normalized["auto_detect_window"] = bool(normalized.get("auto_detect_window", True))
    normalized["lol_window_title"] = str(normalized.get("lol_window_title", "League of Legends")).strip()

    capture_size = normalized.get("template_capture_size", [220, 70])
    if isinstance(capture_size, (list, tuple)) and len(capture_size) == 2:
        try:
            cap_w = max(40, int(capture_size[0]))
            cap_h = max(20, int(capture_size[1]))
            normalized["template_capture_size"] = [cap_w, cap_h]
        except (TypeError, ValueError):
            normalized["template_capture_size"] = [220, 70]
    else:
        normalized["template_capture_size"] = [220, 70]

    normalized["start_hotkey"] = str(normalized.get("start_hotkey", "ctrl+alt+-"))
    normalized["stop_hotkey"] = str(normalized.get("stop_hotkey", "ctrl+alt+="))
    return normalized


def load_config(config_path=None):
    """Load configuration from JSON file."""
    global CONFIG_PATH
    chosen_path = config_path or get_default_config_path()
    CONFIG_PATH = chosen_path
    if not os.path.exists(chosen_path):
        logging.warning("Config file missing at '%s'. Creating with defaults.", chosen_path)
        default_cfg = normalize_config({}, os.path.dirname(chosen_path))
        save_config(default_cfg, chosen_path)
        return default_cfg

    with open(chosen_path, "r", encoding="utf-8") as config_file:
        loaded = json.load(config_file)
    return normalize_config(loaded, os.path.dirname(chosen_path))


def save_config(cfg, config_path=None):
    """Persist current config values."""
    path = config_path or CONFIG_PATH or get_default_config_path()
    base_dir = os.path.dirname(path)
    os.makedirs(base_dir, exist_ok=True)

    serializable = dict(cfg)
    serializable["template_path"] = serialize_path(serializable.get("template_path"), base_dir)
    serializable["template_variants"] = [
        serialize_path(path_item, base_dir) for path_item in serializable.get("template_variants", [])
    ]
    serializable["region"] = list(serializable["region"]) if serializable.get("region") else None

    with open(path, "w", encoding="utf-8") as config_file:
        json.dump(serializable, config_file, indent=2)
    logging.info("Configuration saved to '%s'", path)


def get_template_paths():
    """Return deduplicated template list from explicit and auto-discovered variants."""
    primary = config.get("template_path", "")
    candidate_paths = [primary] if primary else []
    candidate_paths.extend(config.get("template_variants", []))

    if config.get("auto_template_variants", True) and primary:
        folder = os.path.dirname(primary)
        name, ext = os.path.splitext(os.path.basename(primary))
        if os.path.isdir(folder):
            for filename in os.listdir(folder):
                if filename.lower().endswith(ext.lower()) and filename.startswith(name):
                    candidate_paths.append(os.path.join(folder, filename))

    seen = set()
    deduped = []
    for path_item in candidate_paths:
        normalized_path = os.path.normpath(path_item)
        if normalized_path not in seen:
            deduped.append(normalized_path)
            seen.add(normalized_path)
    return deduped


def load_template(path_item):
    """Load template image and edge map from cache or disk."""
    if not path_item or not os.path.exists(path_item):
        return None
    if path_item in template_cache:
        return template_cache[path_item]

    template_gray = cv2.imread(path_item, cv2.IMREAD_GRAYSCALE)
    if template_gray is None:
        return None

    template_edge = cv2.Canny(template_gray, 90, 180)
    template_cache[path_item] = {"gray": template_gray, "edge": template_edge}
    return template_cache[path_item]


def generate_scale_candidates(screen_width, screen_height):
    """Generate adaptive scales using current and source screen resolution."""
    if not config.get("enable_multiscale", True):
        return [1.0]

    min_scale = config.get("min_scale", 0.45)
    max_scale = config.get("max_scale", 2.2)
    step = config.get("scale_step", 0.08)
    scale_window = config.get("scale_window", 0.45)

    source_res = config.get("template_source_resolution")
    if source_res:
        source_w, source_h = source_res
        center_scale = ((screen_width / source_w) + (screen_height / source_h)) / 2.0
        lower = clamp(center_scale - scale_window, min_scale, max_scale)
        upper = clamp(center_scale + scale_window, min_scale, max_scale)
    else:
        # Baseline heuristic keeps single-template matching usable across resolutions.
        baseline_center = screen_width / 1920.0
        lower = clamp(baseline_center - max(scale_window, 0.65), min_scale, max_scale)
        upper = clamp(baseline_center + max(scale_window, 0.65), min_scale, max_scale)

    scales = []
    current = lower
    while current <= upper + 1e-9:
        scales.append(round(current, 4))
        current += step

    anchor_scales = [0.5, 0.75, 1.0, 1.2, 1.5, 1.8, 2.0]
    for anchor in anchor_scales:
        if min_scale <= anchor <= max_scale:
            scales.append(anchor)

    return sorted(set(scales))


def match_template_at_scales(screen_matrix, template_matrix, scales):
    """Return best match information for a template across scales."""
    best = None
    for scale in scales:
        if abs(scale - 1.0) > 1e-5:
            scaled_template = cv2.resize(
                template_matrix,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_CUBIC,
            )
        else:
            scaled_template = template_matrix

        temp_h, temp_w = scaled_template.shape[:2]
        scr_h, scr_w = screen_matrix.shape[:2]
        if temp_w > scr_w or temp_h > scr_h:
            continue

        result = cv2.matchTemplate(screen_matrix, scaled_template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if best is None or max_val > best["confidence"]:
            best = {
                "confidence": float(max_val),
                "top_left": (int(max_loc[0]), int(max_loc[1])),
                "size": (int(temp_w), int(temp_h)),
                "scale": float(scale),
            }
    return best


def detect_lol_window_region():
    """Try to detect League window bounds using window title."""
    title = config.get("lol_window_title", "League of Legends")
    if not title:
        return None
    try:
        windows = pyautogui.getWindowsWithTitle(title)
    except Exception:
        return None

    for window in windows:
        try:
            width = int(window.width)
            height = int(window.height)
            if width <= 0 or height <= 0:
                continue
            if getattr(window, "isMinimized", False):
                continue
            return (int(window.left), int(window.top), width, height)
        except Exception:
            continue
    return None


def get_primary_search_region():
    """Resolve the main search region for this iteration."""
    manual_region = parse_region(config.get("region"))
    if manual_region:
        return manual_region
    if config.get("auto_detect_window", True):
        return detect_lol_window_region()
    return None


def expand_roi(top_left, size, padding):
    """Create padded ROI around a detected target, clipped to screen bounds."""
    screen_w, screen_h = pyautogui.size()
    left = max(0, top_left[0] - padding)
    top = max(0, top_left[1] - padding)
    right = min(screen_w, top_left[0] + size[0] + padding)
    bottom = min(screen_h, top_left[1] + size[1] + padding)
    width = max(1, right - left)
    height = max(1, bottom - top)
    return (int(left), int(top), int(width), int(height))


def perform_detection(search_region, search_mode):
    """Run one detection pass and return metadata-rich result."""
    threshold = config.get("threshold", 0.8)

    try:
        screenshot = pyautogui.screenshot(region=search_region)
        screenshot_rgb = np.array(screenshot)
        screenshot_gray = cv2.cvtColor(screenshot_rgb, cv2.COLOR_RGB2GRAY)
    except Exception:
        logging.exception("Failed taking screenshot for detection")
        return DetectionResult(found=False, region_used=search_region, search_mode=search_mode)

    scales = generate_scale_candidates(screenshot_gray.shape[1], screenshot_gray.shape[0])
    screenshot_edge = cv2.Canny(screenshot_gray, 100, 200) if config.get("edge_fallback", True) else None

    best_candidate = None
    for template_path in get_template_paths():
        template_data = load_template(template_path)
        if not template_data:
            continue

        direct_match = match_template_at_scales(screenshot_gray, template_data["gray"], scales)
        if direct_match:
            direct_match["template_path"] = template_path
            direct_match["mode"] = "grayscale"
            if best_candidate is None or direct_match["confidence"] > best_candidate["confidence"]:
                best_candidate = direct_match

        if screenshot_edge is not None:
            edge_match = match_template_at_scales(screenshot_edge, template_data["edge"], scales)
            if edge_match:
                edge_match["template_path"] = template_path
                edge_match["mode"] = "edge"
                if best_candidate is None or edge_match["confidence"] > best_candidate["confidence"]:
                    best_candidate = edge_match

    if not best_candidate or best_candidate["confidence"] < threshold:
        return DetectionResult(
            found=False,
            confidence=(best_candidate["confidence"] if best_candidate else 0.0),
            region_used=search_region,
            search_mode=search_mode,
        )

    region_offset_x = search_region[0] if search_region else 0
    region_offset_y = search_region[1] if search_region else 0
    top_left = (
        best_candidate["top_left"][0] + region_offset_x,
        best_candidate["top_left"][1] + region_offset_y,
    )
    width, height = best_candidate["size"]
    click_point = (int(top_left[0] + width / 2), int(top_left[1] + height / 2))

    if config.get("debug", False):
        debug_view = screenshot_rgb.copy()
        local_x, local_y = best_candidate["top_left"]
        cv2.rectangle(debug_view, (local_x, local_y), (local_x + width, local_y + height), (0, 255, 0), 2)
        cv2.imshow("Auto Accept Debug Match", debug_view)
        cv2.waitKey(300)
        cv2.destroyAllWindows()

    return DetectionResult(
        found=True,
        confidence=best_candidate["confidence"],
        top_left=top_left,
        size=(width, height),
        click_point=click_point,
        scale=best_candidate["scale"],
        template_path=best_candidate["template_path"],
        match_mode=best_candidate["mode"],
        region_used=search_region,
        search_mode=search_mode,
    )


def verify_match_stability(result):
    """Double-check the candidate on a tighter crop before clicking."""
    if not config.get("stability_check", True):
        return True
    if not result.top_left or not result.size or not result.template_path:
        return False

    template_data = load_template(result.template_path)
    if not template_data:
        return False

    margin = 25
    check_region = expand_roi(result.top_left, result.size, margin)
    try:
        screenshot = pyautogui.screenshot(region=check_region)
        screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
    except Exception:
        logging.exception("Stability check screenshot failed")
        return False

    template_gray = template_data["gray"]
    scale = result.scale
    if abs(scale - 1.0) > 1e-5:
        template_gray = cv2.resize(template_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    temp_h, temp_w = template_gray.shape[:2]
    scr_h, scr_w = screenshot_gray.shape[:2]
    if temp_w > scr_w or temp_h > scr_h:
        return False

    match_res = cv2.matchTemplate(screenshot_gray, template_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(match_res)
    min_required = max(0.4, config.get("threshold", 0.8) - config.get("stability_delta", 0.05))
    return float(max_val) >= min_required


def click_is_safe(result):
    """Apply click cooldown and duplicate-click guards."""
    global last_click_time, last_click_pos
    now = time.time()
    click_point = result.click_point
    if click_point is None:
        return False, "No click point."

    if now - last_click_time < config.get("click_cooldown", 1.2):
        return False, "Global click cooldown active."

    if last_click_pos:
        distance = hypot(click_point[0] - last_click_pos[0], click_point[1] - last_click_pos[1])
        if distance <= config.get("duplicate_click_radius", 18):
            if now - last_click_time < config.get("duplicate_click_window", 4.0):
                return False, "Duplicate click guard blocked this click."

    if not verify_match_stability(result):
        return False, "Stability check failed."
    return True, "OK"


def do_click(result):
    """Execute click and update click history."""
    global last_click_time, last_click_pos
    if not result.click_point:
        return
    pyautogui.click(result.click_point[0], result.click_point[1])
    last_click_time = time.time()
    last_click_pos = result.click_point


def compute_retry_wait(retry_attempts):
    """Adaptive backoff based on failure streak."""
    base = config.get("retry_interval", 1.8)
    backoff = config.get("adaptive_backoff", 1.25)
    max_wait = config.get("max_retry_wait", 8.0)
    return min(max_wait, base * (backoff ** max(0, retry_attempts - 1)))


def start_auto_accept():
    """Main loop with lock-on ROI search and adaptive fallback."""
    global is_running, loop_state
    retry_attempts = 0
    loop_state = STATE_SEARCHING

    lock_region = None
    lock_misses = 0
    cycle_count = 0

    while is_running and not stop_event.is_set():
        cycle_count += 1
        primary_region = get_primary_search_region()
        do_reacquire = lock_region is None or (cycle_count % config.get("reacquire_interval", 8) == 0)

        if do_reacquire:
            search_region = primary_region
            search_mode = STATE_SEARCHING
        else:
            search_region = lock_region
            search_mode = STATE_LOCKED

        result = perform_detection(search_region, search_mode)
        if result.found:
            retry_attempts = 0
            lock_misses = 0
            lock_region = expand_roi(result.top_left, result.size, config.get("roi_padding", 120))
            loop_state = STATE_LOCKED

            safe_to_click, reason = click_is_safe(result)
            if safe_to_click:
                do_click(result)
                loop_state = STATE_COOLDOWN
                logging.info(
                    "Clicked at %s, confidence=%.3f, scale=%.3f, mode=%s, template=%s",
                    result.click_point,
                    result.confidence,
                    result.scale,
                    result.match_mode,
                    os.path.basename(result.template_path),
                )
                print("Accept button found and clicked!")
                wait_time = config.get("cooldown_after_click", 0.7)
            else:
                logging.info("Candidate found but skipped click: %s", reason)
                wait_time = config.get("retry_interval", 1.8)

            if stop_event.wait(wait_time):
                break
            continue

        print("Accept button not found. Retrying...")
        retry_attempts += 1
        loop_state = STATE_SEARCHING

        if search_mode == STATE_LOCKED:
            lock_misses += 1
            if lock_misses >= config.get("lock_miss_tolerance", 2):
                lock_region = None
                lock_misses = 0
        else:
            lock_region = None

        max_retries = config.get("max_retries", 20)
        if max_retries > 0 and retry_attempts >= max_retries:
            logging.error("Max retries reached. Stopping auto accept.")
            print("Max retries reached. Stopping auto accept.")
            is_running = False
            stop_event.set()
            break

        if stop_event.wait(compute_retry_wait(retry_attempts)):
            break

    loop_state = STATE_STOPPED


def stop_auto_accept():
    """Stop the auto accept loop."""
    global is_running, loop_state
    is_running = False
    loop_state = STATE_STOPPED
    stop_event.set()
    print("Auto Accept Stopped")


def start_thread():
    """Start the auto accept process in a new thread."""
    global is_running
    if is_running:
        messagebox.showinfo("Info", "Already running.")
        return
    if not config.get("template_path"):
        messagebox.showerror("Error", "Please select a template image first.")
        return
    stop_event.clear()
    is_running = True
    threading.Thread(target=start_auto_accept, daemon=True).start()


def start_auto_accept_hotkey():
    """Start auto-accept from keyboard shortcut."""
    if not is_running:
        print("Starting auto-accept using hotkey...")
        start_thread()


def stop_auto_accept_hotkey():
    """Stop auto-accept from keyboard shortcut."""
    if is_running:
        print("Stopping auto-accept using hotkey...")
        stop_auto_accept()


def setup_hotkeys():
    """Setup hotkeys for start and stop actions."""
    global hotkey_handles
    start_hotkey = config.get("start_hotkey", "ctrl+alt+-")
    stop_hotkey = config.get("stop_hotkey", "ctrl+alt+=")

    try:
        for handle in hotkey_handles.values():
            if handle is not None:
                keyboard.remove_hotkey(handle)

        hotkey_handles["start"] = keyboard.add_hotkey(start_hotkey, start_auto_accept_hotkey)
        hotkey_handles["stop"] = keyboard.add_hotkey(stop_hotkey, stop_auto_accept_hotkey)
        print(f"Hotkeys set: Start ({start_hotkey}), Stop ({stop_hotkey})")
        logging.info("Hotkeys configured: Start (%s), Stop (%s)", start_hotkey, stop_hotkey)
    except Exception as exc:
        hotkey_handles = {"start": None, "stop": None}
        logging.warning("Hotkey setup failed: %s", exc)
        messagebox.showwarning(
            "Hotkey Warning",
            "Could not setup hotkeys. You may need admin/root permissions.\n"
            "You can still use GUI buttons.",
        )


def run_detection_test():
    """Run a single detection pass and return text summary."""
    test_region = get_primary_search_region()
    result = perform_detection(test_region, "test")
    if not result.found:
        return f"Not found. Best confidence: {result.confidence:.3f}"
    return (
        f"Found with confidence {result.confidence:.3f}\n"
        f"Scale: {result.scale:.3f}, Mode: {result.match_mode}\n"
        f"Template: {os.path.basename(result.template_path)}\n"
        f"Click point: {result.click_point}"
    )


def create_template_from_cursor():
    """Capture template around current cursor as a wizard helper."""
    capture_w, capture_h = config.get("template_capture_size", [220, 70])
    mouse_pos = pyautogui.position()
    screen_w, screen_h = pyautogui.size()

    left = clamp(mouse_pos.x - capture_w // 2, 0, screen_w - 1)
    top = clamp(mouse_pos.y - capture_h // 2, 0, screen_h - 1)
    width = min(capture_w, screen_w - left)
    height = min(capture_h, screen_h - top)
    screenshot = pyautogui.screenshot(region=(int(left), int(top), int(width), int(height)))
    screenshot.save(config["template_path"])
    template_cache.clear()


def run_setup_wizard():
    """Guided setup for window detection and single-template calibration."""
    screen_w, screen_h = pyautogui.size()
    config["template_source_resolution"] = [int(screen_w), int(screen_h)]

    detected_region = detect_lol_window_region()
    if detected_region:
        config["region"] = detected_region
        logging.info("Setup wizard detected window region: %s", detected_region)
    else:
        config["region"] = None

    should_capture = messagebox.askyesno(
        "Setup Wizard",
        "Wizard can capture a fresh template from your mouse position.\n\n"
        "1) Open League queue pop-up.\n"
        "2) Move mouse to center of the Accept button.\n"
        "3) Click 'Yes' to start 4-second countdown.\n\n"
        "Do you want to capture a new template now?",
    )
    if should_capture:
        messagebox.showinfo("Setup Wizard", "Capturing in 4 seconds. Place cursor on Accept button center.")
        time.sleep(4)
        create_template_from_cursor()
        messagebox.showinfo("Setup Wizard", "Template captured successfully.")

    save_config(config)
    return detected_region


def create_gui():
    """Create GUI for configuration and controls."""
    global config
    root = tk.Tk()
    root.title("Auto Accept Configuration")
    root.geometry("540x690")

    status_var = tk.StringVar(value=f"State: {loop_state}")
    template_var = tk.StringVar(value="")
    region_var = tk.StringVar(value="")

    threshold_var = tk.StringVar(value=str(config.get("threshold", 0.8)))
    retry_interval_var = tk.StringVar(value=str(config.get("retry_interval", 1.8)))
    max_retries_var = tk.StringVar(value=str(config.get("max_retries", 20)))
    min_scale_var = tk.StringVar(value=str(config.get("min_scale", 0.45)))
    max_scale_var = tk.StringVar(value=str(config.get("max_scale", 2.2)))
    scale_step_var = tk.StringVar(value=str(config.get("scale_step", 0.08)))
    click_cooldown_var = tk.StringVar(value=str(config.get("click_cooldown", 1.2)))
    roi_padding_var = tk.StringVar(value=str(config.get("roi_padding", 120)))
    reacquire_var = tk.StringVar(value=str(config.get("reacquire_interval", 8)))
    title_var = tk.StringVar(value=config.get("lol_window_title", "League of Legends"))

    auto_window_var = tk.BooleanVar(value=config.get("auto_detect_window", True))
    edge_fallback_var = tk.BooleanVar(value=config.get("edge_fallback", True))
    multiscale_var = tk.BooleanVar(value=config.get("enable_multiscale", True))
    stability_var = tk.BooleanVar(value=config.get("stability_check", True))

    def refresh_labels():
        variant_count = len(config.get("template_variants", []))
        template_path = config.get("template_path", "")
        template_var.set(f"Template: {os.path.basename(template_path) if template_path else 'Not Selected'} | variants: {variant_count}")
        region = config.get("region")
        if region:
            region_var.set(f"Region: x={region[0]}, y={region[1]}, w={region[2]}, h={region[3]}")
        else:
            region_var.set("Region: Auto (window/fullscreen)")

    def parse_float(var_value, field_name, min_value=None, max_value=None):
        try:
            parsed = float(var_value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a number.") from exc
        if min_value is not None and parsed < min_value:
            raise ValueError(f"{field_name} must be >= {min_value}.")
        if max_value is not None and parsed > max_value:
            raise ValueError(f"{field_name} must be <= {max_value}.")
        return parsed

    def parse_int(var_value, field_name, min_value=None):
        try:
            parsed = int(var_value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an integer.") from exc
        if min_value is not None and parsed < min_value:
            raise ValueError(f"{field_name} must be >= {min_value}.")
        return parsed

    def apply_form_values():
        config["threshold"] = parse_float(threshold_var.get(), "Threshold", 0.0, 1.0)
        config["retry_interval"] = parse_float(retry_interval_var.get(), "Retry interval", 0.1)
        config["max_retries"] = parse_int(max_retries_var.get(), "Max retries", 1)
        config["min_scale"] = parse_float(min_scale_var.get(), "Min scale", 0.2)
        config["max_scale"] = parse_float(max_scale_var.get(), "Max scale", config["min_scale"])
        config["scale_step"] = parse_float(scale_step_var.get(), "Scale step", 0.01)
        config["click_cooldown"] = parse_float(click_cooldown_var.get(), "Click cooldown", 0.0)
        config["roi_padding"] = parse_int(roi_padding_var.get(), "ROI padding", 20)
        config["reacquire_interval"] = parse_int(reacquire_var.get(), "Reacquire interval", 1)
        config["lol_window_title"] = title_var.get().strip() or "League of Legends"

        config["auto_detect_window"] = bool(auto_window_var.get())
        config["edge_fallback"] = bool(edge_fallback_var.get())
        config["enable_multiscale"] = bool(multiscale_var.get())
        config["stability_check"] = bool(stability_var.get())
        return normalize_config(config, os.path.dirname(CONFIG_PATH))

    def on_select_template():
        chosen = filedialog.askopenfilename(title="Select template", filetypes=[("PNG files", "*.png")])
        if not chosen:
            return
        config["template_path"] = chosen
        template_cache.clear()
        refresh_labels()

    def on_add_template_variant():
        chosen_files = filedialog.askopenfilenames(title="Add template variants", filetypes=[("PNG files", "*.png")])
        if not chosen_files:
            return
        existing = set(config.get("template_variants", []))
        for file_path in chosen_files:
            if file_path and file_path != config.get("template_path") and file_path not in existing:
                config.setdefault("template_variants", []).append(file_path)
                existing.add(file_path)
        template_cache.clear()
        refresh_labels()

    def on_clear_variants():
        config["template_variants"] = []
        template_cache.clear()
        refresh_labels()

    def on_detect_window():
        detected = detect_lol_window_region()
        if detected:
            config["region"] = detected
            refresh_labels()
            messagebox.showinfo("Window Detection", f"Detected region: {detected}")
        else:
            messagebox.showwarning("Window Detection", "Could not detect a matching League window.")

    def on_save_settings():
        try:
            updated = apply_form_values()
            config.update(updated)
            save_config(config)
            setup_hotkeys()
            refresh_labels()
            messagebox.showinfo("Saved", "Settings saved.")
        except ValueError as exc:
            messagebox.showerror("Validation Error", str(exc))

    def on_start():
        try:
            updated = apply_form_values()
            config.update(updated)
            save_config(config)
            setup_hotkeys()
            start_thread()
            refresh_labels()
        except ValueError as exc:
            messagebox.showerror("Validation Error", str(exc))

    def on_test_detection():
        try:
            updated = apply_form_values()
            config.update(updated)
            refresh_labels()
            summary = run_detection_test()
            messagebox.showinfo("Detection Test", summary)
        except ValueError as exc:
            messagebox.showerror("Validation Error", str(exc))

    def on_run_wizard():
        try:
            updated = apply_form_values()
            config.update(updated)
            detected = run_setup_wizard()
            refresh_labels()
            if detected:
                messagebox.showinfo("Setup Wizard", f"Wizard finished.\nDetected region: {detected}")
            else:
                messagebox.showinfo("Setup Wizard", "Wizard finished.\nWindow region was not auto-detected.")
        except ValueError as exc:
            messagebox.showerror("Validation Error", str(exc))

    controls_frame = tk.Frame(root)
    controls_frame.pack(fill="x", padx=10, pady=8)

    tk.Label(controls_frame, textvariable=status_var, fg="blue").pack(anchor="w")
    tk.Label(controls_frame, textvariable=template_var).pack(anchor="w")
    tk.Label(controls_frame, textvariable=region_var).pack(anchor="w")

    action_frame = tk.Frame(root)
    action_frame.pack(fill="x", padx=10, pady=8)
    tk.Button(action_frame, text="Select Template", command=on_select_template).grid(row=0, column=0, padx=4, pady=4, sticky="ew")
    tk.Button(action_frame, text="Add Variant(s)", command=on_add_template_variant).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
    tk.Button(action_frame, text="Clear Variants", command=on_clear_variants).grid(row=0, column=2, padx=4, pady=4, sticky="ew")
    tk.Button(action_frame, text="Detect LoL Window", command=on_detect_window).grid(row=1, column=0, padx=4, pady=4, sticky="ew")
    tk.Button(action_frame, text="Run Setup Wizard", command=on_run_wizard).grid(row=1, column=1, padx=4, pady=4, sticky="ew")
    tk.Button(action_frame, text="Test Detection", command=on_test_detection).grid(row=1, column=2, padx=4, pady=4, sticky="ew")

    settings_frame = tk.LabelFrame(root, text="Core Settings")
    settings_frame.pack(fill="x", padx=10, pady=8)

    fields = [
        ("Threshold (0-1)", threshold_var),
        ("Retry interval (s)", retry_interval_var),
        ("Max retries", max_retries_var),
        ("Min scale", min_scale_var),
        ("Max scale", max_scale_var),
        ("Scale step", scale_step_var),
        ("Click cooldown (s)", click_cooldown_var),
        ("ROI padding", roi_padding_var),
        ("Reacquire interval", reacquire_var),
        ("Window title contains", title_var),
    ]

    for row_index, (label_text, variable) in enumerate(fields):
        tk.Label(settings_frame, text=label_text).grid(row=row_index, column=0, sticky="w", padx=6, pady=3)
        tk.Entry(settings_frame, textvariable=variable, width=25).grid(row=row_index, column=1, sticky="ew", padx=6, pady=3)

    options_frame = tk.LabelFrame(root, text="Options")
    options_frame.pack(fill="x", padx=10, pady=8)
    tk.Checkbutton(options_frame, text="Auto detect LoL window", variable=auto_window_var).pack(anchor="w")
    tk.Checkbutton(options_frame, text="Enable multi-scale matching", variable=multiscale_var).pack(anchor="w")
    tk.Checkbutton(options_frame, text="Edge fallback matching", variable=edge_fallback_var).pack(anchor="w")
    tk.Checkbutton(options_frame, text="Stability check before click", variable=stability_var).pack(anchor="w")

    bottom_frame = tk.Frame(root)
    bottom_frame.pack(fill="x", padx=10, pady=10)
    tk.Button(bottom_frame, text="Save Settings", command=on_save_settings).grid(row=0, column=0, padx=4, pady=4, sticky="ew")
    tk.Button(bottom_frame, text="Start Auto Accept", command=on_start).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
    tk.Button(bottom_frame, text="Stop Auto Accept", command=stop_auto_accept).grid(row=0, column=2, padx=4, pady=4, sticky="ew")

    for col_idx in range(3):
        bottom_frame.grid_columnconfigure(col_idx, weight=1)
        action_frame.grid_columnconfigure(col_idx, weight=1)

    def update_status_label():
        status_var.set(f"State: {loop_state}")
        root.after(400, update_status_label)

    refresh_labels()
    update_status_label()
    root.mainloop()


def main():
    """Main entry point."""
    global config
    config = load_config()
    create_gui()


if __name__ == "__main__":
    main()