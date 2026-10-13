import os
import re
import math
import json
import time
import argparse
import subprocess
import datetime
import shutil
import tempfile

import threading

# Global tracking set for processed files (loaded in main)
_processed_set = set()

# Lock for callers that share console output
print_lock = threading.Lock()

def safe_print(message):
    with print_lock:
        print(message)

def parse_srt_data(srt_path):
    """
    Parses a DJI SRT file to extract the first valid GPS coordinate, timestamp,
    and camera settings (ISO, Shutter, F-stop).
    Returns a dictionary with 'latitude', 'longitude', 'datetime', 'iso', 'shutter', 'fnum'.
    """
    data = {}
    
    try:
        with open(srt_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
            # Find the first timestamp block (e.g., 2025-08-09 18:53:47.246)
            # Regex for Date/Time
            date_match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', content)
            if date_match:
                data['datetime'] = date_match.group(1)
            
            # Regex for GPS and Altitude
            number = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)'
            lat_match = re.search(r'\[latitude\s*:\s*(' + number + r')\]', content)
            lon_match = re.search(r'\[longitude\s*:\s*(' + number + r')\]', content)
            alt_match = re.search(r'abs_alt:\s*(' + number + r')(?=\s|\])', content)
            
            if lat_match and lon_match:
                data['latitude'] = float(lat_match.group(1))
                data['longitude'] = float(lon_match.group(1))
            
            if alt_match:
                data['altitude'] = float(alt_match.group(1))
            elif 'abs_alt:' in content:
                return None

            # Regex for Camera Settings
            # [iso : 100] [shutter : 1/160.0] [fnum : 170]
            iso_match = re.search(r'\[iso\s*:\s*(\d+)\]', content)
            shutter_match = re.search(r'\[shutter\s*:\s*([^\]]+)\]', content)
            fnum_match = re.search(r'\[fnum\s*:\s*(\d+)\]', content)

            if iso_match:
                data['iso'] = iso_match.group(1)
            
            if shutter_match:
                shutter_raw = shutter_match.group(1)
                # Clean up "1/160.0" -> "1/160"
                if ".0" in shutter_raw:
                     data['shutter'] = shutter_raw.replace('.0', '')
                else:
                     data['shutter'] = shutter_raw

            if fnum_match:
                # F-stop is usually multiplied by 100 (e.g. 170 = f/1.7)
                try:
                    f_val = float(fnum_match.group(1))
                    data['fnum'] = f_val / 100.0
                except ValueError:
                    pass
                
    except Exception as e:
        print(f"Error parsing SRT {srt_path}: {e}")
        return None

    if valid_metadata(data):
        return data
    return None


def valid_metadata(metadata):
    """Require complete, usable GPS and local capture time before modifying media."""
    try:
        datetime.datetime.strptime(metadata['datetime'], '%Y-%m-%d %H:%M:%S')
        lat, lon = float(metadata['latitude']), float(metadata['longitude'])
        alt = float(metadata.get('altitude', 0))
        return (all(math.isfinite(value) for value in (lat, lon, alt))
                and -90 <= lat <= 90 and -180 <= lon <= 180)
    except (KeyError, TypeError, ValueError):
        return False


def format_iso6709(lat, lon, alt):
    """
    Formats coordinates as ISO 6709 string: +DD.DDdd+DDD.DDdd+AAA.AAA/
    This is critical for Google Photos recognition.
    """
    lat_sign = '+' if lat >= 0 else '-'
    lon_sign = '+' if lon >= 0 else '-'
    alt_sign = '+' if alt >= 0 else '-'
    
    return f"{lat_sign}{abs(lat):08.5f}{lon_sign}{abs(lon):09.5f}{alt_sign}{abs(alt):08.3f}/"

