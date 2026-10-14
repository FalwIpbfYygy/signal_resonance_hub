#!/bin/bash
set -u
LOG_ROOT="/home/ubuntu/logs"
VENV_PY="/home/ubuntu/gold/venv_proj/bin/python3"
TRAE_DIR="/home/ubuntu/TRAE"
HOME_DIR="/home/ubuntu"
PROJ_DIR="/home/ubuntu/zhizu_cesuan"
HUNT_DIR="/home/ubuntu/hunting_system"
mkdir -p "${LOG_ROOT}"
# ---------- 终止旧进程（加入 new_price/main.py + daily_push.py + TRADING_AGENT/main.py） ----------
for pat in master.py price_action_main.py ICT_main.py high_main.py report.py jin10_monitor.py resonance.py support_resistance_report.py live_signal.py new_price/main.py daily_push.py TRADING_AGENT/main.py; do
    pkill -f "${pat}" 2>/dev/null
done
# 最多等 10 秒，确认进程全部退出；超时用 -9 强杀
deadline=$(( $(date +%s) + 10 ))
while pgrep -f "master.py|price_action_main.py|ICT_main.py|high_main.py|report.py|jin10_monitor.py|resonance.py|support_resistance_report.py|live_signal.py|new_price/main.py|daily_push.py|TRADING_AGENT/main.py" >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        pkill -9 -f "master.py|price_action_main.py|ICT_main.py|high_main.py|report.py|jin10_monitor.py|resonance.py|support_resistance_report.py|live_signal.py|new_price/main.py|daily_push.py|TRADING_AGENT/main.py"
        sleep 1
        break
    fi
    sleep 1
done
# ---------- 清空日志（logs 目录 + 家目录的 jin10_monitor.log） ----------
find "${LOG_ROOT}" -type f -exec truncate -s 0 {} \;
truncate -s 0 "${HOME_DIR}/jin10_monitor.log" 2>/dev/null || true
echo "[$(date +'%Y-%m-%d %H:%M:%S')] 重启前置：日志已清空"
# ---------- 启动 TRAE 目录下业务脚本 ----------
cd "${TRAE_DIR}" || { echo "TRAE 目录不存在: ${TRAE_DIR}"; exit 1; }
nohup "${VENV_PY}" core_code/master.py            > "${LOG_ROOT}/master.log"            2>&1 &
nohup "${VENV_PY}" Al_Brookss/price_action_main.py > "${LOG_ROOT}/price_action.log"     2>&1 &
nohup "${VENV_PY}" ICT/ICT_main.py                > "${LOG_ROOT}/ict_main.log"          2>&1 &
nohup "${VENV_PY}" high_winss/high_main.py        > "${LOG_ROOT}/high_win.log"          2>&1 &
nohup "${VENV_PY}" report.py                      > "${LOG_ROOT}/report.log"            2>&1 &
nohup "${VENV_PY}" resonance.py                   > "${LOG_ROOT}/resonance.log"         2>&1 &
nohup "${VENV_PY}" support_resistance_report.py  > "${LOG_ROOT}/support_resistance.log" 2>&1 &
# ========== 新增：启动 new_price/main.py ==========
# 进入 TRAE/new_price 目录运行
cd "${TRAE_DIR}/new_price" || { echo "new_price目录不存在"; exit 1; }
nohup "${VENV_PY}" main.py > "${LOG_ROOT}/new_price_main.log" 2>&1 &
# =================================================
# ---------- jin10 监控（家目录） ----------
cd "${HOME_DIR}" || exit 1
nohup "${VENV_PY}" jin10_monitor.py               > "${LOG_ROOT}/jin10_monitor.log"     2>&1 &
# ---------- 启动 zhizu_cesuan 实时信号 live_signal.py ----------
cd "${PROJ_DIR}" || { echo "zhizu_cesuan目录不存在: ${PROJ_DIR}"; exit 1; }
nohup "${VENV_PY}" live_signal.py                 > "${LOG_ROOT}/zhizu_live.log"        2>&1 &
# ---------- 新增：hunting_system daily_push.py ----------
cd "${HUNT_DIR}" || { echo "hunting_system目录不存在: ${HUNT_DIR}"; exit 1; }
nohup "${VENV_PY}" daily_push.py                  > "${LOG_ROOT}/daily_push.log"        2>&1 &
# ========== 新增：启动  Trading Decision Agent（常驻 serve；每日 22:00 由 cron 调 report 推交割单）==========
# 用绝对路径启动，确保进程命令行含 "TRADING_AGENT/main.py"，上面的 pkill/pgrep 模式才能可靠匹配
nohup "${VENV_PY}" /home/ubuntu/TRADING_AGENT/main.py serve > "${LOG_ROOT}/agent_serve.log" 2>&1 &
# ============================================================================================================
# 等 2 秒后打印存活状态，方便确认启动成功
sleep 2
echo "[$(date +'%Y-%m-%d %H:%M:%S')] 启动完成，当前进程："
pgrep -af "master.py|price_action_main.py|ICT_main.py|high_main.py|report.py|jin10_monitor.py|resonance.py|support_resistance_report.py|live_signal.py|new_price/main.py|daily_push.py|TRADING_AGENT/main.py" || echo "⚠️ 未检测到任何策略进程"
