"""
Deploy EV charging helpers and automations to Home Assistant.

Usage:
    python deploy.py --token YOUR_HA_TOKEN [--host 192.168.68.88] [--port 8123]

What this script does:
    1. Connects to HA WebSocket API
    2. Creates all input_* helpers (idempotent – skips if already exist)
    3. Creates / updates the three HA automations
    4. Prints pyscript deployment instructions
"""

import argparse
import asyncio
import json
import sys
import os

try:
    import websockets
except ImportError:
    print("Installing websockets...")
    os.system(f"{sys.executable} -m pip install websockets -q")
    import websockets

HA_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJhN2JmMGIxNWY5NWE0M2U2YTBmZDQ1MjBmMjNiNDlmMCIsImlhdCI6MTc2NjI2NDEwNiwiZXhwIjoyMDgxNjI0MTA2fQ"
    ".D5dimshN8LYs2fGX3Z8Y_NfJr16xdQofd97g8n1WZDE"
)

# ── Helper definitions ──────────────────────────────────────────────────────────
INPUT_NUMBERS = [
    {
        "name": "EV Target SOC",
        "object_id": "ev_target_soc",
        "min": 50, "max": 100, "step": 5, "initial": 90,
        "unit_of_measurement": "%", "mode": "slider",
        "icon": "mdi:battery-charging-90",
    },
    {
        "name": "EV Max House Current",
        "object_id": "ev_max_house_current",
        "min": 10, "max": 20, "step": 1, "initial": 18,
        "unit_of_measurement": "A", "mode": "slider",
        "icon": "mdi:current-ac",
    },
    {
        "name": "EV Cheap Price Threshold",
        "object_id": "ev_cheap_price_threshold",
        "min": 0.0, "max": 5.0, "step": 0.05, "initial": 0.80,
        "unit_of_measurement": "SEK/kWh", "mode": "box",
        "icon": "mdi:cash-clock",
    },
    {
        "name": "EV Charging Current Setpoint",
        "object_id": "ev_charging_current_setpoint",
        "min": 0, "max": 16, "step": 1, "initial": 0,
        "unit_of_measurement": "A", "mode": "box",
        "icon": "mdi:lightning-bolt",
    },
    {
        "name": "EV Phase Switch Hysteresis",
        "object_id": "ev_phase_switch_hysteresis_min",
        "min": 1, "max": 30, "step": 1, "initial": 5,
        "unit_of_measurement": "min", "mode": "box",
        "icon": "mdi:timer-sand",
    },
]

INPUT_BOOLEANS = [
    {
        "name": "EV Smart Charging Enabled",
        "object_id": "ev_smart_charging_enabled",
        "icon": "mdi:ev-station",
        "initial": True,
    },
]

INPUT_DATETIMES = [
    {
        "name": "EV Charge Deadline",
        "object_id": "ev_charge_deadline",
        "has_date": False,
        "has_time": True,
        "initial": "06:00:00",
        "icon": "mdi:clock-end",
    },
]

INPUT_SELECTS = [
    {
        "name": "EV Charging Mode",
        "object_id": "ev_charging_mode",
        "options": ["3-phase", "1-phase-p1", "1-phase-p2", "1-phase-p3", "paused", "disconnected"],
        "initial": "disconnected",
        "icon": "mdi:ev-plug-type2",
    },
]