def inject_metadata(video_path, metadata):
    """
    Uses exiftool to inject GPS and Date metadata into the video file.
    Optimized for Google Photos recognition.
    """
    if not valid_metadata(metadata):
        return False

    # Format datetime for exiftool (YYYY:MM:DD HH:MM:SS)
    dt_str = metadata['datetime'].replace('-', ':')
    
    # We'll use the local time from SRT. 
    # Note: If no timezone is provided, Google Photos usually assumes local time of the upload location.
    
    lat = metadata['latitude']
    lon = metadata['longitude']
    alt = metadata.get('altitude', 0)
    
    iso_gps = format_iso6709(lat, lon, alt)
    
    # Metadata Summary for Description/Comments
    cam_summary = f"DJI Mini 3 Pro | ISO {metadata.get('iso','?')}, {metadata.get('shutter','?')}, f/{metadata.get('fnum','')}"
    keywords = "Drone; DJI; Mini 3 Pro; Telemetry"
    
    # Construct exiftool command
    # Google Photos prefers the 'Keys' group for MP4 metadata, especially for GPS.
    # It also likes standard ISO 6709 formatting for location.
    
    cmd = [
        'exiftool',
        '-overwrite_original',
        
        # Camera Info
        '-Make=DJI',
        '-Model=DJI Mini 3 Pro',
        '-Keys:Make=DJI',
        '-Keys:Model=DJI Mini 3 Pro',
        
        # Description/Searchability (Visible in Explorer/Finder/Google Photos)
        f'-Description={cam_summary}',
        f'-UserComment={cam_summary}',
        f'-Keys:Description={cam_summary}',
        f'-Keywords={keywords}',
        f'-Keys:Keywords={keywords}',
        f'-Keys:DisplayName={os.path.basename(video_path)}',
        
        # Date/Time (CreationDate in Keys is critical for Google Photos)
        f'-Keys:CreationDate={dt_str}',
        f'-QuickTime:CreateDate={dt_str}',
        f'-QuickTime:ModifyDate={dt_str}',
        f'-QuickTime:TrackCreateDate={dt_str}',
        f'-QuickTime:TrackModifyDate={dt_str}',
        f'-QuickTime:MediaCreateDate={dt_str}',
        f'-QuickTime:MediaModifyDate={dt_str}',
        
        # GPS (Strict ISO 6709 format for Google Photos)
        f'-Keys:GPSCoordinates={iso_gps}',
        f'-QuickTime:GPSCoordinates={iso_gps}',
        f'-UserData:GPSCoordinates={iso_gps}',
        
        # XMP (Good for Adobe/Lightroom)
        f'-XMP:GPSLatitude={lat}',
        f'-XMP:GPSLongitude={lon}',
        f'-XMP:GPSAltitude={alt}',
        '-XMP:GPSAltitudeRef=Above Sea Level',
        
        '--', os.path.abspath(video_path)
    ]

    # Add optional camera settings if found
    if 'iso' in metadata:
        cmd.insert(2, f'-ISO={metadata["iso"]}')
    if 'shutter' in metadata:
        cmd.insert(2, f'-ExposureTime={metadata["shutter"]}')
    if 'fnum' in metadata:
        cmd.insert(2, f'-FNumber={metadata["fnum"]}')
    
    safe_print(f"  Injecting metadata into {os.path.basename(video_path)}...")
    safe_print(f"    Date: {dt_str}")
    safe_print(f"    GPS: {lat}, {lon} (Alt: {metadata.get('altitude', 'N/A')}m)")
    if 'iso' in metadata:
        safe_print(f"    Cam: ISO {metadata['iso']}, {metadata.get('shutter','')}, f/{metadata.get('fnum','')}")
    
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except (subprocess.CalledProcessError, OSError) as e:
        safe_print(f"    Error running exiftool: {e}")
        return False


