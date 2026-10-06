"""No desktop input: verify ordering and fail-closed selection guards."""
import importlib.util
import os
import tempfile
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtGui import QImage, QColor

spec = importlib.util.spec_from_file_location('jianying_macro_execution_tests', Path(__file__).resolve().parents[1] / 'scripts/jianying_track_macro.py')
macro = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = macro
spec.loader.exec_module(macro)


class ExecutionTests(unittest.TestCase):
    def test_auto_edit_skips_only_gaps_too_narrow_to_select(self):
        gaps=[macro.Gap(100,111),macro.Gap(120,132),macro.Gap(140,157)]
        ready,ignored=macro.partition_auto_gaps(gaps)
        self.assertEqual(ready,[gaps[1],gaps[2]])
        self.assertEqual(ignored,[gaps[0]])
        self.assertEqual(macro.AUTO_MIN_GAP_PIXELS,12)

    def test_second_cut_and_delete_rebase_after_first_cut_scrolls(self):
        calls=[]
        adapter=SimpleNamespace(timeline_shift=0,timeline_bounds=(0,300,50),view_guard=Mock(),
            begin_gap=Mock(),progress=Mock(),delete_gap=lambda gap,y,r:(calls.append(('delete',gap)) or True))
        def cut(gap,side,y,r):
            calls.append((side,gap))
            if side=='right':
                adapter.timeline_shift=-40
        adapter.cut_boundary=cut
        self.assertEqual(macro.split_visible_gaps(adapter,[macro.Gap(100,150)],100,50),1)
        self.assertEqual(calls,[('right',macro.Gap(100,150)),
                                ('left',macro.Gap(60,110)),('delete',macro.Gap(60,110))])

    def test_real_saved_scroll_matches_caption_translation(self):
        folder=Path(__file__).resolve().parents[1]/'tmp'/'jianying-macro'
        before=folder/'识别_20260922-140705-886129_原始画面.png'
        after=folder/'执行记录_20260922-140716-034270_视图变化_7.png'
        if not before.exists() or not after.exists():
            self.skipTest('user incident screenshots are not present')
        a,b=QImage(str(before)),QImage(str(after))
        pix=lambda image:lambda x,y:image.pixelColor(x,y).getRgb()[:3]
        shift=macro.caption_horizontal_shift(pix(a),pix(b),268,2541,1022,a.height())
        self.assertIsNotNone(shift)
        self.assertEqual(shift[0],-746)
        self.assertGreater(shift[1],.95)

    def test_rollback_only_own_verified_splits(self):
        app=macro.QApplication.instance() or macro.QApplication([])
        image=QImage(100,100,QImage.Format.Format_RGB32);image.fill(QColor(210,145,40))
        user=Mock();user.GetAsyncKeyState.return_value=0
        audit=SimpleNamespace(record=Mock())
        adapter=SimpleNamespace(gap_transaction={'gap':macro.Gap(10,30),'sticker_y':50,
                'baseline':image,'confirmed':2,'unverified':0,'delete_sent':False},
                check=Mock(),view_guard=None,user32=user,mapping='same',audit=audit)
        with patch.object(macro,'single_primary_capture',return_value=(SimpleNamespace(toImage=lambda:image),None,'same')),patch.object(macro.time,'sleep'):
            self.assertTrue(macro.WindowsInput.rollback_gap(adapter,'test'))
        self.assertEqual(user.keybd_event.call_count,8)
        self.assertIsNone(adapter.gap_transaction)

    def test_uncertain_key_does_not_undo_older_history(self):
        adapter=SimpleNamespace(gap_transaction={'gap':macro.Gap(10,30),'confirmed':1,
                 'unverified':1,'delete_sent':False},audit=SimpleNamespace(record=Mock()),user32=Mock())
        self.assertFalse(macro.WindowsInput.rollback_gap(adapter,'test'))
        adapter.user32.keybd_event.assert_not_called()

    def test_execution_rejects_window_different_from_detection(self):
        app=macro.QApplication.instance() or macro.QApplication([])
        window=macro.Macro()
        window.context=SimpleNamespace(gaps=[macro.Gap(10,30)],window_identity=(100,10,'old.exe'))
        try:
            with patch.object(window,'focus_target',return_value=(200,20,'new.exe')), patch.object(macro,'single_primary_capture') as capture:
                window.split_one()
                capture.assert_not_called()
            self.assertIn('不是定位时的窗口',window.info.text())
        finally:
            window.close()

    def test_audit_identifies_exact_report_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'识别_unique.json'
            audit=macro.JsonAudit(Path(directory)/'run.json',report)
            self.assertEqual(audit.payload['app_version'],macro.APP_VERSION)
            self.assertEqual(audit.payload['detection_report'],str(report))

    def test_strict_batch_does_not_leave_unfinished_gap_for_next(self):
        adapter=SimpleNamespace(require_gap_completion=True,click_image_point=Mock(),
            select_sticker_for_split=Mock(),ctrl_b=Mock(),delete_gap=Mock(return_value=False))
        with self.assertRaisesRegex(macro.SafetyAbort,'尚未确认删除'):
            macro.split_visible_gaps(adapter,[macro.Gap(10,30),macro.Gap(50,80)],100,50)
        self.assertEqual(adapter.ctrl_b.call_count,2)
        adapter.delete_gap.assert_called_once_with(macro.Gap(50,80),100,50)

    def test_verified_rollback_defers_local_gap_and_continues_page(self):
        calls=[]
        audit=SimpleNamespace(payload={'skipped_gap_count':1},record=Mock())
        adapter=SimpleNamespace(require_gap_completion=True,continue_after_verified_rollback=True,
            audit=audit,begin_gap=Mock(),progress=Mock(),rollback_gap=Mock(return_value=True))
        def cut(gap,side,y,r):
            calls.append(('cut',gap.start,side))
        def delete(gap,y,r):
            calls.append(('delete',gap.start))
            return gap.start != 50
        adapter.cut_boundary=cut;adapter.delete_gap=delete
        self.assertEqual(macro.split_visible_gaps(adapter,[macro.Gap(10,30),macro.Gap(50,80)],100,50),1)
        self.assertEqual([item for item in calls if item[0]=='delete'],[('delete',50),('delete',10)])
        adapter.rollback_gap.assert_called_once()
        self.assertTrue(any(call.args[0].get('kind')=='gap_deferred_after_rollback'
                            for call in audit.record.call_args_list))

    def test_unverified_rollback_still_stops_before_next_gap(self):
        audit=SimpleNamespace(payload={},record=Mock())
        adapter=SimpleNamespace(require_gap_completion=True,continue_after_verified_rollback=True,
            audit=audit,begin_gap=Mock(),progress=Mock(),cut_boundary=Mock(),
            delete_gap=Mock(return_value=False),rollback_gap=Mock(return_value=False))
        with self.assertRaisesRegex(macro.SafetyAbort,'无法确认安全撤销'):
            macro.split_visible_gaps(adapter,[macro.Gap(10,30),macro.Gap(50,80)],100,50)
        adapter.delete_gap.assert_called_once_with(macro.Gap(50,80),100,50)

    def test_changed_view_blocks_mouse_input(self):
        adapter=SimpleNamespace(check=Mock(),view_guard=Mock(side_effect=macro.SafetyAbort('shifted')),
                                user32=Mock(),selected_sticker_token=True)
        with self.assertRaises(macro.SafetyAbort):
            macro.WindowsInput.click_image_point(adapter,macro.Point(100,100),'test')
        adapter.user32.SetCursorPos.assert_not_called()
        adapter.user32.mouse_event.assert_not_called()

    def test_cut_induced_scroll_blocks_all_post_cut_clicks(self):
        app=macro.QApplication.instance() or macro.QApplication([])
        frame=QImage(250,200,QImage.Format.Format_RGB32);frame.fill(QColor('#202020'))
        events=[]
        with tempfile.TemporaryDirectory() as directory:
            adapter=SimpleNamespace(check=Mock(),view_guard=Mock(side_effect=[None,macro.SafetyAbort('scroll after cut')]),mapping='same',
                click_image_point=lambda p,label:events.append('click'),select_sticker_for_split=Mock(),
                ctrl_b=lambda side:events.append('cut'),audit=SimpleNamespace(path=Path(directory)/'audit.json',record=Mock()))
            with patch.object(macro,'single_primary_capture',return_value=(SimpleNamespace(toImage=lambda:frame),None,'same')), patch.object(macro,'sticker_selection_frame',return_value=True), patch.object(macro,'selected_vertical_edge',return_value=True):
                with self.assertRaisesRegex(macro.SafetyAbort,'scroll after cut'):
                    macro.WindowsInput.cut_boundary(adapter,macro.Gap(100,160),'right',100,50)
            self.assertEqual(events,['click','click','cut'])

    def test_production_cut_last_click_is_boundary_not_material(self):
        app=macro.QApplication.instance() or macro.QApplication([])
        frame=QImage(250,200,QImage.Format.Format_RGB32); frame.fill(QColor('#202020'))
        calls=[]
        with tempfile.TemporaryDirectory() as directory:
            adapter=SimpleNamespace(check=Mock(),view_guard=None,mapping='same',
                click_image_point=lambda p,label:calls.append(('click',p.x,p.y)),
                select_sticker_for_split=lambda p:calls.append(('select',p.x,p.y)),
                ctrl_b=lambda name:calls.append(('cut',name)),
                audit=SimpleNamespace(path=Path(directory)/'audit.json',record=Mock()))
            with patch.object(macro,'single_primary_capture',return_value=(SimpleNamespace(toImage=lambda:frame),None,'same')), patch.object(macro,'sticker_selection_frame',return_value=True), patch.object(macro,'selected_vertical_edge',return_value=True):
                macro.WindowsInput.cut_boundary(adapter,macro.Gap(100,160),'right',100,50)
            index=calls.index(('cut','right'))
            self.assertEqual(calls[index-1],('click',160,50))
            self.assertTrue((Path(directory)/'audit_100_right_分割前.png').exists())
            self.assertTrue((Path(directory)/'audit_100_right_分割后.png').exists())

    def test_recovery_selection_failure_is_bounded_without_keys(self):
        adapter = SimpleNamespace(check=Mock(),view_guard=None,click_image_point=Mock(),
            select_sticker_for_split=Mock(side_effect=macro.GapSelectionError('not selected')),
            audit=Mock(),ctrl_b=Mock())
        with self.assertRaises(macro.GapSelectionError):
            macro.WindowsInput.cut_boundary(adapter,macro.Gap(100,160),'left',100,50)
        self.assertEqual(adapter.select_sticker_for_split.call_count,3)
        adapter.ctrl_b.assert_not_called()

    def test_recovery_rejects_missing_playhead_without_cutting(self):
        app = macro.QApplication.instance() or macro.QApplication([])
        frame=QImage(250,180,QImage.Format.Format_RGB32); frame.fill(QColor('#202020'))
        adapter=SimpleNamespace(check=Mock(),view_guard=None,click_image_point=Mock(),
            select_sticker_for_split=Mock(),mapping='same',audit=Mock(),ctrl_b=Mock())
        with patch.object(macro,'single_primary_capture',return_value=(SimpleNamespace(toImage=lambda:frame),None,'same')):
            with self.assertRaises(macro.GapSelectionError):
                macro.WindowsInput.cut_boundary(adapter,macro.Gap(100,160),'right',100,50)
        adapter.ctrl_b.assert_not_called()

    def test_nearby_ruler_click_can_recover_a_snapped_playhead(self):
        app = macro.QApplication.instance() or macro.QApplication([])
        frame = QImage(250,180,QImage.Format.Format_RGB32); frame.fill(QColor('#202020'))
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            adapter = SimpleNamespace(check=Mock(),view_guard=None,mapping='same',
                click_image_point=lambda p,label:calls.append(('click',p.x,p.y)),
                select_sticker_for_split=Mock(),ctrl_b=lambda side:calls.append(('cut',side)),
                audit=SimpleNamespace(path=Path(directory)/'audit.json',record=Mock()))
            def white_line(*args):
                # Before Ctrl+B, the exact ruler click snapped elsewhere;
                # +2 moved the actual playhead to the original boundary.
                if ('cut','left') in calls:
                    return True
                return calls[-1] == ('click',102,50)
            with patch.object(macro,'single_primary_capture',return_value=(SimpleNamespace(toImage=lambda:frame),None,'same')), \
                 patch.object(macro,'sticker_selection_frame',return_value=True), \
                 patch.object(macro,'selected_vertical_edge',side_effect=white_line):
                macro.WindowsInput.cut_boundary(adapter,macro.Gap(100,160),'left',100,50)
            self.assertIn(('click',100,50),calls)
            self.assertIn(('click',102,50),calls)
            self.assertEqual(sum(item == ('cut','left') for item in calls),1)

    def test_narrow_gap_verification_reselects_without_second_split(self):
        app = macro.QApplication.instance() or macro.QApplication([])
        frame = QImage(250,180,QImage.Format.Format_RGB32); frame.fill(QColor('#202020'))
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            def select(point):
                calls.append(('select',point.x,point.y))
                if ('cut','right') in calls and point.x == 60:
                    raise macro.GapSelectionError('cut handle blocked this click')
            adapter = SimpleNamespace(check=Mock(),view_guard=None,mapping='same',
                click_image_point=lambda p,label:calls.append(('click',p.x,p.y)),
                select_sticker_for_split=select,
                ctrl_b=lambda side:calls.append(('cut',side)),
                audit=SimpleNamespace(path=Path(directory)/'audit.json',record=Mock()))
            # The first post-cut selection cannot be confirmed; recovery may
            # select farther inside the left segment, but must never cut twice.
            with patch.object(macro,'single_primary_capture',return_value=(SimpleNamespace(toImage=lambda:frame),None,'same')), \
                 patch.object(macro,'sticker_selection_frame',return_value=True), \
                 patch.object(macro,'selected_vertical_edge',return_value=True):
                macro.WindowsInput.cut_boundary(adapter,macro.Gap(100,112),'right',100,50)
            self.assertEqual(sum(item == ('cut','right') for item in calls),1)
            self.assertIn(('select',60,100),calls)
            self.assertIn(('select',68,100),calls)

    def test_narrow_gap_verification_stays_inside_timeline_bounds(self):
        self.assertEqual(macro.cut_verification_candidates(macro.Gap(10,22),16,100,(0,90,50)),[16])

    def test_narrow_left_cut_prefers_contiguous_left_material(self):
        self.assertEqual(macro.cut_selection_candidates(macro.Gap(100,112),'left',250)[:2],[86,78])
        orange=lambda x,y:(214,147,41)
        blank=lambda x,y:(34,34,35) if 90 <= x <= 94 else (214,147,41)
        self.assertTrue(macro.sticker_spans_from_left(orange,86,100,50))
        self.assertFalse(macro.sticker_spans_from_left(blank,86,100,50))

    def test_saved_narrow_gap_left_material_was_contiguous(self):
        path=Path(__file__).resolve().parents[1]/'tmp'/'jianying-macro'/'执行记录_20260922-143534-243818_2291_right_发键后未点击.png'
        if not path.exists():
            self.skipTest('user incident screenshot is not present')
        image=QImage(str(path))
        # Saved crop begins at x=2251, y=892. Left-selection x=2277 and
        # intended next cut x=2291 are both in the same orange sticker.
        self.assertTrue(macro.sticker_spans_from_left(macro.image_pixels(image),26,40,260))

    def test_local_left_selection_failure_skips_delete_and_continues(self):
        audit = SimpleNamespace(payload={},record=Mock())
        adapter = SimpleNamespace(audit=audit,click_image_point=Mock(),
            select_sticker_for_split=Mock(side_effect=[None,macro.GapSelectionError('local'),None,None]),
            ctrl_b=Mock(),delete_gap=Mock(return_value=True))
        self.assertEqual(macro.split_visible_gaps(adapter,[macro.Gap(10,30),macro.Gap(50,80)],100,50),1)
        adapter.delete_gap.assert_called_once_with(macro.Gap(10,30),100,50)
        self.assertEqual(adapter.ctrl_b.call_count,3)
        self.assertEqual(audit.payload['skipped_gap_count'],1)

    def test_global_safety_failure_still_stops_batch(self):
        adapter = SimpleNamespace(click_image_point=Mock(),
            select_sticker_for_split=Mock(side_effect=macro.SafetyAbort('window changed')),
            ctrl_b=Mock(),delete_gap=Mock())
        with self.assertRaises(macro.SafetyAbort):
            macro.split_visible_gaps(adapter,[macro.Gap(10,30),macro.Gap(50,80)],100,50)
        adapter.ctrl_b.assert_not_called()
        adapter.delete_gap.assert_not_called()
        self.assertEqual(adapter.select_sticker_for_split.call_count,1)

    def test_right_cut_then_select_left_piece_for_every_gap(self):
        selections, cuts, deletes = [], [], []
        adapter = SimpleNamespace(click_image_point=lambda p,label:cuts.append(p.x),
            select_sticker_for_split=lambda p:selections.append(p.x), ctrl_b=Mock(),
            delete_gap=lambda gap,y,r:(deletes.append(gap.start) or True))
        self.assertEqual(macro.split_visible_gaps(adapter,[macro.Gap(20,80),macro.Gap(100,160)],100,50),2)
        self.assertEqual(cuts,[160,100,80,20])
        self.assertEqual(selections,[130,120,50,40])
        self.assertEqual(deletes,[100,20])

    def test_occluded_frame_uses_same_piece_not_neighbor_across_gap(self):
        def pixel(x,y):
            if x < 43: return (214,147,41)
            if y in (17,46): return (238,221,195)
            return (214,147,41)
        self.assertTrue(macro.sticker_selection_frame(pixel,40,30,80,60))
        def with_gap(x,y):
            return (30,30,30) if x in (41,42) else pixel(x,y)
        self.assertFalse(macro.sticker_selection_frame(with_gap,40,30,80,60))

    def test_dimmer_scaled_selection_border(self):
        def pixel(x,y):
            if y == 13: return (195,190,183)
            if y == 42: return (238,221,195)
            return (214,147,41)
        self.assertTrue(macro.sticker_selection_frame(pixel,40,30,80,60))
        self.assertFalse(macro.sticker_selection_frame(lambda x,y:(214,147,41),40,30,80,60))

    def test_step_timeout_aborts_before_more_input(self):
        adapter = SimpleNamespace(step_deadline=0, user32=Mock())
        with self.assertRaisesRegex(macro.SafetyAbort, '超过 15 秒'):
            macro.WindowsInput.check(adapter)
        adapter.user32.GetAsyncKeyState.assert_not_called()

    def test_batch_progress_reports_each_gap_and_each_stage(self):
        progress = Mock()
        adapter = SimpleNamespace(progress=progress, click_image_point=Mock(),
            select_sticker_for_split=Mock(), ctrl_b=Mock(), delete_gap=Mock(return_value=True))
        self.assertEqual(macro.split_visible_gaps(adapter, [macro.Gap(10,30),macro.Gap(50,80)],100,50),2)
        self.assertEqual([call.args[:2] for call in progress.call_args_list], [(1,2)]*3+[(2,2)]*3)

    def test_retry_remains_available_after_safe_stop_and_redetect_clears_it(self):
        app = macro.QApplication.instance() or macro.QApplication([])
        window = macro.Macro()
        context = SimpleNamespace(gaps=[macro.Gap(10,30)])
        window.context = context
        try:
            with patch.object(window, 'focus_target', side_effect=macro.SafetyAbort('test stop')):
                window.split_one()
            self.assertIs(window.context, context)
            self.assertTrue(window.retry.isEnabled())
            with patch.object(macro.QTimer, 'singleShot') as timer:
                window.redetect.click()
                timer.assert_called_once()
            self.assertIsNone(window.context)
            self.assertFalse(window.retry.isEnabled())
        finally:
            window.close()

    def test_retry_button_queues_same_guarded_execution(self):
        app = macro.QApplication.instance() or macro.QApplication([])
        window = macro.Macro()
        window.context = SimpleNamespace(gaps=[macro.Gap(10,30)])
        window.retry.setEnabled(True)
        try:
            with patch.object(macro.QTimer, 'singleShot') as timer:
                window.retry.click()
                timer.assert_called_once_with(5000, window.split_one)
            self.assertFalse(window.retry.isEnabled())
        finally:
            window.close()

    def test_every_split_is_immediately_preceded_by_sticker_selection(self):
        calls = []
        adapter = SimpleNamespace(
            click_image_point=lambda point,label:calls.append(('ruler', point.y)),
            select_sticker_for_split=lambda point:calls.append(('sticker', point.y)),
            ctrl_b=lambda side:calls.append(('split', side)),
            delete_gap=lambda gap,y,r:(calls.append(('delete', gap.start)) or True),
        )
        self.assertEqual(macro.split_visible_gaps(adapter, [macro.Gap(10,30),macro.Gap(50,80)],100,50),2)
        for index,(kind,_) in enumerate(calls):
            if kind == 'split':
                self.assertEqual(calls[index-1],('sticker',100))
                self.assertEqual(calls[index-2],('ruler',50))
        self.assertEqual(sum(kind=='delete' for kind,_ in calls),2)

    def test_unverified_selection_never_sends_keys(self):
        adapter = SimpleNamespace(selected_sticker_token=False, user32=Mock())
        with self.assertRaises(macro.SafetyAbort): macro.WindowsInput.ctrl_b(adapter,'left')
        adapter.user32.keybd_event.assert_not_called()

    def test_successful_selection_token_is_single_use(self):
        user = Mock(); user.GetAsyncKeyState.return_value = 0
        adapter = SimpleNamespace(selected_sticker_token=True, user32=user, check=Mock(), view_guard=None,time_reader=None,audit=Mock())
        with patch.object(macro.time,'sleep'):
            macro.WindowsInput.ctrl_b(adapter,'left')
            count = user.keybd_event.call_count
            with self.assertRaises(macro.SafetyAbort): macro.WindowsInput.ctrl_b(adapter,'left')
        self.assertEqual(count,4)
        self.assertEqual(user.keybd_event.call_count,count)

    def test_held_shift_never_turns_split_into_split_all(self):
        user=Mock(); user.GetAsyncKeyState.side_effect=lambda key:0x8000 if key==0x10 else 0
        adapter=SimpleNamespace(selected_sticker_token=True,user32=user,check=Mock())
        with self.assertRaises(macro.SafetyAbort): macro.WindowsInput.ctrl_b(adapter,'left')
        user.keybd_event.assert_not_called()

    def test_selection_frame_rejects_material_icon_and_playhead(self):
        gray=lambda x,y:(30,30,30)
        glyph=lambda x,y:(240,240,240) if y==50 or x==60 else gray(x,y)
        self.assertFalse(macro.sticker_selection_frame(glyph,60,50,120,180))
        frame=lambda x,y:(240,240,240) if y in (40,60) and 20<=x<=100 else gray(x,y)
        self.assertTrue(macro.sticker_selection_frame(frame,60,50,120,180))

    def test_linked_highlight_prevents_selection_authorization(self):
        before=QImage(120,180,QImage.Format.Format_RGB32);before.fill(QColor('#202020'))
        after=before.copy()
        for y in (40,60,100,120):
            for x in range(10,111): after.setPixelColor(x,y,QColor('white'))
        adapter=SimpleNamespace(timeline_bounds=(0,120,10),mapping='same',click_image_point=Mock(),park_pointer=Mock(),selected_sticker_token=False,audit=Mock())
        captures=[(SimpleNamespace(toImage=lambda:before),None,'same'),(SimpleNamespace(toImage=lambda:after),None,'same')]
        with patch.object(macro,'single_primary_capture',side_effect=captures):
            with self.assertRaises(macro.SafetyAbort): macro.WindowsInput.select_sticker_for_split(adapter,macro.Point(60,50))
        self.assertFalse(adapter.selected_sticker_token)

    def test_scaled_border_with_hover_axis_is_recognized(self):
        def pixel(x,y):
            if x in (40,41): return (212,165,0)
            if y == 19: return (199,195,190)
            if y == 48: return (239,224,200)
            return (210,140,30)
        self.assertTrue(macro.sticker_selection_frame(pixel,40,30,80,60))
        self.assertFalse(macro.sticker_selection_frame(lambda x,y:(212,165,0) if x in (40,41) else (210,140,30),40,30,80,60))


if __name__ == '__main__': unittest.main()
