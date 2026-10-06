"""Recording decoding: noaa-apt for NOAA APT, sstv for ISS Robot 36."""
import os
import subprocess

def decode_recording(wav_path):
    """Decode a pass recording to a PNG next to the WAV.

    The decoder is chosen by satellite, detected from the recording filename
    (the SDR thread names files '<sat>_<timestamp>.wav').

    Returns (success, png_path, error_message).
    """
    output_png = wav_path.replace('.wav', '.png')
    name_lower = os.path.basename(wav_path).lower()

    if 'iss' in name_lower:
        return _decode_sstv(wav_path, output_png)
    return _decode_apt(wav_path, output_png)

def _decode_sstv(wav_path, output_png):
    """Decode ISS Slow-Scan TV (Robot 36 during ARISS events).

    The sstv library auto-detects the mode from the VIS header and returns
    every image in the recording; the ISS repeats images during a pass, so
    extras are saved as <base>_2.png, <base>_3.png, ...
    """
    try:
        import sstv
    except ImportError:
        return False, None, 'sstv decoder not installed (pip install sstv)'
    try:
        images = sstv.decode_from_wav(wav_path)
    except Exception as e:
        return False, None, f'SSTV decode error: {e}'
    if not images:
        return False, None, 'No SSTV transmission found in recording'
    images[0].save(output_png)
    base, _ = os.path.splitext(output_png)
    for i, image in enumerate(images[1:], start=2):
        image.save(f'{base}_{i}.png')
    return True, output_png, None

def _decode_apt(wav_path, output_png):
    """Decode NOAA APT weather images with noaa-apt."""
    name_lower = os.path.basename(wav_path).lower()
    sat_arg = None
    if 'noaa_15' in name_lower or 'noaa15' in name_lower:
        sat_arg = 'noaa_15'
    elif 'noaa_18' in name_lower or 'noaa18' in name_lower:
        sat_arg = 'noaa_18'
    elif 'noaa_19' in name_lower or 'noaa19' in name_lower:
        sat_arg = 'noaa_19'
    cmd = ['noaa-apt', wav_path, '-o', output_png, '-q', '-m', 'yes',
           '-R', 'auto', '-T', '/var/log/noaa/weather.txt']
    if sat_arg:
        cmd.extend(['-s', sat_arg])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=120, cwd='/opt/noaa-apt')
    except subprocess.TimeoutExpired:
        return False, None, 'Decode timeout'
    except Exception as e:
        return False, None, str(e)

    if os.path.exists(output_png):
        return True, output_png, None
    return False, None, result.stderr or 'No output image'
