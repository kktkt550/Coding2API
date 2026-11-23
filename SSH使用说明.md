
# SSH 里后台启动（断开 SSH 也不会停）
cd /var/www/Coding2API && nohup uv run python -m src.main > server.log 2>&1 &

# 看实时日志
tail -f /var/www/api/server.log

# 停止
pkill -f src.main

# 确认在跑
ps aux | grep src.main | grep -v grep

# 1. 按 PID 强杀
kill -9 168 171


# nohup 只是不随 SSH 断开而死， 容器/服务器重启后不会自动拉起 。如果想开机自启，配一个 systemd 服务更稳：
cat > /etc/systemd/system/coding2api.service <<'EOF'
[Unit]
Description=Coding2API
After=network.target

[Service]
WorkingDirectory=/var/www/api
ExecStart=/root/.local/bin/uv run python -m src.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now coding2api
