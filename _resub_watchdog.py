"""Re-subscribe watchdog stocks to L2 daemon"""
import sys
sys.path.insert(0, "/root/maneki-agent")
from scripts.l2_daemon_client import daemon_cmd

codes = ["001896.SZ", "601101.SH"]
resp = daemon_cmd(f"SUB {' '.join(codes)}")
print("Subscribe:", resp)

resp2 = daemon_cmd("SUBSCRIBED")
print("Subscribed:", resp2)
