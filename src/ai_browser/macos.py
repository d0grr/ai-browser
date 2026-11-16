"""Read macOS window/Space metadata and match it to extension window IDs.

No Accessibility access, window titles, page contents, screen capture or window
activation is used. SkyLight is private: unavailable APIs disable this evidence
instead of substituting the last-focused browser window.
"""
import ctypes
import math
import sys


class WindowSelectionError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class _Unavailable(RuntimeError):
    pass


def _bounds(value):
    if not isinstance(value, dict):
        return None
    values = tuple(value.get(key) for key in ('left', 'top', 'width', 'height'))
    if any(isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) for number in values):
        return None
    return values if values[2] > 0 and values[3] > 0 else None


def _same_bounds(first, second):
    # Chrome rounds its integer bounds; CoreGraphics can return fractional points.
    return all(abs(left - right) <= 1 for left, right in zip(first, second))


def choose_window(connections, desktop):
    """Return the unique current-Space/display window, None, or WindowSelectionError.

    Uniqueness is checked before Space/minimization filtering, against every
    native window of the browser PID and every extension window in every supplied
    connection. A focused flag never substitutes for native Space evidence.
    """
    if not isinstance(desktop, dict) or not desktop.get('available'):
        return None
    space = desktop.get('active_space_id')
    display = desktop.get('active_display_id')
    if not isinstance(space, int) or isinstance(space, bool) or space <= 0 or not isinstance(display, str) or not display:
        return None
    if isinstance(connections, dict):
        connections = connections.get('connections', [])
    native = []
    for window in desktop.get('windows', []):
        bounds = _bounds(window.get('bounds'))
        if bounds is not None and isinstance(window.get('pid'), int):
            native.append((window, bounds))
    extension = []
    for connection in connections:
        if connection.get('connected') is False:
            continue
        pid = connection.get('browser_pid')
        for window in connection.get('windows', []):
            extension.append((connection, window, pid, _bounds(window)))

    def current(window):
        return (window.get('on_screen') is True and space in window.get('space_ids', []) and
                window.get('display_id') == display)

    known_pids = {item[2] for item in extension if isinstance(item[2], int) and not isinstance(item[2], bool) and item[2] > 0}
    for window, bounds in native:
        if window['pid'] not in known_pids or not current(window):
            continue
        matches = [item for item in extension if item[3] is not None and
                   (item[2] == window['pid'] or item[2] is None) and _same_bounds(item[3], bounds)]
        if len(matches) > 1:
            raise WindowSelectionError('Несколько окон расширения соответствуют текущему окну macOS.', 'window_mapping_ambiguous')
        if not matches or matches[0][1].get('state') == 'minimized':
            raise WindowSelectionError('Текущее окно Chrome нельзя надёжно сопоставить с данными расширения.', 'window_mapping_incomplete')

    candidates = []
    for connection, window, pid, bounds in extension:
        if window.get('state') == 'minimized' or bounds is None or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        matches = [(item, rect) for item, rect in native if item['pid'] == pid and _same_bounds(bounds, rect)]
        if not any(current(item) for item, _ in matches):
            continue
        if len(matches) != 1:
            raise WindowSelectionError('Неоднозначное соответствие окна Chrome окнам macOS.', 'window_mapping_ambiguous')
        matched, matched_bounds = matches[0]
        reverse = [item for item in extension if item[3] is not None and
                   (item[2] == pid or item[2] is None) and _same_bounds(item[3], matched_bounds)]
        if len(reverse) != 1:
            raise WindowSelectionError('Несколько подключений или окон Chrome соответствуют одному окну macOS.', 'window_mapping_ambiguous')
        connection_id, window_id = connection.get('connection_id'), window.get('id')
        if not isinstance(connection_id, str) or not connection_id or not isinstance(window_id, int) or isinstance(window_id, bool):
            continue
        candidates.append({'connection_id': connection_id, 'window_id': window_id, 'desktop': {
            'source': 'current_space',
            'active_space_id': space, 'active_display_id': display,
            'native_window_id': matched['native_window_id'], 'browser_pid': pid,
            'space_ids': list(matched.get('space_ids', [])), 'display_id': matched['display_id'],
        }})
    if len(candidates) > 1:
        raise WindowSelectionError('На текущем рабочем столе найдено несколько окон Chrome; выберите окно явно.', 'desktop_window_ambiguous')
    return candidates[0] if candidates else None