def verify_metadata(video_path, metadata):
    """Read actual Keys GPS/date and QuickTime creation date, not a model hint."""
    try:
        result = subprocess.run(
            ['exiftool', '-j', '-G1', '-n', '-Keys:GPSCoordinates',
             '-Keys:CreationDate', '-QuickTime:CreateDate', '--', os.path.abspath(video_path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8')
        records = json.loads(result.stdout)
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            return False
        tags = records[0]
        gps = [float(value) for value in tags['Keys:GPSCoordinates'].split()]
        expected = (metadata['latitude'], metadata['longitude'], metadata.get('altitude', 0))
        # The existing ISO 6709 writer rounds GPS to five decimals and altitude to three.
        if len(gps) != 3 or not all(
                math.isfinite(value) and math.isclose(value, wanted, rel_tol=0, abs_tol=tolerance)
                for value, wanted, tolerance in zip(gps, expected, (0.0000051, 0.0000051, 0.00051))):
            return False
        expected_date = metadata['datetime'].replace('-', ':')
        # Keys:CreationDate may gain a local timezone suffix; SRT has no timezone.
        creation_date = tags['Keys:CreationDate']
        if not isinstance(creation_date, str) or not re.fullmatch(
                re.escape(expected_date) + r'(?:Z|[-+](?:0\d|1\d|2[0-3]):[0-5]\d)?', creation_date):
            return False
        return tags['QuickTime:CreateDate'] == expected_date
    except (subprocess.CalledProcessError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def verify_subtitle(video_path):
    """Require the requested telemetry subtitle in the final exported file."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 's', '-show_entries',
             'stream=codec_name:stream_tags=title,handler_name', '-of', 'json', os.path.abspath(video_path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8')
        streams = json.loads(result.stdout)['streams']
        return any(stream.get('codec_name') == 'mov_text'
                   and 'DJI Telemetry' in (stream.get('tags', {}).get('title'),
                                          stream.get('tags', {}).get('handler_name'))
                   for stream in streams)
    except (subprocess.CalledProcessError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return False

def check_ffmpeg():
    """Checks if ffmpeg is available in the system PATH."""
    return check_tool('ffmpeg', '-version')


def check_tool(program, version_option):
    """Preflight an executable without modifying any media."""
    try:
        subprocess.run([program, version_option], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False

def embed_subtitle(video_path, srt_path, metadata=None):
    """
    Embeds the SRT file as a subtitle track into the video using ffmpeg.
    Optionally sets creation_time metadata during the same remux pass.
    Returns True if successful, False otherwise.
    """
    # Create a temp output file
    dir_name = os.path.dirname(video_path)
    base_name = os.path.basename(video_path)
    fd, temp_output = tempfile.mkstemp(prefix='.dji_', suffix=os.path.splitext(base_name)[1],
                                       dir=dir_name or '.')
    os.close(fd)
    
    # FFmpeg command to embed subtitle
    # -c copy (copy video/audio streams)
    # -c:s mov_text (convert srt to mp4 compatible subtitle)
    # -movflags +faststart (move moov atom to start for better playback/seeking)
    cmd = [
        'ffmpeg', '-y', # Overwrite temp if exists
        '-i', os.path.abspath(video_path),
        '-i', os.path.abspath(srt_path),
        '-map', '0:v?', '-map', '0:a?', '-map', '1:0',
        '-c', 'copy',
        '-c:s', 'mov_text',
        '-movflags', '+faststart',
        '-metadata:s:s:0', 'language=eng',
        '-metadata:s:s:0', 'title=DJI Telemetry',
        '-metadata:s:s:0', 'handler_name=DJI Telemetry',
    ]
    # Piggyback creation_time during remux to reduce exiftool work
    if metadata and 'datetime' in metadata:
        creation_time = metadata['datetime'].replace(' ', 'T')
        cmd.extend(['-metadata', f'creation_time={creation_time}'])
    cmd.append(temp_output)
    
    safe_print(f"Embedding SRT into {base_name}...")
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        
        # If successful, replace original with temp
        shutil.move(temp_output, video_path)
        safe_print("  Subtitle embedding successful!")
        return True
    except subprocess.CalledProcessError as e:
        details = (e.stderr or b'').decode('utf-8', errors='replace')
        safe_print(f"  Error embedding subtitle: {details or str(e)}")
        if os.path.exists(temp_output):
            os.remove(temp_output)
        return False
    except Exception as e:
        safe_print(f"  Error: {e}")
        if os.path.exists(temp_output):
            os.remove(temp_output)
        return False

def check_if_processed(video_path):
    """
    Checks if the video has already been processed by looking for specific metadata.
    Uses tracking or camera model only as a hint to request full verification.
    A True result never establishes successful processing or permits recycling.
    """
    # Fast path: check in-memory tracking set (loaded from .dji_processed.json)
    if os.path.basename(video_path) in _processed_set:
        return True
    
    try:
        # Check for our injected Model tag (-fast reads only file header, much less I/O)
        result = subprocess.run(
            ['exiftool', '-fast', '-Model', '-s3', '--', os.path.abspath(video_path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        model = result.stdout.strip()
        if "DJI Mini 3 Pro" in model:
            return True
                
    except Exception:
        pass
        
    return False

def process_single_video(video_info):
    """Process one export; preserve the public nine-element tuple/status API."""
    export_path, matched_srt, has_ffmpeg, force, delete_source, source_map, video_count, current_idx, no_subtitle = video_info
    filename = os.path.basename(export_path)
    
    safe_print(f"[{current_idx}/{video_count}] Processing {filename}...")
    
    # Filename tracking is a hint only; failures must invalidate stale entries.
    processed_hint = not force and check_if_processed(export_path)
    _processed_set.discard(filename)
    if not os.path.isfile(export_path):
        safe_print("  Error: export video file not found.")
        return "error"
    sources = source_video_candidates(matched_srt)
    if len(sources) > 1 or any(same_file(source, export_path) for source in sources):
        safe_print("  Error: ambiguous source or source/export refer to the same file.")
        return "error"

    metadata = parse_srt_data(matched_srt)
    if not metadata:
        safe_print(f"  Warning: Could not extract metadata from {os.path.basename(matched_srt)}")
        return "error"
    if not no_subtitle and not has_ffmpeg:
        safe_print("  Error: subtitle tools required; use --no-subtitle for metadata only.")
        return "error"

    if (processed_hint and verify_metadata(export_path, metadata)
            and (no_subtitle or verify_subtitle(export_path))):
        _processed_set.add(filename)
        # Skips never authorize source removal, including legacy cached entries.
        return "already_processed"

    if not no_subtitle and not embed_subtitle(export_path, matched_srt, metadata):
        return "error"
    if not inject_metadata(export_path, metadata) or not verify_metadata(export_path, metadata):
        safe_print("  Error: metadata write/read-back verification failed; source preserved.")
        return "error"
    if not no_subtitle and not verify_subtitle(export_path):
        safe_print("  Error: subtitle read-back verification failed; source preserved.")
        return "error"
    final_status = f"success|{filename}|{metadata.get('datetime','?')}|{metadata.get('latitude',0)},{metadata.get('longitude',0)}"
    _processed_set.add(filename)
    if delete_source:
        recycle_source(matched_srt, export_path)
    return final_status


def same_file(first, second):
    """Reject Windows case aliases, symlinks and hardlinks when identity is available."""
    if os.path.realpath(os.path.abspath(first)).casefold() == os.path.realpath(os.path.abspath(second)).casefold():
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def source_video_candidates(matched_srt):
    """Resolve actual case-insensitive sibling MP4/MOV names without picking an ambiguity."""
    directory = os.path.dirname(os.path.abspath(matched_srt))
    stem = os.path.splitext(os.path.basename(matched_srt))[0].casefold()
    try:
        return [os.path.join(directory, name) for name in os.listdir(directory)
                if os.path.splitext(name)[0].casefold() == stem
                and os.path.splitext(name)[1].casefold() in ('.mp4', '.mov')
                and os.path.isfile(os.path.join(directory, name))]
    except OSError:
        return []


def recycle_source(matched_srt, export_path):
    """Locates the source video file for an SRT and moves it to the Recycle Bin."""
    sources = source_video_candidates(matched_srt)
    if len(sources) != 1 or same_file(sources[0], export_path):
        return
    src_vid = sources[0]
    try:
        # Fixed code; paths (including $, backticks and quotes) travel only as data.
        recycle_script = (
            'Add-Type -AssemblyName Microsoft.VisualBasic; '
            '[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('
            '$env:DJI_RECYCLE_SOURCE, "OnlyErrorDialogs", "SendToRecycleBin")')
        environment = os.environ.copy()
        environment['DJI_RECYCLE_SOURCE'] = src_vid
        subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', recycle_script],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=environment)
        safe_print(f"  [SPACE SAVER] Moved original source to Recycle Bin: {os.path.basename(src_vid)}")
    except (subprocess.CalledProcessError, OSError) as e:
        safe_print(f"  Error moving source to Recycle Bin {src_vid}: {e}")

def main():
    parser = argparse.ArgumentParser(description="Inject DJI GPS data from SRT files into exported videos.")
    parser.add_argument('src_dir', help="Directory containing original source files (MP4 + SRT)")
    parser.add_argument('export_dir', help="Directory containing exported videos to process")
    parser.add_argument('--force', action='store_true', help="Force processing even if already processed")
    parser.add_argument('--threads', type=int, default=1, help="Deprecated compatibility option; processing is always serial (default: 1)")
    parser.add_argument('--delete-source', action='store_true', help="Delete original source MP4/MOV after successful injection (Keeps SRT)")
    parser.add_argument('--no-subtitle', action='store_true', help="Skip ffmpeg subtitle embedding (metadata only, halves disk I/O)")
    parser.add_argument('--delay', type=int, default=5, help="Seconds to wait between files to let SSD cool down (default: 5, 0 to disable)")
    
    args = parser.parse_args()
    
    src_dir = args.src_dir
    export_dir = args.export_dir
    force = args.force
    delete_source = args.delete_source
    no_subtitle = args.no_subtitle
    
    global _processed_set
    _processed_set = set()
    if not os.path.isdir(src_dir):
        print(f"Source directory not found: {src_dir}")
        return 1
    if not os.path.isdir(export_dir):
        print(f"Export directory not found: {export_dir}")
        return 1
    if not check_tool('exiftool', '-ver'):
        print("ExifTool unavailable or failed its version check; install it on PATH.")
        return 1
    has_ffmpeg = False
    if not no_subtitle:
        has_ffmpeg = check_ffmpeg()
        if not has_ffmpeg or not check_tool('ffprobe', '-version'):
            print("FFmpeg and ffprobe required for subtitles; install them or use --no-subtitle.")
            return 1
    if args.threads != 1:
        print("--threads is deprecated and ignored; processing is serial.")

    # Tracking entries are hints and must pass full current-file verification.
    tracking_path = os.path.join(export_dir, ".dji_processed.json")
    if os.path.exists(tracking_path) and not force:
        try:
            with open(tracking_path, 'r') as f:
                _processed_set = set(json.load(f))
            print(f"Loaded {len(_processed_set)} previously processed entries from tracking file.")
        except Exception:
            _processed_set = set()
    
    if delete_source:
        print("\n!!! WARNING: --delete-source is ENABLED !!!")
        print("Original source videos move to RECYCLE BIN only after new processing and read-back verification.")
        print("SRT files will be preserved.\n")
        # Optional: Add a pause or confirmation here if running interactively?
        # For batch automation, we skip confirmation.
    
    if no_subtitle:
        print("\n--no-subtitle: Skipping subtitle embedding and verification (metadata only).")
    else:
        print("\nSubtitle embedding enabled. Processing serially.")

    # Map source filenames (without extension) to their SRT paths
    source_map = {}
    ambiguous_sources = set()
    print(f"Scanning source directory: {src_dir} ...")
    for root, dirs, files in os.walk(src_dir):
        for filename in files:
            if filename.lower().endswith('.srt'):
                base_name = os.path.splitext(filename)[0].lower()
                if base_name in source_map:
                    ambiguous_sources.add(base_name)
                source_map[base_name] = os.path.join(root, filename)

    if ambiguous_sources:
        print("Ambiguous SRT filenames in source tree: " + ', '.join(sorted(ambiguous_sources)))
        print("Use source folders with unique SRT filenames; no exports processed.")
        return 1
            
    print(f"Found {len(source_map)} SRT templates in source directory tree.")

    # Find all videos to process
    videos_to_process = []
    print(f"Scanning export directory: {export_dir} ...")
    for root, dirs, files in os.walk(export_dir):
        for filename in files:
            if filename.lower().endswith(('.mp4', '.mov')):
                export_path = os.path.join(root, filename)
                if "_exiftool_tmp" in export_path or "temp_" in filename or filename.startswith('.dji_'):
                    continue # Skip our own temp files
                    
                base_name = os.path.splitext(filename)[0].lower()
                
                matched_srt = None
                if base_name in source_map:
                    matched_srt = source_map[base_name]
                else:
                    matches = [key for key in source_map.keys() if key in base_name]
                    if matches:
                        best_match = sorted(matches, key=len, reverse=True)[0]
                        matched_srt = source_map[best_match]
                
                if matched_srt:
                    videos_to_process.append((export_path, matched_srt))

    total_videos = len(videos_to_process)
    print(f"Found {total_videos} videos with matching source data.")

    # Process serially; --threads remains accepted for existing batch files.
    processed_count = 0
    skipped_count = 0
    already_processed_count = 0
    successful_log = []
    
    # Process videos sequentially with optional cooldown delay
    results = []
    for i, (export_path, matched_srt) in enumerate(videos_to_process, 1):
        task = (export_path, matched_srt, has_ffmpeg, force, delete_source, source_map, total_videos, i, no_subtitle)
        result = process_single_video(task)
        results.append(result)
        
        # Cooldown delay between files to reduce sustained SSD heat
        if args.delay > 0 and i < total_videos and result != "already_processed":
            safe_print(f"  Cooling down {args.delay}s before next file...")
            time.sleep(args.delay)

    # Tally results
    for res in results:
        if res.startswith("success"):
            processed_count += 1
            successful_log.append(res.split('|')[1:])
        elif res == "already_processed": 
            already_processed_count += 1
        else: 
            skipped_count += 1

    # Write Summary Log
    log_path = os.path.join(export_dir, "injection_summary.txt")
    with open(log_path, 'w', encoding='utf-8') as f:
        f.write(f"DJI Metadata Injection Summary - {datetime.datetime.now()}\n")
        f.write(f"====================================================\n\n")
        f.write(f"Total Videos Matched: {total_videos}\n")
        f.write(f"Successfully Processed: {processed_count}\n")
        f.write(f"Skipped (Already Processed): {already_processed_count}\n")
        f.write(f"Errors/No Match: {total_videos - processed_count - already_processed_count}\n\n")
        f.write(f"Details of Processed Files:\n")
        f.write(f"{'-'*50}\n")
        for entry in successful_log:
            f.write(f"File: {entry[0]} | Date: {entry[1]} | GPS: {entry[2]}\n")

    # Save processed tracking file for fast future re-runs
    try:
        with open(tracking_path, 'w') as f:
            json.dump(sorted(_processed_set), f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save tracking file: {e}")

    print(f"\nProcessing complete.")
    print(f"Processed (New): {processed_count}")
    print(f"Skipped (Already Processed): {already_processed_count}")
    print(f"Skipped (No Match/Error): {total_videos - processed_count - already_processed_count}")
    print(f"Summary saved to: {log_path}")
    return 1 if skipped_count else 0

if __name__ == "__main__":
    raise SystemExit(main())
