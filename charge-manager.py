execute failed:
    Could not start serviceManager:
        Cannot locate HTTP port of service:
            Request for service HTTP address failed:
                Cannot connect to ipc via "/tmp/state-ipc/state-release.sock":
                    'dial unix /tmp/state-ipc/state-release.sock: connect: operation not permitted':
                        'connect: operation not permitted': operation not permitted
#!/usr/bin/env python3
"""Charge cap, automatic smart battery saving and low-battery notification."""

import argparse
import csv
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

APP_DIR = Path(__file__).resolve().parent
HELPER = APP_DIR / 'macbook-charge-limit'
POWER_INSTALLER = APP_DIR / 'install-extreme-power.sh'
CONFIG_DIR = Path.home() / '.config' / 'macbook-charge'
CONFIG_FILE = CONFIG_DIR / 'settings.json'
STATE_DIR = Path.home() / '.local' / 'state' / 'macbook-charge'
POWER_DIR = Path('/sys/class/power_supply')
POWER_STATUS = Path('/run/macbook-extreme-power/status.json')
PROFILE = 'balanced-rapl-v2'


def read_power_status():
    try:
        status = json.loads(POWER_STATUS.read_text())
        return status if isinstance(status, dict) else {}
    except (OSError, ValueError):
        return {}


def diagnostics_text():
    status = read_power_status()
    if status.get('profile') != PROFILE:
        return ('Il nuovo profilo non è ancora attivo.\n'
                'Torna alla finestra principale, scegli Attivo e premi Applica.\n'
                'L’aggiornamento richiede la password amministratore nel terminale.')
    age = time.time() - status.get('updated_at', 0)
    if not 0 <= age < 10:
        return 'Il servizio non sta aggiornando le misure. Premi Applica per riavviarlo.'
    modes = {'balanced': 'Prestazioni e autonomia', 'thermal': 'Raffreddamento',
             'battery-low': 'Batteria bassa', 'battery-critical': 'Batteria quasi scarica',
             'unknown': 'Sensori da verificare', 'ac': 'Alimentatore collegato'}
    lines = ['Profilo: ' + modes.get(status.get('mode'), 'Da verificare'),
             'Gestione CPU: ' + ('budget di potenza Intel' if status.get('controller') == 'rapl'
                                else 'frequenza adattiva')]
    for key, label, unit in (('battery_w', 'Consumo attuale del computer', 'W'),
                             ('battery_average_w', 'Consumo medio recente', 'W'),
                             ('cpu_package_w', 'Consumo CPU', 'W'),
                             ('temperature', 'Temperatura CPU', '°C')):
        value = status.get(key)
        lines.append(f'{label}: {value:.1f} {unit}' if isinstance(value, (float, int))
                     else f'{label}: misura non disponibile')
    limits = status.get('power_limits_w', {})
    if limits:
        lines.append(f'Budget CPU: {limits.get("long_term", "?")} W prolungati / '
                     f'{limits.get("short_term", "?")} W di picco')
    lines.append('Turbo consentito: ' + ('sì' if status.get('turbo') else 'no'))
    if status.get('busy_processes'):
        lines.append('\nProcessi più impegnativi (100% = un core):')
        lines.extend(f'{item["name"]}: {item["cpu_percent"]:.0f}% CPU'
                     for item in status['busy_processes'])
    try:
        gpu = json.loads((STATE_DIR / 'gpu-status.json').read_text())
        lines.append('\nScheda video: ' + gpu.get('message', 'da verificare'))
    except (OSError, ValueError):
        pass
    if status.get('warnings'):
        lines.append('\nControlli non applicati:\n' + '\n'.join(status['warnings']))
    if not status.get('complete'):
        lines.append('\nApplicazione parziale: ' + '; '.join(status.get('errors', [])))
    lines.append('\nStorico dei consumi: consumi.csv nella cartella di stato dell’app.\n'
                 'Confronta sessioni con luminosità e attività simili.')
    return '\n'.join(lines)


