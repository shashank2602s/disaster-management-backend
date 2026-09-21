"""
Sends fake sensor readings from several nodes to your backend.
No installs needed (uses only Python's standard library).

Usage:
  python simulate_nodes.py --url https://YOUR-APP.onrender.com --key YOUR_BACKEND_API_KEY
Options:
  --nodes 3        number of fake nodes
  --interval 2     seconds between rounds
  --count 0        rounds to send (0 = run until Ctrl+C)
"""
import argparse
import json
import random
import time
import urllib.error
import urllib.request

# Placeholder coordinates (centre of India). Change to your project area.
BASE_LAT, BASE_LON = 20.5937, 78.9629


def severity_of(s):
    if s >= 0.85: return "critical"
    if s >= 0.65: return "high"
    if s >= 0.40: return "medium"
    if s >= 0.20: return "low"
    return "informational"


def make_reading(i, state):
    rain = max(0.0, state["rain"] + random.uniform(-3, 4))
    state["rain"] = min(rain, 90)
    soil = min(100.0, max(0.0, state["soil"] + (rain - 15) * 0.05 + random.uniform(-1, 1)))
    state["soil"] = soil
    temp = random.uniform(24, 42)
    hum = random.uniform(30, 90)
    mq2 = random.uniform(150, 700)
    vib = random.uniform(0, 0.15)
    landslide = min(1.0, (rain / 90) * 0.5 + (soil / 100) * 0.4 + vib)
    fire = min(1.0, max(0.0, (temp - 30) / 15 * 0.5 + (mq2 - 250) / 500 * 0.5))
    flood = min(1.0, rain / 80)
    air = random.uniform(0.0, 0.5)
    scores = {"flood": flood, "fire": fire, "air": air, "landslide": landslide}
    driver = max(scores, key=scores.get)
    ai = scores[driver]
    return {
        "node_id": f"node-{i+1}",
        "lat": BASE_LAT + i * 0.01 + random.uniform(-0.001, 0.001),
        "lon": BASE_LON + i * 0.01 + random.uniform(-0.001, 0.001),
        "temperature_c": round(temp, 1),
        "humidity_pct": round(hum, 1),
        "pressure_hpa": round(random.uniform(1000, 1015), 1),
        "mq2_raw": round(mq2, 0),
        "mq135_raw": round(random.uniform(150, 600), 0),
        "soil_moisture_pct": round(soil, 1),
        "rainfall_mm_h": round(rain, 1),
        "vibration_rms": round(vib, 3),
        "flood_score": round(flood, 2),
        "fire_score": round(fire, 2),
        "air_score": round(air, 2),
        "landslide_score": round(landslide, 2),
        "ai_score": round(ai, 2),
        "severity": severity_of(ai),
        "driver": driver,
    }


def post(url, key, payload):
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v1/telemetry",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--nodes", type=int, default=3)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--count", type=int, default=0)
    a = ap.parse_args()

    states = [{"rain": random.uniform(0, 20), "soil": random.uniform(20, 60)} for _ in range(a.nodes)]
    n = 0
    try:
        while a.count == 0 or n < a.count:
            for i in range(a.nodes):
                r = make_reading(i, states[i])
                try:
                    res = post(a.url, a.key, r)
                    print(f"{r['node_id']} sev={r['severity']:<13} driver={r['driver']:<9} -> {res}")
                except urllib.error.HTTPError as e:
                    print(f"{r['node_id']} HTTP {e.code}: {e.read().decode()[:200]}")
                except Exception as e:
                    print(f"{r['node_id']} error: {e}")
            n += 1
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