# ── Automations ─────────────────────────────────────────────────────────────────
AUTOMATIONS = [
    {
        "alias": "EV – Enable smart charging at night",
        "description": "Enables smart charging at 22:00 if car is connected.",
        "triggers": [{"trigger": "time", "at": "22:00:00"}],
        "conditions": [
            {
                "condition": "state",
                "entity_id": "sensor.gpn007772_charger_mode",
                "state": ["connected_charging", "connected_requesting"],
            }
        ],
        "actions": [
            {"action": "input_boolean.turn_on",
             "target": {"entity_id": "input_boolean.ev_smart_charging_enabled"}},
            {"action": "logbook.log",
             "data": {"name": "EV Charger", "message": "Smart charging enabled for the night."}},
        ],
        "mode": "single",
    },
    {
        "alias": "EV – Disable smart charging in the morning",
        "description": "Turns off smart charging at 06:00 and restores max current.",
        "triggers": [{"trigger": "time", "at": "06:00:00"}],
        "conditions": [],
        "actions": [
            {"action": "input_boolean.turn_off",
             "target": {"entity_id": "input_boolean.ev_smart_charging_enabled"}},
            {"action": "zaptec.limit_current",
             "data": {
                 "installation_id": "8180b165-484b-47e0-9dc4-eb2630ae0dad",
                 "available_current": 16,
             }},
            {"action": "logbook.log",
             "data": {"name": "EV Charger", "message": "Smart charging ended. Charger restored to 16A."}},
        ],
        "mode": "single",
    },
    {
        "alias": "EV – Deadline warning at 03:00",
        "description": "Sends notification at 03:00 if car won't be fully charged by 06:00.",
        "triggers": [{"trigger": "time", "at": "03:00:00"}],
        "conditions": [
            {"condition": "template",
             "value_template": (
                 "{{ states('sensor.gpn007772_charger_mode') in "
                 "['connected_charging', 'connected_requesting'] and "
                 "states('sensor.volvo_ex30_battery') | float(0) < "
                 "states('input_number.ev_target_soc') | float(90) }}"
             )},
        ],
        "actions": [
            {
                "action": "persistent_notification.create",
                "data": {
                    "title": "⚡ EV Charging Warning",
                    "message": (
                        "Car at {{ states('sensor.volvo_ex30_battery') }}% "
                        "(target {{ states('input_number.ev_target_soc') | int }}%). "
                        "Estimated time remaining: "
                        "{{ states('sensor.volvo_ex30_estimated_charging_time') }} min."
                    ),
                    "notification_id": "ev_deadline_warning",
                },
            }
        ],
        "mode": "single",
    },
]


# ── WebSocket helper ────────────────────────────────────────────────────────────

async def ws_command(ws, msg: dict) -> dict:
    """Send a WebSocket command and return the response."""
    await ws.send(json.dumps(msg))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == msg.get("id"):
            return resp


