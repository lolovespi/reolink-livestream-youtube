def build_ffmpeg_cmd(env: dict, rtmp_url_with_key: str):
    """
    - Stable RTSP over TCP for Reolink.
    - Generates silent stereo track so YouTube always sees audio.
    - Downscales/transcodes to 720p H.264 + AAC (Pi 4 friendly).
    """
    rtsp = env["RTSP_URL"]
    vw = env.get("VIDEO_WIDTH", "1280")
    vh = env.get("VIDEO_HEIGHT", "720")
    fps = env.get("VIDEO_FPS", "30")
    vbr = env.get("VIDEO_BITRATE", "2500k")
    keyint = env.get("KEYINT", "60")
    asr = env.get("AUDIO_SAMPLE_RATE", "44100")
    abr = env.get("AUDIO_BITRATE", "128k")

    return [
        "ffmpeg",
        "-hide_banner", "-loglevel", "warning",

        "-rtsp_transport", "tcp",
        "-thread_queue_size", "512",
        "-i", rtsp,

        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={asr}",

        "-map", "0:v:0",
        "-map", "1:a:0",

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", vbr,
        "-maxrate", vbr,
        "-bufsize", "6000k",
        "-g", keyint,
        "-r", fps,
        "-vf", f"scale={vw}:{vh},format=yuv420p",

        "-c:a", "aac",
        "-b:a", abr,
        "-ar", asr,
        "-ac", "2",

        "-f", "flv",
        rtmp_url_with_key
    ]
