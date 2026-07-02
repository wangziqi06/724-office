#!/bin/bash
# 每日 v2.db 一致快照备份：先 wal_checkpoint(TRUNCATE) 把 WAL 落主库，再 cp。
# 保留 7 天（按星期几 1-7 轮转覆盖）。由 crontab 04:17 触发。
# 备份含全部对话/ESM/记忆等隐私数据，umask 077 + chmod 600 收紧到仅 owner 可读（同主机其它账户读不到）。
umask 077
cd /opt/xiaowang-v2 || exit 1
/usr/local/bin/node -e "const{DatabaseSync}=require('node:sqlite');const d=new DatabaseSync('/opt/xiaowang-v2/v2.db');d.exec('PRAGMA busy_timeout=10000');d.prepare('PRAGMA wal_checkpoint(TRUNCATE)').get();"
cp /opt/xiaowang-v2/v2.db "/opt/xiaowang-v2/v2.db.bak.$(date +%u)"
chmod 600 "/opt/xiaowang-v2/v2.db.bak.$(date +%u)"
echo "[backup] $(date '+%F %T') -> v2.db.bak.$(date +%u)"
