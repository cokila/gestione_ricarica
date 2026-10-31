#!/usr/bin/env bash
# Run through sudo from the desktop app; service code is copied into a root-owned directory.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo 'Apri Gestione ricarica per autenticare questa installazione.' >&2
    exit 1
fi
mode=${1:-}
if [[ "$mode" != enable && "$mode" != disable ]]; then
    echo 'Uso: install-extreme-power.sh enable|disable' >&2
    exit 1
fi
unit=macbook-extreme-power.service
if [[ "$mode" == disable ]]; then
    if [[ -f /etc/systemd/system/$unit ]]; then
        /usr/bin/systemctl disable --now "$unit"
        if [[ -f /run/macbook-extreme-power/restore.json ]]; then
            echo 'Ripristino hardware incompleto. Controlla journalctl -u macbook-extreme-power.' >&2
            exit 1
        fi
    fi
    exit 0
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -f /etc/systemd/system/$unit ]]; then
    /usr/bin/systemctl stop "$unit"
    if [[ -f /run/macbook-extreme-power/restore.json ]]; then
        echo 'Ripristino precedente incompleto. Aggiornamento interrotto: controlla journalctl -u macbook-extreme-power.' >&2
        exit 1
    fi
fi
/usr/bin/install -d -o root -g root -m 755 /usr/local/lib/macbook-charge
/usr/bin/install -o root -g root -m 644 "$source_dir/extreme-power.py" /usr/local/lib/macbook-charge/extreme-power.py
cat > "/etc/systemd/system/$unit" <<'SERVICE'
[Unit]
Description=Risparmio energetico intelligente MacBook a batteria
After=systemd-udevd.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/lib/macbook-charge/extreme-power.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
RuntimeDirectory=macbook-extreme-power
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
UMask=0022
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
SERVICE
/usr/bin/chmod 644 "/etc/systemd/system/$unit"
/usr/bin/chown root:root "/etc/systemd/system/$unit"
/usr/bin/systemctl daemon-reload
/usr/bin/rm -f /run/macbook-extreme-power/status.json
/usr/bin/systemctl enable --now "$unit"
/usr/bin/systemctl is-active --quiet "$unit"
/usr/bin/python3 - <<'PYTHON'
import json
from pathlib import Path
import sys
import time

status = Path('/run/macbook-extreme-power/status.json')
for attempt in range(30):
    if status.exists():
        result = json.loads(status.read_text())
        if result.get('complete') and result.get('profile') == 'balanced-rapl-v2':
            break
        print('Servizio avviato, ma controlli hardware incompleti: '
              + '; '.join(result.get('errors', [])), file=sys.stderr)
        sys.exit(1)
    time.sleep(0.2)
else:
    sys.exit('Il servizio non ha confermato lo stato. Controlla journalctl -u macbook-extreme-power.')
PYTHON
echo 'Profilo prestazioni e autonomia installato. Controlla Consumi per i controlli effettivamente applicati.'
