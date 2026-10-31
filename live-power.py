#!/usr/bin/python3
"""Read-only live power window. Charge policy remains in charge-manager.py."""

import argparse
from collections import deque
import json
import math
from pathlib import Path
import subprocess
import sys
import time


APP_DIR = Path(__file__).resolve().parent
POWER_DIR = Path('/sys/class/power_supply')
STATUS_FILE = Path('/run/macbook-extreme-power/status.json')
SETTINGS_FILE = Path.home() / '.config/macbook-charge/settings.json'
SERIES = (('charge_w', 'Verso la batteria', (0.15, 0.65, 0.38)),
          ('computer_w', 'Computer a batteria', (0.91, 0.48, 0.15)),
          ('cpu_w', 'CPU', (0.20, 0.49, 0.91)))


def read_text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


def read_number(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_json(path):
    try:
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def measured_watts(device):
    """Use NOW measurements, never a nominal rating or a programmed limit."""
    power = read_number(device / 'power_now')
    if power is not None:
        watts = abs(power) / 1e6
    else:
        current = read_number(device / 'current_now')
        voltage = read_number(device / 'voltage_now')
        if current is None or voltage is None or voltage <= 0:
            return None
        watts = abs(current) * voltage / 1e12
    return watts if 0 <= watts <= 1000 else None


def cpu_temperature(sys_dir):
    readings = []
    for device in (sys_dir / 'class/hwmon').glob('*'):
        if read_text(device / 'name') == 'coretemp':
            for path in device.glob('temp*_input'):
                value = read_number(path)
                if value is not None and 0 < value < 150000:
                    readings.append(value / 1000)
    return max(readings) if readings else None


def live_readings(power_dir=POWER_DIR, status_file=STATUS_FILE,
                  sys_dir=Path('/sys'), now=None):
    now = time.time() if now is None else now
    battery = power_dir / 'BAT0'
    capacity = read_number(battery / 'capacity')
    if capacity is not None and not 0 <= capacity <= 100:
        capacity = None
    state = read_text(battery / 'status')
    watts = measured_watts(battery)
    if state != read_text(battery / 'status'):
        watts = None  # Plug/unplug during a sample: wait for a coherent reading.
    adapters = list(power_dir.glob('*/online'))
    adapter_states = [read_number(path) for path in adapters]
    online = any(adapter_states) if adapters and all(value in (0, 1) for value in adapter_states) else None
    adapter_w = None
    if online is False:
        adapter_w = 0.0
    elif online:
        powers = [measured_watts(path.parent) for path, value in zip(adapters, adapter_states) if value == 1]
        if all(value is not None for value in powers):
            adapter_w = sum(powers)
    charge_w = discharge_w = None
    if watts is not None and state in ('Charging', 'Discharging'):
        charge_w = watts if state == 'Charging' else 0.0
        discharge_w = watts if state == 'Discharging' else 0.0
    elif watts == 0 and state in ('Full', 'Not charging'):
        charge_w = discharge_w = 0.0
    status = read_json(status_file)
    updated = status.get('updated_at')
    fresh = isinstance(updated, (int, float)) and 0 <= now - updated < 10

    def metric(key, maximum):
        value = status.get(key) if fresh else None
        return value if isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= maximum else None

    temperature = metric('temperature', 150)
    if temperature is None:
        temperature = cpu_temperature(sys_dir)
    current = read_number(battery / 'current_now')
    voltage = read_number(battery / 'voltage_now')
    return {'online': online, 'capacity': capacity, 'battery_status': state,
            'charge_w': charge_w, 'discharge_w': discharge_w,
            'adapter_w': adapter_w,
            'computer_w': discharge_w if online is False and state == 'Discharging' else None,
            'cpu_w': metric('cpu_package_w', 300), 'temperature': temperature,
            'current_a': abs(current) / 1e6 if current is not None else None,
            'voltage_v': voltage / 1e6 if voltage is not None and voltage > 0 else None,
            'fresh_service': fresh, 'service': status if fresh else {}, 'sample_time': now}


def display_watts(value):
    return f'{value:.1f} W' if value is not None else '—'


def draw_history(context, width, height, history, foreground=(0.25, 0.25, 0.25)):
    """Render the actual window plot; also usable for a headless preview."""
    left, right, top, bottom = 48, max(49, width - 16), 12, max(13, height - 27)
    valid = [row.get(key) for _, row in history for key, _, _ in SERIES
             if row.get(key) is not None]
    ceiling = max(10, math.ceil(max(valid, default=0) / 10) * 10)
    context.select_font_face('Sans')
    context.set_font_size(10)
    for step in range(5):
        y = bottom - (bottom - top) * step / 4
        context.set_source_rgba(*foreground, 0.15)
        context.set_line_width(1)
        context.move_to(left, y)
        context.line_to(right, y)
        context.stroke()
        context.set_source_rgb(*foreground)
        context.move_to(3, y + 3)
        context.show_text(f'{ceiling * step / 4:g} W')
    end = history[-1][0] if history else time.monotonic()
    for seconds in (120, 60, 0):
        x = right - (right - left) * seconds / 120
        context.set_source_rgb(*foreground)
        context.move_to(max(0, x - 15), height - 5)
        context.show_text('ora' if seconds == 0 else f'-{seconds} s')
    context.save()
    context.rectangle(left, top, right-left, bottom-top)
    context.clip()
    for key, _, color in SERIES:
        context.set_source_rgb(*color)
        context.set_line_width(2)
        drawing = False
        previous_time = None
        for stamp, row in history:
            value = row.get(key)
            if value is None:
                if drawing:
                    context.stroke()
                drawing = False
                previous_time = None
                continue
            x = right - (right-left) * (end-stamp) / 120
            y = bottom - (bottom-top) * value / ceiling
            if drawing and stamp - previous_time <= 3:
                context.line_to(x, y)
            else:
                if drawing:
                    context.stroke()
                context.move_to(x, y)
            drawing = True
            previous_time = stamp
        context.stroke()
    context.restore()


def run_window():
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import GLib, Gtk

    available, _ = Gtk.init_check(None)
    if not available:
        print('Apri Gestione ricarica dalla sessione grafica del Desktop.', file=sys.stderr)
        return 1
    window = Gtk.Window(title='Gestione ricarica')
    window.set_icon_name('battery')
    window.set_default_size(880, 710)
    header = Gtk.HeaderBar(title='Gestione ricarica', subtitle='Ricarica e consumi in tempo reale')
    header.set_show_close_button(True)
    window.set_titlebar(header)
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    window.add(scroll)
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content.set_border_width(18)
    scroll.add(content)

    def label(text='', dim=False):
        widget = Gtk.Label(label=text)
        widget.set_xalign(0)
        widget.set_line_wrap(True)
        if dim:
            widget.get_style_context().add_class('dim-label')
        return widget

    supply_label = label()
    content.pack_start(supply_label, False, False, 0)
    battery_bar = Gtk.ProgressBar()
    battery_bar.set_show_text(True)
    content.pack_start(battery_bar, False, False, 0)
    grid = Gtk.Grid(column_spacing=12, row_spacing=12)
    grid.set_column_homogeneous(True)
    content.pack_start(grid, False, False, 0)
    cards = {}
    titles = (('charge_w', 'Entrata nella batteria'), ('computer_w', 'Consumo del computer'),
              ('cpu_w', 'Consumo CPU'), ('adapter_w', 'Totale dall’alimentatore'))
    for index, (key, title) in enumerate(titles):
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(12)
        box.pack_start(label(title), False, False, 0)
        value = label('—')
        value.set_markup('<span size="24000" weight="bold">—</span>')
        box.pack_start(value, False, False, 0)
        note = label('', dim=True)
        note.set_max_width_chars(44)
        box.pack_start(note, False, False, 0)
        frame.add(box)
        grid.attach(frame, index % 2, index // 2, 1, 1)
        cards[key] = (value, note)
    detail_label = label('', dim=True)
    content.pack_start(detail_label, False, False, 0)
    graph_heading = label('Ultimi 2 minuti · aggiornamento ogni secondo')
    content.pack_start(graph_heading, False, False, 0)
    graph = Gtk.DrawingArea()
    graph.set_size_request(500, 150)
    content.pack_start(graph, True, True, 0)
    legend = Gtk.Box(spacing=18)
    for _, text, color in SERIES:
        rgb = '#' + ''.join(f'{int(channel*255):02x}' for channel in color)
        item = label()
        item.set_markup(f'<span foreground="{rgb}">●</span> {text}')
        legend.pack_start(item, False, False, 0)
    content.pack_start(legend, False, False, 0)
    policy_label = label()
    content.pack_start(policy_label, False, False, 0)
    service_label = label('', dim=True)
    content.pack_start(service_label, False, False, 0)
    controls = Gtk.Box(spacing=12)
    settings_button = Gtk.Button(label='Impostazioni 80% / 100% e risparmio')
    controls.pack_start(settings_button, False, False, 0)
    stamp_label = label('', dim=True)
    controls.pack_end(stamp_label, False, False, 0)
    content.pack_start(controls, False, False, 0)
    history = deque(maxlen=121)
    settings_process = None

    def open_settings(_button):
        nonlocal settings_process
        if settings_process is None or settings_process.poll() is not None:
            settings_process = subprocess.Popen([sys.executable, str(APP_DIR / 'charge-manager.py'), '--settings'])
            settings_button.set_sensitive(False)

    settings_button.connect('clicked', open_settings)

    def draw(widget, context):
        color = widget.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        draw_history(context, widget.get_allocated_width(), widget.get_allocated_height(),
                     history, (color.red, color.green, color.blue))
        return False

    graph.connect('draw', draw)

    def refresh():
        data = live_readings()
        online, state = data['online'], data['battery_status']
        supply_label.set_text('Alimentatore collegato' if online is True else
                              'Alimentazione a batteria' if online is False else 'Alimentazione non leggibile')
        capacity = data['capacity']
        battery_bar.set_fraction(capacity / 100 if capacity is not None else 0)
        battery_bar.set_text(f'Batteria {capacity}%' if capacity is not None else 'Batteria non leggibile')
        for key, (value, note) in cards.items():
            value.set_markup(f'<span size="24000" weight="bold">{display_watts(data[key])}</span>')
        if state == 'Charging':
            cards['charge_w'][1].set_text('Potenza netta di ricarica, misurata alla batteria.')
        elif state == 'Discharging':
            cards['charge_w'][1].set_text('Batteria in scarica: ' + display_watts(data['discharge_w']))
        elif state in ('Full', 'Not charging'):
            cards['charge_w'][1].set_text('Ricarica ferma: batteria piena o soglia raggiunta.')
        else:
            cards['charge_w'][1].set_text('Misura di ricarica non disponibile.')
        cards['computer_w'][1].set_text('Assorbimento misurato dalla batteria.' if data['computer_w'] is not None else
                                         'Il consumo totale su corrente non è esposto dai sensori.' if online else
                                         'Misura del consumo non disponibile.')
        cards['cpu_w'][1].set_text('Solo processore; esclusi schermo, GPU e altri componenti.' if data['cpu_w'] is not None else
                                   'Misura CPU in attesa del servizio energetico.')
        cards['adapter_w'][1].set_text('Ingresso misurato sul lato alimentatore.' if data['adapter_w'] is not None and online else
                                       'Alimentatore scollegato.' if online is False else
                                       'Il MacBook non espone la potenza totale in ingresso.')
        details = []
        if data['voltage_v'] is not None:
            details.append(f'Batteria {data["voltage_v"]:.2f} V')
        if data['current_a'] is not None:
            details.append(f'{data["current_a"]:.2f} A')
        if data['temperature'] is not None:
            details.append(f'CPU {data["temperature"]:.0f} °C')
        detail_label.set_text(' · '.join(details))
        settings = read_json(SETTINGS_FILE)
        limit = settings.get('limit')
        policy_label.set_text((f'Soglia scelta: {limit}% · ' if limit in (80, 100) else '')
                              + 'La corrente di ricarica resta gestita dal controller.')
        status = data['service']
        if online:
            message = 'Con alimentatore: impostazioni precedenti ripristinate.' if status.get('complete') and status.get('mode') == 'ac' else 'Alimentatore collegato: in attesa dello stato del servizio.'
        elif status.get('active'):
            message = 'Risparmio intelligente: ' + {'balanced': 'prestazioni e autonomia', 'thermal': 'raffreddamento',
                       'battery-low': 'batteria bassa', 'battery-critical': 'batteria quasi scarica',
                       'unknown': 'sensori da verificare'}.get(status.get('mode'), 'attivo')
        else:
            message = 'Le misure della batteria si aggiornano anche senza il servizio energetico.'
        if status and not status.get('complete'):
            message += ' Alcuni controlli richiedono verifica nelle impostazioni.'
        service_label.set_text(message)
        history.append((time.monotonic(), data))
        graph.queue_draw()
        stamp_label.set_text('Aggiornato ' + time.strftime('%H:%M:%S'))
        if settings_process is not None and settings_process.poll() is not None:
            settings_button.set_sensitive(True)
        return True

    refresh()
    timer = GLib.timeout_add_seconds(1, refresh)

    def close(_window):
        GLib.source_remove(timer)
        Gtk.main_quit()

    window.connect('destroy', close)
    window.show_all()
    Gtk.main()
    # Keep the desktop terminal available for an already opened authentication.
    if settings_process is not None and settings_process.poll() is None:
        settings_process.wait()
    return 0


def main():
    parser = argparse.ArgumentParser(description='Watt di ricarica e consumi in tempo reale.')
    parser.add_argument('--readings', action='store_true', help='Mostra una lettura JSON senza aprire finestre.')
    args = parser.parse_args()
    if args.readings:
        print(json.dumps(live_readings(), indent=2))
        return 0
    return run_window()


if __name__ == '__main__':
    sys.exit(main())
