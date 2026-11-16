"""Space selection rejects uncertain native/extension window associations."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_browser import macos


def connection(identifier='profile-a', pid=100, window_id=7, **bounds):
    return {'connection_id': identifier, 'connected': True, 'browser_pid': pid, 'windows': [
        {'id': window_id, 'focused': False, 'state': 'normal', 'left': 10, 'top': 30,
         'width': 800, 'height': 600, **bounds}
    ]}


def native(window_id=70, pid=100, spaces=None, display='display-a', on_screen=True, **bounds):
    return {'native_window_id': window_id, 'pid': pid, 'space_ids': [1] if spaces is None else spaces,
            'display_id': display, 'on_screen': on_screen,
            'bounds': {'left': 10, 'top': 30, 'width': 800, 'height': 600, **bounds}}


def desktop(*windows):
    return {'available': True, 'active_space_id': 1, 'active_display_id': 'display-a',
            'windows': list(windows) or [native()]}


class ChooseWindowTests(unittest.TestCase):
    def assert_error(self, code, connections, context):
        with self.assertRaises(macos.WindowSelectionError) as caught:
            macos.choose_window(connections, context)
        self.assertEqual(caught.exception.code, code)

    def test_unique_native_space_match_returns_both_transport_and_chrome_window_ids(self):
        result = macos.choose_window([connection()], desktop())
        self.assertEqual((result['connection_id'], result['window_id']), ('profile-a', 7))
        self.assertEqual(result['desktop'], {'source': 'current_space', 'active_space_id': 1,
                         'active_display_id': 'display-a', 'native_window_id': 70, 'browser_pid': 100,
                         'space_ids': [1], 'display_id': 'display-a'})

    def test_geometry_rounding_is_tolerated_only_when_both_directions_remain_unique(self):
        result = macos.choose_window([connection()], desktop(native(left=10.5, width=800.5)))
        self.assertEqual(result['window_id'], 7)

    def test_last_focused_window_on_another_space_is_not_selected(self):
        old = connection('old', 100, 7)
        old['windows'][0]['focused'] = True
        current = connection('current', 100, 8, left=1000)
        result = macos.choose_window([old, current], desktop(native(spaces=[2], on_screen=False), native(80, left=1000)))
        self.assertEqual((result['connection_id'], result['window_id']), ('current', 8))

    def test_duplicate_native_bounds_on_current_and_other_spaces_are_ambiguous(self):
        self.assert_error('window_mapping_ambiguous', [connection()],
                          desktop(native(), native(71, spaces=[2], on_screen=False)))

    def test_duplicate_extension_bounds_in_different_profiles_are_ambiguous(self):
        self.assert_error('window_mapping_ambiguous', [connection(), connection('profile-b', 100, 8)], desktop())

    def test_a_minimized_duplicate_still_prevents_a_false_geometry_match(self):
        first = connection()
        duplicate = connection('profile-b', 100, 8)
        duplicate['windows'][0]['state'] = 'minimized'
        self.assert_error('window_mapping_ambiguous', [first, duplicate], desktop())

    def test_identical_bounds_in_another_process_do_not_match_the_wrong_profile(self):
        result = macos.choose_window([connection(), connection('profile-b', 200, 8)],
                                    desktop(native(), native(80, pid=200, spaces=[2], on_screen=False)))
        self.assertEqual(result['connection_id'], 'profile-a')

    def test_a_native_window_with_the_wrong_browser_pid_is_not_used(self):
        self.assertIsNone(macos.choose_window([connection()], desktop(native(pid=200))))

    def test_native_windows_on_other_spaces_are_not_candidates(self):
        self.assertIsNone(macos.choose_window([connection()], desktop(native(spaces=[2]))))

    def test_sticky_window_membership_includes_the_current_space(self):
        result = macos.choose_window([connection()], desktop(native(spaces=[2, 1, 3])))
        self.assertEqual(result['desktop']['space_ids'], [2, 1, 3])

    def test_minimized_or_hidden_windows_are_not_candidates(self):
        minimized = connection()
        minimized['windows'][0]['state'] = 'minimized'
        self.assertIsNone(macos.choose_window([minimized], desktop(native(on_screen=False))))
        self.assertIsNone(macos.choose_window([connection()], desktop(native(on_screen=False))))

    def test_native_visible_but_extension_minimized_metadata_is_rejected(self):
        minimized = connection()
        minimized['windows'][0]['state'] = 'minimized'
        self.assert_error('window_mapping_incomplete', [minimized], desktop())

    def test_current_space_on_another_monitor_is_not_the_active_display(self):
        self.assertIsNone(macos.choose_window([connection()], desktop(native(display='display-b'))))

    def test_multiple_monitors_use_the_active_display_without_activating_either(self):
        first, second = connection(), connection('profile-b', 100, 8, left=2000)
        context = desktop(native(), native(80, display='display-b', spaces=[9], left=2000))
        original = copy.deepcopy((first, second, context))
        self.assertEqual(macos.choose_window([first, second], context)['window_id'], 7)
        self.assertEqual((first, second, context), original)

    def test_multiple_current_windows_require_explicit_selection_even_if_one_is_focused(self):
        first, second = connection(), connection('profile-b', 100, 8, left=1000)
        first['windows'][0]['focused'] = True
        self.assert_error('desktop_window_ambiguous', [first, second], desktop(native(), native(80, left=1000)))

    def test_unmapped_current_native_browser_window_blocks_an_otherwise_unique_match(self):
        self.assert_error('window_mapping_incomplete', [connection()], desktop(native(), native(80, left=1000)))

    def test_an_extension_window_missing_geometry_cannot_be_ignored(self):
        old = connection('old', 100, 8, left=1000)
        del old['windows'][0]['width']
        self.assert_error('window_mapping_incomplete', [connection(), old], desktop(native(), native(80, left=1000)))

    def test_unknown_pid_with_matching_bounds_prevents_claiming_another_connections_window(self):
        self.assert_error('window_mapping_ambiguous', [connection(), connection('old', None, 8)], desktop())

    def test_connection_list_response_is_accepted_and_unavailable_desktop_has_no_fallback(self):
        self.assertEqual(macos.choose_window({'connections': [connection()]}, desktop())['window_id'], 7)
        self.assertIsNone(macos.choose_window([connection()], {'available': False, 'reason': 'missing_native_symbol'}))

    def test_incomplete_desktop_identity_does_not_select_a_window(self):
        for key in ('active_space_id', 'active_display_id'):
            value = desktop()
            del value[key]
            self.assertIsNone(macos.choose_window([connection()], value))


class NativeReaderTests(unittest.TestCase):
    def test_unsupported_platform_never_loads_native_libraries(self):
        with patch('ai_browser.macos.sys.platform', 'linux'), patch('ai_browser.macos.ctypes.CDLL') as library:
            self.assertEqual(macos.desktop_context(), {'available': False, 'reason': 'unsupported_platform', 'windows': []})
            library.assert_not_called()

    def test_missing_skylight_library_disables_space_evidence(self):
        with patch('ai_browser.macos.sys.platform', 'darwin'), \
             patch('ai_browser.macos.ctypes.CDLL', side_effect=OSError('Unavailable')):
            self.assertEqual(macos.desktop_context()['reason'], 'native_framework_unavailable')

    def test_missing_skylight_symbol_is_reported_without_guessing(self):
        with patch('ai_browser.macos.sys.platform', 'darwin'), \
             patch('ai_browser.macos.ctypes.CDLL', side_effect=[Mock(), Mock(), SimpleNamespace()]):
            value = macos.desktop_context()
            self.assertFalse(value['available'])
            self.assertEqual(value['reason'], 'missing_native_symbol:SLSMainConnectionID')
            self.assertEqual(value['windows'], [])

    def test_window_server_unavailability_is_not_replaced_with_last_focus(self):
        with patch('ai_browser.macos.sys.platform', 'darwin'), \
             patch('ai_browser.macos._NativeReader', side_effect=macos._Unavailable('window_server_connection_unavailable')):
            self.assertEqual(macos.desktop_context()['reason'], 'window_server_connection_unavailable')

    def test_native_reader_releases_resources_after_success_and_errors(self):
        for outcome in ({'available': True, 'windows': []}, macos._Unavailable('desktop_changed_during_read')):
            reader = Mock()
            if isinstance(outcome, Exception):
                reader.read.side_effect = outcome
            else:
                reader.read.return_value = outcome
            with patch('ai_browser.macos.sys.platform', 'darwin'), patch('ai_browser.macos._NativeReader', return_value=reader):
                macos.desktop_context()
                reader.close.assert_called_once()

    def test_a_space_change_during_enumeration_disables_the_snapshot(self):
        reader = object.__new__(macos._NativeReader)
        reader._active = Mock(side_effect=[('display-a', 1), ('display-a', 2)])
        reader._array = Mock(return_value=[])
        reader.cg, reader.cf = Mock(), Mock()
        with self.assertRaisesRegex(macos._Unavailable, 'desktop_changed_during_read'):
            reader.read()
        reader.cf.CFRelease.assert_called_once()

    def test_reader_never_requests_a_window_title_or_returns_page_content(self):
        reader = object.__new__(macos._NativeReader)
        requested = []
        window = {'kCGWindowNumber': 70, 'kCGWindowOwnerPID': 100,
                  'kCGWindowBounds': {'X': 10, 'Y': 30, 'Width': 800, 'Height': 600},
                  'kCGWindowIsOnscreen': True, 'kCGWindowName': 'Private page https://private.test/'}

        def get(value, key):
            self.assertNotEqual(key, 'kCGWindowName')
            requested.append(key)
            return value.get(key)

        reader.connection = 1
        reader._active = Mock(return_value=('display-a', 1))
        reader._array = Mock(return_value=[window])
        reader._get = get
        reader._number = lambda value, floating=False: value
        reader._string = lambda value: value
        reader._window_spaces = Mock(return_value=[1])
        reader.cf = SimpleNamespace(CFRelease=Mock(), CFGetTypeID=lambda value: 1,
                                    CFBooleanGetTypeID=lambda: 1, CFBooleanGetValue=bool)
        reader.cg = SimpleNamespace(CGWindowListCopyWindowInfo=Mock(return_value='metadata'))
        reader.sls = SimpleNamespace(SLSCopyManagedDisplayForWindow=Mock(return_value='display-a'))
        with patch('ai_browser.macos._frontmost_application', return_value={'pid': 100, 'bundle_id': 'com.google.Chrome'}):
            result = reader.read()
        self.assertEqual(result['windows'], [native()])
        self.assertEqual(result['frontmost']['pid'], 100)
        self.assertNotIn('Private', str(result))
        self.assertNotIn('https://', str(result))


if __name__ == '__main__':
    unittest.main()
