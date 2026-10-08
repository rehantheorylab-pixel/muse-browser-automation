import subprocess, time
# find pc_agent.py PIDs via wmic and kill them
out = subprocess.run(
    ["wmic", "process", "where", "name='pythonw.exe'",
     "get", "ProcessId,CommandLine", "/format:csv"],
    capture_output=True, text=True).stdout
killed = []
for line in out.splitlines():
    if "pc_agent.py" in line:
        # CSV: Node,CommandLine,ProcessId
        parts = line.rsplit(",", 1)
        if len(parts) == 2:
            pid = parts[1].strip()
            if pid.isdigit():
                subprocess.run(["taskkill", "/PID", pid, "/F"],
                               capture_output=True)
                killed.append(pid)
print("killed:", killed)
