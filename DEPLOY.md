# WebChat Deployment Guide

## Quick Deploy

```bash
# 1. Git clone to target machine
 git clone https://github.com/super123dog-dev/openclaw-webchat.git

# 2. Start service (optional: use systemd)
sudo cp webchat/webchat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable webchat
sudo systemctl start webchat

# 4. Add to gateway tab from openclaw.json
"http": {
  "endpoints": {
    "chatCompletions": {
      "enabled": true
    }
  }
},

# OR run manually:
cd /webchat
python3 server.py &
```

## Configuration

- Port: 8080 (edit server.py to change)
- Sessions stored in: /webchat/sessions.json
- Users configured in: /webchat/users.json

## Update OpenClaw Gateway Port

If your OpenClaw gateway runs on a different port, edit server.py and change:
```python
gw_port = 18789  # Change to your gateway port
```
