#!/usr/bin/python3
"""Battery-only balanced power: native CPU scaling, RAPL and reversible tuning."""

import json
import logging
import math
import os
from pathlib import Path
import signal
import threading
import time


PROFILE = 'balanced-rapl-v2'


class PowerMetrics:
    """Measured discharge and package energy; missing readings stay unknown."""
    def __init__(self, sys_dir, proc_dir, clock):
        self.sys_dir, self.proc_dir, self.clock = sys_dir, proc_dir, clock
        self.last_time = None
        self.last_battery_w = None
        self.average_w = None
        self.energy_wh = 0.0
        self.duration = 0.0
        self.package_samples = {}
        self.process_samples = {}
        self.process_time = None
        self.busy_processes = []

    def processes(self, now):
        if self.process_time is not None and now - self.process_time < 10:
            return self.busy_processes
        samples, busy = {}, []
        elapsed = now - self.process_time if self.process_time is not None else 0
        ticks_per_second = os.sysconf('SC_CLK_TCK')
        for directory in self.proc_dir.iterdir():
            if not directory.name.isdigit():
                continue
            try:
                stat = (directory / 'stat').read_text()
                left, right = stat.index('('), stat.rindex(')')
                fields = stat[right + 2:].split()
                key = (directory.name, fields[19])  # PID plus start time: no PID reuse.
                ticks = int(fields[11]) + int(fields[12])
                samples[key] = ticks
                if elapsed > 0 and key in self.process_samples:
                    percent = 100 * (ticks - self.process_samples[key]) / ticks_per_second / elapsed
                    if percent >= 1:
                        busy.append({'name': stat[left + 1:right], 'cpu_percent': round(percent, 1)})
            except (OSError, ValueError, IndexError):
                continue
        self.process_samples, self.process_time = samples, now
        self.busy_processes = sorted(busy, key=lambda item: item['cpu_percent'], reverse=True)[:5]
        return self.busy_processes

    def sample(self, online):
        now = self.clock()
        battery_w, package_w = None, None
        batteries = list((self.sys_dir / 'class/power_supply').glob('*/capacity'))
        powers = []
        for capacity in batteries:
            battery = capacity.parent
            try:
                if (battery / 'status').read_text().strip() != 'Discharging':
                    continue
                if (battery / 'power_now').exists():
                    power = abs(int((battery / 'power_now').read_text())) / 1e6
                else:
                    power = abs(int((battery / 'current_now').read_text())
                                * int((battery / 'voltage_now').read_text())) / 1e12
                if 0 <= power <= 250:
                    powers.append(power)
            except (OSError, ValueError):
                continue
        if not online and batteries and len(powers) == len(batteries):
            battery_w = sum(powers)
        dt = now - self.last_time if self.last_time is not None else 0
        if online:
            self.average_w = None
            self.energy_wh = self.duration = 0.0
        elif battery_w is not None:
            if self.average_w is None or dt > 10:
                self.average_w = battery_w
            elif 0 < dt <= 10:
                self.average_w += (1 - math.exp(-dt / 60)) * (battery_w - self.average_w)
            if self.last_battery_w is not None and 0 < dt <= 10:
                self.energy_wh += (battery_w + self.last_battery_w) / 2 * dt / 3600
                self.duration += dt
        self.last_time, self.last_battery_w = now, battery_w
        package_powers = []
        zones = [zone for zone in (self.sys_dir / 'class/powercap').glob('intel-rapl:*')
                 if zone.name.count(':') == 1]
        for zone in zones:
            try:
                energy = int((zone / 'energy_uj').read_text())
                previous = self.package_samples.get(zone.name)
                if previous:
                    delta = energy - previous[1]
                    if delta < 0:
                        delta += int((zone / 'max_energy_range_uj').read_text())
                    interval = now - previous[0]
                    if 0 < interval <= 10 and delta >= 0:
                        watts = delta / 1e6 / interval
                        if 0 <= watts <= 250:
                            package_powers.append(watts)
                self.package_samples[zone.name] = (now, energy)
            except (OSError, ValueError):
                self.package_samples.pop(zone.name, None)
        if package_powers and len(package_powers) == len(zones):
            package_w = sum(package_powers)
        try:
            processes = self.processes(now)
        except OSError:
            processes = []
        return {'battery_w': battery_w, 'battery_average_w': self.average_w if battery_w is not None else None,
                'cpu_package_w': package_w, 'measured_energy_wh': self.energy_wh,
                'measured_seconds': self.duration, 'busy_processes': processes}