class _NativeReader:
    _REF = ctypes.c_void_p
    _INDEX = ctypes.c_long

    def __init__(self):
        self.keys = {}
        try:
            self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            self.cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
            self.sls = ctypes.CDLL('/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight')
        except OSError as error:
            raise _Unavailable('native_framework_unavailable') from error
        ref, index = self._REF, self._INDEX
        signatures = {
            'CFRelease': (None, [ref]), 'CFGetTypeID': (ctypes.c_ulong, [ref]),
            'CFArrayGetTypeID': (ctypes.c_ulong, []), 'CFDictionaryGetTypeID': (ctypes.c_ulong, []),
            'CFStringGetTypeID': (ctypes.c_ulong, []), 'CFNumberGetTypeID': (ctypes.c_ulong, []),
            'CFBooleanGetTypeID': (ctypes.c_ulong, []), 'CFBooleanGetValue': (ctypes.c_bool, [ref]),
            'CFStringCreateWithCString': (ref, [ref, ctypes.c_char_p, ctypes.c_uint32]),
            'CFStringGetLength': (index, [ref]),
            'CFStringGetMaximumSizeForEncoding': (index, [index, ctypes.c_uint32]),
            'CFStringGetCString': (ctypes.c_bool, [ref, ctypes.c_void_p, index, ctypes.c_uint32]),
            'CFDictionaryGetValue': (ref, [ref, ref]), 'CFArrayGetCount': (index, [ref]),
            'CFArrayGetValueAtIndex': (ref, [ref, index]),
            'CFNumberGetValue': (ctypes.c_bool, [ref, ctypes.c_int, ctypes.c_void_p]),
            'CFNumberCreate': (ref, [ref, ctypes.c_int, ctypes.c_void_p]),
            'CFArrayCreate': (ref, [ref, ctypes.POINTER(ref), index, ref]),
        }
        for name, (result, arguments) in signatures.items():
            self._bind(self.cf, name, result, arguments)
        self._bind(self.cg, 'CGWindowListCopyWindowInfo', ref, [ctypes.c_uint32, ctypes.c_uint32])
        # Read-only SkyLight signatures: yabai/src/misc/extern.h and
        # Hammerspoon/extensions/spaces/libspaces.m (upstream implementations).
        self._bind(self.sls, 'SLSMainConnectionID', ctypes.c_int, [])
        self._bind(self.sls, 'SLSCopyActiveMenuBarDisplayIdentifier', ref, [ctypes.c_int])
        self._bind(self.sls, 'SLSManagedDisplayGetCurrentSpace', ctypes.c_uint64, [ctypes.c_int, ref])
        self._bind(self.sls, 'SLSCopyManagedDisplayForWindow', ref, [ctypes.c_int, ctypes.c_uint32])
        self._bind(self.sls, 'SLSCopySpacesForWindows', ref, [ctypes.c_int, ctypes.c_int, ref])
        self.connection = self.sls.SLSMainConnectionID()
        if not self.connection:
            raise _Unavailable('window_server_connection_unavailable')

    @staticmethod
    def _bind(library, name, result, arguments):
        try:
            function = getattr(library, name)
        except AttributeError as error:
            raise _Unavailable('missing_native_symbol:' + name) from error
        function.restype, function.argtypes = result, arguments

    def close(self):
        for value in self.keys.values():
            self.cf.CFRelease(value)
        self.keys.clear()

    def _key(self, name):
        if name not in self.keys:
            value = self.cf.CFStringCreateWithCString(None, name.encode(), 0x08000100)
            if not value:
                raise _Unavailable('metadata_key_unavailable')
            self.keys[name] = value
        return self.keys[name]

    def _get(self, dictionary, key):
        if not dictionary or self.cf.CFGetTypeID(dictionary) != self.cf.CFDictionaryGetTypeID():
            return None
        return self.cf.CFDictionaryGetValue(dictionary, self._key(key))

    def _array(self, array):
        if not array or self.cf.CFGetTypeID(array) != self.cf.CFArrayGetTypeID():
            return []
        count = self.cf.CFArrayGetCount(array)
        if count < 0 or count > 100000:
            raise _Unavailable('invalid_native_array')
        return [self.cf.CFArrayGetValueAtIndex(array, index) for index in range(count)]

    def _string(self, value):
        if not value or self.cf.CFGetTypeID(value) != self.cf.CFStringGetTypeID():
            return None
        size = self.cf.CFStringGetMaximumSizeForEncoding(self.cf.CFStringGetLength(value), 0x08000100) + 1
        if size <= 0 or size > 65536:
            return None
        buffer = ctypes.create_string_buffer(size)
        return buffer.value.decode() if self.cf.CFStringGetCString(value, buffer, size, 0x08000100) else None

    def _number(self, value, floating=False):
        if not value or self.cf.CFGetTypeID(value) != self.cf.CFNumberGetTypeID():
            return None
        output = ctypes.c_double() if floating else ctypes.c_int64()
        return output.value if self.cf.CFNumberGetValue(value, 6 if floating else 4, ctypes.byref(output)) else None

    def _active(self):
        display = self.sls.SLSCopyActiveMenuBarDisplayIdentifier(self.connection)
        if not display:
            raise _Unavailable('active_display_unavailable')
        try:
            identifier = self._string(display)
            space = int(self.sls.SLSManagedDisplayGetCurrentSpace(self.connection, display))
            if not identifier or not space:
                raise _Unavailable('active_space_unavailable')
            return identifier, space
        finally:
            self.cf.CFRelease(display)

    def _window_spaces(self, window_id):
        number = ctypes.c_int64(window_id)
        number_ref = self.cf.CFNumberCreate(None, 4, ctypes.byref(number))
        if not number_ref:
            return []
        array = spaces = None
        try:
            values = (self._REF * 1)(number_ref)
            array = self.cf.CFArrayCreate(None, values, 1, None)
            if not array:
                return []
            # 0x7 includes current/user/other Spaces, including sticky windows.
            spaces = self.sls.SLSCopySpacesForWindows(self.connection, 0x7, array)
            return sorted({number for value in self._array(spaces) if (number := self._number(value)) is not None and number > 0})
        finally:
            for value in (spaces, array, number_ref):
                if value:
                    self.cf.CFRelease(value)

    def read(self):
        display, space = self._active()
        info = self.cg.CGWindowListCopyWindowInfo(0, 0)
        if not info:
            raise _Unavailable('native_windows_unavailable')
        windows = []
        try:
            for value in self._array(info):
                window_id = self._number(self._get(value, 'kCGWindowNumber'))
                pid = self._number(self._get(value, 'kCGWindowOwnerPID'))
                bounds_ref = self._get(value, 'kCGWindowBounds')
                bounds = {key: self._number(self._get(bounds_ref, native), floating=True) for key, native in
                          (('left', 'X'), ('top', 'Y'), ('width', 'Width'), ('height', 'Height'))}
                if not window_id or not pid:
                    continue
                if any(number is None or not math.isfinite(number) for number in bounds.values()):
                    raise _Unavailable('native_window_bounds_unavailable')
                visible = self._get(value, 'kCGWindowIsOnscreen')
                on_screen = bool(visible and self.cf.CFGetTypeID(visible) == self.cf.CFBooleanGetTypeID() and self.cf.CFBooleanGetValue(visible))
                native_display = self.sls.SLSCopyManagedDisplayForWindow(self.connection, window_id)
                try:
                    window_display = self._string(native_display)
                finally:
                    if native_display:
                        self.cf.CFRelease(native_display)
                windows.append({'native_window_id': window_id, 'pid': pid, 'bounds': bounds,
                                'space_ids': self._window_spaces(window_id), 'on_screen': on_screen,
                                'display_id': window_display})
        finally:
            self.cf.CFRelease(info)
        if self._active() != (display, space):
            raise _Unavailable('desktop_changed_during_read')
        result = {'available': True, 'active_space_id': space, 'active_display_id': display, 'windows': windows}
        frontmost = _frontmost_application()
        if frontmost:
            result['frontmost'] = frontmost
        return result


