"""Explicit macOS page capture and physical input for browser-native popups.

No GUI access happens at import. The caller supplies a freshly verified extension
tab state and a browser PID from the native connection, never from page content.
"""
import base64
from contextlib import contextmanager
import ctypes
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

from . import macos


class NativeInputError(RuntimeError):
    def __init__(self, message, code='native_target_changed', *, action_may_have_run=False):
        super().__init__(message)
        self.code = code
        self.action_may_have_run = action_may_have_run
        self.input_dispatched = None if action_may_have_run else False
        self.requires_user_action = code in ('native_accessibility_required', 'native_screen_recording_required', 'native_platform_unsupported', 'native_focus_required', 'native_target_obscured', 'native_dialog_unsupported')
        self.retryable = False


class _Point(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


class _Size(ctypes.Structure):
    _fields_ = [('width', ctypes.c_double), ('height', ctypes.c_double)]


class _System(macos._NativeReader):
    def __init__(self):
        super().__init__()
        self.ax = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        ref = ctypes.c_void_p
        for name, result, args in (
            ('AXIsProcessTrusted', ctypes.c_bool, []),
            ('AXUIElementCreateApplication', ref, [ctypes.c_int]),
            ('AXUIElementCopyAttributeValue', ctypes.c_int, [ref, ref, ctypes.POINTER(ref)]),
            ('AXUIElementSetMessagingTimeout', ctypes.c_int, [ref, ctypes.c_float]),
            ('AXValueGetValue', ctypes.c_bool, [ref, ctypes.c_int, ctypes.c_void_p]),
            ('AXUIElementPerformAction', ctypes.c_int, [ref, ref]),
            ('AXUIElementCopyActionNames', ctypes.c_int, [ref, ctypes.POINTER(ref)]),
        ):
            self._bind(self.ax, name, result, args)
        self._bind(self.cf, 'CFRetain', ref, [ref])
        self._bind(self.cf, 'CFEqual', ctypes.c_bool, [ref, ref])
        self._bind(self.cf, 'CFURLGetTypeID', ctypes.c_ulong, [])
        self._bind(self.cf, 'CFURLGetString', ref, [ref])
        self._bind(self.cg, 'CGPreflightScreenCaptureAccess', ctypes.c_bool, [])
        self._bind(self.cg, 'CGPreflightPostEventAccess', ctypes.c_bool, [])
        self._bind(self.cg, 'CGEventCreateMouseEvent', ref, [ref, ctypes.c_uint32, _Point, ctypes.c_uint32])
        self._bind(self.cg, 'CGEventPost', None, [ctypes.c_uint32, ref])
        self._bind(self.cg, 'CGEventSetIntegerValueField', None, [ref, ctypes.c_uint32, ctypes.c_int64])
        self._bind(self.cg, 'CGEventSetFlags', None, [ref, ctypes.c_uint64])
        self._bind(self.cg, 'CGEventSourceButtonState', ctypes.c_bool, [ctypes.c_int, ctypes.c_uint32])

    def permissions(self, capture=False):
        if not self.ax.AXIsProcessTrusted() or not self.cg.CGPreflightPostEventAccess():
            raise NativeInputError('Разрешите Accessibility для приложения, запускающего MCP.', 'native_accessibility_required')
        if capture and not self.cg.CGPreflightScreenCaptureAccess():
            raise NativeInputError('Разрешите Screen Recording для приложения, запускающего MCP.', 'native_screen_recording_required')

    def accessibility_permission(self):
        if not self.ax.AXIsProcessTrusted():
            raise NativeInputError('Разрешите Accessibility для приложения, запускающего MCP.', 'native_accessibility_required')

    def _text_attribute(self, element, name):
        value = self._attribute(element, name)
        try:
            return self._string(value)
        finally:
            if value:
                self.cf.CFRelease(value)

    def _url_attribute(self, element):
        value = self._attribute(element, 'AXURL')
        try:
            string = self.cf.CFURLGetString(value) if value and self.cf.CFGetTypeID(value) == self.cf.CFURLGetTypeID() else value
            return self._string(string)
        finally:
            if value:
                self.cf.CFRelease(value)

    def _boolean_attribute(self, element, name):
        value = self._attribute(element, name)
        try:
            return bool(value and self.cf.CFGetTypeID(value) == self.cf.CFBooleanGetTypeID() and self.cf.CFBooleanGetValue(value))
        finally:
            if value:
                self.cf.CFRelease(value)

    def _attribute(self, element, name):
        value = ctypes.c_void_p()
        if self.ax.AXUIElementCopyAttributeValue(element, self._key(name), ctypes.byref(value)) != 0:
            return None
        return value.value

    def _ax_bounds(self, element):
        position, size = self._attribute(element, 'AXPosition'), self._attribute(element, 'AXSize')
        try:
            point, extent = _Point(), _Size()
            if not position or not size or not self.ax.AXValueGetValue(position, 1, ctypes.byref(point)) or not self.ax.AXValueGetValue(size, 2, ctypes.byref(extent)):
                return None
            return macos._bounds({'left': point.x, 'top': point.y, 'width': extent.width, 'height': extent.height})
        finally:
            for value in (position, size):
                if value:
                    self.cf.CFRelease(value)

    def webarea(self, pid, window_bounds, expected_url):
        application = self.ax.AXUIElementCreateApplication(pid)
        if not application:
            raise NativeInputError('Окно Chrome недоступно через Accessibility.')
        window = None
        owned = []
        try:
            self.ax.AXUIElementSetMessagingTimeout(application, 1.0)
            window = self._attribute(application, 'AXFocusedWindow')
            bounds = self._ax_bounds(window) if window else None
            if not bounds or not macos._same_bounds(bounds, window_bounds):
                raise NativeInputError('Активное окно macOS отличается от закреплённого окна Chrome.', 'native_focus_required')
            if self._modal_window(window):
                raise NativeInputError('Закреплённое окно закрыто системным диалогом. Завершите работу с диалогом.', 'native_target_obscured')
            pending = [(window, 0)]
            areas = []
            visited = 0
            while pending:
                element, depth = pending.pop(0)
                visited += 1
                if visited > 2000 or depth > 24:
                    raise NativeInputError('Дерево Accessibility слишком велико для точного выбора страницы.')
                role = self._attribute(element, 'AXRole')
                try:
                    is_webarea = role and self._string(role) == 'AXWebArea'
                finally:
                    if role:
                        self.cf.CFRelease(role)
                if is_webarea:
                    bounds = self._ax_bounds(element)
                    if bounds and _contained(bounds, window_bounds):
                        url = self._attribute(element, 'AXURL')
                        try:
                            url_string = self.cf.CFURLGetString(url) if url and self.cf.CFGetTypeID(url) == self.cf.CFURLGetTypeID() else url
                            if self._string(url_string) != expected_url:
                                raise NativeInputError('Активная страница macOS отличается от закреплённой вкладки.')
                            areas.append(bounds)
                        finally:
                            if url:
                                self.cf.CFRelease(url)
                    # Page descendants include documents and native pickers. The
                    # top WebArea is the viewport boundary, not their contents.
                    continue
                children = self._attribute(element, 'AXChildren')
                try:
                    for child in self._array(children):
                        retained = self.cf.CFRetain(child)
                        owned.append(retained)
                        pending.append((retained, depth + 1))
                finally:
                    if children:
                        self.cf.CFRelease(children)
            if len(areas) != 1:
                raise NativeInputError('Не удалось однозначно определить видимую область выбранной страницы.')
            return areas[0]
        finally:
            for value in owned:
                self.cf.CFRelease(value)
            if window:
                self.cf.CFRelease(window)
            self.cf.CFRelease(application)

    def _modal_window(self, window):
        sheets, modal = self._attribute(window, 'AXSheets'), self._attribute(window, 'AXModal')
        try:
            return bool(self._array(sheets) or modal and self.cf.CFGetTypeID(modal) == self.cf.CFBooleanGetTypeID() and self.cf.CFBooleanGetValue(modal))
        finally:
            for value in (sheets, modal):
                if value:
                    self.cf.CFRelease(value)

    def post(self, event_type, x, y):
        event = self.cg.CGEventCreateMouseEvent(None, event_type, _Point(x, y), 0)
        if not event:
            raise NativeInputError('Не удалось создать событие мыши macOS.')
        try:
            # kCGMouseEventClickState = 1. CGEventPost itself has no return value.
            self.cg.CGEventSetIntegerValueField(event, 1, 1 if event_type in (1, 2) else 0)
            self.cg.CGEventSetFlags(event, 0)
            self.cg.CGEventPost(0, event)  # kCGHIDEventTap
        finally:
            self.cf.CFRelease(event)

    def mouse_idle(self):
        return not any(self.cg.CGEventSourceButtonState(0, button) for button in (0, 1, 2))


def _contained(inner, outer):
    return inner[0] >= outer[0] - 1 and inner[1] >= outer[1] - 1 and inner[0] + inner[2] <= outer[0] + outer[2] + 1 and inner[1] + inner[3] <= outer[1] + outer[3] + 1


def _intersects(first, second):
    return min(first[0] + first[2], second[0] + second[2]) - max(first[0], second[0]) > 1 and min(first[1] + first[3], second[1] + second[3]) - max(first[1], second[1]) > 1


def _state_identity(state):
    if not isinstance(state, dict):
        raise NativeInputError('Получите актуальное состояние закреплённой вкладки.')
    for key in ('tab_id', 'browser_pid'):
        if type(state.get(key)) is not int or state[key] <= 0:
            raise NativeInputError('Не подтверждён процесс или идентификатор вкладки Chrome.')
    if any(not isinstance(state.get(key), str) or not state[key] for key in ('context_id', 'document_id', 'url')):
        raise NativeInputError('Не подтверждён документ закреплённой вкладки.')
    window, viewport = state.get('window'), state.get('viewport')
    if not isinstance(window, dict) or not isinstance(viewport, dict) or state.get('active') is not True or window.get('focused') is not True or window.get('state') not in ('normal', 'maximized', 'fullscreen'):
        raise NativeInputError('Для нативного ввода закреплённая вкладка должна быть активной в текущем окне Chrome.', 'native_focus_required')
    bounds = macos._bounds(window)
    if bounds is None or type(window.get('id')) is not int or window['id'] <= 0:
        raise NativeInputError('Не подтверждены размеры закреплённого окна.')
    for key in ('width', 'height'):
        value = viewport.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise NativeInputError('Не подтверждены размеры страницы.')
    return {'tab_id': state['tab_id'], 'browser_pid': state['browser_pid'], 'context_id': state['context_id'],
            'document_id': state['document_id'], 'url': state['url'], 'window_id': window['id'], 'window_bounds': bounds,
            'viewport': {key: viewport.get(key) for key in ('width', 'height', 'page_x', 'page_y', 'scale')}}


def _target(system, state):
    identity = _state_identity(state)
    desktop = system.read()
    if not desktop.get('available') or desktop.get('frontmost', {}).get('pid') != state['browser_pid']:
        raise NativeInputError('Закреплённое окно Chrome не находится на переднем плане.', 'native_focus_required')
    windows = desktop.get('windows', [])
    matches = [(index, window) for index, window in enumerate(windows) if window.get('pid') == state['browser_pid'] and
               (bounds := macos._bounds(window.get('bounds'))) is not None and macos._same_bounds(bounds, identity['window_bounds'])]
    if len(matches) != 1:
        raise NativeInputError('Не удалось однозначно сопоставить закреплённое окно Chrome окну macOS.')
    index, window = matches[0]
    if window.get('on_screen') is not True or desktop.get('active_space_id') not in window.get('space_ids', []):
        raise NativeInputError('Закреплённое окно Chrome находится на другом рабочем столе.')
    area = system.webarea(state['browser_pid'], identity['window_bounds'], state['url'])
    if not _contained(area, identity['window_bounds']):
        raise NativeInputError('Видимая область страницы выходит за закреплённое окно.')
    for covering in windows[:index]:
        bounds = macos._bounds(covering.get('bounds'))
        if covering.get('on_screen') is True and covering.get('pid') != state['browser_pid'] and bounds and _intersects(bounds, area):
            raise NativeInputError('Другое приложение перекрывает видимую область страницы.', 'native_target_obscured')
    # AX gives screen points; page coordinates are CSS pixels, with uniform zoom.
    sx, sy = area[2] / state['viewport']['width'], area[3] / state['viewport']['height']
    if not .2 <= sx <= 5 or not .2 <= sy <= 5 or abs(sx - sy) > max(sx, sy) * .02:
        raise NativeInputError('Не удалось сопоставить масштаб страницы с её нативной областью.')
    left, top = math.ceil(area[0]), math.ceil(area[1])
    crop = (left, top, math.floor(area[0] + area[2]) - left, math.floor(area[1] + area[3]) - top)
    if crop[2] < 1 or crop[3] < 1:
        raise NativeInputError('Видимая область страницы пуста.')
    return {'native_window_id': window['native_window_id'], 'active_space_id': desktop['active_space_id'],
            'active_display_id': desktop.get('active_display_id'), 'crop_bounds': crop, 'state_identity': identity}


def _jpeg_size(data):
    if not data.startswith(b'\xff\xd8'):
        raise NativeInputError('macOS вернула некорректный JPEG.', 'native_capture_failed')
    offset = 2
    while offset + 8 < len(data) and data[offset] == 255:
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset + 8 >= len(data):
            break
        marker = data[offset]
        offset += 1
        length = int.from_bytes(data[offset:offset + 2], 'big')
        if length < 2:
            break
        if marker in (192, 193, 194):
            return int.from_bytes(data[offset + 5:offset + 7], 'big'), int.from_bytes(data[offset + 3:offset + 5], 'big')
        offset += length
    raise NativeInputError('Не удалось прочитать размеры JPEG.', 'native_capture_failed')


def _capture_image(bounds):
    with tempfile.TemporaryDirectory(prefix='ai-browser-native-') as directory:
        path = Path(directory) / 'viewport.jpg'
        result = subprocess.run(['/usr/sbin/screencapture', '-x', '-t', 'jpg', '-R' + ','.join(map(str, bounds)), str(path)],
                                capture_output=True, timeout=10, check=False)
        if result.returncode or not path.is_file():
            raise NativeInputError('Не удалось снять видимую область страницы.', 'native_capture_failed')
        # Fixed bounds keep native-message replies below the existing 512 KiB cap.
        for maximum in (1280, 960, 640):
            result = subprocess.run(['/usr/bin/sips', '-Z', str(maximum), '-s', 'formatOptions', '60', str(path)],
                                    capture_output=True, timeout=10, check=False)
            if result.returncode:
                raise NativeInputError('Не удалось подготовить скриншот страницы.', 'native_capture_failed')
            data = path.read_bytes()
            encoded = base64.b64encode(data).decode('ascii')
            if len(encoded) <= 350000:
                width, height = _jpeg_size(data)
                return {'image': {'data': encoded, 'mime_type': 'image/jpeg'}, 'image_width': width, 'image_height': height}
        raise NativeInputError('Нативный скриншот превышает лимит транспорта.', 'native_capture_failed')


def _open():
    if sys.platform != 'darwin':
        raise NativeInputError('Нативный ввод доступен только в macOS.', 'native_platform_unsupported')
    try:
        return _System()
    except (OSError, AttributeError, macos._Unavailable) as error:
        raise NativeInputError('Нативные API macOS недоступны.', 'native_platform_unsupported') from error


def _capture(state):
    """Capture only the active pinned page, including visible browser popups."""
    created = time.monotonic()
    _state_identity(state)
    system = _open()
    try:
        system.permissions(capture=True)
        target = _target(system, state)
        image = _capture_image(target['crop_bounds'])
        if _target(system, state) != target:
            raise NativeInputError('Окно или рабочий стол изменились во время скриншота.')
        return {**image, 'tab_id': state['tab_id'], 'context_id': state['context_id'],
                'viewport': {'width': state['viewport']['width'], 'height': state['viewport']['height']},
                'coordinate_system': 'css_viewport', '_native': target, '_created': created}
    except (OSError, subprocess.SubprocessError) as error:
        raise NativeInputError('Нативный скриншот не завершился.', 'native_capture_failed') from error
    finally:
        system.close()


def _click(capture_record, x, y, current_state):
    """Click once, with native state revalidated immediately before mouse-down."""
    identity = _state_identity(current_state)
    target = capture_record.get('_native') if isinstance(capture_record, dict) else None
    if not isinstance(target, dict) or identity != target.get('state_identity'):
        raise NativeInputError('Документ или вкладка изменились; получите новый нативный скриншот.')
    viewport = capture_record.get('viewport', {})
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (x, y)) or not 0 <= x < viewport.get('width', 0) or not 0 <= y < viewport.get('height', 0):
        raise NativeInputError('Выберите точку внутри нативного скриншота.')
    system = _open()
    attempted = False
    point = None
    try:
        system.permissions()
        if not system.mouse_idle():
            raise NativeInputError('Мышь используется; получите новый скриншот после завершения действия.')
        if _target(system, current_state) != target:
            raise NativeInputError('Окно, страница или рабочий стол изменились; получите новый скриншот.')
        left, top, width, height = target['crop_bounds']
        point = left + x * width / viewport['width'], top + y * height / viewport['height']
        from .bridge import check_request
        check_request()
        _check_fresh(capture_record)
        system.post(5, *point)  # kCGEventMouseMoved
        if _target(system, current_state) != target or not system.mouse_idle():
            raise NativeInputError('Окно изменилось перед нажатием мыши.')
        check_request()
        _check_fresh(capture_record)
        attempted = True
        system.post(1, *point)  # kCGEventLeftMouseDown
        system.post(2, *point)  # kCGEventLeftMouseUp
        attempted = False
        return {'clicked': True, 'input_method': 'native', 'input_dispatched': True,
                'effect_confirmed': False, 'verification_required': True}
    except Exception as error:
        if attempted:
            # A failed reply can still mean the press reached the OS. Release
            # once for cleanup, but never repeat the press or claim no effect.
            try:
                system.post(2, *point)
            except Exception:
                pass
            raise NativeInputError(str(error), 'native_dispatch_failed', action_may_have_run=True) from error
        if isinstance(error, NativeInputError):
            raise
        raise NativeInputError('Нативный ввод не завершился.') from error
    finally:
        system.close()