class TurboBudget:
    """Allow a short burst for new work, never repeated boosts under steady load."""
    def __init__(self):
        self.active = False
        self.armed = True
        self.until = 0
        self.next_burst = 0
        self.idle_since = None

    def update(self, now, load, capacity, temperature, permitted):
        quiet = load is not None and load < 0.35
        if quiet:
            if self.idle_since is None:
                self.idle_since = now
            if now - self.idle_since >= 3:
                self.armed = True
        else:
            self.idle_since = None
        safe = (permitted and load is not None and capacity is not None
                and capacity > 20 and temperature is not None and temperature < 80)
        if self.active and (not safe or quiet or now >= self.until):
            self.active = False
        if (not self.active and safe and temperature < 75 and load >= 0.65
                and self.armed and now >= self.next_burst):
            self.active = True
            self.armed = False
            self.until = now + 6
            self.next_burst = now + 30
        return self.active


class ExtremePower:
    def __init__(self, sys_dir=Path('/sys'), state_dir=Path('/run/macbook-extreme-power'),
                 proc_dir=Path('/proc'), clock=time.monotonic):
        self.sys_dir = Path(sys_dir)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.journal = self.state_dir / 'restore.json'
        self.status = self.state_dir / 'status.json'
        self.saved = json.loads(self.journal.read_text()) if self.journal.exists() else []
        self.first = True
        self.retry = set()
        self.proc_dir = Path(proc_dir)
        self.clock = clock
        self.previous_cpu = {}
        self.turbo = TurboBudget()
        self.telemetry = {}
        self.metrics = PowerMetrics(self.sys_dir, self.proc_dir, self.clock)
        self.rapl_ready = False
        self.rapl_blocked = False
        self.hot = False
        self.mode = 'balanced'
        self.warnings = []
        self.rapl_warning = None
        self.ac_unknown = False

    def operating_mode(self):
        if self.ac_unknown:
            return 'unknown'
        capacity, temperature = self.telemetry.get('capacity'), self.telemetry.get('temperature')
        if temperature is not None:
            if temperature >= 80:
                self.hot = True
            elif temperature <= 72:
                self.hot = False
        if capacity is not None and capacity <= 10:
            return 'battery-critical'
        if self.hot:
            return 'thermal'
        if capacity is not None and capacity <= 20:
            return 'battery-low'
        if capacity is None or temperature is None:
            return 'unknown'
        return 'balanced'

    def rapl_plan(self):
        changes = []
        zones = [zone for zone in (self.sys_dir / 'class/powercap').glob('intel-rapl:*')
                 if zone.name.count(':') == 1]
        for zone in zones:
            try:
                if (zone / 'enabled').read_text().strip() != '1':
                    return []
                roles = {(path.read_text().strip()): path.name[:-5]
                         for path in zone.glob('constraint_*_name')}
                if not {'long_term', 'short_term'} <= roles.keys():
                    return []
                for role in ('long_term', 'short_term'):
                    prefix = roles[role]
                    path = zone / (prefix + '_power_limit_uw')
                    original = int(path.read_text())
                    if original <= 0:
                        return []
                    maximum_path = zone / (prefix + '_max_power_uw')
                    minimum_path = zone / (prefix + '_min_power_uw')
                    maximum = int(maximum_path.read_text()) if maximum_path.exists() else 0
                    minimum = int(minimum_path.read_text()) if minimum_path.exists() else 0
                    changes.append({'path': str(path.relative_to(self.sys_dir)),
                                    'original': str(original), 'target': str(original),
                                    'continuous': True, 'kind': 'rapl', 'role': role,
                                    'maximum': maximum, 'minimum': minimum, 'tolerance': 0.01})
                    # A shorter long-term averaging window limits sustained draw.
                    # Leave the firmware's short-term window intact.
                    window = zone / (prefix + '_time_window_us')
                    if role == 'long_term' and window.exists():
                        original_window = int(window.read_text())
                        if original_window > 0:
                            changes.append({'path': str(window.relative_to(self.sys_dir)),
                                            'original': str(original_window),
                                            'target': str(min(original_window, 8000000)),
                                            'continuous': True, 'kind': 'rapl',
                                            'role': 'window', 'tolerance': 0.05})
            except (OSError, ValueError):
                return []
        return changes

    @staticmethod
    def adaptive_governor(policy):
        governors = (policy / 'scaling_available_governors').read_text().split()
        driver_path = policy / 'scaling_driver'
        driver = driver_path.read_text().strip() if driver_path.exists() else ''
        # Active intel_pstate powersave is adaptive; generic powersave is fixed.
        choices = ('powersave',) if driver == 'intel_pstate' else ('schedutil', 'ondemand', 'conservative')
        for governor in choices:
            if governor in governors:
                return governor
        raise RuntimeError('Governor CPU adattivo non disponibile: ' + str(policy))

    def cpu_load(self):
        samples = {}
        for line in (self.proc_dir / 'stat').read_text().splitlines():
            fields = line.split()
            if fields and fields[0].startswith('cpu') and fields[0][3:].isdigit():
                values = [int(value) for value in fields[1:9]]
                if len(values) < 5:
                    raise ValueError('Contatori CPU incompleti.')
                samples[fields[0]] = (sum(values), values[3] + values[4])
        loads = []
        for cpu, (total, idle) in samples.items():
            if cpu in self.previous_cpu:
                old_total, old_idle = self.previous_cpu[cpu]
                elapsed, idle_delta = total - old_total, idle - old_idle
                if elapsed > 0 and 0 <= idle_delta <= elapsed:
                    loads.append(1 - idle_delta / elapsed)
        self.previous_cpu = samples
        # One busy core also matters for interactive, single-threaded work.
        return max(loads) if loads else None

    def cpu_temperature(self):
        readings = []
        sensors = []
        for device in (self.sys_dir / 'class/hwmon').glob('*'):
            try:
                if (device / 'name').read_text().strip() in ('coretemp', 'k10temp'):
                    sensors.extend(device.glob('temp*_input'))
            except OSError:
                continue
        for zone in (self.sys_dir / 'class/thermal').glob('thermal_zone*'):
            try:
                if (zone / 'type').read_text().strip() in ('x86_pkg_temp', 'cpu-thermal'):
                    sensors.append(zone / 'temp')
            except OSError:
                continue
        for sensor in sensors:
            try:
                value = int(sensor.read_text()) / 1000
                if 0 < value < 150:
                    readings.append(value)
            except (OSError, ValueError):
                continue
        return max(readings) if readings else None

    def sample(self):
        capacities = []
        for path in (self.sys_dir / 'class/power_supply').glob('*/capacity'):
            try:
                value = int(path.read_text())
                if not 0 <= value <= 100:
                    return {'load': None, 'capacity': None, 'temperature': None}
                capacities.append(value)
            except (OSError, ValueError):
                return {'load': None, 'capacity': None, 'temperature': None}
        try:
            load = self.cpu_load()
        except (OSError, ValueError):
            self.previous_cpu = {}
            load = None
        return {'load': load, 'capacity': min(capacities) if capacities else None,
                'temperature': self.cpu_temperature()}

    @staticmethod
    def atomic_json(path, data):
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2) + '\n')
        temporary.replace(path)

    def online(self):
        # AC online is authoritative, including a full battery or a charge cap.
        adapters = list((self.sys_dir / 'class/power_supply').glob('*/online'))
        if not adapters:
            raise RuntimeError('Nessun alimentatore leggibile; nessuna modifica.')
        values = [int(path.read_text().strip()) for path in adapters]
        if any(value not in (0, 1) for value in values):
            raise ValueError('Stato alimentatore non valido.')
        return any(values)

    def plan(self):
        changes = []

        def add(path, target, continuous=True, optional=False):
            if path.exists():
                try:
                    original = path.read_text().strip()
                except OSError as error:
                    if not optional:
                        raise
                    self.warnings.append(str(error))
                    return
                changes.append({'path': str(path.relative_to(self.sys_dir)),
                                'original': original, 'target': str(target),
                                'continuous': continuous, 'optional': optional})

        cpu = self.sys_dir / 'devices/system/cpu'
        # Capture every original value before no_turbo can clamp policy limits.
        add(cpu / 'intel_pstate/no_turbo', 1)
        for policy in sorted((cpu / 'cpufreq').glob('policy*')):
            minimum = int((policy / 'cpuinfo_min_freq').read_text())
            original_max = int((policy / 'scaling_max_freq').read_text())
            add(policy / 'scaling_min_freq', minimum)
            add(policy / 'scaling_max_freq', min(original_max, max(minimum, 2400000)))
            add(policy / 'scaling_governor', self.adaptive_governor(policy))
        for device in sorted((self.sys_dir / 'class/backlight').glob('*')):
            maximum = int((device / 'max_brightness').read_text())
            current = int((device / 'brightness').read_text())
            add(device / 'brightness', min(current, max(1, maximum * 40 // 100)), False)
        for device in sorted((self.sys_dir / 'class/leds').glob('*kbd_backlight*')):
            add(device / 'brightness', 0, False)
        # Audio auto-suspend and moderate SATA link saving retain active I/O.
        audio = self.sys_dir / 'module/snd_hda_intel/parameters'
        timeout = audio / 'power_save'
        try:
            if timeout.exists():
                add(timeout, max(10, int(timeout.read_text())), optional=True)
        except (OSError, ValueError) as error:
            self.warnings.append(str(error))
        add(audio / 'power_save_controller', 'Y', optional=True)
        for host in (self.sys_dir / 'class/scsi_host').glob('host*'):
            policy = host / 'link_power_management_policy'
            try:
                if policy.exists() and policy.read_text().strip() in ('max_performance', 'medium_power', 'med_power_with_dipm'):
                    add(policy, 'med_power_with_dipm', optional=True)
            except OSError as error:
                self.warnings.append(str(error))
        changes.extend(self.rapl_plan())
        if not any(item['path'].endswith('/scaling_max_freq') for item in changes):
            raise RuntimeError('Controlli della frequenza CPU non disponibili.')
        return changes

    def update_targets(self):
        permitted = any(item['path'].endswith('/no_turbo') and item['original'] == '0'
                        for item in self.saved)
        permitted = permitted and self.mode == 'balanced'
        if self.rapl_ready:
            # The governor chooses frequency; hardware enforces power and bursts.
            boost = permitted and self.mode == 'balanced'
            self.turbo.active = boost
        else:
            boost = self.turbo.update(self.clock(), permitted=permitted, **self.telemetry)
        capacity, temperature = self.telemetry['capacity'], self.telemetry['temperature']
        constrained = ((capacity is not None and capacity <= 20)
                       or (temperature is not None and temperature >= 80))
        ceiling = 1800000 if constrained else 2400000
        if self.rapl_ready and self.mode in ('battery-low', 'thermal'):
            ceiling = 2400000
        changes = []
        for saved in self.saved:
            item = saved.copy()
            path = self.sys_dir / item['path']
            if path.name == 'no_turbo':
                item['target'] = '0' if boost else '1'
            elif path.name in ('scaling_min_freq', 'scaling_max_freq'):
                minimum = int((path.parent / 'cpuinfo_min_freq').read_text())
                target = minimum
                if path.name == 'scaling_max_freq':
                    maximum = int(item['original'])
                    target = maximum if boost else min(maximum, max(minimum, ceiling))
                item['target'] = str(target)
            elif path.name == 'scaling_governor':
                item['target'] = self.adaptive_governor(path.parent)
            changes.append(item)
        if changes != self.saved:
            # Also migrates old fixed-frequency journals without losing originals.
            self.atomic_json(self.journal, changes)
            self.saved = changes

    def apply_rapl(self):
        self.rapl_ready = False
        items = [item for item in self.saved if item.get('kind') == 'rapl']
        if not items:
            self.warnings.append('RAPL non disponibile: controllo adattivo della frequenza.')
            return []
        budgets = {'balanced': (25000000, 40000000), 'battery-low': (18000000, 28000000),
                   'battery-critical': (12000000, 18000000), 'thermal': (18000000, 25000000),
                   'unknown': (18000000, 25000000)}
        long_term, short_term = budgets[self.mode]
        changes = []
        for saved in self.saved:
            item = saved.copy()
            if item.get('kind') == 'rapl' and item['role'] != 'window':
                desired = long_term if item['role'] == 'long_term' else short_term
                maximum = item.get('maximum', 0)
                target = min(int(item['original']), desired, maximum or desired)
                # A firmware minimum above this budget cannot meet the profile.
                if target < item.get('minimum', 0):
                    self.rapl_blocked = True
                    self.rapl_warning = 'Il minimo RAPL supera il budget richiesto.'
                item['target'] = str(target)
            changes.append(item)
        if changes != self.saved:
            self.atomic_json(self.journal, changes)
            self.saved = changes
        items = [item for item in self.saved if item.get('kind') == 'rapl']
        errors = []
        if not self.rapl_blocked:
            for item in items:
                try:
                    self.write(item, 'target')
                except (OSError, RuntimeError) as error:
                    errors.append(str(error))
            if not errors:
                self.rapl_ready = True
                return []
            self.rapl_blocked = True
            self.rapl_warning = '; '.join(errors)
        self.warnings.append('RAPL rifiutato; uso controllo della frequenza. ' + (self.rapl_warning or ''))
        # Undo a partially accepted budget before using the frequency fallback.
        restore_errors = []
        for item in items:
            try:
                self.write(item, 'original')
            except (OSError, RuntimeError) as error:
                restore_errors.append(str(error))
        return restore_errors

    def write(self, item, key):
        path = self.sys_dir / item['path']
        value = item[key]
        def matches(actual):
            if item.get('tolerance'):
                try:
                    return (int(actual) > 0 and abs(int(actual) - int(value))
                            <= max(1, int(value) * item['tolerance']))
                except ValueError:
                    return False
            return actual == value

        if not matches(path.read_text().strip()):
            path.write_text(value + '\n')
        if not matches(path.read_text().strip()):
            raise RuntimeError(f'Valore non confermato: {path}')

    def activate(self):
        self.warnings = []
        initial = not self.saved or self.first
        if not self.saved:
            changes = self.plan()
            # Write-ahead journal survives a process crash/restart, but not reboot.
            self.atomic_json(self.journal, changes)
            self.saved = changes
        elif self.first:
            # Add new controls to an older journal without replacing its originals.
            paths = {item['path'] for item in self.saved}
            additions = [item for item in self.plan() if item['path'] not in paths]
            if additions:
                self.atomic_json(self.journal, self.saved + additions)
                self.saved.extend(additions)
        self.mode = self.operating_mode()
        try:
            errors = self.apply_rapl()
            self.update_targets()
        except (OSError, ValueError, RuntimeError):
            # A failed governor/journal read must not leave an earlier burst on.
            self.turbo.active = False
            for item in self.saved:
                if item['path'].endswith('/no_turbo'):
                    try:
                        self.write({**item, 'target': '1'}, 'target')
                    except (OSError, RuntimeError):
                        logging.exception('Impossibile interrompere il turbo dopo un errore')
            raise
        retry = set()
        for item in self.saved:
            if item.get('kind') == 'rapl':
                continue
            if initial or item['continuous'] or item['path'] in self.retry:
                try:
                    self.write(item, 'target')
                except (OSError, RuntimeError) as error:
                    if item.get('optional'):
                        self.warnings.append(str(error))
                    else:
                        errors.append(str(error))
                    retry.add(item['path'])
        self.first = False
        self.retry = retry
        return errors

    def restore(self):
        errors = []
        # Re-enable turbo before raising limits; restore max before a higher min.
        def order(item):
            name = Path(item['path']).name
            return {'no_turbo': 0, 'scaling_max_freq': 1,
                    'scaling_min_freq': 2, 'scaling_governor': 3}.get(name, 4)

        # Keep the complete snapshot on partial failure: the next attempt needs
        # the same ordering and dependencies as this one.
        for item in sorted(self.saved, key=order):
            try:
                self.write(item, 'original')
            except (OSError, RuntimeError) as error:
                errors.append(str(error))
        if not errors:
            self.saved = []
            self.journal.unlink(missing_ok=True)
            self.rapl_ready = self.rapl_blocked = False
            self.rapl_warning = None
            self.hot = False
        return errors

    def step(self):
        try:
            online = self.online()
        except (OSError, ValueError, RuntimeError) as error:
            # Unknown AC must never activate a new profile or leave turbo latched on.
            errors = [str(error)]
            self.ac_unknown = True
            if self.saved:
                self.telemetry = self.sample()
                self.telemetry['load'] = None
                errors.extend(self.activate())
            self.atomic_json(self.status, {
                'online': None, 'active': bool(self.saved), 'complete': False,
                'errors': errors, 'profile': PROFILE, 'turbo': False,
                'updated_at': time.time(), 'controller': 'rapl' if self.rapl_ready else 'frequency',
                'mode': 'unknown', 'warnings': self.warnings, **self.telemetry,
            })
            raise
        self.ac_unknown = False
        if online:
            self.turbo = TurboBudget()
            self.previous_cpu = {}
            self.telemetry = self.sample()
            self.mode = 'ac'
            self.warnings = []
        else:
            self.telemetry = self.sample()
        try:
            errors = self.restore() if online else self.activate()
        except (OSError, RuntimeError, ValueError) as error:
            self.atomic_json(self.status, {
                'online': online, 'active': bool(self.saved), 'complete': False,
                'errors': [str(error)], 'profile': PROFILE, 'turbo': False,
                'mode': self.mode, 'warnings': self.warnings, 'updated_at': time.time(),
                'controller': 'rapl' if self.rapl_ready else 'frequency', **self.telemetry,
            })
            raise
        measured = self.metrics.sample(online)
        limits = {}
        if self.rapl_ready:
            for item in self.saved:
                if item.get('kind') == 'rapl' and item['role'] != 'window':
                    limits[item['role']] = int((self.sys_dir / item['path']).read_text()) / 1e6
        self.atomic_json(self.status, {
            'online': online, 'active': bool(self.saved),
            'complete': not errors, 'errors': errors,
            'profile': PROFILE, 'turbo': self.turbo.active,
            'controller': 'rapl' if self.rapl_ready else 'frequency', 'mode': self.mode,
            'power_limits_w': limits, 'warnings': self.warnings,
            'updated_at': time.time(), **self.telemetry, **measured,
        })
        if errors:
            logging.error('Risparmio energetico parziale: %s', '; '.join(errors))
        return errors


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    power = ExtremePower()
    try:
        while not stop.is_set():
            try:
                power.step()
            except (OSError, ValueError, RuntimeError):
                logging.exception('Alimentazione non leggibile; mantengo lo stato e riprovo')
            stop.wait(1)
    finally:
        errors = power.restore()
        power.atomic_json(power.status, {'active': bool(power.saved),
                                        'complete': not errors, 'errors': errors})
        if errors:
            raise RuntimeError('Ripristino incompleto: ' + '; '.join(errors))


if __name__ == '__main__':
    main()