def _frontmost_application():
    """Optional NSWorkspace process metadata; never infer focus from window order."""
    try:
        ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
        objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = objc.sel_registerName.argtypes = [ctypes.c_char_p]
        ref = ctypes.c_void_p
        send = ctypes.CFUNCTYPE(ref, ref, ref)(('objc_msgSend', objc))
        integer = ctypes.CFUNCTYPE(ctypes.c_int, ref, ref)(('objc_msgSend', objc))
        string = ctypes.CFUNCTYPE(ctypes.c_char_p, ref, ref)(('objc_msgSend', objc))
        selector = lambda name: objc.sel_registerName(name.encode())
        pool = send(objc.objc_getClass(b'NSAutoreleasePool'), selector('new'))
        try:
            workspace = send(objc.objc_getClass(b'NSWorkspace'), selector('sharedWorkspace'))
            application = send(workspace, selector('frontmostApplication'))
            if not application:
                return None
            pid = integer(application, selector('processIdentifier'))
            identifier = send(application, selector('bundleIdentifier'))
            bundle = string(identifier, selector('UTF8String')) if identifier else None
            return {'pid': pid, 'bundle_id': bundle.decode() if bundle else None} if pid > 0 else None
        finally:
            if pool:
                send(pool, selector('drain'))
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def desktop_context():
    """Return read-only native metadata, or available=False with a stable reason."""
    if sys.platform != 'darwin':
        return {'available': False, 'reason': 'unsupported_platform', 'windows': []}
    reader = None
    try:
        reader = _NativeReader()
        return reader.read()
    except _Unavailable as error:
        return {'available': False, 'reason': str(error), 'windows': []}
    except (OSError, AttributeError, TypeError, ValueError, RuntimeError):
        return {'available': False, 'reason': 'native_metadata_unavailable', 'windows': []}
    finally:
        if reader is not None:
            reader.close()