def _check_fresh(record):
    created = record.get('_created')
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created) or not 0 <= time.monotonic() - created <= 30:
        raise NativeInputError('Нативный скриншот устарел; получите новый.')


@contextmanager
def _input_lock():
    # Different MCP connections run in different processes but share one mouse.
    from .bridge import runtime
    if sys.platform == 'win32':
        raise NativeInputError('Нативный ввод доступен только в macOS.', 'native_platform_unsupported')
    import fcntl
    descriptor = None
    try:
        descriptor = os.open(runtime() / 'native-input.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise NativeInputError('Небезопасный файл блокировки нативного ввода.')
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise NativeInputError('Нативный ввод занят другим MCP-подключением.', 'native_input_busy') from error
        yield
    except OSError as error:
        raise NativeInputError('Не удалось безопасно заблокировать нативный ввод.', 'native_input_busy') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def capture(state):
    """Capture only the active pinned page, including visible browser popups."""
    with _input_lock():
        return _capture(state)


def click(capture_record, x, y, current_state):
    """Serialize physical mouse input across all local MCP connections."""
    with _input_lock():
        return _click(capture_record, x, y, current_state)


# Browser-owned strings, not page-provided JavaScript messages. Chromium source:
# components/javascript_dialogs_strings.grdp and app_modal_dialog_manager.cc.
# Russian translations checked against the installed Chrome 157 locale resources.
_BEFOREUNLOAD_TEXT = (
    ('Leave site?', 'Changes you made may not be saved.', 'Leave', 'Cancel'),
    ('Reload site?', 'Changes you made may not be saved.', 'Reload', 'Cancel'),
    ('Закрыть сайт?', 'Изменения могут не сохраниться.', 'Выйти', 'Отмена'),
    ('Перезагрузить сайт?', 'Изменения могут не сохраниться.', 'Перезагрузить', 'Отмена'),
)


def _dialog_state_identity(state):
    if not isinstance(state, dict) or any(type(state.get(key)) is not int or state[key] <= 0 for key in ('tab_id', 'browser_pid')):
        raise NativeInputError('Не подтверждены вкладка и процесс диалога.')
    if any(not isinstance(state.get(key), str) or not state[key] for key in ('context_id', 'url')):
        raise NativeInputError('Не подтверждён адрес закреплённой вкладки.')
    window = state.get('window')
    if not isinstance(window, dict) or state.get('active') is not True or window.get('focused') is not True or window.get('state') not in ('normal', 'maximized', 'fullscreen'):
        raise NativeInputError('Для диалога требуется активная закреплённая вкладка Chrome.', 'native_focus_required')
    bounds = macos._bounds(window)
    if bounds is None or type(window.get('id')) is not int or window['id'] <= 0:
        raise NativeInputError('Не подтверждено окно диалога.')
    return {'tab_id': state['tab_id'], 'browser_pid': state['browser_pid'], 'profile_id': state.get('profile_id'),
            'context_id': state['context_id'], 'url': state['url'], 'window_id': window['id'], 'window_bounds': bounds}


def _dialog_native_target(system, identity):
    desktop = system.read()
    if not desktop.get('available') or desktop.get('frontmost', {}).get('pid') != identity['browser_pid']:
        raise NativeInputError('Закреплённое окно Chrome не находится на переднем плане.', 'native_focus_required')
    windows = desktop.get('windows', [])
    matches = [(index, window) for index, window in enumerate(windows) if window.get('pid') == identity['browser_pid'] and
               (bounds := macos._bounds(window.get('bounds'))) is not None and macos._same_bounds(bounds, identity['window_bounds'])]
    if len(matches) != 1:
        raise NativeInputError('Не удалось однозначно определить окно диалога.')
    index, window = matches[0]
    if window.get('on_screen') is not True or desktop.get('active_space_id') not in window.get('space_ids', []):
        raise NativeInputError('Окно диалога находится на другом рабочем столе.', 'native_focus_required')
    for covering in windows[:index]:
        bounds = macos._bounds(covering.get('bounds'))
        if covering.get('on_screen') is True and covering.get('pid') != identity['browser_pid'] and bounds and _intersects(bounds, identity['window_bounds']):
            raise NativeInputError('Другое приложение перекрывает окно диалога.', 'native_target_obscured')
    return {'native_window_id': window['native_window_id'], 'active_space_id': desktop['active_space_id'],
            'active_display_id': desktop.get('active_display_id')}


def _release_ax(system, references):
    for value in references:
        if value:
            system.cf.CFRelease(value)


def _scan_beforeunload(system, identity):
    """Read only browser chrome and top-level URL; never page descendants."""
    owned = []
    def attribute(element, name):
        value = system._attribute(element, name)
        if value:
            owned.append(value)
        return value
    def children(element):
        found = []
        for name in ('AXChildren', 'AXSheets'):
            value = attribute(element, name)
            for child in system._array(value):
                found.append(system.cf.CFRetain(child))
                owned.append(found[-1])
        return found
    def equal(first, second):
        return bool(first and second and system.cf.CFEqual(first, second))
    def name(element):
        return ' '.join((system._text_attribute(element, 'AXTitle') or '').split())
    try:
        application = system.ax.AXUIElementCreateApplication(identity['browser_pid'])
        if not application:
            raise NativeInputError('Приложение Chrome недоступно через Accessibility.')
        owned.append(application)
        system.ax.AXUIElementSetMessagingTimeout(application, 1.0)
        windows = attribute(application, 'AXWindows')
        matches = [window for window in system._array(windows) if (bounds := system._ax_bounds(window)) and macos._same_bounds(bounds, identity['window_bounds'])]
        if len(matches) != 1:
            raise NativeInputError('Не удалось однозначно определить закреплённое AX-окно.')
        window = matches[0]
        focused = attribute(application, 'AXFocusedWindow')
        pending, visited, dialogs, urls = [(window, 0)], [], [], []
        while pending:
            element, depth = pending.pop(0)
            if any(equal(element, previous) for previous in visited):
                continue
            visited.append(element)
            if len(visited) > 400 or depth > 24:
                raise NativeInputError('Не удалось ограничить поиск нативного диалога.')
            role = system._text_attribute(element, 'AXRole')
            if role == 'AXWebArea':
                urls.append(system._url_attribute(element))
                continue
            subrole = system._text_attribute(element, 'AXSubrole')
            if role in ('AXSheet', 'AXDialog') or subrole == 'AXDialog':
                dialogs.append(element)
                # A browser dialog can have a nested group, not another window.
                continue
            pending.extend((child, depth + 1) for child in children(element))
        if urls != [identity['url']]:
            raise NativeInputError('Активная AX-страница отличается от закреплённой вкладки.')
        if not dialogs:
            if focused and not equal(focused, window) or system._boolean_attribute(window, 'AXModal'):
                raise NativeInputError('Неизвестный нативный диалог Chrome.', 'native_dialog_unsupported')
            return None, owned
        if len(dialogs) != 1:
            raise NativeInputError('Найдено несколько нативных диалогов.', 'native_dialog_unsupported')
        dialog = dialogs[0]
        if focused and not equal(focused, window) and not equal(focused, dialog):
            raise NativeInputError('Фокус принадлежит другому окну Chrome.', 'native_focus_required')
        default, cancel = attribute(dialog, 'AXDefaultButton'), attribute(dialog, 'AXCancelButton')
        if not default or not cancel or equal(default, cancel):
            raise NativeInputError('У диалога нет однозначных кнопок подтверждения и отмены.', 'native_dialog_unsupported')
        title, texts, buttons = name(dialog), [], []
        pending, visited = [(dialog, 0)], []
        while pending:
            element, depth = pending.pop(0)
            if any(equal(element, previous) for previous in visited):
                continue
            visited.append(element)
            if len(visited) > 80 or depth > 12:
                raise NativeInputError('Структура диалога не поддерживается.', 'native_dialog_unsupported')
            role = system._text_attribute(element, 'AXRole')
            if role not in ('AXWindow', 'AXSheet', 'AXDialog', 'AXGroup', 'AXStaticText', 'AXButton', 'AXImage'):
                raise NativeInputError('Диалог содержит неподдерживаемые элементы.', 'native_dialog_unsupported')
            if role == 'AXButton':
                if not system._boolean_attribute(element, 'AXEnabled'):
                    raise NativeInputError('Кнопка диалога отключена.', 'native_dialog_unsupported')
                buttons.append(element)
            if role == 'AXStaticText':
                texts.append(' '.join((system._text_attribute(element, 'AXValue') or system._text_attribute(element, 'AXTitle') or '').split()))
            pending.extend((child, depth + 1) for child in children(element))
        if len(buttons) != 2 or not all(any(equal(button, expected) for button in buttons) for expected in (default, cancel)):
            raise NativeInputError('Кнопки диалога не соответствуют семантике подтверждения и отмены.', 'native_dialog_unsupported')
        template = next((entry for entry in _BEFOREUNLOAD_TEXT if title == entry[0] and entry[1] in texts and name(default) == entry[2] and name(cancel) == entry[3] and all(text in ('', entry[0], entry[1]) for text in texts)), None)
        if template is None:
            raise NativeInputError('Это не распознанный браузерный диалог о несохранённых изменениях.', 'native_dialog_unsupported')
        return {'window': window, 'dialog': dialog, 'leave': default, 'stay': cancel,
                'title': title, 'message': template[1], 'leave_label': template[2], 'stay_label': template[3]}, owned
    except BaseException:
        _release_ax(system, owned)
        raise


class _NativeDialogCapability:
    def __init__(self, system, references, dialog, identity, target, created):
        self.system, self.references, self.dialog = system, references, dialog
        self.identity, self.target, self.created = identity, target, created
        self.used = False

    def close(self):
        if self.system is not None:
            system, self.system = self.system, None
            _release_ax(system, self.references)
            self.references = []
            system.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def inspect_beforeunload(state):
    """Inspect an already open browser-owned beforeunload prompt, without JS."""
    identity = _dialog_state_identity(state)
    with _input_lock():
        created = time.monotonic()
        system = _open()
        transferred = False
        references = []
        try:
            system.accessibility_permission()
            target = _dialog_native_target(system, identity)
            dialog, references = _scan_beforeunload(system, identity)
            if _dialog_native_target(system, identity) != target:
                raise NativeInputError('Окно изменилось при чтении диалога.')
            if dialog is None:
                return {'dialog': None}
            capability = _NativeDialogCapability(system, references, dialog, identity, target, created)
            transferred = True
            return {'dialog': {'type': 'beforeunload', 'title': dialog['title'], 'message': dialog['message'],
                               'actions': {'leave': dialog['leave_label'], 'stay': dialog['stay_label']}, 'input_method': 'native_ax'},
                    '_native_dialog': capability, '_created': created}
        finally:
            if not transferred:
                _release_ax(system, references)
                system.close()


def handle_beforeunload(record, action, current_state):
    """Apply one explicit decision to the exact inspected native prompt."""
    capability = record.get('_native_dialog') if isinstance(record, dict) else None
    if action not in ('leave', 'stay') or not isinstance(capability, _NativeDialogCapability) or capability.used or capability.system is None:
        raise NativeInputError('Получите актуальный диалог и выберите leave или stay.')
    capability.used = True
    references, attempted = [], False
    system = capability.system
    try:
        with _input_lock():
            if _dialog_state_identity(current_state) != capability.identity:
                raise NativeInputError('Вкладка или окно диалога изменились.')
            _check_fresh({'_created': capability.created})
            system.accessibility_permission()
            if _dialog_native_target(system, capability.identity) != capability.target:
                raise NativeInputError('Нативное окно диалога изменилось.')
            current, references = _scan_beforeunload(system, capability.identity)
            if current is None or any(not system.cf.CFEqual(current[key], capability.dialog[key]) for key in ('window', 'dialog', 'leave', 'stay')) or any(current[key] != capability.dialog[key] for key in ('title', 'message', 'leave_label', 'stay_label')):
                raise NativeInputError('Диалог был закрыт или заменён; прочитайте его заново.')
            from .bridge import check_request
            check_request()
            _check_fresh({'_created': capability.created})
            if _dialog_native_target(system, capability.identity) != capability.target:
                raise NativeInputError('Окно изменилось перед выбором ответа.')
            check_request()
            _check_fresh({'_created': capability.created})
            attempted = True
            result = system.ax.AXUIElementPerformAction(current[action], system._key('AXPress'))
            if result != 0:
                raise NativeInputError('Не удалось подтвердить результат нативного ответа.', 'native_dispatch_failed', action_may_have_run=True)
            return {'handled': True, 'type': 'beforeunload', 'action': action, 'input_method': 'native_ax',
                    'input_dispatched': True, 'effect_confirmed': False, 'verification_required': True}
    except Exception as error:
        if attempted:
            raise NativeInputError(str(error), 'native_dispatch_failed', action_may_have_run=True) from error
        raise
    finally:
        _release_ax(system, references)
        capability.close()
