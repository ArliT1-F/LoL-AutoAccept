import cv2
import numpy as np
import pyautogui
import logging
import os
import json
import sys
import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import keyboard


# Setup logging
logging.basicConfig(filename='app.log', level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Global variables to control the script and store settings
is_running = False
stop_event = threading.Event()
config = {}
hotkey_handles = {"start": None, "stop": None}


def normalize_config(cfg, base_dir):
    """Normalize config keys, defaults, and relative paths."""
    threshold = cfg.get("threshold", cfg.get("threshhold", 0.8))
    cfg["threshold"] = float(threshold)
    cfg.pop("threshhold", None)

    template_path = cfg.get("template_path", "accept_button.png")
    if template_path and not os.path.isabs(template_path):
        template_path = os.path.join(base_dir, template_path)
    cfg["template_path"] = template_path

    cfg["retry_interval"] = float(cfg.get("retry_interval", 2))
    cfg["max_retries"] = int(cfg.get("max_retries", 10))
    cfg["region"] = cfg.get("region", None)
    cfg["debug"] = bool(cfg.get("debug", False))
    cfg["enable_multiscale"] = bool(cfg.get("enable_multiscale", True))
    cfg["start_hotkey"] = cfg.get("start_hotkey", "ctrl+alt+-")
    cfg["stop_hotkey"] = cfg.get("stop_hotkey", "ctrl+alt+=")
    return cfg

def load_config(config_path=None):
    """Load configuration from JSON file."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    config_path = config_path or os.path.join(base, 'config.json')
    if not os.path.exists(config_path):
        logging.error(f'Configuration file not found at path: {config_path}')
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, 'r') as f:
        cfg = json.load(f)
    config_dir = os.path.dirname(config_path)
    return normalize_config(cfg, config_dir)

def find_accept_button(template_path, threshold=0.8, region=None, debug=False, enable_multiscale=True):
    """Find and click the 'Accept' button with multi-scale matching."""
    try:
        screenshot = pyautogui.screenshot(region=region)
        screenshot = np.array(screenshot)
        screenshot_gray = cv2.cvtColor(screenshot, cv2.COLOR_RGB2GRAY)

        if not os.path.exists(template_path):
            logging.error(f'Template image not found at path: {template_path}')
            return False
    
        template = cv2.imread(template_path, 0)
        if template is None:
            logging.error(f'Failed to read template image: {template_path}')
            return False
        
        best_match = None
        best_val = threshold
        best_scale = 1.0
        
        scales = [1.0]
        if enable_multiscale:
            scales = [0.8, 0.9, 1.0, 1.1, 1.2]
        
        for scale in scales:
            if scale != 1.0:
                scaled_template = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            else:
                scaled_template = template
            
            w, h = scaled_template.shape[::-1]
            
            if w > screenshot_gray.shape[1] or h > screenshot_gray.shape[0]:
                continue
            
            res = cv2.matchTemplate(screenshot_gray, scaled_template, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            
            if max_val > best_val:
                best_val = max_val
                best_match = max_loc
                best_scale = scale
                best_w, best_h = w, h

        if best_match is not None:
            offset_x, offset_y = (region[0], region[1]) if region else (0, 0)
            click_x = int(best_match[0] + best_w/2 + offset_x)
            click_y = int(best_match[1] + best_h/2 + offset_y)
            pyautogui.click(click_x, click_y)
            logging.info(f"Accept button found at ({click_x},{click_y}) with scale {best_scale:.2f} and confidence {best_val:.2f}, clicked!")
            
            if debug:
                cv2.rectangle(screenshot, best_match, (best_match[0] + best_w, best_match[1] + best_h), (0, 255, 0), 2)
                cv2.imshow("Matched Area", screenshot)
                cv2.waitKey(0)
                cv2.destroyAllWindows()
            
            return True
        
        logging.info("Accept button not found.")
        return False

    except Exception as e:
        logging.exception("Error during button search")
        return False

def start_auto_accept():
    """Start the auto accept loop."""
    global is_running, config, stop_event
    retry_attempts = 0
    template_path = config['template_path']
    threshold = config.get('threshold', 0.8)
    retry_interval = config['retry_interval']
    region = config.get('region', None)
    max_retries = config.get('max_retries', 10)
    debug = config.get('debug', False)
    enable_multiscale = config.get('enable_multiscale', True)

    while is_running and not stop_event.is_set():
        if find_accept_button(template_path, threshold, region, debug, enable_multiscale):
            retry_attempts = 0
            print("Accept button found and clicked!")
            if stop_event.wait(retry_interval):
                break
        else:
            print("Accept button not found. Retrying...")
            retry_attempts += 1
            if retry_attempts >= max_retries:
                logging.error("Max retries reached. Stopping auto accept.")
                print("Max retries reached. Stopping auto accept.")
                is_running = False
                stop_event.set()
                break

            if stop_event.wait(min(10, retry_interval * retry_attempts)):
                break

def stop_auto_accept():
    """Stop the auto accept loop."""
    global is_running, stop_event
    is_running = False
    stop_event.set()
    print("Auto Accept Stopped")

def start_thread():
    """Start the auto accept process in a new thread."""
    global is_running, stop_event
    if is_running:
        messagebox.showinfo("Info", "Already running.")
        return
    if not config['template_path']:
        messagebox.showerror("Error", "Please select a template image first.")
        return
    stop_event.clear()
    is_running = True
    threading.Thread(target=start_auto_accept, daemon=True).start()

def start_auto_accept_hotkey():
    """Function to start auto-accept using hotkey."""
    if not is_running:
        print("Starting auto-accept using hotkey...")
        start_thread()

def stop_auto_accept_hotkey():
    """Function to stop auto-accept using hotkey."""
    if is_running:
        print("Stopping auto-accept using hotkey...")
        stop_auto_accept()

def setup_hotkeys():
    """Setup the hotkeys for start and stop actions."""
    global hotkey_handles
    start_hotkey = config.get('start_hotkey', 'ctrl+alt+-')
    stop_hotkey = config.get('stop_hotkey', 'ctrl+alt+=')

    try:
        for handle in hotkey_handles.values():
            if handle is not None:
                keyboard.remove_hotkey(handle)

        hotkey_handles["start"] = keyboard.add_hotkey(start_hotkey, start_auto_accept_hotkey)
        hotkey_handles["stop"] = keyboard.add_hotkey(stop_hotkey, stop_auto_accept_hotkey)
        print(f"Hotkeys set: Start ({start_hotkey}), Stop ({stop_hotkey})")
        logging.info(f"Hotkeys configured: Start ({start_hotkey}), Stop ({stop_hotkey})")
    except Exception as e:
        hotkey_handles = {"start": None, "stop": None}
        error_msg = f"Failed to setup hotkeys. You may need administrator/root permissions.\nError: {str(e)}"
        print(error_msg)
        logging.warning(f"Hotkey setup failed: {e}")
        messagebox.showwarning("Hotkey Warning", "Could not setup hotkeys. You may need to run with administrator/root permissions.\nYou can still use the GUI buttons.")

def create_gui():
    """Create the GUI for configuring settings."""
    global config

    root = tk.Tk()
    root.title("Auto Accept Configuration")

    def select_template():
        config['template_path'] = filedialog.askopenfilename(title="Select Template", filetypes=[("PNG files", "*.png")])
        template_label.config(text=f"Template: {os.path.basename(config['template_path'])}")

    def start_button_pressed():
        try:
            config['threshold'] = float(threshold_entry.get())
            config['retry_interval'] = float(retry_interval_entry.get())
            config['max_retries'] = int(max_retries_entry.get())
            setup_hotkeys()  # Setup hotkeys when starting
            start_thread()
        except ValueError:
            messagebox.showerror("Error", "Please enter valid numeric values.")

    # GUI layout
    template_button = tk.Button(root, text="Select Template", command=select_template)
    template_button.pack(pady=10)

    template_label = tk.Label(root, text="Template: Not Selected")
    template_label.pack(pady=5)

    threshold_label = tk.Label(root, text="Threshold (0-1):")
    threshold_label.pack(pady=5)
    threshold_entry = tk.Entry(root)
    threshold_entry.insert(0, str(config['threshold']))
    threshold_entry.pack(pady=5)

    retry_interval_label = tk.Label(root, text="Retry Interval (seconds):")
    retry_interval_label.pack(pady=5)
    retry_interval_entry = tk.Entry(root)
    retry_interval_entry.insert(0, str(config['retry_interval']))
    retry_interval_entry.pack(pady=5)

    max_retries_label = tk.Label(root, text="Max Retries:")
    max_retries_label.pack(pady=5)
    max_retries_entry = tk.Entry(root)
    max_retries_entry.insert(0, str(config.get('max_retries', 10)))
    max_retries_entry.pack(pady=5)

    start_button = tk.Button(root, text="Start Auto Accept", command=start_button_pressed)
    start_button.pack(pady=10)

    stop_button = tk.Button(root, text="Stop Auto Accept", command=stop_auto_accept)
    stop_button.pack(pady=10)

    root.mainloop()

def main():
    """Main function to start the GUI and load the configuration."""
    global config
    config = load_config()
    create_gui()

if __name__ == '__main__':
    main()