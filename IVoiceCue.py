import voicemeeterlib
import time
from cuesdk import CueSdk, CorsairDeviceType, CorsairDeviceFilter, CorsairError, CorsairLedColor
from pynput.keyboard import Listener, KeyCode

CHECK_INTERVAL = 0.1  # How often to check for external changes

# ---------------------------------------------------------
# COLORS
# ---------------------------------------------------------
LED_ON_COLOR = (0, 255, 0)    # Green for ON (non-gain)
LED_OFF_COLOR = (255, 0, 0)   # Red for OFF (non-gain)

BLUE = (0, 0, 255)            # If gain < origin
GREEN = (0, 255, 0)           # Exactly origin
RED = (255, 0, 0)             # Exactly end

# ---------------------------------------------------------
# STRIP CONFIGURATION (NO REORDERING)
# ---------------------------------------------------------
# key -> (strip_index, param_name, led_id, is_gain, (origin_val, end_val)) 
# 
# Gains:
#   - Below origin => BLUE
#   - At origin => GREEN
#   - In between => gradient from GREEN => RED
#   - At end => RED
#   - Above end => RED
#
# If user sets (0.0, -30.0) we now handle it in color_for_gain() so that:
#   0.0 => green
#  -30.0 => red
#
STRIP_CONFIG = {
    # Non-gain examples
    97:  (0, "B1", 116, False, None),
    98:  (0, "B2", 117, False, None),
    99:  (0, "B3", 118, False, None),
    100: (5, "A1", 113, False, None),
    101: (6, "A1", 114, False, None),
    102: (7, "A1", 115, False, None),

    # Gains
    # strip 5 => origin=0.0 (green), end=-30.0 (red)
    104: (5, "gain", 110, True, (0.0, -30.0)),

    # strip 6 => origin=0.0 (green), end=0.40 (red)
    103: (6, "gain", 109, True, (0.0, 0.40)),
    105: (6, "A3", 111, False, None),
}

# ---------------------------------------------------------
# HELPER: clamp function
# ---------------------------------------------------------
def clamp(value, min_val, max_val):
    return max(min_val, min(value, max_val))

# ---------------------------------------------------------
# HELPER: color_for_gain with reversed-range logic
# ---------------------------------------------------------
def color_for_gain(value, origin, end):
    """
    Use 'origin' as 100% green and 'end' as 100% red.
    - If origin < end (e.g. 0.0 -> 0.40), do normal forward logic
    - If origin > end (e.g. 0.0 -> -30.0), do reversed logic
    - If value < min(origin,end) => BLUE
    - If value > max(origin,end) => RED
    """
    # If range is zero, just return RED
    if origin == end:
        return RED

    if origin < end:
        # Forward range (0.0 -> 0.40)
        if value < origin:
            return BLUE
        if value > end:
            return RED

        # Between origin..end => 0 => green, 1 => red
        ratio = (value - origin) / float(end - origin)
        ratio = clamp(ratio, 0.0, 1.0)
        r = int(255 * ratio)
        g = int(255 * (1.0 - ratio))
        b = 0
        return (r, g, b)
    else:
        # Reversed range (e.g. 0.0 -> -30.0)
        # "less than end" => below range => RED
        # "greater than origin" => above range => BLUE
        if value < end:
            return RED
        if value > origin:
            return BLUE

        # Now in [end..origin], compute ratio from 0 => green, 1 => red
        # For example, origin=0, end=-30 => ratio= (value - 0)/(-30 - 0) = value / -30
        # value=0 => ratio=0 => green
        # value=-30 => ratio=1 => red
        ratio = (value - origin) / float(end - origin)
        ratio = clamp(ratio, 0.0, 1.0)
        r = int(255 * ratio)
        g = int(255 * (1.0 - ratio))
        b = 0
        return (r, g, b)

# ---------------------------------------------------------
# KeyLightingController
# ---------------------------------------------------------
class KeyLightingController:
    def __init__(self, sdk, device_id, led_ids):
        self.sdk = sdk
        self.device_id = device_id
        self.led_states = {lid: False for lid in led_ids}

    def set_color(self, led_id, state=None, gain=None, gain_range=None):
        """
        - Non-gain => ON=GREEN, OFF=RED
        - Gains => EXACT interpret (origin,end)
          plus reversed-range logic if origin > end
        """
        color = LED_OFF_COLOR  # default

        if gain is not None and gain_range is not None and len(gain_range) == 2:
            origin, end = gain_range
            color = color_for_gain(gain, origin, end)

        elif state is not None:
            color = LED_ON_COLOR if state else LED_OFF_COLOR

        try:
            self.sdk.set_led_colors(
                self.device_id,
                [CorsairLedColor(id=led_id, r=color[0], g=color[1], b=color[2], a=255)]
            )
        except Exception as e:
            print(f"[KeyLightingController] Error setting LED {led_id} color: {e}")

