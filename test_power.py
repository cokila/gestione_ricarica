execute failed:
    Could not start serviceManager:
        Cannot locate HTTP port of service:
            Request for service HTTP address failed:
                Cannot connect to ipc via "/tmp/state-ipc/state-release.sock":
                    'dial unix /tmp/state-ipc/state-release.sock: connect: operation not permitted':
                        'connect: operation not permitted': operation not permitted
"""Run with python3 -m unittest discover -s this-directory -p 'test_*.py'."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hardware = load('extreme_power', 'extreme-power.py')
manager = load('charge_manager', 'charge-manager.py')


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sys = self.root / 'sys'
        self.state = self.root / 'run'
        self.values = {
            'class/power_supply/ADP1/online': '1',
            'devices/system/cpu/intel_pstate/no_turbo': '0',
            'devices/system/cpu/cpufreq/policy0/cpuinfo_min_freq': '1200000',
            'devices/system/cpu/cpufreq/policy0/scaling_min_freq': '2000000',
            'devices/system/cpu/cpufreq/policy0/scaling_max_freq': '3600000',
            'devices/system/cpu/cpufreq/policy0/scaling_governor': 'schedutil',
            'devices/system/cpu/cpufreq/policy0/scaling_driver': 'intel_cpufreq',
            'devices/system/cpu/cpufreq/policy0/scaling_available_governors': 'powersave schedutil',
            'class/backlight/acpi_video0/max_brightness': '15',
            'class/backlight/acpi_video0/brightness': '14',
            'class/leds/smc::kbd_backlight/brightness': '123',
        }
        for name, value in self.values.items():
            self.put(name, value)
        self.now = 0
        self.power = hardware.ExtremePower(self.sys, self.state, self.root / 'proc',
                                           clock=lambda: self.now)

    def put(self, name, value):
        path = self.sys / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value) + '\n')

    def get(self, name):
        return (self.sys / name).read_text().strip()

    def unplug(self):
        self.put('class/power_supply/ADP1/online', 0)

    def test_ac_leaves_hardware_untouched(self):
        self.assertEqual(self.power.step(), [])
        self.assertFalse(self.power.journal.exists())
        for name, value in self.values.items():
            self.assertEqual(self.get(name), value)

    def test_unplug_caps_then_ac_restores_all_originals_in_valid_order(self):
        original_write = self.power.write

        def constrained_write(item, key):
            base = 'devices/system/cpu/cpufreq/policy0/'
            value = int(item[key]) if item['path'].endswith('_freq') else None
            if item['path'] == base + 'scaling_min_freq':
                self.assertLessEqual(value, int(self.get(base + 'scaling_max_freq')))
            if item['path'] == base + 'scaling_max_freq':
                self.assertGreaterEqual(value, int(self.get(base + 'scaling_min_freq')))
            original_write(item, key)

        with patch.object(self.power, 'write', side_effect=constrained_write):
            self.unplug()
            self.assertEqual(self.power.step(), [])
            self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '1')
            self.assertEqual(self.get('devices/system/cpu/cpufreq/policy0/scaling_max_freq'), '2400000')
            self.assertEqual(self.get('devices/system/cpu/cpufreq/policy0/scaling_min_freq'), '1200000')
            self.assertEqual(self.get('devices/system/cpu/cpufreq/policy0/scaling_governor'), 'schedutil')
            self.assertEqual(self.get('class/backlight/acpi_video0/brightness'), '6')
            self.assertEqual(self.get('class/leds/smc::kbd_backlight/brightness'), '0')
            self.put('class/power_supply/ADP1/online', 1)
            self.assertEqual(self.power.step(), [])
        for name, value in self.values.items():
            self.assertEqual(self.get(name), value)

    def test_restart_does_not_replace_originals_with_reduced_values(self):
        self.unplug()
        self.power.step()
        restarted = hardware.ExtremePower(self.sys, self.state)
        restarted.step()
        self.put('class/power_supply/ADP1/online', 1)
        restarted.step()
        self.assertEqual(self.get('class/backlight/acpi_video0/brightness'), '14')
        self.assertEqual(self.get('devices/system/cpu/cpufreq/policy0/scaling_max_freq'), '3600000')

    def test_unknown_ac_never_activates(self):
        self.put('class/power_supply/ADP1/online', 'unknown')
        with self.assertRaises(ValueError):
            self.power.step()
        self.assertFalse(self.power.saved)

    def test_unknown_ac_preserves_restore_snapshot(self):
        self.unplug()
        self.power.step()
        (self.sys / 'class/power_supply/ADP1/online').unlink()
        with self.assertRaises(RuntimeError):
            self.power.step()
        self.assertTrue(self.power.journal.exists())

    def test_any_connected_adapter_prevents_activation(self):
        self.unplug()
        self.put('class/power_supply/USB/online', 1)
        self.power.step()
        self.assertFalse(self.power.saved)

    def test_cpu_caps_are_enforced_but_brightness_can_be_adjusted(self):
        self.unplug()
        self.power.step()
        self.put('devices/system/cpu/cpufreq/policy0/scaling_governor', 'powersave')
        self.put('class/backlight/acpi_video0/brightness', 5)
        self.power.step()
        self.assertEqual(self.get('devices/system/cpu/cpufreq/policy0/scaling_governor'), 'schedutil')
        self.assertEqual(self.get('class/backlight/acpi_video0/brightness'), '5')

    def test_does_not_brighten_already_dim_screen(self):
        self.put('class/backlight/acpi_video0/brightness', 1)
        self.unplug()
        self.power.step()
        self.assertEqual(self.get('class/backlight/acpi_video0/brightness'), '1')

    def test_failed_brightness_is_retried_and_partial_status_reported(self):
        self.unplug()
        original_write = self.power.write

        def fail(item, key):
            if 'backlight/acpi_video0' in item['path']:
                raise OSError('simulated backlight failure')
            original_write(item, key)

        with patch.object(self.power, 'write', side_effect=fail):
            self.assertTrue(self.power.step())
        self.assertFalse(json.loads(self.power.status.read_text())['complete'])
        self.assertEqual(self.power.step(), [])
        self.assertEqual(self.get('class/backlight/acpi_video0/brightness'), '6')

    def test_failed_restore_keeps_snapshot_until_retry_succeeds(self):
        self.unplug()
        self.power.step()
        self.put('class/power_supply/ADP1/online', 1)
        with patch.object(self.power, 'write', side_effect=OSError('simulated failure')):
            self.assertTrue(self.power.step())
        self.assertTrue(self.power.journal.exists())
        self.assertEqual(self.power.step(), [])
        self.assertFalse(self.power.journal.exists())

    def test_turbo_burst_expires_and_ac_restores_original_settings(self):
        self.unplug()
        sample = {'load': 0.9, 'capacity': 80, 'temperature': 60}
        base = 'devices/system/cpu/cpufreq/policy0/'
        original_write = self.power.write

        def constrained_write(item, key):
            # Model intel_pstate's limit clamping when turbo is disabled.
            if item['path'] == base + 'scaling_max_freq':
                if self.get('devices/system/cpu/intel_pstate/no_turbo') == '1':
                    self.assertLessEqual(int(item[key]), 2600000)
            original_write(item, key)
            if item['path'].endswith('/no_turbo') and item[key] == '1':
                self.put(base + 'scaling_max_freq',
                         min(2600000, int(self.get(base + 'scaling_max_freq'))))

        with patch.object(self.power, 'sample', return_value=sample), \
             patch.object(self.power, 'write', side_effect=constrained_write):
            self.assertEqual(self.power.step(), [])
            self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '0')
            self.assertEqual(self.get(base + 'scaling_max_freq'), '3600000')
            self.now = 6
            self.assertEqual(self.power.step(), [])
            self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '1')
            self.assertEqual(self.get(base + 'scaling_max_freq'), '2400000')
            self.put('class/power_supply/ADP1/online', 1)
            self.assertEqual(self.power.step(), [])
        for name, value in self.values.items():
            self.assertEqual(self.get(name), value)

    def test_battery_low_or_hot_cpu_reduce_ceiling_and_prevent_turbo(self):
        self.unplug()
        for capacity, temperature in ((20, 60), (80, 80)):
            with self.subTest(capacity=capacity, temperature=temperature), \
                 patch.object(self.power, 'sample', return_value={
                     'load': 0.95, 'capacity': capacity, 'temperature': temperature}):
                self.assertEqual(self.power.step(), [])
                self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '1')
                self.assertEqual(self.get('devices/system/cpu/cpufreq/policy0/scaling_max_freq'), '1800000')

    def test_explicit_original_turbo_disable_is_respected(self):
        self.put('devices/system/cpu/intel_pstate/no_turbo', 1)
        self.unplug()
        with patch.object(self.power, 'sample', return_value={
                'load': 0.95, 'capacity': 80, 'temperature': 60}):
            self.power.step()
        self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '1')

    def test_existing_lower_ceiling_is_preserved_even_during_turbo(self):
        base = 'devices/system/cpu/cpufreq/policy0/'
        self.put(base + 'scaling_max_freq', 2000000)
        self.unplug()
        with patch.object(self.power, 'sample', return_value={
                'load': 0.95, 'capacity': 80, 'temperature': 60}):
            self.power.step()
        self.assertEqual(self.get(base + 'scaling_max_freq'), '2000000')

    def test_unknown_adapter_turns_off_active_turbo_without_losing_snapshot(self):
        self.unplug()
        with patch.object(self.power, 'sample', return_value={
                'load': 0.95, 'capacity': 80, 'temperature': 60}):
            self.power.step()
            self.assertTrue(self.power.turbo.active)
            self.put('class/power_supply/ADP1/online', 'unknown')
            with self.assertRaises(ValueError):
                self.power.step()
        self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '1')
        self.assertTrue(self.power.journal.exists())
        self.assertFalse(json.loads(self.power.status.read_text())['complete'])

    def test_old_journal_is_upgraded_without_overwriting_originals(self):
        self.unplug()
        base = 'devices/system/cpu/cpufreq/policy0/'
        old_targets = {'devices/system/cpu/intel_pstate/no_turbo': '1',
                       base + 'scaling_min_freq': '1200000',
                       base + 'scaling_max_freq': '1200000',
                       base + 'scaling_governor': 'powersave'}
        snapshot = [{'path': name, 'original': self.values[name],
                     'target': target, 'continuous': True}
                    for name, target in old_targets.items()]
        self.power.journal.write_text(json.dumps(snapshot))
        for name, target in old_targets.items():
            self.put(name, target)
        self.assertEqual(self.get('devices/system/cpu/cpufreq/policy0/scaling_max_freq'), '1200000')
        upgraded = hardware.ExtremePower(self.sys, self.state, self.root / 'proc')
        upgraded.step()
        self.assertEqual(self.get('devices/system/cpu/cpufreq/policy0/scaling_max_freq'), '2400000')
        self.put('class/power_supply/ADP1/online', 1)
        upgraded.step()
        for name, value in self.values.items():
            if name == 'class/power_supply/ADP1/online':
                continue
            self.assertEqual(self.get(name), value)

    def test_failed_governor_read_during_burst_immediately_disables_turbo(self):
        self.unplug()
        with patch.object(self.power, 'sample', return_value={
                'load': 0.95, 'capacity': 80, 'temperature': 60}):
            self.power.step()
            self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '0')
            with patch.object(self.power, 'adaptive_governor', side_effect=OSError('unreadable governor')):
                with self.assertRaises(OSError):
                    self.power.step()
        self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '1')
        self.assertTrue(self.power.journal.exists())

    def test_governor_fallback_and_active_intel_pstate(self):
        policy = self.sys / 'devices/system/cpu/cpufreq/policy0'
        self.put('devices/system/cpu/cpufreq/policy0/scaling_available_governors', 'ondemand powersave')
        self.assertEqual(self.power.adaptive_governor(policy), 'ondemand')
        self.put('devices/system/cpu/cpufreq/policy0/scaling_available_governors', 'powersave performance')
        with self.assertRaises(RuntimeError):
            self.power.adaptive_governor(policy)
        self.put('devices/system/cpu/cpufreq/policy0/scaling_driver', 'intel_pstate')
        self.assertEqual(self.power.adaptive_governor(policy), 'powersave')

    def test_load_detects_one_busy_core_and_ignores_iowait_and_guest_double_count(self):
        self.power.proc_dir.mkdir()
        stat = self.power.proc_dir / 'stat'
        stat.write_text('cpu0 100 0 0 100 0 0 0 0 50 0\ncpu1 0 0 0 100 0 0 0 0 0 0\n')
        self.assertIsNone(self.power.cpu_load())
        stat.write_text('cpu0 190 0 0 110 0 0 0 0 140 0\ncpu1 0 0 0 110 90 0 0 0 0 0\n')
        self.assertAlmostEqual(self.power.cpu_load(), 0.9)

    def test_temperature_uses_hottest_cpu_sensor_and_ignores_battery_sensor(self):
        self.put('class/hwmon/hwmon0/name', 'BAT0')
        self.put('class/hwmon/hwmon0/temp1_input', 95000)
        self.put('class/hwmon/hwmon1/name', 'coretemp')
        self.put('class/hwmon/hwmon1/temp1_input', 60000)
        self.put('class/hwmon/hwmon1/temp2_input', 82000)
        self.assertEqual(self.power.cpu_temperature(), 82)


class TurboBudgetTests(unittest.TestCase):
    def setUp(self):
        self.budget = hardware.TurboBudget()

    def update(self, now, load=0.9, capacity=80, temperature=60, permitted=True):
        return self.budget.update(now, load, capacity, temperature, permitted)

    def test_sustained_load_never_retriggers_turbo_after_six_seconds(self):
        self.assertTrue(self.update(0))
        self.assertTrue(self.update(5))
        for now in (6, 30, 60, 300):
            self.assertFalse(self.update(now))

    def test_new_work_needs_three_quiet_seconds_and_thirty_second_cooldown(self):
        self.assertTrue(self.update(0))
        self.assertFalse(self.update(1, load=0.1))
        self.assertFalse(self.update(4, load=0.1))
        self.assertFalse(self.update(29))
        self.assertTrue(self.update(30))

    def test_short_idle_does_not_rearm_turbo(self):
        self.assertTrue(self.update(0))
        self.assertFalse(self.update(10, load=0.1))
        self.assertFalse(self.update(12))
        self.assertFalse(self.update(40))

    def test_temperature_hysteresis_and_immediate_cutoff(self):
        self.assertFalse(self.update(0, temperature=75))
        self.assertTrue(self.update(1, temperature=74))
        self.assertTrue(self.update(2, temperature=79))
        self.assertFalse(self.update(3, temperature=80))

    def test_missing_telemetry_or_low_battery_stops_turbo(self):
        for telemetry in ({'load': None}, {'capacity': None}, {'temperature': None},
                          {'capacity': 20}, {'permitted': False}):
            with self.subTest(telemetry=telemetry):
                self.budget = hardware.TurboBudget()
                self.assertTrue(self.update(0))
                self.assertFalse(self.update(1, **telemetry))


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(manager, 'STATE_DIR', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.originals = {'sleep-display-battery': '600', 'idle-dim-battery': 'false',
                          'idle-brightness': '40', 'idle-dim-time': '120'}
        self.values = self.originals.copy()

    def command(self, action, schema, key, value=None):
        if action == 'set':
            self.values[key] = value
        return self.values[key]

    def test_restore_after_monitor_restart(self):
        saver = manager.DesktopPowerSaver()
        saver.command = self.command
        saver.sync(True)
        self.assertEqual(self.values['sleep-display-battery'], '180')
        restarted = manager.DesktopPowerSaver()
        restarted.command = self.command
        restarted.sync(False)
        self.assertEqual(self.values, self.originals)

    def test_failed_apply_is_retried_without_losing_originals(self):
        saver = manager.DesktopPowerSaver()
        saver.command = self.command
        with patch.object(saver, 'command', side_effect=[*self.originals.values(), RuntimeError('failed')]):
            with self.assertRaises(RuntimeError):
                saver.sync(True)
        saver.sync(True)
        self.assertTrue(saver.applied)
        saver.sync(False)
        self.assertEqual(self.values, self.originals)

    def test_charge_settings_preserve_extreme_mode(self):
        directory = Path(self.temp.name)
        with patch.object(manager, 'CONFIG_DIR', directory), patch.object(manager, 'CONFIG_FILE', directory / 'settings.json'):
            manager.save_settings(80, True)
            manager.save_settings(100)
            self.assertTrue(manager.read_settings()['extreme_battery'])
            self.assertEqual(manager.read_settings()['limit'], 100)

    def test_battery_return_during_partial_restore_reapplies_profile(self):
        saver = manager.DesktopPowerSaver()
        saver.command = self.command
        saver.sync(True)
        with patch.object(saver, 'command', side_effect=RuntimeError('restore failed')):
            with self.assertRaises(RuntimeError):
                saver.sync(False)
        saver.sync(True)
        self.assertTrue(saver.applied)
        self.assertEqual(self.values, saver.targets)
        saver.sync(False)
        self.assertEqual(self.values, self.originals)

    def test_gui_applies_charge_and_installs_selected_mode(self):
        results = [subprocess.CompletedProcess([], 0, '80:enable\n', ''),
                   subprocess.CompletedProcess([], 0),
                   subprocess.CompletedProcess([], 0, 'installed', '')]
        with patch.object(manager, 'read_settings', return_value={'limit': 80}), \
             patch.object(manager, 'battery_state', return_value=(70, 'Discharging', False)), \
             patch.object(manager.subprocess, 'run', side_effect=results) as run, \
             patch.object(manager.subprocess, 'Popen'), \
             patch.object(manager, 'apply_limit') as charge, \
             patch.object(manager, 'save_settings') as save, \
             patch.object(manager, 'dialog'):
            self.assertEqual(manager.gui(), 0)
            charge.assert_called_once_with(80)
            self.assertEqual(run.call_args.args[0][-1], 'enable')
            save.assert_called_with(80, True)

    def test_low_battery_alert_still_rearms_on_ac(self):
        alert = manager.LowBatteryAlert()
        self.assertTrue(alert.should_alert(20, False))
        alert.sent = True
        self.assertFalse(alert.should_alert(19, False))
        self.assertFalse(alert.should_alert(19, True))
        self.assertTrue(alert.should_alert(18, False))


if __name__ == '__main__':
    unittest.main()
