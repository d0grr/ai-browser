"""Native fallback never guesses a window, leaks a changed capture, or replays."""
import copy
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_browser import native_input as native


def state():
    return {'tab_id': 7, 'context_id': 'context-a', 'document_id': 'document-a', 'url': 'https://a.test/',
            'browser_pid': 100, 'active': True,
            'window': {'id': 3, 'focused': True, 'state': 'normal', 'left': 10, 'top': 30, 'width': 800, 'height': 590},
            'viewport': {'width': 800, 'height': 500}}


def desktop():
    return {'available': True, 'active_space_id': 11, 'active_display_id': 'display-a', 'frontmost': {'pid': 100},
            'windows': [{'native_window_id': 90, 'pid': 100, 'on_screen': True, 'space_ids': [11],
                         'bounds': {'left': 10, 'top': 30, 'width': 800, 'height': 590}}]}


def system():
    result = Mock()
    result.read.side_effect = lambda: desktop()
    result.webarea.return_value = (10, 120, 800, 500)
    result.mouse_idle.return_value = True
    return result


IMAGE = {'image': {'data': 'jpeg-data', 'mime_type': 'image/jpeg'}, 'image_width': 1280, 'image_height': 800}


class NativeInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = patch('ai_browser.bridge.runtime', return_value=Path(self.temporary.name))
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

    def capture(self, fake=None, value=None):
        fake = fake or system()
        with patch.object(native, '_open', return_value=fake), patch.object(native, '_capture_image', return_value=copy.deepcopy(IMAGE)):
            return native.capture(value or state()), fake

    def test_import_and_unsupported_platform_do_not_open_native_libraries(self):
        with patch.object(native.sys, 'platform', 'linux'), patch.object(native, '_System') as constructor:
            with self.assertRaises(native.NativeInputError) as error:
                native.capture(state())
            self.assertEqual(error.exception.code, 'native_platform_unsupported')
            constructor.assert_not_called()

    def test_capture_crops_only_verified_webarea_and_returns_css_coordinates(self):
        fake = system()
        with patch.object(native, '_open', return_value=fake), patch.object(native, '_capture_image', return_value=copy.deepcopy(IMAGE)) as image:
            result = native.capture(state())
        image.assert_called_once_with((10, 120, 800, 500))
        fake.permissions.assert_called_once_with(capture=True)
        self.assertEqual(result['viewport'], {'width': 800, 'height': 500})
        self.assertEqual(result['coordinate_system'], 'css_viewport')
        self.assertEqual(result['_native']['native_window_id'], 90)
        self.assertEqual(result['_native']['active_space_id'], 11)
        self.assertEqual(result['image_width'], 1280)
        self.assertEqual(fake.read.call_count, 2)
        fake.webarea.assert_called_with(100, (10, 30, 800, 590), 'https://a.test/')
        fake.close.assert_called_once()

    def test_permission_denial_precedes_capture_and_does_not_prompt(self):
        for code in ('native_accessibility_required', 'native_screen_recording_required'):
            fake = system()
            fake.permissions.side_effect = native.NativeInputError('Permission missing', code)
            with patch.object(native, '_open', return_value=fake), patch.object(native, '_capture_image') as image:
                with self.assertRaises(native.NativeInputError) as caught:
                    native.capture(state())
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.requires_user_action)
                self.assertFalse(caught.exception.action_may_have_run)
                image.assert_not_called()
                fake.read.assert_not_called()
                fake.close.assert_called_once()

    def test_inactive_or_unverified_extension_states_never_access_gui(self):
        for update in (lambda s: s.update(active=False), lambda s: s.update(browser_pid=True),
                       lambda s: s['window'].update(focused=False), lambda s: s['window'].update(state='minimized'),
                       lambda s: s['viewport'].update(width=float('nan')), lambda s: s.update(document_id='')):
            value = state(); update(value)
            with patch.object(native, '_open') as opened:
                with self.assertRaises(native.NativeInputError):
                    native.capture(value)
                opened.assert_not_called()

    def test_foreground_space_window_identity_and_occlusion_are_all_required(self):
        mutations = (
            lambda d: d['frontmost'].update(pid=200),
            lambda d: d.update(active_space_id=12),
            lambda d: d['windows'][0].update(on_screen=False),
            lambda d: d['windows'][0].update(pid=200),
            lambda d: d['windows'].append(copy.deepcopy(d['windows'][0])),
            lambda d: d['windows'].insert(0, {'pid': 200, 'on_screen': True,
                'bounds': {'left': 40, 'top': 150, 'width': 100, 'height': 100}}),
        )
        for mutate in mutations:
            value = desktop(); mutate(value)
            fake = system(); fake.read.side_effect = None; fake.read.return_value = value
            with patch.object(native, '_open', return_value=fake), patch.object(native, '_capture_image') as image:
                with self.assertRaises(native.NativeInputError):
                    native.capture(state())
                image.assert_not_called()
                fake.close.assert_called_once()

    def test_same_browser_native_popup_is_allowed_above_its_page(self):
        value = desktop()
        value['windows'].insert(0, {'pid': 100, 'on_screen': True, 'bounds': {'left': 40, 'top': 150, 'width': 100, 'height': 100}})
        fake = system(); fake.read.side_effect = None; fake.read.return_value = value
        result, _ = self.capture(fake)
        self.assertEqual(result['_native']['native_window_id'], 90)

    def test_changed_window_during_capture_discards_image(self):
        before, after = desktop(), desktop(); after['active_space_id'] = 12
        fake = system(); fake.read.side_effect = [before, after]
        with patch.object(native, '_open', return_value=fake), patch.object(native, '_capture_image', return_value=copy.deepcopy(IMAGE)):
            with self.assertRaises(native.NativeInputError):
                native.capture(state())
        fake.close.assert_called_once()

    def test_native_click_maps_css_to_screen_points_and_does_not_activate_windows(self):
        record, _ = self.capture()
        fake = system()
        with patch.object(native, '_open', return_value=fake):
            result = native.click(record, 100, 200, state())
        self.assertEqual(fake.post.call_args_list[0].args, (5, 110.0, 320.0))
        self.assertEqual([call.args[0] for call in fake.post.call_args_list], [5, 1, 2])
        self.assertEqual(fake.read.call_count, 2)
        self.assertTrue(result['input_dispatched'])
        self.assertFalse(result['effect_confirmed'])
        self.assertTrue(result['verification_required'])
        self.assertEqual(result['input_method'], 'native')
        fake.close.assert_called_once()

    def test_native_click_obeys_zoom_mapping(self):
        value = state(); value['viewport'] = {'width': 400, 'height': 250}
        record, _ = self.capture(value=value)
        fake = system()
        with patch.object(native, '_open', return_value=fake):
            native.click(record, 50, 100, value)
        self.assertEqual(fake.post.call_args_list[1].args, (1, 110.0, 320.0))

    def test_document_tab_or_origin_change_and_invalid_points_reject_before_gui(self):
        record, _ = self.capture()
        for key, value in [('document_id', 'replacement'), ('context_id', 'other'), ('tab_id', 8), ('browser_pid', 200), ('url', 'https://other.test/')]:
            changed = state(); changed[key] = value
            with patch.object(native, '_open') as opened:
                with self.assertRaises(native.NativeInputError):
                    native.click(record, 10, 20, changed)
                opened.assert_not_called()
        for x, y in [(float('nan'), 0), (True, 0), (-1, 0), (800, 10), (10, 500)]:
            with patch.object(native, '_open') as opened:
                with self.assertRaises(native.NativeInputError):
                    native.click(record, x, y, state())
                opened.assert_not_called()

    def test_native_change_after_mousemove_prevents_mousedown(self):
        record, _ = self.capture()
        before, after = desktop(), desktop(); after['frontmost']['pid'] = 200
        fake = system(); fake.read.side_effect = [before, after]
        with patch.object(native, '_open', return_value=fake):
            with self.assertRaises(native.NativeInputError) as caught:
                native.click(record, 100, 200, state())
        self.assertFalse(caught.exception.action_may_have_run)
        self.assertEqual([call.args[0] for call in fake.post.call_args_list], [5])
        fake.close.assert_called_once()

    def test_user_mouse_button_activity_never_receives_synthetic_down(self):
        record, _ = self.capture()
        fake = system(); fake.mouse_idle.return_value = False
        with patch.object(native, '_open', return_value=fake):
            with self.assertRaises(native.NativeInputError):
                native.click(record, 100, 200, state())
        fake.post.assert_not_called()

    def test_uncertain_native_press_is_not_replayed_and_mouseup_is_attempted(self):
        record, _ = self.capture()
        fake = system()
        def post(kind, x, y):
            if kind == 1:
                raise RuntimeError('Uncertain mouse press')
        fake.post.side_effect = post
        with patch.object(native, '_open', return_value=fake):
            with self.assertRaises(native.NativeInputError) as caught:
                native.click(record, 100, 200, state())
        self.assertEqual(caught.exception.code, 'native_dispatch_failed')
        self.assertTrue(caught.exception.action_may_have_run)
        self.assertIsNone(caught.exception.input_dispatched)
        self.assertEqual([call.args[0] for call in fake.post.call_args_list], [5, 1, 2])
        fake.close.assert_called_once()

    def test_cross_process_lock_is_private_nonblocking_and_rejects_symlinks(self):
        with native._input_lock():
            self.assertEqual((Path(self.temporary.name) / 'native-input.lock').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(native.NativeInputError) as caught:
                with native._input_lock():
                    pass
            self.assertEqual(caught.exception.code, 'native_input_busy')
        path = Path(self.temporary.name) / 'native-input.lock'
        path.unlink()
        path.symlink_to(Path(self.temporary.name) / 'elsewhere')
        with self.assertRaises(native.NativeInputError):
            with native._input_lock():
                pass

    def test_capture_temp_files_are_removed_after_encoding_or_subprocess_failure(self):
        jpeg = bytes([255, 216, 255, 192, 0, 17, 8, 3, 32, 5, 0, 3, 1, 17, 0, 2, 17, 0, 3, 17, 0, 255, 217])
        paths = []
        def run(command, **arguments):
            if command[0].endswith('screencapture'):
                path = Path(command[-1]); paths.append(path); path.write_bytes(jpeg)
            return Mock(returncode=0)
        with patch.object(native.subprocess, 'run', side_effect=run):
            image = native._capture_image((10, 120, 800, 500))
        self.assertEqual((image['image_width'], image['image_height']), (1280, 800))
        self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_modal_sheet_and_modal_window_are_detected_before_page_traversal(self):
        for sheet, modal in [(True, False), (False, True), (False, False)]:
            reader = object.__new__(native._System)
            reader._attribute = Mock(side_effect=[10 if sheet else None, 20 if modal else None])
            reader._array = Mock(return_value=[100] if sheet else [])
            reader.cf = Mock()
            reader.cf.CFGetTypeID.return_value = reader.cf.CFBooleanGetTypeID.return_value = 5
            reader.cf.CFBooleanGetValue.return_value = modal
            self.assertEqual(reader._modal_window(1), sheet or modal)
            self.assertEqual(reader.cf.CFRelease.call_count, int(sheet) + int(modal))

    def test_cancelled_request_after_mousemove_never_presses(self):
        record, _ = self.capture()
        fake = system()
        with patch.object(native, '_open', return_value=fake), patch('ai_browser.bridge.check_request', side_effect=[None, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                native.click(record, 100, 200, state())
        self.assertEqual([call.args[0] for call in fake.post.call_args_list], [5])
        fake.close.assert_called_once()

    def test_slow_final_native_scan_cannot_press_after_screenshot_expires(self):
        record, _ = self.capture()
        fake = system()
        with patch.object(native, '_open', return_value=fake), patch.object(native.time, 'monotonic', side_effect=[record['_created'] + 29, record['_created'] + 31]):
            with self.assertRaises(native.NativeInputError) as caught:
                native.click(record, 100, 200, state())
        self.assertFalse(caught.exception.action_may_have_run)
        self.assertEqual([call.args[0] for call in fake.post.call_args_list], [5])
        fake.close.assert_called_once()

    def test_malformed_jpeg_padding_is_rejected_without_index_error(self):
        for data in (b'\xff\xd8' + b'\xff' * 20, b'bad', b'\xff\xd8\xff\xc0\x00\x00' + b'x' * 20):
            with self.assertRaises(native.NativeInputError):
                native._jpeg_size(data)


def dialog_system():
    """A browser AX tree, with page descendants deliberately out of bounds."""
    value = Mock()
    value.read.side_effect = lambda: desktop()
    value.ax.AXUIElementCreateApplication.return_value = 1
    value.ax.AXUIElementPerformAction.return_value = 0
    value.cf.CFRetain.side_effect = lambda ref: ref
    value.cf.CFEqual.side_effect = lambda first, second: first == second
    value._key.side_effect = lambda name: name
    value.nodes = {
        1: {'AXWindows': [2], 'AXFocusedWindow': 2},
        2: {'AXRole': 'AXWindow', 'AXChildren': [3, 4]},
        3: {'AXRole': 'AXWebArea', 'AXURL': 'https://a.test/', 'AXChildren': [99]},
        4: {'AXRole': 'AXSheet', 'AXSubrole': 'AXDialog', 'AXTitle': 'Leave site?',
            'AXChildren': [7, 5, 6], 'AXDefaultButton': 5, 'AXCancelButton': 6},
        5: {'AXRole': 'AXButton', 'AXTitle': 'Leave', 'AXEnabled': True},
        6: {'AXRole': 'AXButton', 'AXTitle': 'Cancel', 'AXEnabled': True},
        7: {'AXRole': 'AXStaticText', 'AXValue': 'Changes you made may not be saved.'},
        99: {'AXRole': 'AXSheet', 'AXTitle': 'Leave site?'},
    }
    value._attribute.side_effect = lambda ref, name: value.nodes.get(ref, {}).get(name)
    value._text_attribute.side_effect = lambda ref, name: value.nodes.get(ref, {}).get(name)
    value._boolean_attribute.side_effect = lambda ref, name: value.nodes.get(ref, {}).get(name) is True
    value._url_attribute.side_effect = lambda ref: value.nodes.get(ref, {}).get('AXURL')
    value._array.side_effect = lambda ref: ref if isinstance(ref, list) else []
    value._ax_bounds.side_effect = lambda ref: (10, 30, 800, 590) if ref == 2 else None
    return value


class BeforeUnloadNativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        patcher = patch('ai_browser.bridge.runtime', return_value=Path(self.temporary.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def inspect(self, fake=None, target=None):
        fake = fake or dialog_system()
        with patch.object(native, '_open', return_value=fake):
            record = native.inspect_beforeunload(target or state())
        self.addCleanup(lambda: record.get('_native_dialog') and record['_native_dialog'].close())
        return record, fake

    def test_exact_browser_prompt_is_inspected_without_page_js_or_screen_recording(self):
        record, fake = self.inspect()
        self.assertEqual(record['dialog']['type'], 'beforeunload')
        self.assertEqual(record['dialog']['actions'], {'leave': 'Leave', 'stay': 'Cancel'})
        fake.accessibility_permission.assert_called_once_with()
        fake.permissions.assert_not_called()
        fake.ax.AXUIElementPerformAction.assert_not_called()
        self.assertNotIn(99, [call.args[0] for call in fake._attribute.call_args_list])
        fake.close.assert_not_called()
        record['_native_dialog'].close()
        fake.close.assert_called_once()

    def test_only_exact_english_and_russian_browser_templates_are_supported(self):
        for title, body, leave, stay in native._BEFOREUNLOAD_TEXT:
            fake = dialog_system()
            fake.nodes[4]['AXTitle'] = title; fake.nodes[7]['AXValue'] = body
            fake.nodes[5]['AXTitle'] = leave; fake.nodes[6]['AXTitle'] = stay
            record, _ = self.inspect(fake)
            self.assertEqual(record['dialog']['title'], title)
        for mutate in [lambda n: n[4].update(AXTitle='a.test says'),
                       lambda n: n[7].update(AXValue='Allow access to local files?'),
                       lambda n: n[5].update(AXTitle='Allow'),
                       lambda n: n[6].update(AXTitle='Yes'),
                       lambda n: n[4].update(AXDefaultButton=6),
                       lambda n: n[5].update(AXEnabled=False),
                       lambda n: n[7].update(AXRole='AXTextField')]:
            fake = dialog_system(); mutate(fake.nodes)
            with patch.object(native, '_open', return_value=fake):
                with self.assertRaises(native.NativeInputError) as caught:
                    native.inspect_beforeunload(state())
                self.assertEqual(caught.exception.code, 'native_dialog_unsupported')
                fake.ax.AXUIElementPerformAction.assert_not_called()
                fake.close.assert_called_once()

    def test_page_owned_fake_dialog_is_never_inspected_or_approved(self):
        fake = dialog_system()
        fake.nodes[2]['AXChildren'] = [3]
        fake.nodes[3]['AXChildren'] = [4]
        record, _ = self.inspect(fake)
        self.assertIsNone(record['dialog'])
        self.assertNotIn('_native_dialog', record)
        fake.ax.AXUIElementPerformAction.assert_not_called()

    def test_each_explicit_decision_presses_only_its_semantic_native_button(self):
        for action, button in [('leave', 5), ('stay', 6)]:
            record, fake = self.inspect()
            result = native.handle_beforeunload(record, action, state())
            fake.ax.AXUIElementPerformAction.assert_called_once_with(button, 'AXPress')
            self.assertTrue(result['handled'])
            self.assertEqual(result['action'], action)
            self.assertFalse(result['effect_confirmed'])
            self.assertTrue(result['verification_required'])
            fake.close.assert_called_once()
            with self.assertRaises(native.NativeInputError):
                native.handle_beforeunload(record, action, state())
            fake.ax.AXUIElementPerformAction.assert_called_once()

    def test_changed_state_window_or_ax_url_never_presses(self):
        for key, replacement in [('tab_id', 8), ('context_id', 'other'), ('url', 'https://other.test/'), ('browser_pid', 200), ('profile_id', 'other')]:
            record, fake = self.inspect()
            changed = state(); changed[key] = replacement
            with self.assertRaises(native.NativeInputError):
                native.handle_beforeunload(record, 'leave', changed)
            fake.ax.AXUIElementPerformAction.assert_not_called()
        record, fake = self.inspect()
        fake.nodes[3]['AXURL'] = 'https://other.test/'
        with self.assertRaises(native.NativeInputError):
            native.handle_beforeunload(record, 'leave', state())
        fake.ax.AXUIElementPerformAction.assert_not_called()

    def test_replaced_dialog_or_buttons_cannot_reuse_old_capability(self):
        for replaced in (4, 5, 6):
            record, fake = self.inspect()
            replacement = replaced + 10
            fake.nodes[replacement] = fake.nodes.pop(replaced)
            for node in fake.nodes.values():
                for key in ('AXDefaultButton', 'AXCancelButton'):
                    if node.get(key) == replaced:
                        node[key] = replacement
                if 'AXChildren' in node:
                    node['AXChildren'] = [replacement if child == replaced else child for child in node['AXChildren']]
            with self.assertRaises(native.NativeInputError):
                native.handle_beforeunload(record, 'leave', state())
            fake.ax.AXUIElementPerformAction.assert_not_called()

    def test_stale_dialog_and_cancellation_never_press(self):
        record, fake = self.inspect()
        with patch.object(native.time, 'monotonic', return_value=record['_created'] + 31):
            with self.assertRaises(native.NativeInputError):
                native.handle_beforeunload(record, 'leave', state())
        fake.ax.AXUIElementPerformAction.assert_not_called()
        record, fake = self.inspect()
        with patch('ai_browser.bridge.check_request', side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                native.handle_beforeunload(record, 'leave', state())
        fake.ax.AXUIElementPerformAction.assert_not_called()
        fake.close.assert_called_once()

    def test_foreground_or_space_change_before_press_never_approves(self):
        record, fake = self.inspect()
        changed = desktop(); changed['frontmost']['pid'] = 200
        fake.read.side_effect = lambda: changed
        with self.assertRaises(native.NativeInputError):
            native.handle_beforeunload(record, 'leave', state())
        fake.ax.AXUIElementPerformAction.assert_not_called()

    def test_uncertain_axpress_is_never_replayed(self):
        record, fake = self.inspect()
        fake.ax.AXUIElementPerformAction.return_value = -25204
        with self.assertRaises(native.NativeInputError) as caught:
            native.handle_beforeunload(record, 'leave', state())
        self.assertTrue(caught.exception.action_may_have_run)
        self.assertEqual(caught.exception.code, 'native_dispatch_failed')
        self.assertIsNone(caught.exception.input_dispatched)
        with self.assertRaises(native.NativeInputError):
            native.handle_beforeunload(record, 'leave', state())
        fake.ax.AXUIElementPerformAction.assert_called_once()


if __name__ == '__main__':
    unittest.main()
