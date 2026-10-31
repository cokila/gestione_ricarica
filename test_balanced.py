execute failed:
    Could not start serviceManager:
        Cannot locate HTTP port of service:
            Request for service HTTP address failed:
                Cannot connect to ipc via "/tmp/state-ipc/state-release.sock":
                    'dial unix /tmp/state-ipc/state-release.sock: connect: operation not permitted':
                        'connect: operation not permitted': operation not permitted
"""Power-budget integration, measurement, GPU recovery and diagnostics tests."""

import csv
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_power as fixtures

hardware, manager = fixtures.hardware, fixtures.manager


class RAPLTests(unittest.TestCase):
    put = fixtures.HardwareTests.put
    get = fixtures.HardwareTests.get
    unplug = fixtures.HardwareTests.unplug
    rapl = 'class/powercap/intel-rapl:0/'
    cpu = 'devices/system/cpu/cpufreq/policy0/'

    def setUp(self):
        fixtures.HardwareTests.setUp(self)
        values = {
            self.rapl + 'enabled': '1',
            self.rapl + 'constraint_0_name': 'long_term',
            self.rapl + 'constraint_0_power_limit_uw': '100000000',
            self.rapl + 'constraint_0_max_power_uw': '45000000',
            self.rapl + 'constraint_0_time_window_us': '27983872',
            self.rapl + 'constraint_1_name': 'short_term',
            self.rapl + 'constraint_1_power_limit_uw': '125000000',
            self.rapl + 'constraint_1_max_power_uw': '0',
            self.rapl + 'constraint_1_time_window_us': '976',
            'module/snd_hda_intel/parameters/power_save': '0',
            'module/snd_hda_intel/parameters/power_save_controller': 'N',
            'class/scsi_host/host0/link_power_management_policy': 'max_performance',
        }
        self.values.update(values)
        for name, value in values.items():
            self.put(name, value)
        self.telemetry = {'load': 0.9, 'capacity': 80, 'temperature': 60}
        patcher = patch.object(self.power, 'sample', side_effect=lambda: self.telemetry.copy())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_native_turbo_is_not_cut_by_a_timer_and_all_controls_restore_on_ac(self):
        self.unplug()
        for now in (0, 6, 30, 300):
            self.now = now
            self.assertEqual(self.power.step(), [])
            self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '0')
            self.assertEqual(self.get(self.cpu + 'scaling_max_freq'), '3600000')
        self.assertEqual(self.get(self.cpu + 'scaling_min_freq'), '1200000')
        self.assertEqual(self.get(self.cpu + 'scaling_governor'), 'schedutil')
        self.assertEqual(self.get(self.rapl + 'constraint_0_power_limit_uw'), '25000000')
        self.assertEqual(self.get(self.rapl + 'constraint_1_power_limit_uw'), '40000000')
        self.assertEqual(self.get(self.rapl + 'constraint_0_time_window_us'), '8000000')
        self.assertEqual(self.get(self.rapl + 'constraint_1_time_window_us'), '976')
        self.assertEqual(self.get('module/snd_hda_intel/parameters/power_save'), '10')
        self.assertEqual(self.get('class/scsi_host/host0/link_power_management_policy'), 'med_power_with_dipm')
        self.put('class/power_supply/ADP1/online', 1)
        self.assertEqual(self.power.step(), [])
        for name, value in self.values.items():
            self.assertEqual(self.get(name), value)

    def test_low_and_critical_battery_reduce_budgets_without_freezing_at_minimum(self):
        self.unplug()
        for capacity, limits in ((20, (18000000, 28000000)), (10, (12000000, 18000000))):
            with self.subTest(capacity=capacity):
                self.telemetry['capacity'] = capacity
                self.assertEqual(self.power.step(), [])
                self.assertEqual(self.get(self.rapl + 'constraint_0_power_limit_uw'), str(limits[0]))
                self.assertEqual(self.get(self.rapl + 'constraint_1_power_limit_uw'), str(limits[1]))
                self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '1')
                self.assertGreater(int(self.get(self.cpu + 'scaling_max_freq')),
                                   int(self.get(self.cpu + 'scaling_min_freq')))

    def test_temperature_hysteresis_retains_cooling_mode_until_72_degrees(self):
        self.unplug()
        self.power.step()
        for temperature, mode in ((80, 'thermal'), (76, 'thermal'), (73, 'thermal'), (72, 'balanced')):
            self.telemetry['temperature'] = temperature
            self.assertEqual(self.power.step(), [])
            self.assertEqual(self.power.mode, mode)
            self.assertEqual(self.power.turbo.active, mode == 'balanced')

    def test_existing_stricter_power_budgets_are_preserved(self):
        self.put(self.rapl + 'constraint_0_power_limit_uw', 15000000)
        self.put(self.rapl + 'constraint_1_power_limit_uw', 20000000)
        self.unplug()
        self.power.step()
        self.assertEqual(self.get(self.rapl + 'constraint_0_power_limit_uw'), '15000000')
        self.assertEqual(self.get(self.rapl + 'constraint_1_power_limit_uw'), '20000000')

    def test_partial_rapl_rejection_restores_previous_values_and_uses_frequency_fallback(self):
        self.unplug()
        write = self.power.write

        def reject_short(item, key):
            if item['path'] == self.rapl + 'constraint_1_power_limit_uw' and key == 'target':
                raise OSError('Firmware locked')
            write(item, key)

        with patch.object(self.power, 'write', side_effect=reject_short):
            self.assertEqual(self.power.step(), [])
        self.assertFalse(self.power.rapl_ready)
        self.assertEqual(self.get(self.rapl + 'constraint_0_power_limit_uw'), '100000000')
        self.assertEqual(self.get(self.rapl + 'constraint_0_time_window_us'), '27983872')
        self.now = 10
        self.assertEqual(self.power.step(), [])
        self.assertEqual(self.get(self.cpu + 'scaling_max_freq'), '2400000')
        status = json.loads(self.power.status.read_text())
        self.assertEqual(status['controller'], 'frequency')
        self.assertTrue(status['warnings'])

    def test_disabled_rapl_is_never_enabled_implicitly(self):
        self.put(self.rapl + 'enabled', 0)
        self.unplug()
        self.power.step()
        self.assertFalse(self.power.rapl_ready)
        self.assertEqual(self.get(self.rapl + 'enabled'), '0')
        self.assertEqual(self.get(self.rapl + 'constraint_0_power_limit_uw'), '100000000')

    def test_firmware_minimum_above_budget_uses_fallback(self):
        self.put(self.rapl + 'constraint_0_min_power_uw', 30000000)
        self.unplug()
        self.power.step()
        self.assertFalse(self.power.rapl_ready)
        self.assertEqual(self.get(self.rapl + 'constraint_0_power_limit_uw'), '100000000')

    def test_hardware_rounding_is_accepted_without_constant_rewrites(self):
        self.unplug()
        write_text = Path.write_text
        rounded_writes = []

        def quantize(path, data, *args, **kwargs):
            if str(path).endswith('constraint_0_time_window_us') and data == '8000000\n':
                rounded_writes.append(path)
                data = '7995392\n'
            return write_text(path, data, *args, **kwargs)

        with patch.object(Path, 'write_text', quantize):
            self.assertEqual(self.power.step(), [])
            self.now = 1
            self.assertEqual(self.power.step(), [])
        self.assertTrue(self.power.rapl_ready)
        self.assertEqual(len(rounded_writes), 1)

    def test_unconfirmed_writes_do_not_count_as_rapl_success(self):
        self.unplug()
        write_text = Path.write_text

        def ignore_rapl(path, data, *args, **kwargs):
            if str(path).endswith('constraint_0_power_limit_uw'):
                return len(data)
            return write_text(path, data, *args, **kwargs)

        with patch.object(Path, 'write_text', ignore_rapl):
            self.assertEqual(self.power.step(), [])
        self.assertFalse(self.power.rapl_ready)
        self.assertTrue(self.power.warnings)

    def test_unknown_ac_disables_native_turbo_and_retains_originals(self):
        self.unplug()
        self.power.step()
        self.put('class/power_supply/ADP1/online', 'unknown')
        with self.assertRaises(ValueError):
            self.power.step()
        self.assertEqual(self.get('devices/system/cpu/intel_pstate/no_turbo'), '1')
        self.assertTrue(self.power.journal.exists())

    def test_optional_audio_failure_does_not_prevent_cpu_profile(self):
        self.unplug()
        write = self.power.write

        def fail_audio(item, key):
            if 'snd_hda_intel' in item['path']:
                raise OSError('Audio control unavailable')
            write(item, key)

        with patch.object(self.power, 'write', side_effect=fail_audio):
            self.assertEqual(self.power.step(), [])
        self.assertTrue(self.power.rapl_ready)
        self.assertTrue(self.power.warnings)

    def test_failed_budget_restore_retains_journal_for_retry(self):
        self.unplug()
        self.power.step()
        self.put('class/power_supply/ADP1/online', 1)
        write = self.power.write

        def fail_restore(item, key):
            if item.get('kind') == 'rapl' and key == 'original':
                raise OSError('Temporary restore failure')
            write(item, key)

        with patch.object(self.power, 'write', side_effect=fail_restore):
            self.assertTrue(self.power.step())
        self.assertTrue(self.power.journal.exists())
        self.assertEqual(self.power.step(), [])
        self.assertFalse(self.power.journal.exists())
        self.assertEqual(self.get(self.rapl + 'constraint_0_power_limit_uw'), '100000000')


