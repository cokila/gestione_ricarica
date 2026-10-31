"""Direction, units, unavailable sensors and app routing for the live window."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_power as fixtures


live = fixtures.load('live_power', 'live-power.py')
manager = fixtures.manager


class LivePowerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sys = self.root / 'sys'
        self.power = self.sys / 'class/power_supply'
        self.status = self.root / 'status.json'
        self.put('BAT0/status', 'Charging')
        self.put('BAT0/capacity', 45)
        self.put('BAT0/current_now', 2200000)
        self.put('BAT0/voltage_now', 11100000)
        self.put('ADP1/online', 1)
        self.status.write_text(json.dumps({'updated_at': 1000, 'cpu_package_w': 7.5,
                                          'temperature': 66, 'mode': 'ac', 'complete': True}))

    def put(self, name, value):
        path = self.power / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))

    def sample(self):
        return live.live_readings(self.power, self.status, self.sys, now=1001)

    def test_charging_watts_are_separate_from_unknown_adapter_input_and_computer_load(self):
        data = self.sample()
        self.assertAlmostEqual(data['charge_w'], 24.42)
        self.assertEqual(data['discharge_w'], 0)
        self.assertIsNone(data['adapter_w'])
        self.assertIsNone(data['computer_w'])
        self.assertEqual(data['cpu_w'], 7.5)
        self.assertEqual(data['capacity'], 45)

    def test_unplugged_discharge_is_total_computer_power(self):
        self.put('ADP1/online', 0)
        self.put('BAT0/status', 'Discharging')
        self.put('BAT0/current_now', -3000000)
        data = self.sample()
        self.assertEqual(data['charge_w'], 0)
        self.assertAlmostEqual(data['computer_w'], 33.3)
        self.assertEqual(data['adapter_w'], 0)

    def test_battery_can_supplement_ac_without_being_mislabeled_as_total_consumption(self):
        self.put('BAT0/status', 'Discharging')
        data = self.sample()
        self.assertAlmostEqual(data['discharge_w'], 24.42)
        self.assertIsNone(data['computer_w'])

    def test_power_now_takes_precedence_over_current_times_voltage(self):
        self.put('BAT0/power_now', 19000000)
        self.assertEqual(self.sample()['charge_w'], 19)

    def test_rated_or_limited_adapter_power_is_never_shown_as_actual_input(self):
        self.put('ADP1/input_power_limit', 85000000)
        self.put('ADP1/power_max', 85000000)
        self.assertIsNone(self.sample()['adapter_w'])

    def test_actual_adapter_input_is_reported_if_sensor_exists(self):
        self.put('ADP1/power_now', 53000000)
        data = self.sample()
        self.assertEqual(data['adapter_w'], 53)
        self.assertAlmostEqual(data['charge_w'], 24.42)
        self.assertIsNone(data['computer_w'])

    def test_incomplete_measurements_from_multiple_adapters_are_not_summed(self):
        self.put('ADP1/power_now', 53000000)
        self.put('USB/online', 1)
        self.assertIsNone(self.sample()['adapter_w'])

    def test_missing_ac_state_does_not_guess_total_power(self):
        (self.power / 'ADP1/online').unlink()
        self.put('BAT0/status', 'Discharging')
        data = self.sample()
        self.assertIsNone(data['online'])
        self.assertIsNone(data['computer_w'])
        self.assertAlmostEqual(data['discharge_w'], 24.42)

    def test_missing_current_does_not_become_zero_watts(self):
        (self.power / 'BAT0/current_now').unlink()
        self.assertIsNone(self.sample()['charge_w'])
        self.assertEqual(live.display_watts(self.sample()['charge_w']), '—')

    def test_stale_current_during_full_state_is_not_shown_as_active_charging(self):
        self.put('BAT0/status', 'Full')
        self.assertIsNone(self.sample()['charge_w'])
        self.put('BAT0/current_now', 0)
        self.assertEqual(self.sample()['charge_w'], 0)

    def test_stale_service_values_are_hidden_and_temperature_uses_direct_sensor(self):
        self.status.write_text(json.dumps({'updated_at': 900, 'cpu_package_w': 20, 'temperature': 99}))
        sensor = self.sys / 'class/hwmon/hwmon2'
        sensor.mkdir(parents=True)
        (sensor / 'name').write_text('coretemp')
        (sensor / 'temp1_input').write_text('64000')
        data = self.sample()
        self.assertIsNone(data['cpu_w'])
        self.assertEqual(data['temperature'], 64)
        self.assertFalse(data['fresh_service'])
        self.assertAlmostEqual(data['charge_w'], 24.42)

    def test_clock_in_future_and_invalid_metrics_are_not_trusted(self):
        self.status.write_text(json.dumps({'updated_at': 1010, 'cpu_package_w': 12}))
        self.assertIsNone(self.sample()['cpu_w'])
        self.status.write_text(json.dumps({'updated_at': 1000, 'cpu_package_w': float('nan')}))
        self.assertIsNone(self.sample()['cpu_w'])

    def test_plug_transition_within_sample_is_not_given_a_false_direction(self):
        with patch.object(live, 'read_text', side_effect=['Charging', 'Discharging']):
            data = self.sample()
        self.assertIsNone(data['charge_w'])
        self.assertIsNone(data['discharge_w'])

    def test_malformed_values_are_not_displayed_as_real_data(self):
        self.put('BAT0/capacity', 120)
        self.put('BAT0/current_now', 'not a number')
        data = self.sample()
        self.assertIsNone(data['capacity'])
        self.assertIsNone(data['charge_w'])


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(manager, 'STATE_DIR', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_desktop_opens_live_window_without_applying_charge_settings(self):
        with patch.object(manager.sys, 'argv', ['charge-manager.py']), \
             patch.object(manager, 'open_live_dashboard', return_value=0) as dashboard, \
             patch.object(manager, 'gui') as settings:
            self.assertEqual(manager.main(), 0)
            dashboard.assert_called_once_with(wait=True)
            settings.assert_not_called()

    def test_settings_flag_keeps_existing_eighty_hundred_percent_flow(self):
        with patch.object(manager.sys, 'argv', ['charge-manager.py', '--settings']), \
             patch.object(manager, 'open_live_dashboard') as dashboard, \
             patch.object(manager, 'gui', return_value=0) as settings:
            self.assertEqual(manager.main(), 0)
            settings.assert_called_once_with()
            dashboard.assert_not_called()

    def test_background_monitor_still_uses_original_entry_point(self):
        with patch.object(manager.sys, 'argv', ['charge-manager.py', '--monitor', '--replace']), \
             patch.object(manager, 'monitor', return_value=0) as monitor, \
             patch.object(manager, 'open_live_dashboard') as dashboard:
            self.assertEqual(manager.main(), 0)
            monitor.assert_called_once_with(True)
            dashboard.assert_not_called()


if __name__ == '__main__':
    unittest.main()