# ---------------------------------------------------------
# ParameterObserver
# ---------------------------------------------------------
class ParameterObserver:
    def __init__(self, vm, lighting_controller):
        self.vm = vm
        self.lighting_controller = lighting_controller

        # monitored_params: {vk: (s_idx, p_name, current_val, led_id, is_gain, rng)}
        self.monitored_params = {}
        for vk, (s_idx, p_name, led_id, is_gain, rng) in STRIP_CONFIG.items():
            current_val = getattr(self.vm.strip[s_idx], p_name)
            self.monitored_params[vk] = (s_idx, p_name, current_val, led_id, is_gain, rng)

    def initialize_leds(self):
        for vk, (s_idx, p_name, cur_val, led_id, is_gain, rng) in self.monitored_params.items():
            if is_gain and rng is not None and len(rng) == 2:
                self.lighting_controller.set_color(led_id, gain=cur_val, gain_range=rng)
            else:
                # non-gain => boolean
                self.lighting_controller.set_color(led_id, state=cur_val)

            print(f"[init] Key {vk}: Strip[{s_idx}].{p_name} => {cur_val}")

    def toggle_strip(self, vk):
        if vk not in self.monitored_params:
            return

        s_idx, p_name, cur_val, led_id, is_gain, rng = self.monitored_params[vk]

        if is_gain and rng is not None and len(rng) == 2:
            origin, end = rng
            # If current == end => go origin, else go end
            new_val = origin if cur_val == end else end
            setattr(self.vm.strip[s_idx], p_name, new_val)
            self.lighting_controller.set_color(led_id, gain=new_val, gain_range=rng)
            print(f"[toggle_gain] Key {vk}: {p_name} => {new_val}")

        else:
            # Non-gain => boolean toggle
            new_val = not bool(cur_val)
            setattr(self.vm.strip[s_idx], p_name, new_val)
            self.lighting_controller.set_color(led_id, state=new_val)
            print(f"[toggle_bool] Key {vk}: {p_name} => {new_val}")

        self.monitored_params[vk] = (s_idx, p_name, new_val, led_id, is_gain, rng)

    def check_updates(self):
        """If user toggles or modifies from Voicemeeter UI, re-sync LED color."""
        for vk, (s_idx, p_name, last_val, led_id, is_gain, rng) in self.monitored_params.items():
            cur_val = getattr(self.vm.strip[s_idx], p_name)
            if cur_val != last_val:
                if is_gain and rng is not None and len(rng) == 2:
                    self.lighting_controller.set_color(led_id, gain=cur_val, gain_range=rng)
                    print(f"[ext_gain] Key {vk}: {p_name} => {cur_val}")
                else:
                    self.lighting_controller.set_color(led_id, state=cur_val)
                    print(f"[ext_toggle] Key {vk}: {p_name} => {cur_val}")

                self.monitored_params[vk] = (s_idx, p_name, cur_val, led_id, is_gain, rng)

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    KIND_ID = "potato"

    try:
        with voicemeeterlib.api(KIND_ID, pdirty=True, ratelimit=0.05) as vm:
            print("[main] Connected to Voicemeeter.")

            # Connect to Corsair SDK
            sdk = CueSdk()
            if sdk.connect(lambda s: print(f"[CueSdk] {s}")) != CorsairError.CE_Success:
                raise RuntimeError("[main] Failed to connect to Corsair SDK")

            time.sleep(2)
            devices, err = sdk.get_devices(CorsairDeviceFilter(device_type_mask=CorsairDeviceType.CDT_Keyboard))
            if err != CorsairError.CE_Success or not devices:
                raise RuntimeError("[main] No Corsair keyboard found.")

            kb = devices[0]

            # Gather LED IDs
            led_ids = [val[2] for val in STRIP_CONFIG.values()]

            lighting = KeyLightingController(sdk, kb.device_id, led_ids)
            observer = ParameterObserver(vm, lighting)

            observer.initialize_leds()

            print("[main] No reordering or inversion needed. Gains use EXACT (origin,end).")
            print("        For strip #5 => 0.0 -> -30.0 is green->red with custom logic.")
            print("        For strip #6 => 0.0 -> 0.40 is green->red with normal logic.")

            def on_release(key):
                if isinstance(key, KeyCode):
                    vk = key.vk
                    if vk in STRIP_CONFIG:
                        observer.toggle_strip(vk)
                return True

            with Listener(on_release=on_release, suppress=False) as listener:
                while listener.running:
                    observer.check_updates()
                    time.sleep(CHECK_INTERVAL)

    except Exception as e:
        print(f"[main] Error connecting to Voicemeeter: {e}")
    finally:
        print("[main] Disconnected from Voicemeeter.")


if __name__ == "__main__":
    main()
