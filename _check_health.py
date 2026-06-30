"""Inspect health_check's check_l2_connection"""
import sys, os
os.chdir("/root/maneki-agent")
sys.path.insert(0, ".")

# Read the function
import inspect
from scripts.health_check import check_l2_connection, preflight_check
print(inspect.getsource(check_l2_connection))
print("="*50)
# Get the updated preflight
src = inspect.getsource(preflight_check)
# Find the L2 part
lines = src.split("\n")
for i, line in enumerate(lines):
    if 'l2' in line.lower() or 'jvquant' in line.lower():
        print(f"L{i}: {line}")
