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
        # ISS SSTV (Robot 36 during ARISS events); the sstv tool
        # auto-detects the mode from the audio.
        cmd = ['sstv', '-d', wav_path, '-o', output_png]
        cwd = None
    else:
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
        cwd = '/opt/noaa-apt'

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=120, cwd=cwd)
    except subprocess.TimeoutExpired:
        return False, None, 'Decode timeout'
    except Exception as e:
        return False, None, str(e)

    if os.path.exists(output_png):
        return True, output_png, None
    return False, None, result.stderr or 'No output image'
