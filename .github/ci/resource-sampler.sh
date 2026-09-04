#!/bin/bash
# Sample whole-VM resource usage every 10s: CPU %, used memory, /dev/shm,
# load average, and the total RSS of docker containers. One line per tick.
echo "utc cpu_pct mem_used_mb shm_used_mb load1 docker_rss_mb"
prev_idle=0; prev_total=0
while :; do
  read -r _ user nice system idle iowait irq softirq steal _ < /proc/stat
  total=$((user+nice+system+idle+iowait+irq+softirq+steal))
  didle=$((idle-prev_idle)); dtotal=$((total-prev_total))
  pct=0
  [ "$dtotal" -gt 0 ] && pct=$(( 100*(dtotal-didle)/dtotal ))
  prev_idle=$idle; prev_total=$total
  mem=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print int((t-a)/1024)}' /proc/meminfo)
  shm=$(df -m /dev/shm 2>/dev/null | awk 'NR==2{print $3}')
  load=$(cut -d' ' -f1 /proc/loadavg)
  drss=$(docker stats --no-stream --format '{{.MemUsage}}' 2>/dev/null \
    | awk '{v=$1; if (v ~ /GiB/) s+=v*1024; else if (v ~ /MiB/) s+=v+0; else if (v ~ /KiB/) s+=v/1024} END {print int(s)}')
  echo "$(date -u +%H:%M:%S) $pct $mem ${shm:-0} $load ${drss:-0}"
  sleep 10
done