async def deploy(host: str, port: int, token: str):
    url = f"ws://{host}:{port}/api/websocket"
    msg_id = 1

    print(f"\n🔌 Connecting to {url} ...")
    async with websockets.connect(url) as ws:
        # Auth
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_resp = json.loads(await ws.recv())
        if auth_resp.get("type") != "auth_ok":
            print("❌ Authentication failed")
            return
        print("✅ Authenticated\n")

        # ── Create input_number helpers ─────────────────────────────────────
        print("📊 Creating input_number helpers...")
        msg_id += 1
        await ws.send(json.dumps({"id": msg_id, "type": "input_number/list"}))
        resp = await ws_command(ws, {"id": msg_id, "type": "input_number/list"})
        existing_numbers = {item["id"] for item in resp.get("result", [])}
        msg_id += 1

        for h in INPUT_NUMBERS:
            if h["object_id"] in existing_numbers:
                print(f"  ✅ input_number.{h['object_id']}: already exists")
                continue
            msg = {
                "id": msg_id, "type": "input_number/create",
                "name": h["name"],
                "min": h["min"], "max": h["max"], "step": h["step"],
                "initial": h["initial"],
                "unit_of_measurement": h.get("unit_of_measurement", ""),
                "mode": h.get("mode", "slider"),
                "icon": h.get("icon", ""),
            }
            resp = await ws_command(ws, msg)
            msg_id += 1
            status = "✅" if resp.get("success") else "❌"
            print(f"  {status} input_number.{h['object_id']}: {h['name']}")

        # ── Create input_boolean helpers ────────────────────────────────────
        print("\n🔘 Creating input_boolean helpers...")
        resp = await ws_command(ws, {"id": msg_id, "type": "input_boolean/list"})
        existing_booleans = {item["id"] for item in resp.get("result", [])}
        msg_id += 1
        for h in INPUT_BOOLEANS:
            if h["object_id"] in existing_booleans:
                print(f"  ✅ input_boolean.{h['object_id']}: already exists")
                continue
            msg = {
                "id": msg_id, "type": "input_boolean/create",
                "name": h["name"],
                "initial": h["initial"],
                "icon": h.get("icon", ""),
            }
            resp = await ws_command(ws, msg)
            msg_id += 1
            status = "✅" if resp.get("success") else "❌"
            print(f"  {status} input_boolean.{h['object_id']}: {h['name']}")

        # ── Create input_datetime helpers ───────────────────────────────────
        print("\n🕐 Creating input_datetime helpers...")
        resp = await ws_command(ws, {"id": msg_id, "type": "input_datetime/list"})
        existing_datetimes = {item["id"] for item in resp.get("result", [])}
        msg_id += 1
        for h in INPUT_DATETIMES:
            if h["object_id"] in existing_datetimes:
                print(f"  ✅ input_datetime.{h['object_id']}: already exists")
                continue
            msg = {
                "id": msg_id, "type": "input_datetime/create",
                "name": h["name"],
                "has_date": h["has_date"],
                "has_time": h["has_time"],
                "icon": h.get("icon", ""),
            }
            resp = await ws_command(ws, msg)
            msg_id += 1
            status = "✅" if resp.get("success") else "❌"
            print(f"  {status} input_datetime.{h['object_id']}: {h['name']}")

        # Set deadline to 06:00 (initial not supported by create API)
        await ws_command(ws, {
            "id": msg_id, "type": "call_service",
            "domain": "input_datetime", "service": "set_datetime",
            "service_data": {"entity_id": "input_datetime.ev_charge_deadline", "time": "06:00:00"},
        })
        msg_id += 1
        print("    (deadline set to 06:00)")

        # ── Create input_select helpers ─────────────────────────────────────
        print("\n📋 Creating input_select helpers...")
        resp = await ws_command(ws, {"id": msg_id, "type": "input_select/list"})
        existing_selects = {item["id"] for item in resp.get("result", [])}
        msg_id += 1
        for h in INPUT_SELECTS:
            if h["object_id"] in existing_selects:
                print(f"  ✅ input_select.{h['object_id']}: already exists")
                continue
            msg = {
                "id": msg_id, "type": "input_select/create",
                "name": h["name"],
                "options": h["options"],
                "initial": h["initial"],
                "icon": h.get("icon", ""),
            }
            resp = await ws_command(ws, msg)
            msg_id += 1
            status = "✅" if resp.get("success") else "❌"
            print(f"  {status} input_select.{h['object_id']}: {h['name']}")

    # ── Create automations via REST API ────────────────────────────────────────
    print("\n⚙️  Creating automations via REST API...")
    import urllib.request, urllib.error, time as _time
    for auto in AUTOMATIONS:
        # HA requires a unique timestamp-based ID in the URL
        auto_id = str(int(_time.time() * 1000))
        _time.sleep(0.01)  # ensure unique IDs
        data = json.dumps(auto).encode()
        req = urllib.request.Request(
            f"http://{host}:{port}/api/config/automation/config/{auto_id}",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                print(f"  ✅ {auto['alias']} (id: {auto_id})")
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"  ⚠️  {auto['alias']}: {e.code} {body[:100]}")

    # ── Pyscript deployment instructions ────────────────────────────────────
    print("\n" + "═" * 60)
    print("📁 PYSCRIPT DEPLOYMENT (manual step required)")
    print("═" * 60)
    print("""
pyscript is not yet installed. Steps:

1. Install pyscript via HACS:
   HA → HACS → Integrations → Search "pyscript" → Install
   (by: David Bomba / custom-components/pyscript)

2. Add to configuration.yaml:
   pyscript:
     allow_all_imports: true

3. Create the pyscript folder and copy files:
   /config/pyscript/algorithm.py           ← from pyscript/algorithm.py
   /config/pyscript/ev_charge_controller.py ← from pyscript/ev_charge_controller.py

   Via HA File Editor add-on, SSH, or Samba share.

4. Restart Home Assistant (Developer Tools → Restart)

5. Verify in HA logs:
   Search for "EV Charge Controller started"
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy EV charging to Home Assistant")
    parser.add_argument("--host", default="192.168.68.88")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--token", default=HA_TOKEN)
    args = parser.parse_args()

    asyncio.run(deploy(args.host, args.port, args.token))