class MeasurementTests(unittest.TestCase):
    put = fixtures.HardwareTests.put

    def setUp(self):
        fixtures.HardwareTests.setUp(self)
        self.power.proc_dir.mkdir()
        self.put('class/power_supply/BAT0/capacity', 80)
        self.put('class/power_supply/BAT0/status', 'Discharging')
        self.put('class/power_supply/BAT0/current_now', 2000000)
        self.put('class/power_supply/BAT0/voltage_now', 12000000)
        self.put('class/powercap/intel-rapl:0/energy_uj', 900000)
        self.put('class/powercap/intel-rapl:0/max_energy_range_uj', 1000000)

    def test_battery_current_voltage_and_package_counter_wrap_are_measured(self):
        first = self.power.metrics.sample(False)
        self.assertEqual(first['battery_w'], 24)
        self.assertIsNone(first['cpu_package_w'])
        self.now = 1
        self.put('class/powercap/intel-rapl:0/energy_uj', 100000)
        second = self.power.metrics.sample(False)
        self.assertAlmostEqual(second['cpu_package_w'], 0.2)
        self.assertAlmostEqual(second['measured_energy_wh'], 24 / 3600)
        self.assertEqual(second['measured_seconds'], 1)

    def test_missing_sensor_does_not_fabricate_zero_power(self):
        self.power.metrics.sample(False)
        (self.sys / 'class/power_supply/BAT0/current_now').unlink()
        self.now = 1
        measured = self.power.metrics.sample(False)
        self.assertIsNone(measured['battery_w'])
        self.assertIsNone(measured['battery_average_w'])

    def test_suspend_gap_is_not_integrated_and_ac_resets_battery_session(self):
        self.power.metrics.sample(False)
        self.now = 100
        self.put('class/power_supply/BAT0/current_now', 1000000)
        measured = self.power.metrics.sample(False)
        self.assertEqual(measured['measured_energy_wh'], 0)
        self.assertEqual(measured['battery_average_w'], 12)
        measured = self.power.metrics.sample(True)
        self.assertIsNone(measured['battery_w'])
        self.assertEqual(measured['measured_seconds'], 0)

    def test_process_deltas_ignore_reused_pids(self):
        proc = self.power.proc_dir / '123'
        proc.mkdir()

        def write_stat(ticks, start):
            fields = ['0'] * 22
            fields[0] = 'S'
            fields[11], fields[19] = str(ticks), str(start)
            (proc / 'stat').write_text('123 (worker job) ' + ' '.join(fields))

        write_stat(100, 1000)
        self.assertEqual(self.power.metrics.processes(0), [])
        write_stat(100 + os.sysconf('SC_CLK_TCK') * 2, 1000)
        self.assertEqual(self.power.metrics.processes(10), [{'name': 'worker job', 'cpu_percent': 20}])
        write_stat(90000, 2000)
        self.assertEqual(self.power.metrics.processes(20), [])


class GPUAndDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(manager, 'STATE_DIR', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gpu_mode = 1

    def command(self, *args):
        if args[0] == '-q':
            return str(self.gpu_mode)
        self.gpu_mode = int(args[1].rsplit('=', 1)[1])
        return ''

    def test_gpu_mode_restores_after_monitor_restart(self):
        saver = manager.NvidiaPowerSaver()
        saver.command = self.command
        saver.sync(True, 0)
        self.assertEqual(self.gpu_mode, 0)
        restarted = manager.NvidiaPowerSaver()
        restarted.command = self.command
        restarted.sync(False, 1)
        self.assertEqual(self.gpu_mode, 1)
        self.assertFalse(restarted.journal.exists())

    def test_partial_gpu_failure_does_not_delay_restore_on_ac(self):
        saver = manager.NvidiaPowerSaver()
        saver.command = self.command
        with patch.object(saver, 'set_mode', side_effect=RuntimeError('Failed verification')):
            saver.sync(True, 0)
        self.assertEqual(saver.retry_at, 60)
        self.gpu_mode = 0
        saver.sync(False, 1)
        self.assertEqual(self.gpu_mode, 1)
        self.assertFalse(saver.journal.exists())

    def test_unavailable_gpu_is_reported_without_claiming_success(self):
        saver = manager.NvidiaPowerSaver()
        with patch.object(saver, 'command', side_effect=RuntimeError('No display')) as command:
            saver.sync(True, 0)
            saver.sync(True, 10)
            self.assertEqual(command.call_count, 1)
        status = json.loads((self.root / 'gpu-status.json').read_text())
        self.assertFalse(status['complete'])
        self.assertFalse(saver.journal.exists())

    def test_consumption_log_uses_fresh_status_and_thirty_second_intervals(self):
        status = {'profile': manager.PROFILE, 'updated_at': 1000,
                  'battery_w': 24, 'controller': 'rapl', 'online': False}
        recorder = manager.PowerLog()
        with patch.object(manager, 'read_power_status', return_value=status), \
             patch.object(manager.time, 'time', return_value=1000):
            recorder.record(0)
            recorder.record(10)
            recorder.record(30)
        with (self.root / 'consumi.csv').open() as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['battery_w'], '24')
        self.assertEqual(rows[0]['cpu_package_w'], '')

    def test_stale_status_is_not_logged_or_shown_as_live(self):
        status = {'profile': manager.PROFILE, 'updated_at': 800}
        with patch.object(manager, 'read_power_status', return_value=status), \
             patch.object(manager.time, 'time', return_value=1000):
            manager.PowerLog().record(0)
            self.assertIn('non sta aggiornando', manager.diagnostics_text())
        self.assertFalse((self.root / 'consumi.csv').exists())

    def test_old_profile_is_reported_as_awaiting_activation(self):
        with patch.object(manager, 'read_power_status', return_value={'profile': 'smart-turbo-v1'}):
            self.assertIn('non è ancora attivo', manager.diagnostics_text())

    def test_consumption_button_opens_live_window_without_applying_settings(self):
        choices = [subprocess.CompletedProcess([], 1, 'Consumi\n', ''),
                   subprocess.CompletedProcess([], 1, '', '')]
        with patch.object(manager, 'read_settings', return_value={'limit': 80}), \
             patch.object(manager, 'read_power_status', return_value={}), \
             patch.object(manager, 'battery_state', return_value=(70, 'Discharging', False)), \
             patch.object(manager.subprocess, 'run', side_effect=choices), \
             patch.object(manager, 'apply_limit') as charge, \
             patch.object(manager, 'save_settings') as save, \
             patch.object(manager, 'open_live_dashboard') as live:
            self.assertEqual(manager.gui(), 0)
            charge.assert_not_called()
            save.assert_not_called()
            live.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
