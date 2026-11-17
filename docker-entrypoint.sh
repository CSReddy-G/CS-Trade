#!/bin/sh
# Refresh the code in /data from the image, keeping settings, token, logs and trades, then start.
set -e
cp -a /opt/kitebot/. /data/
cd /data
exec python app.py --no-browser
