import os
import re
import math
import argparse
import subprocess
import datetime
import shutil

import threading
from concurrent.futures import ThreadPoolExecutor

# Lock for clean console output during multi-threading
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
            lat_match = re.search(r'\[latitude\s*:\s*([-+]?\d*\.\d+|\d+)\]', content)
            lon_match = re.search(r'\[longitude\s*:\s*([-+]?\d*\.\d+|\d+)\]', content)
            alt_match = re.search(r'abs_alt:\s*([-+]?\d*\.\d+|\d+)', content)
            
            if lat_match and lon_match:
                data['latitude'] = float(lat_match.group(1))
                data['longitude'] = float(lon_match.group(1))
            
            if alt_match:
                data['altitude'] = float(alt_match.group(1))

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

    if 'latitude' in data and 'longitude' in data:
        return data
    return None


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
    if not metadata:
        return

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
        
        video_path
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
    
    # Cleanup any leftover exiftool temp files
    tmp_path = video_path + "_exiftool_tmp"
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
            safe_print(f"    Cleaned up leftover temp file: {os.path.basename(tmp_path)}")
        except Exception as e:
            safe_print(f"    Warning: Could not remove {tmp_path}: {e}")

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        safe_print("    Success!")
    except subprocess.CalledProcessError as e:
        safe_print(f"    Error running exiftool: {e.stderr.decode()}")

