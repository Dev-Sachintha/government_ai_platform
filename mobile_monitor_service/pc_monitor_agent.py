"""Opt-in Windows/Linux PC telemetry agent for Government AI Platform.
Run locally on each computer the owner has agreed to monitor.
"""
import argparse
import os
import platform
import socket
import subprocess
import time
import uuid
from datetime import datetime, timezone

import psutil
import requests


def gpu_stats():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip():
            row = [x.strip() for x in result.stdout.strip().splitlines()[0].split(",")]
            if len(row) >= 5:
                return {"name": row[0], "usage_percent": float(row[1]),
                        "memory_used_mb": float(row[2]), "memory_total_mb": float(row[3]),
                        "temperature_c": float(row[4])}
    except Exception:
        pass
    # Windows fallback for Intel/AMD and other adapters: GPU Engine performance counters.
    if platform.system() == "Windows":
        try:
            command = r"""$g=Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop; $v=@($g.CounterSamples | ForEach-Object {[math]::Round($_.CookedValue,1)}); $name=(Get-CimInstance Win32_VideoController | Select-Object -First 1 -ExpandProperty Name); [pscustomobject]@{name=$name;usage_percent=[math]::Min(100,(($v | Measure-Object -Maximum).Maximum))} | ConvertTo-Json -Compress"""
            p = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                               capture_output=True, text=True, timeout=5)
            if p.returncode == 0 and p.stdout.strip():
                import json
                return json.loads(p.stdout)
        except Exception:
            pass
    return None


def temperatures():
    try:
        values = psutil.sensors_temperatures(fahrenheit=False) or {}
        for entries in values.values():
            for entry in entries:
                if entry.current is not None:
                    return float(entry.current)
    except (AttributeError, OSError):
        pass
    return None


def recent_errors():
    if platform.system() != "Windows":
        return []
    command = (
        "Get-WinEvent -FilterHashtable @{LogName='System','Application'; Level=2; StartTime=(Get-Date).AddHours(-24)} "
        "-MaxEvents 8 -ErrorAction SilentlyContinue | "
        "Select-Object TimeCreated,LogName,ProviderName,Id,Message | ConvertTo-Json -Compress"
    )
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                           capture_output=True, text=True, timeout=8)
        import json
        data = json.loads(p.stdout) if p.stdout.strip() else []
        if isinstance(data, dict):
            data = [data]
        return [{"time": str(x.get("TimeCreated", "")), "log": str(x.get("LogName", "")),
                 "source": str(x.get("ProviderName", "")), "event_id": x.get("Id"),
                 "message": str(x.get("Message", ""))[:700]} for x in data[:8]]
    except Exception:
        return []



def _internet_reachable():
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=1.5):
            return True
    except OSError:
        return False


def snapshot(previous_net):
    cpu = psutil.cpu_percent(interval=0.5)
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\" if platform.system() == "Windows" else "/")
    net = psutil.net_io_counters()
    now = time.time()
    down = up = 0.0
    if previous_net:
        elapsed = max(0.1, now - previous_net[0])
        down = max(0, net.bytes_recv - previous_net[1]) / elapsed
        up = max(0, net.bytes_sent - previous_net[2]) / elapsed
    processes = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
        try:
            info = p.info
            processes.append({"pid": info.get("pid"), "name": info.get("name") or "Unknown",
                              "cpu_percent": round(info.get("cpu_percent") or 0, 1),
                              "memory_percent": round(info.get("memory_percent") or 0, 1),
                              "status": info.get("status") or "unknown"})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    processes.sort(key=lambda x: x["cpu_percent"], reverse=True)
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(), "platform": platform.platform(),
        "cpu_percent": cpu, "cpu_count": psutil.cpu_count(logical=True),
        "ram_percent": vm.percent, "ram_used_mb": round(vm.used / 1048576),
        "ram_total_mb": round(vm.total / 1048576), "disk_percent": disk.percent,
        "disk_used_gb": round(disk.used / 1073741824, 2),
        "disk_total_gb": round(disk.total / 1073741824, 2),
        "cpu_temperature_c": temperatures(), "gpu": gpu_stats(),
        "download_bytes_per_sec": round(down, 1), "upload_bytes_per_sec": round(up, 1),
        "download_total_mb": round(net.bytes_recv / 1048576, 2),
        "upload_total_mb": round(net.bytes_sent / 1048576, 2),
        "internet_connected": _internet_reachable(),
        "processes": processes[:12], "recent_errors": recent_errors(),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
    }, (now, net.bytes_recv, net.bytes_sent)


def main():
    parser = argparse.ArgumentParser(description="Consent-based PC monitoring agent")
    parser.add_argument("--url", default=os.getenv("MOBILE_MONITOR_API_URL"), help="Monitor service base URL")
    parser.add_argument("--key", default=os.getenv("MOBILE_MONITOR_API_KEY"), help="Same pairing key as the dashboard")
    parser.add_argument("--name", default=socket.gethostname(), help="Friendly device name")
    parser.add_argument("--interval", type=int, default=5, help="Update interval in seconds")
    args = parser.parse_args()
    if not args.url or not args.key:
        parser.error("Provide --url and --key, or set MOBILE_MONITOR_API_URL and MOBILE_MONITOR_API_KEY")
    device_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname() + platform.node()))
    headers = {"X-Monitor-Key": args.key}
    previous = None
    print(f"Monitoring started for {args.name}. Press Ctrl+C to stop.")
    print("Only system metrics, process names and recent error summaries are sent; no screen, audio or remote control.")
    while True:
        try:
            data, previous = snapshot(previous)
            payload = {"device_id": device_id, "device_name": args.name, "kind": "pc", "metrics": data}
            response = requests.post(args.url.rstrip("/") + "/v1/pc/metrics", json=payload,
                                     headers=headers, timeout=10)
            response.raise_for_status()
            print(f"Sent: CPU {data['cpu_percent']}% | RAM {data['ram_percent']}% | Disk {data['disk_percent']}%")
        except KeyboardInterrupt:
            print("Monitoring stopped by user.")
            break
        except Exception as exc:
            print(f"Could not send metrics: {exc}")
        time.sleep(max(3, args.interval))


if __name__ == "__main__":
    main()
