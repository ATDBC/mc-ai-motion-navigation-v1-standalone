#!/bin/sh
# 审查方的空闲计时（补记 4）：T1 → T2 → T3 依次运行，不并行。
set -e
cd /tmp/claude-0/-home-user-mc-ai-motion-navigation-v1-standalone/7b6c23a8-548e-536b-b677-503d1715db15/scratchpad/main
export PYTHONPATH=.:/home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial:/home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/m1:/home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/m2
uptime > /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T1.uptime
python3 /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/m1/run_m1.py timing --per-bin 10 > /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T1.stdout 2>&1
cp /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/m1/g7b_timing_single_process.json /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T1_g7b_timing_single_process.json
uptime > /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T2.uptime
python3 /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/m2/run_m2.py perf_record --no-setd > /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T2.stdout 2>&1
mv /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/m2/performance_record.json /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T2_performance_record_AC.json
mv /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/m2/performance_record_samples.json /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T2_performance_record_samples_AC.json
uptime > /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T3.uptime
python3 /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/m2/run_m2.py perf_record --passes 1 > /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T3.stdout 2>&1
mv /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/m2/performance_record.json /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T3_performance_record_ACD.json
mv /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/m2/performance_record_samples.json /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/T3_performance_record_samples_ACD.json
date -u > /home/user/mc-ai-motion-navigation-v1-standalone/reviews/2026-10-10-d097a-trial/timing/DONE
