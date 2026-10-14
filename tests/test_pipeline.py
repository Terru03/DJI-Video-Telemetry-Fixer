import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import inject_dji_metadata as app

REAL_RECYCLE_SOURCE = app.recycle_source


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_dir = self.root / 'source'
        self.export_dir = self.root / 'export'
        self.source_dir.mkdir()
        self.export_dir.mkdir()
        self.source = self.source_dir / 'DJI_0001.MP4'
        self.source.write_bytes(b'original synthetic source')
        self.export = self.export_dir / 'DJI_0001_Edited.mp4'
        self.export.write_bytes(b'synthetic export')
        self.srt = self.source_dir / 'DJI_0001.SRT'
        self.srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n'
                            '2026-10-01 12:34:56.123\n'
                            '[latitude : -12.345678] [longitude : 123.456789] '
                            '[abs_alt: -10.25] [iso : 100]\n', encoding='utf-8')
        self.tags = {'SourceFile': str(self.export),
                     'Keys:GPSCoordinates': '-12.34568 123.45679 -10.25',
                     'Keys:CreationDate': '2026:10:01 12:34:56+03:00',
                     'QuickTime:CreateDate': '2026:10:01 12:34:56'}
        self.streams = {'streams': [{'codec_name': 'mov_text',
                                     'tags': {'title': 'DJI Telemetry'}}]}
        self.failure = None
        self.commands = []
        app._processed_set.clear()
        self.addCleanup(app._processed_set.clear)
        self.addCleanup(patch.stopall)
        patch.object(app.subprocess, 'run', side_effect=self.run_tool).start()
        output_capture = contextlib.redirect_stdout(io.StringIO())
        output_capture.__enter__()
        self.addCleanup(output_capture.__exit__, None, None, None)
        self.recycle = patch.object(app, 'recycle_source',
                                    side_effect=lambda *args: self.source.unlink()).start()

    def run_tool(self, cmd, **kwargs):
        self.commands.append(cmd)
        tool = cmd[0]
        if tool == 'exiftool':
            if '-ver' in cmd:
                return subprocess.CompletedProcess(cmd, 0, b'13.58', b'')
            if '-j' in cmd:
                if self.failure == 'readback':
                    raise subprocess.CalledProcessError(1, cmd, stderr=b'read failed')
                value = self.tags if isinstance(self.tags, str) else json.dumps([self.tags])
                return subprocess.CompletedProcess(cmd, 0, value, '')
            if '-Model' in cmd:
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if self.failure == 'exiftool':
                raise subprocess.CalledProcessError(1, cmd, stderr=b'write failed')
            if self.failure == 'missing-exiftool':
                raise FileNotFoundError('exiftool')
            return subprocess.CompletedProcess(cmd, 0, b'1 image files updated', b'')
        if tool == 'ffmpeg':
            if '-version' in cmd:
                return subprocess.CompletedProcess(cmd, 0, b'version', b'')
            if self.failure in ('subtitle', 'subtitle-no-stderr'):
                raise subprocess.CalledProcessError(1, cmd,
                                                    stderr=b'remux failed' if self.failure == 'subtitle' else None)
            if self.failure == 'missing-ffmpeg':
                raise FileNotFoundError('ffmpeg')
            Path(cmd[-1]).write_bytes(b'synthetic remux with subtitle')
            return subprocess.CompletedProcess(cmd, 0, b'', b'')
        if tool == 'ffprobe':
            if '-version' in cmd:
                return subprocess.CompletedProcess(cmd, 0, b'version', b'')
            if self.failure == 'missing-ffprobe':
                raise FileNotFoundError('ffprobe')
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.streams), '')
        raise AssertionError('Unexpected external command: ' + repr(cmd))

    def process(self, *, force=True, no_subtitle=False, has_ffmpeg=True):
        return app.process_single_video((str(self.export), str(self.srt), has_ffmpeg,
                                         force, True, {}, 1, 1, no_subtitle))

    def assert_preserved_error(self, **kwargs):
        try:
            result = self.process(**kwargs)
        except Exception as error:
            self.fail('Pipeline raised instead of returning error: ' + repr(error))
        self.assertEqual(result, 'error')
        self.assertEqual(self.source.read_bytes(), b'original synthetic source')
        self.assertNotIn(self.export.name, app._processed_set)
        self.recycle.assert_not_called()

    def test_exiftool_failure_preserves_source_and_does_not_cache(self):
        self.failure = 'exiftool'
        self.assert_preserved_error()

    def test_missing_exiftool_preserves_source_and_does_not_cache(self):
        self.failure = 'missing-exiftool'
        self.assert_preserved_error()

    def test_subtitle_failure_cannot_report_success(self):
        self.failure = 'subtitle'
        self.assert_preserved_error()

    def test_subtitle_failure_without_stderr_returns_error(self):
        self.failure = 'subtitle-no-stderr'
        self.assert_preserved_error()

    def test_missing_ffmpeg_cannot_report_success(self):
        self.failure = 'missing-ffmpeg'
        self.assert_preserved_error()

    def test_unavailable_subtitle_tool_requires_explicit_metadata_only(self):
        self.assert_preserved_error(has_ffmpeg=False)

    def test_readback_mismatch_preserves_source(self):
        self.tags['Keys:GPSCoordinates'] = '1 2 3'
        self.assert_preserved_error()

    def test_date_mismatch_preserves_source(self):
        self.tags['QuickTime:CreateDate'] = '2025:01:01 00:00:00'
        self.assert_preserved_error()

    def test_missing_required_readback_tags_preserves_source(self):
        del self.tags['Keys:CreationDate']
        self.assert_preserved_error()

    def test_malformed_readback_preserves_source(self):
        self.tags = 'not json'
        self.assert_preserved_error()

    def test_failed_readback_preserves_source(self):
        self.failure = 'readback'
        self.assert_preserved_error()

    def test_missing_datetime_preserves_export_and_source(self):
        self.srt.write_text('[latitude : 1.0] [longitude : 2.0]', encoding='utf-8')
        self.assert_preserved_error()
        self.assertEqual(self.export.read_bytes(), b'synthetic export')

    def test_invalid_datetime_preserves_export_and_source(self):
        content = self.srt.read_text().replace('2026-10-01', '2026-99-99')
        self.srt.write_text(content, encoding='utf-8')
        self.assert_preserved_error()
        self.assertEqual(self.export.read_bytes(), b'synthetic export')

    def test_out_of_range_gps_preserves_export_and_source(self):
        content = self.srt.read_text().replace('-12.345678', '91.0')
        self.srt.write_text(content, encoding='utf-8')
        self.assert_preserved_error()

    def test_malformed_present_altitude_preserves_source(self):
        self.srt.write_text(self.srt.read_text().replace('-10.25', 'invalid'), encoding='utf-8')
        self.assert_preserved_error()
        self.assertEqual(self.export.read_bytes(), b'synthetic export')

    def test_malformed_timezone_readback_preserves_source(self):
        self.tags['Keys:CreationDate'] = '2026:10:01 12:34:56+99:99'
        self.assert_preserved_error()

    def test_success_requires_metadata_and_subtitle_verification(self):
        self.assertEqual(self.process(),
                         'success|DJI_0001_Edited.mp4|2026-10-01 12:34:56|-12.345678,123.456789')
        self.assertFalse(self.source.exists())
        self.assertIn(self.export.name, app._processed_set)
        self.assertTrue(any('-j' in cmd for cmd in self.commands))
        self.assertTrue(any(cmd[0] == 'ffprobe' for cmd in self.commands))
        write = next(cmd for cmd in self.commands if '-overwrite_original' in cmd)
        self.assertIn('-Keys:GPSCoordinates=-12.34568+123.45679-0010.250/', write)
        self.assertIn('-Keys:CreationDate=2026:10:01 12:34:56', write)
        self.assertIn('-QuickTime:CreateDate=2026:10:01 12:34:56', write)
        self.assertEqual(write[-1], str(self.export))

    def test_no_subtitle_mode_can_succeed_without_ffmpeg(self):
        self.assertTrue(self.process(no_subtitle=True, has_ffmpeg=False).startswith('success|'))
        self.assertFalse(self.source.exists())
        self.assertFalse(any(cmd[0] in ('ffmpeg', 'ffprobe') for cmd in self.commands))
        self.assertTrue(any('-j' in cmd for cmd in self.commands))

    def test_missing_subtitle_track_preserves_source_after_zero_exit(self):
        self.streams = {'streams': []}
        self.assert_preserved_error()

    def test_handler_name_subtitle_output_can_verify_without_title(self):
        self.streams = {'streams': [{'codec_name': 'mov_text',
                                    'tags': {'handler_name': 'DJI Telemetry'}}]}
        self.assertTrue(self.process().startswith('success|'))
        self.assertFalse(self.source.exists())

    def test_integer_signed_gps_and_absent_altitude_metadata_only(self):
        self.srt.write_text('2026-10-01 12:34:56\n[latitude : -12] [longitude : +123]',
                            encoding='utf-8')
        self.tags['Keys:GPSCoordinates'] = '-12 123 0'
        self.assertEqual(self.process(no_subtitle=True, has_ffmpeg=False),
                         'success|DJI_0001_Edited.mp4|2026-10-01 12:34:56|-12.0,123.0')

    def test_nonfinite_readback_gps_preserves_source(self):
        self.tags['Keys:GPSCoordinates'] = 'NaN 123.45679 -10.25'
        self.assert_preserved_error()

    def test_case_insensitive_source_export_alias_rejected(self):
        self.export = Path(str(self.source).swapcase())
        self.assert_preserved_error()

    def test_missing_ffprobe_preserves_source(self):
        self.failure = 'missing-ffprobe'
        self.assert_preserved_error()

    def test_verified_cached_shortcut_never_recycles_source(self):
        app._processed_set.add(self.export.name)
        self.assertEqual(self.process(force=False), 'already_processed')
        self.assertTrue(self.source.exists())
        self.recycle.assert_not_called()
        self.assertTrue(any('-j' in cmd for cmd in self.commands))

    def test_stale_cached_entry_does_not_authorize_recycling(self):
        app._processed_set.add(self.export.name)
        self.tags['Keys:GPSCoordinates'] = '1 2 3'
        self.failure = 'exiftool'
        self.assert_preserved_error(force=False)

    def test_source_export_alias_rejected_before_mutation(self):
        self.export = self.source
        self.assert_preserved_error()
        self.assertEqual(self.source.read_bytes(), b'original synthetic source')

    def test_hardlink_source_export_alias_rejected(self):
        self.export.unlink()
        os.link(self.source, self.export)
        self.assert_preserved_error()

    def test_preexisting_remux_temp_media_not_overwritten(self):
        existing = self.export_dir / ('temp_' + self.export.name)
        existing.write_bytes(b'unrelated existing media')
        self.assertTrue(self.process().startswith('success|'))
        self.assertEqual(existing.read_bytes(), b'unrelated existing media')

    def test_ambiguous_source_media_preserved(self):
        sibling = self.source.with_suffix('.MOV')
        sibling.write_bytes(b'another original source')
        self.assert_preserved_error()
        self.assertEqual(sibling.read_bytes(), b'another original source')

    def test_recycle_metacharacters_pass_as_data_not_powershell_code(self):
        self.source.unlink()
        self.source = self.source_dir / 'DJI_$(`id`)_\u00e9.MP4'
        self.source.write_bytes(b'synthetic source')
        self.srt = self.source.with_suffix('.SRT')
        calls = []
        with patch.object(app.subprocess, 'run',
                          side_effect=lambda cmd, **kw: calls.append((cmd, kw))):
            REAL_RECYCLE_SOURCE(str(self.srt), str(self.export))
        self.assertEqual(len(calls), 1)
        command, options = calls[0]
        self.assertNotIn(str(self.source), command[command.index('-Command') + 1])
        self.assertEqual(options['env']['DJI_RECYCLE_SOURCE'], str(self.source))
        self.assertTrue(self.source.exists())

    def test_real_recycle_rejects_export_hardlink(self):
        self.export.unlink()
        os.link(self.source, self.export)
        REAL_RECYCLE_SOURCE(str(self.srt), str(self.export))
        self.assertEqual(self.commands, [])
        self.assertTrue(self.source.exists())

    def test_missing_export_rejected_before_recycling(self):
        self.export.unlink()
        self.assert_preserved_error()

    def test_cli_missing_subtitle_tools_preserves_export(self):
        def fail_subtitle_tool(cmd, **options):
            if cmd[0] == 'ffmpeg':
                raise FileNotFoundError('ffmpeg')
            return self.run_tool(cmd, **options)

        with patch.object(app.subprocess, 'run', side_effect=fail_subtitle_tool), patch.object(
                sys, 'argv', ['inject_dji_metadata.py', str(self.source_dir), str(self.export_dir)]):
            self.assertEqual(app.main(), 1)
        self.assertEqual(self.export.read_bytes(), b'synthetic export')
        self.assertTrue(self.source.exists())
        self.assertFalse((self.export_dir / '.dji_processed.json').exists())

    def test_cli_failure_not_counted_or_persisted_as_success(self):
        self.failure = 'exiftool'
        with patch.object(sys, 'argv', ['inject_dji_metadata.py', str(self.source_dir),
                                      str(self.export_dir), '--delete-source', '--delay', '0']):
            self.assertEqual(app.main(), 1)
        summary = (self.export_dir / 'injection_summary.txt').read_text()
        self.assertIn('Successfully Processed: 0', summary)
        self.assertEqual(json.loads((self.export_dir / '.dji_processed.json').read_text()), [])
        self.assertTrue(self.source.exists())

    def test_duplicate_srt_stems_abort_before_any_export_write(self):
        nested = self.source_dir / 'other-flight'
        nested.mkdir()
        (nested / self.srt.name).write_text(self.srt.read_text())
        with patch.object(sys, 'argv', ['inject_dji_metadata.py', str(self.source_dir),
                                      str(self.export_dir), '--delay', '0']):
            self.assertEqual(app.main(), 1)
        self.assertEqual(self.export.read_bytes(), b'synthetic export')
        self.assertTrue(self.source.exists())
        self.assertFalse((self.export_dir / '.dji_processed.json').exists())


class CLITests(unittest.TestCase):
    def run_cli(self, src, export, *options):
        env = dict(os.environ)
        env['PATH'] = ''
        return subprocess.run([sys.executable, 'inject_dji_metadata.py', str(src),
                               str(export), *options], capture_output=True, text=True, env=env)

    def test_missing_directory_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_cli(Path(temp) / 'missing', temp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Source directory not found', result.stdout)

    def test_missing_exiftool_exits_nonzero_without_touching_media(self):
        with tempfile.TemporaryDirectory() as temp:
            src, export = Path(temp) / 'source', Path(temp) / 'export'
            src.mkdir()
            export.mkdir()
            media = export / 'DJI.mp4'
            media.write_bytes(b'synthetic media')
            result = self.run_cli(src, export, '--no-subtitle')
            self.assertEqual(media.read_bytes(), b'synthetic media')
            self.assertFalse((export / '.dji_processed.json').exists())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ExifTool', result.stdout)


if __name__ == '__main__':
    unittest.main()