def check_ffmpeg():
    """Checks if ffmpeg is available in the system PATH."""
    try:
        subprocess.run(['ffmpeg', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def embed_subtitle(video_path, srt_path):
    """
    Embeds the SRT file as a subtitle track into the video using ffmpeg.
    Returns True if successful, False otherwise.
    """
    # Create a temp output file
    dir_name = os.path.dirname(video_path)
    base_name = os.path.basename(video_path)
    temp_output = os.path.join(dir_name, f"temp_{base_name}")
    
    # FFmpeg command to embed subtitle
    # -c copy (copy video/audio streams)
    # -c:s mov_text (convert srt to mp4 compatible subtitle)
    # -metadata:s:s:0 language=eng (set language)
    cmd = [
        'ffmpeg', '-y', # Overwrite temp if exists
        '-i', video_path,
        '-i', srt_path,
        '-c', 'copy',
        '-c:s', 'mov_text',
        '-metadata:s:s:0', 'language=eng',
        '-metadata:s:s:0', 'handler_name=Telemetry',
        '-metadata:s:s:0', 'title=DJI Telemetry',
        temp_output
    ]
    
    safe_print(f"Embedding SRT into {base_name}...")
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        
        # If successful, replace original with temp
        shutil.move(temp_output, video_path)
        safe_print("  Subtitle embedding successful!")
        return True
    except subprocess.CalledProcessError as e:
        safe_print(f"  Error embedding subtitle: {e.stderr.decode()}")
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
    Returns True if valid DJI metadata or subtitle track is found.
    """
    try:
        # Check for our injected Model tag
        result = subprocess.run(
            ['exiftool', '-Model', '-s3', video_path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        model = result.stdout.strip()
        if "DJI Mini 3 Pro" in model:
            return True
            
        # Check for Telemetry subtitle track using ffprobe (if available)
        if check_ffmpeg():
            cmd = [
                'ffprobe', '-v', 'error', '-select_streams', 's', 
                '-show_entries', 'stream_tags=title', 
                '-of', 'default=noprint_wrappers=1:nokey=1', 
                video_path
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            if "DJI Telemetry" in result.stdout:
                return True
                
    except Exception:
        pass
        
    return False

from concurrent.futures import ThreadPoolExecutor

def process_single_video(video_info):
    """Worker function for parallel processing."""
    export_path, matched_srt, has_ffmpeg, force, delete_source, source_map, video_count, current_idx = video_info
    filename = os.path.basename(export_path)
    
    safe_print(f"[{current_idx}/{video_count}] Processing {filename}...")
    
    # Check if already processed
    if not force and check_if_processed(export_path):
        # We still might want to recycle the source even if the export is already matched/processed
        if delete_source:
             recycle_source(matched_srt, export_path)
        return "already_processed"
    
    # 1. Embed Subtitle
    srt_ok = True
    if has_ffmpeg:
        srt_ok = embed_subtitle(export_path, matched_srt)
    
    # 2. Inject Metadata
    meta_ok = False
    metadata = parse_srt_data(matched_srt)
    final_status = "error"
    
    if metadata:
        inject_metadata(export_path, metadata)
        meta_ok = True
        final_status = f"success|{filename}|{metadata.get('datetime','?')}|{metadata.get('latitude',0)},{metadata.get('longitude',0)}"
    else:
        safe_print(f"  Warning: Could not extract metadata from {os.path.basename(matched_srt)}")
    
    # 3. Delete Source (Create Space)
    if delete_source and meta_ok and srt_ok:
        recycle_source(matched_srt, export_path)

    return final_status

def recycle_source(matched_srt, export_path):
    """Locates the source video file for an SRT and moves it to the Recycle Bin."""
    src_dir = os.path.dirname(matched_srt)
    src_base = os.path.splitext(os.path.basename(matched_srt))[0]
    
    # Check for MP4 or MOV
    potential_srcs = [
        os.path.join(src_dir, src_base + ".MP4"),
        os.path.join(src_dir, src_base + ".mp4"),
        os.path.join(src_dir, src_base + ".MOV"),
        os.path.join(src_dir, src_base + ".mov")
    ]
    
    for src_vid in potential_srcs:
        if os.path.exists(src_vid):
            # SAFETY: Ensure we aren't deleting the export file itself!
            if os.path.normpath(src_vid) == os.path.normpath(export_path):
                return
                
            try:
                # Use the VisualBasic Shell API for moving to Recycle Bin (more robust than Remove-Item -Recycle)
                # We escape double quotes in the path for the PowerShell string
                escaped_path = src_vid.replace('"', '`"')
                cmd_recycle = [
                    'powershell', 
                    '-Command', 
                    f'Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("{escaped_path}", "OnlyErrorDialogs", "SendToRecycleBin")'
                ]
                subprocess.run(cmd_recycle, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                safe_print(f"  [SPACE SAVER] Moved original source to Recycle Bin: {os.path.basename(src_vid)}")
                return
            except Exception as e:
                safe_print(f"  Error moving source to Recycle Bin {src_vid}: {e}")
                return

def main():
    parser = argparse.ArgumentParser(description="Inject DJI GPS data from SRT files into exported videos.")
    parser.add_argument('src_dir', help="Directory containing original source files (MP4 + SRT)")
    parser.add_argument('export_dir', help="Directory containing exported videos to process")
    parser.add_argument('--force', action='store_true', help="Force processing even if already processed")
    parser.add_argument('--threads', type=int, default=4, help="Number of concurrent threads (default: 4)")
    parser.add_argument('--delete-source', action='store_true', help="Delete original source MP4/MOV after successful injection (Keeps SRT)")
    
    args = parser.parse_args()
    
    src_dir = args.src_dir
    export_dir = args.export_dir
    force = args.force
    delete_source = args.delete_source
    
    if delete_source:
        print("\n!!! WARNING: --delete-source is ENABLED !!!")
        print("Original source video files will be moved to the RECYCLE BIN after successful matching.")
        print("SRT files will be preserved.\n")
        # Optional: Add a pause or confirmation here if running interactively?
        # For batch automation, we skip confirmation.
    
    has_ffmpeg = check_ffmpeg()
    if has_ffmpeg:
        print(f"\nFFmpeg detected. Subtitle embedding enabled (using {args.threads} threads).")
    else:
        print(f"\nFFmpeg NOT detected. Skipping subtitle embedding (metadata only).")
    
    if not os.path.exists(src_dir):
        print(f"Source directory not found: {src_dir}")
        return
    if not os.path.exists(export_dir):
        print(f"Export directory not found: {export_dir}")
        return

    # Map source filenames (without extension) to their SRT paths
    source_map = {}
    print(f"Scanning source directory: {src_dir} ...")
    for root, dirs, files in os.walk(src_dir):
        for filename in files:
            if filename.lower().endswith('.srt'):
                base_name = os.path.splitext(filename)[0].lower()
                source_map[base_name] = os.path.join(root, filename)
            
    print(f"Found {len(source_map)} SRT templates in source directory tree.")

    # Find all videos to process
    videos_to_process = []
    print(f"Scanning export directory: {export_dir} ...")
    for root, dirs, files in os.walk(export_dir):
        for filename in files:
            if filename.lower().endswith(('.mp4', '.mov')):
                export_path = os.path.join(root, filename)
                if "_exiftool_tmp" in export_path or "temp_" in filename:
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

    # Process in parallel
    processed_count = 0
    skipped_count = 0
    already_processed_count = 0
    successful_log = []
    
    # Prepare task list for executor
    tasks = []
    for i, (export_path, matched_srt) in enumerate(videos_to_process, 1):
        tasks.append((export_path, matched_srt, has_ffmpeg, force, delete_source, source_map, total_videos, i))

    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        results = list(executor.map(process_single_video, tasks))

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

    print(f"\nProcessing complete.")
    print(f"Processed (New): {processed_count}")
    print(f"Skipped (Already Processed): {already_processed_count}")
    print(f"Skipped (No Match/Error): {total_videos - processed_count - already_processed_count}")
    print(f"Summary saved to: {log_path}")

if __name__ == "__main__":
    main()