class PowerLog:
    fields = ('timestamp', 'online', 'profile', 'controller', 'mode', 'capacity',
              'battery_w', 'battery_average_w', 'cpu_package_w', 'temperature', 'load', 'turbo')

    def __init__(self):
        self.last = None

    def record(self, now):
        if self.last is not None and now - self.last < 30:
            return
        status = read_power_status()
        if status.get('profile') != PROFILE or not 0 <= time.time() - status.get('updated_at', 0) < 10:
            return
        path = STATE_DIR / 'consumi.csv'
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            path.replace(path.with_suffix('.previous.csv'))
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open('a', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=self.fields)
            if new_file:
                writer.writeheader()
            writer.writerow({'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                             **{key: status.get(key) for key in self.fields if key != 'timestamp'}})
        self.last = now


def read_settings():
    try:
        settings = json.loads(CONFIG_FILE.read_text())
        if not isinstance(settings, dict):
            return {}
        return settings
    except (OSError, ValueError):
        return {}


def save_settings(limit, extreme=None):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix('.tmp')
    settings = read_settings()
    settings.update({
        'limit': limit,
        'low_battery_percent': 20,
        'low_battery_action': 'notify',
    })
    if extreme is not None:
        settings['extreme_battery'] = bool(extreme)
    temporary.write_text(json.dumps(settings, indent=2) + '\n')
    temporary.replace(CONFIG_FILE)


def battery_state(power_dir=POWER_DIR):
    """Never substitute a guessed percentage or AC state on read failure."""
    battery = power_dir / 'BAT0'
    capacity = int((battery / 'capacity').read_text().strip())
    status = (battery / 'status').read_text().strip()
    online = int((power_dir / 'ADP1' / 'online').read_text().strip())
    if not 0 <= capacity <= 100 or online not in (0, 1):
        raise ValueError('Valori batteria/alimentatore non validi.')
    return capacity, status, bool(online)


def parse_limits(output):
    match = re.search(r'\bBCLM=(\d+) BFCL=(\d+)\b', output)
    if not match:
        raise RuntimeError('Il controller non ha restituito i limiti attesi.')
    return tuple(int(value) for value in match.groups())


def hardware_command(*args):
    command = ['/usr/bin/sudo', '-n', '--', str(HELPER), *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or 'Comando SMC non riuscito.')
    return parse_limits(result.stdout)


def apply_limit(limit, run=hardware_command):
    if limit not in (80, 100):
        raise ValueError('Sono disponibili solo i limiti 80% e 100%.')
    # Read first: abort before changing anything if both required keys are unreadable.
    run('read')
    result = run('set', str(limit))
    if result != (limit, limit):
        raise RuntimeError(f'Valore richiesto {limit}%, risposta SMC {result}.')
    return result


def dialog(kind, text):
    subprocess.run([
        '/usr/bin/zenity', kind, '--title=Gestione ricarica',
        '--no-markup', '--width=480', '--text=' + text,
    ], check=False)


def open_live_dashboard(wait=False):
    command = [sys.executable, str(APP_DIR / 'live-power.py')]
    if wait:
        return subprocess.run(command, check=False).returncode
    return subprocess.Popen(command)


def gui():
    settings = read_settings()
    try:
        capacity, status, online = battery_state()
        current = f'Batteria: {capacity}% — alimentatore ' + ('collegato' if online else 'scollegato')
    except (OSError, ValueError):
        current = 'Stato batteria non disponibile.'
    last = settings.get('limit')
    previous = f'Ultimo limite verificato: {last}%.' if last in (80, 100) else 'Nessun limite ancora verificato da questo programma.'
    preferred = last if last in (80, 100) else 80
    extreme = settings.get('extreme_battery', True)
    options = []
    for enabled in (True, False):
        for limit in (80, 100):
            options.extend([
                str(preferred == limit and bool(extreme) == enabled).upper(),
                f'{limit}:' + ('enable' if enabled else 'disable'),
                f'{limit}%', 'Attivo' if enabled else 'Disattivo',
            ])
    command = [
        '/usr/bin/zenity', '--list', '--radiolist',
        '--title=Gestione ricarica', '--width=720', '--height=500',
        '--text=' + current + '\n' + previous + '\n\n'
        'Risparmio intelligente automatico solo senza alimentatore:\n'
        'CPU adattiva: sale quando serve, scende quando è poco impegnata.\n'
        'Turbo gestito dal sistema entro un budget di potenza Intel.\n'
        'Turbo spento con batteria al 20% o meno o CPU troppo calda.\n'
        'Schermo fino al 40% (regolabile manualmente)\n'
        'e tastiera senza retroilluminazione. Schermo spento dopo 3 min\n'
        'di inattività. Avviso batteria al 20%.\n'
        'Ricollegando la corrente si ripristinano i valori precedenti.\n'
        'Premi Applica per installare o aggiornare il profilo.',
        '--column=Scelta', '--column=ID', '--column=Limite di ricarica',
        '--column=Risparmio intelligente a batteria', '--hide-column=2', '--print-column=2',
        '--ok-label=Applica', '--cancel-label=Chiudi', '--extra-button=Consumi',
        *options,
    ]
    while True:
        choice = subprocess.run(command, capture_output=True, text=True)
        if choice.stdout.strip() != 'Consumi':
            break
        open_live_dashboard()
    if choice.returncode:
        return 0
    fields = choice.stdout.strip().split(':')
    if len(fields) != 2 or fields[0] not in ('80', '100') or fields[1] not in ('enable', 'disable'):
        dialog('--error', 'Scelta non valida. Nessuna modifica effettuata.')
        return 1
    limit = int(fields[0])
    extreme = fields[1] == 'enable'
    print('Autenticazione necessaria per ricarica e risparmio energetico.', flush=True)
    auth = subprocess.run(['/usr/bin/sudo', '-v'], check=False)
    if auth.returncode:
        dialog('--error', 'Autenticazione non riuscita. Il limite non è stato applicato.')
        return 1
    try:
        apply_limit(limit)
        save_settings(limit)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        logging.exception('Impostazione del limite non completata')
        dialog('--error', 'Impostazione non completata:\n' + str(error)
               + '\n\nNon considerare attivo il limite richiesto. Puoi riprovare selezionando 100%.')
        return 1
    try:
        install = subprocess.run([
            '/usr/bin/sudo', '-n', '--', '/bin/bash', str(POWER_INSTALLER),
            'enable' if extreme else 'disable',
        ], capture_output=True, text=True, timeout=90)
        if install.returncode:
            raise RuntimeError((install.stderr or install.stdout).strip()
                               or 'Installazione del risparmio energetico non riuscita.')
        save_settings(limit, extreme)
        # Start the user monitor immediately. Its lock prevents duplicate instances.
        subprocess.Popen(
            ['/usr/bin/python3', str(Path(__file__).resolve()), '--monitor', '--replace'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        logging.info('Limite SMC verificato: %s%%', limit)
        dialog('--info',
               f'Limite massimo {limit}% scritto e riletto dal controller.\n\n'
               + ('Risparmio intelligente automatico attivo senza alimentatore.\n'
                  'CPU adattiva, turbo e budget energetico secondo carica e temperatura.\n'
                  'Usa Consumi per vedere misure e controlli effettivamente applicati.\n'
                  'Ricollegando la corrente si ripristinano i valori precedenti.\n\n'
                  if extreme else 'Risparmio intelligente disattivato.\n\n') +
               'Quando scolleghi l’alimentatore, riceverai un avviso al 20%.\n'
               'Una batteria già oltre il limite non viene scaricata automaticamente.\n'
               'Il rispetto del limite va verificato durante una ricarica.')
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        logging.exception('Configurazione del risparmio energetico non completata')
        dialog('--error', f'Limite di ricarica {limit}% verificato.\n\n'
               'Configurazione del risparmio energetico non completata:\n' + str(error)
               + '\n\nRiapri Gestione ricarica e premi Applica per riprovare.')
        return 1


class LowBatteryAlert:
    def __init__(self):
        self.sent = False

    def should_alert(self, capacity, online):
        if online or capacity > 25:
            self.sent = False
        if not online and capacity <= 20 and not self.sent:
            return True
        return False


def notify_low_battery(capacity):
    result = subprocess.run([
        '/usr/bin/notify-send', '--app-name=Gestione ricarica',
        '--urgency=critical', '--expire-time=0', '--icon=battery-caution',
        'Batteria al 20% o meno',
        f'Carica attuale: {capacity}%. Collega l’alimentatore. Il computer resta acceso.',
    ], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Notifica non riuscita.')


class NvidiaPowerSaver:
    """Request adaptive clocks via the user's X session, preserving its mode."""
    attribute = '[gpu:0]/GPUPowerMizerMode'

    def __init__(self):
        self.journal = STATE_DIR / 'nvidia-restore.json'
        self.saved = json.loads(self.journal.read_text()) if self.journal.exists() else None
        self.applied = False
        self.retry_at = 0

    def command(self, *args):
        result = subprocess.run(['/usr/bin/nvidia-settings', *args], capture_output=True,
                                text=True, timeout=10)
        if result.returncode or 'ERROR:' in result.stderr:
            raise RuntimeError(result.stderr.strip() or 'Controllo NVIDIA non disponibile.')
        return result.stdout.strip()

    def query(self):
        mode = int(self.command('-q', self.attribute, '-t'))
        if mode not in (0, 1, 2, 3):
            raise ValueError('Modalità NVIDIA non riconosciuta.')
        return mode

    def set_mode(self, mode):
        self.command('-a', f'{self.attribute}={mode}')
        if self.query() != mode:
            raise RuntimeError('Modalità adattiva NVIDIA non confermata.')

    def sync(self, active, now):
        if now < self.retry_at and (active or self.saved is None):
            return
        try:
            if active and not self.applied:
                if self.saved is None:
                    saved = {'mode': self.query()}
                    temporary = self.journal.with_suffix('.tmp')
                    temporary.write_text(json.dumps(saved) + '\n')
                    temporary.replace(self.journal)
                    self.saved = saved
                self.set_mode(0)
                self.applied = True
            elif not active and self.saved is not None:
                self.applied = False
                self.set_mode(int(self.saved['mode']))
                self.journal.unlink(missing_ok=True)
                self.saved = None
            message = 'modalità adattiva' if active else 'impostazioni originali'
            status = {'complete': True, 'active': active, 'message': message}
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            self.retry_at = now + 60
            status = {'complete': False, 'active': False,
                      'message': 'controllo adattivo non disponibile: ' + str(error)}
        temporary = STATE_DIR / 'gpu-status.tmp'
        temporary.write_text(json.dumps(status) + '\n')
        temporary.replace(STATE_DIR / 'gpu-status.json')


class DesktopPowerSaver:
    """Battery-only Cinnamon idle settings; persist originals for crash recovery."""
    schema = 'org.cinnamon.settings-daemon.plugins.power'
    targets = {'sleep-display-battery': '180', 'idle-dim-battery': 'true',
               'idle-brightness': '20', 'idle-dim-time': '45'}

    def __init__(self):
        self.journal = STATE_DIR / 'desktop-restore.json'
        self.saved = json.loads(self.journal.read_text()) if self.journal.exists() else {}
        self.applied = False

    def command(self, *args):
        result = subprocess.run(['/usr/bin/gsettings', *args], capture_output=True,
                                text=True, timeout=10)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or 'Impostazioni schermo non disponibili.')
        return result.stdout.strip()

    def sync(self, active):
        if active and not self.saved:
            saved = {key: self.command('get', self.schema, key) for key in self.targets}
            temporary = self.journal.with_suffix('.tmp')
            temporary.write_text(json.dumps(saved) + '\n')
            temporary.replace(self.journal)
            self.saved = saved
        if active and not self.applied:
            for key, value in self.targets.items():
                self.command('set', self.schema, key, value)
                if self.command('get', self.schema, key) != value:
                    raise RuntimeError('Impostazione schermo non confermata: ' + key)
            self.applied = True
        elif not active and self.saved:
            self.applied = False
            for key, value in self.saved.items():
                self.command('set', self.schema, key, value)
            self.journal.unlink(missing_ok=True)
            self.saved = {}
            self.applied = False


def stop_previous_monitor():
    """Replace the earlier autostart version, without touching other Python apps."""
    script = str(Path(__file__).resolve())
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            argv = (process / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
            argv = [arg.decode() for arg in argv]
            if argv in ([sys.executable, script, '--monitor'],
                        [sys.executable, script, '--monitor', '--replace']):
                os.kill(int(process.name), signal.SIGTERM)
        except (OSError, UnicodeError):
            continue


def monitor(replace=False):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / 'monitor.lock').open('a') as lock:
        if replace:
            stop_previous_monitor()
        deadline = time.monotonic() + (5 if replace else 0)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return 0
                time.sleep(0.1)
        alert = LowBatteryAlert()
        desktop = DesktopPowerSaver()
        gpu = NvidiaPowerSaver()
        measurements = PowerLog()
        while True:
            try:
                settings = read_settings()
                capacity, _, online = battery_state()
                active = bool(settings.get('extreme_battery')) and not online
                try:
                    desktop.sync(active)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    logging.exception('Impostazioni schermo non applicate; riprovo')
                try:
                    gpu.sync(active, time.monotonic())
                    measurements.record(time.monotonic())
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    logging.exception('Controlli GPU o registrazione consumi non completati')
                if settings.get('limit') in (80, 100):
                    if alert.should_alert(capacity, online):
                        notify_low_battery(capacity)
                        alert.sent = True
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                logging.exception('Controllo batteria non riuscito; riprovo tra 10 secondi')
            time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description='Ricarica 80/100%, risparmio intelligente a batteria e avviso al 20%.')
    parser.add_argument('--monitor', action='store_true')
    parser.add_argument('--settings', action='store_true', help='Apri le impostazioni 80% / 100% e risparmio.')
    parser.add_argument('--replace', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=STATE_DIR / 'charge-manager.log',
                        level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.monitor:
        return monitor(args.replace)
    return gui() if args.settings else open_live_dashboard(wait=True)


if __name__ == '__main__':
    sys.exit(main())
