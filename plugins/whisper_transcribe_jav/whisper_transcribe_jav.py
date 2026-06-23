#!/usr/bin/env python3
"""
Whisper Transcribe Plugin

This plugin integrates with a whisper.cpp server
to automatically generate subtitles (SRT) for video files when a scene is updated.
It follows the same structure as the example RenameFile plugin.
"""

import os
import sys
import re
import traceback
import subprocess
import tempfile
import json
import urllib.request
import urllib.error

# Stash helper classes (same as in the RenameFile example)
try:
    from StashPluginHelper import StashPluginHelper, taskQueue
except Exception:
    # Fallback to local minimal helper if StashPluginHelper isn't available
    from stash_helper_fallback import StashPluginHelper, taskQueue  # type: ignore

# GraphQL helper utilities to fetch plugin settings when the helper can't.
def _build_graphql_url(server_connection: dict) -> str:
    scheme = (server_connection or {}).get("Scheme") or (server_connection or {}).get("scheme") or "http"
    host = (server_connection or {}).get("Host") or (server_connection or {}).get("host") or "127.0.01"
    port = (server_connection or {}).get("Port") or (server_connection or {}).get("port")
    if port:
        return f"{scheme}://{host}:{port}/graphql"
    return f"{scheme}://{host}/graphql"


def _cookie_header(session_cookie: object) -> str | None:
    # Expecting a dict-like cookie with Name/Value (case-insensitive)
    try:
        if isinstance(session_cookie, dict):
            name = session_cookie.get("Name") or session_cookie.get("name")
            value = session_cookie.get("Value") or session_cookie.get("value")
            if name and value:
                return f"{name}={value}"
    except Exception:
        pass
    return None


def _fetch_server_url_from_settings(json_input: dict) -> str | None:
    """
    Best-effort fetch of this plugin's saved 'serverUrl' from Stash via GraphQL.
    Works even when running with the minimal fallback helper.
    """
    try:
        conn = (json_input or {}).get("server_connection") or (json_input or {}).get("ServerConnection") or {}
        graphql_url = _build_graphql_url(conn)
        cookie = (conn.get("SessionCookie") or conn.get("session_cookie") or conn.get("sessionCookie"))
        cookie_hdr = _cookie_header(cookie)

        # Query the configuration endpoint, which includes plugin configuration values.
        query = """
            query($ids: [ID!]) {
                configuration {
                    plugins(include: $ids)
                }
            }
        """
        headers = {"Content-Type": "application/json"}
        if cookie_hdr:
            headers["Cookie"] = cookie_hdr
        # Ask for both common plugin IDs to maximise compatibility.
        variables = {"ids": ["whisper_transcribe_jav", "WhisperTranscribeJAV"]}
        payload = json.dumps({"query": query, "variables": variables})

        try:
            import requests  # type: ignore
        except Exception:
            requests = None

        if requests is not None:
            resp = requests.post(graphql_url, data=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        else:
            req = urllib.request.Request(graphql_url, data=payload.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        config_plugins = (((data or {}).get("data") or {}).get("configuration") or {}).get("plugins") or {}
        if not isinstance(config_plugins, dict):
            return None

        # Configuration plugins come back as a map of pluginID -> map of settings.
        for pid in variables["ids"]:
            settings_map = config_plugins.get(pid)
            if isinstance(settings_map, dict):
                v = settings_map.get("serverUrl")
                if isinstance(v, str) and v.strip():
                    return v.strip()

        return None
    except Exception:
        # Silent best-effort
        return None


def _fetch_plugin_settings(json_input: dict) -> dict:
    """
    Fetch this plugin's full saved settings map from Stash via GraphQL.
    Needed because the minimal fallback helper's Setting() only returns defaults,
    so every UI setting (translate, language, maxCaption, etc.) must be read here.
    """
    try:
        conn = (json_input or {}).get("server_connection") or (json_input or {}).get("ServerConnection") or {}
        graphql_url = _build_graphql_url(conn)
        cookie = (conn.get("SessionCookie") or conn.get("session_cookie") or conn.get("sessionCookie"))
        cookie_hdr = _cookie_header(cookie)
        query = """
            query($ids: [ID!]) {
                configuration { plugins(include: $ids) }
            }
        """
        headers = {"Content-Type": "application/json"}
        if cookie_hdr:
            headers["Cookie"] = cookie_hdr
        variables = {"ids": ["whisper_transcribe_jav", "WhisperTranscribeJAV"]}
        payload = json.dumps({"query": query, "variables": variables})

        try:
            import requests  # type: ignore
        except Exception:
            requests = None

        if requests is not None:
            resp = requests.post(graphql_url, data=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        else:
            req = urllib.request.Request(graphql_url, data=payload.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        config_plugins = (((data or {}).get("data") or {}).get("configuration") or {}).get("plugins") or {}
        if isinstance(config_plugins, dict):
            for pid in variables["ids"]:
                settings_map = config_plugins.get(pid)
                if isinstance(settings_map, dict):
                    return settings_map
        return {}
    except Exception:
        return {}

# Self-contained transcription logic (no external imports).
def _post_whisper_audio(wav_path: str, server_url: str, translate: bool, extra_fields: dict | None = None) -> str:
    extra_fields = extra_fields or {}
    try:
        import requests  # type: ignore
    except Exception:
        requests = None

    if requests is not None:
        with open(wav_path, "rb") as audio_file:
            files = {"file": (os.path.basename(wav_path), audio_file, "audio/wav")}
            # Always send translate explicitly so the server's CLI default (e.g. "-tr")
            # cannot override our choice when translation is turned off.
            data = {"response_format": "srt", "translate": "true" if translate else "false"}
            # Extra whisper.cpp form fields (language, temperature, etc.) from UI settings.
            data.update(extra_fields)
            try:
                resp = requests.post(server_url, files=files, data=data, timeout=3600)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                raise RuntimeError(f"Error sending request to whisper server at {server_url}. Is it running and reachable? {e}") from e
    else:
        boundary = "----WhisperBoundary7MA4YWxkTrZu0gW"

        def _encode_part(name: str, value: str) -> bytes:
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")

        with open(wav_path, "rb") as f:
            file_content = f.read()

        parts = []
        parts.append(_encode_part("response_format", "srt"))
        # Always send translate explicitly so the server's CLI default (e.g. "-tr")
        # cannot override our choice when translation is turned off.
        parts.append(_encode_part("translate", "true" if translate else "false"))
        # Extra whisper.cpp form fields (language, temperature, etc.) from UI settings.
        for _k, _v in extra_fields.items():
            parts.append(_encode_part(_k, str(_v)))
        file_header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{os.path.basename(wav_path)}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
        parts.append(file_header)
        parts.append(file_content)
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        req = urllib.request.Request(
            server_url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else str(e)
            raise RuntimeError(f"HTTP error from whisper server: {e.code} {e.reason}. {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error contacting whisper server at {server_url}: {e}") from e
        except Exception as e:
            raise RuntimeError(f"Unexpected error contacting whisper server: {e}") from e


def _check_whisper_server(server_url: str, timeout: float = 5.0) -> None:
    """
    Best-effort connectivity check. We only verify that a TCP connection can be made.
    Any HTTP status code is considered "reachable".
    """
    try:
        import requests  # type: ignore
    except Exception:
        requests = None

    if requests is not None:
        try:
            # OPTIONS is commonly allowed; even a 4xx/5xx means the server is reachable.
            requests.options(server_url, timeout=timeout)
        except Exception as e:
            raise RuntimeError(
                f"Cannot reach whisper server at {server_url}. "
                "Configure the 'Whisper Server URL' plugin setting or set WHISPER_SERVER_URL. "
                f"Underlying error: {e}"
            ) from e
    else:
        # Fallback to urllib request if `requests` is unavailable.
        req = urllib.request.Request(server_url, method="OPTIONS")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as _:
                return
        except urllib.error.HTTPError:
            # Server reachable (wrong method) – that's sufficient to proceed.
            return
        except Exception as e:
            raise RuntimeError(
                f"Cannot reach whisper server at {server_url}. "
                "Configure the 'Whisper Server URL' plugin setting or set WHISPER_SERVER_URL. "
                f"Underlying error: {e}"
            ) from e


def _build_caption_path(video_path: str, language_code: str | None = None) -> str:
    """
    Build an SRT path next to the video. If a language code is provided, suffix it
    (e.g. video.en.srt) so Stash can associate captions without a full rescan.
    """
    base, _ = os.path.splitext(video_path)
    lang = (language_code or "").strip().lower()
    if lang:
        return f"{base}.{lang}.srt"
    return f"{base}.srt"


def _trigger_metadata_scan(paths: list[str]) -> None:
    """
    Kick off a targeted metadata scan for the provided paths so Stash will register
    newly created caption files without needing a full library rescan.
    """
    if not paths:
        return

    mutation = """
    mutation ScanCaptions($input: ScanMetadataInput!) {
      metadataScan(input: $input)
    }
    """
    variables = {
        "input": {
            "paths": paths,
            "rescan": True,
            "scanGenerateCovers": False,
            "scanGeneratePreviews": False,
            "scanGenerateImagePreviews": False,
            "scanGenerateSprites": False,
            "scanGeneratePhashes": False,
            "scanGenerateThumbnails": False,
            "scanGenerateClipPreviews": False,
        }
    }

    try:
        resp = stash._graphql(mutation, variables)
        job_id = None
        if isinstance(resp, dict):
            data = resp.get("data") or {}
            job_id = data.get("metadataScan")
        if job_id:
            stash.Log(f"Triggered caption metadata scan (job {job_id}) for: {paths}")
        else:
            stash.Warn(f"Metadata scan triggered for captions but no job id returned. Paths={paths}")
    except Exception as e:
        stash.Warn(f"Failed to start metadata scan for captions: {e}")


# ----------------------------------------------------------------------
# SRT post-processing — emulate WhisperJAV's cleanup: strip non-lexical
# markers, drop repetition loops, and cap over-long lingering captions.
# ----------------------------------------------------------------------
_SRT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)
# Non-lexical markers like (moans), [music], （あえぎ）, 【BGM】
_MARKER_RE = re.compile(r"[\(\[（【][^\)\]）】]*[\)\]）】]")


def _srt_time_to_ms(h, m, s, ms) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def _ms_to_srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_srt(srt_text: str) -> list:
    cues = []
    for block in re.split(r"\n\s*\n", srt_text.strip()):
        lines = block.splitlines()
        time_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if time_idx is None:
            continue
        mt = _SRT_TIME_RE.search(lines[time_idx])
        if not mt:
            continue
        start = _srt_time_to_ms(*mt.group(1, 2, 3, 4))
        end = _srt_time_to_ms(*mt.group(5, 6, 7, 8))
        text = "\n".join(lines[time_idx + 1:]).strip()
        cues.append([start, end, text])
    return cues


def _postprocess_srt(srt_text: str, max_caption_seconds: float | None = None, dedupe_repeats: bool = False, strip_markers: bool = False) -> str:
    """Return a cleaned SRT. No-op if nothing is enabled or parsing fails."""
    if not srt_text or not (dedupe_repeats or strip_markers or max_caption_seconds):
        return srt_text
    cues = _parse_srt(srt_text)
    if not cues:
        return srt_text

    if dedupe_repeats or strip_markers:
        processed = []
        for start, end, text in cues:
            t = _MARKER_RE.sub("", text) if strip_markers else text  # markers kept unless strip is on
            t = re.sub(r"\s+", " ", t).strip()    # normalise whitespace
            if not t:
                continue                          # drop marker-only / empty cues
            # Collapse consecutive duplicate lines but keep the FIRST cue's timing
            # (do NOT extend the end) so repeated lines disappear instead of lingering.
            if dedupe_repeats and processed and processed[-1][2] == t:
                continue
            processed.append([start, end, t])
        cues = processed

    if max_caption_seconds and max_caption_seconds > 0:
        cap = int(max_caption_seconds * 1000)
        for cue in cues:
            if cue[1] - cue[0] > cap:
                cue[1] = cue[0] + cap

    return "\n".join(
        f"{i}\n{_ms_to_srt_time(s)} --> {_ms_to_srt_time(e)}\n{t}\n"
        for i, (s, e, t) in enumerate(cues, 1)
    )


def transcribe_video(video_path: str, translate: bool = False, server_url: str = "http://127.0.0.1:9191/inference", caption_language: str | None = None, extra_fields: dict | None = None, max_caption_seconds: float | None = None, dedupe_repeats: bool = False, strip_markers: bool = False) -> str:
    """
    Transcribes a video file using a whisper.cpp server. Produces an .srt next to the video
    and returns the caption path.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found at '{video_path}'")

    # Verify server reachability before doing any work.
    _check_whisper_server(server_url, timeout)

    tmp_wav_path = None
    try:
        # 1. Extract audio to 16kHz mono WAV using ffmpeg
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav_file:
            tmp_wav_path = tmp_wav_file.name

        command = [
            "ffmpeg",
            "-i", video_path,
            "-ar", "16000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            "-y",
            "-loglevel", "error",
            tmp_wav_path,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, errors="ignore")
        except FileNotFoundError:
            raise RuntimeError("'ffmpeg' not found. Please ensure it is installed and in PATH.")
        except subprocess.CalledProcessError as e:
            stderr = getattr(e, "stderr", "") or ""
            raise RuntimeError(f"ffmpeg failed to extract audio: {stderr}") from e

        # 2. Send audio to whisper.cpp server
        response_text = _post_whisper_audio(tmp_wav_path, server_url, translate, extra_fields)

    finally:
        # Clean up temporary WAV file
        if tmp_wav_path and os.path.exists(tmp_wav_path):
            try:
                os.remove(tmp_wav_path)
            except Exception:
                pass

    # 3. Clean up the SRT (dedupe loops, strip markers, cap long cues) then save.
    _cues_before = response_text.count("-->")
    response_text = _postprocess_srt(response_text, max_caption_seconds, dedupe_repeats, strip_markers)
    _cues_after = response_text.count("-->")
    try:
        stash.Log(f"[WhisperTranscribeJAV] post-process: cues {_cues_before}->{_cues_after} "
                  f"(max_caption={max_caption_seconds}, dedupe={dedupe_repeats}, strip={strip_markers})")
    except Exception:
        pass
    srt_path = _build_caption_path(video_path, caption_language)
    try:
        with open(srt_path, "w", encoding="utf-8") as srt_file:
            srt_file.write(response_text)
    except OSError as e:
        raise RuntimeError(f"Failed to write SRT file '{srt_path}': {e}") from e
    return srt_path
# ----------------------------------------------------------------------
# Minimal plugin settings – can be extended via a settings file if needed.
# ----------------------------------------------------------------------
settings = {
    # Defaults here are overridden by saved plugin settings from the UI.
    # The server URL is resolved dynamically; we do not set a hard‑coded default here
    # to avoid overriding a user‑provided UI setting. Fallbacks are handled in
    # _resolve_server_url() (environment variable, then built‑in default).
    "translateToEnglish": False,
    "zzdebugTracing": False,
    "zzdryRun": False,
}
# Load optional configuration (currently empty, but kept for parity with RenameFile)
try:
    from whisper_transcribe_settings import config  # type: ignore
except Exception:
    config = {}

stash = StashPluginHelper(
    settings=settings,
    config=config,
    maxbytes=10 * 1024 * 1024,
)

# The minimal fallback helper cannot read UI settings (only defaults), so fetch the
# real saved settings via GraphQL once, and read every setting through get_setting().
_ui_settings = _fetch_plugin_settings(stash.JSON_INPUT or {})
if not _ui_settings:
    stash.Warn("Could not read saved plugin settings via GraphQL; falling back to defaults. "
               "Check that the plugin is configured and the server connection is valid.")


def get_setting(key, default):
    """Read a plugin setting: prefer the GraphQL-fetched UI value, then the helper, then default."""
    if isinstance(_ui_settings, dict) and key in _ui_settings:
        v = _ui_settings.get(key)
        if v is not None and not (isinstance(v, str) and v.strip() == ""):
            return v
    return stash.Setting(key, default)

# ----------------------------------------------------------------------
# Resolve the Whisper server URL – this logic works for every Stash payload
# format (dict settings, list of {key,value}, or ``pluginSettings``).
# ----------------------------------------------------------------------
def _resolve_server_url() -> str:
    """
    Resolve the Whisper server URL using the Stash helper's Setting() method,
    which already knows how to read UI settings in all supported formats.
    Precedence (high → low):
        1. Explicit ``serverUrl`` argument passed in ``args``.
        2. UI‑saved setting (dict, list of {key,value}, or ``pluginSettings``).
        3. Direct extraction from raw JSON payload (covers cases where the helper
           does not correctly read the UI settings).
        4. Fallback to GraphQL fetch of plugin settings (for edge cases).
        5. Environment variable ``WHISPER_SERVER_URL``.
        6. Built‑in default.
    """
    # 1️⃣ explicit arg
    arg_url = ((stash.JSON_INPUT or {}).get("args") or {}).get("serverUrl")
    if isinstance(arg_url, str) and arg_url.strip():
        return arg_url.strip()

    # 2️⃣ UI‑saved setting via the helper (covers dict, list, pluginSettings)
    ui_url = stash.Setting("serverUrl", None)
    if isinstance(ui_url, str) and ui_url.strip():
        return ui_url.strip()

    # 3️⃣ Direct extraction from raw JSON payload (fallback if helper fails)
    raw_url = None
    if isinstance(stash.JSON_INPUT, dict):
        # Check top‑level 'settings' (dict or list)
        settings_src = stash.JSON_INPUT.get("settings") or {}
        if isinstance(settings_src, dict):
            raw_url = settings_src.get("serverUrl")
        elif isinstance(settings_src, list):
            for item in settings_src:
                if isinstance(item, dict) and item.get("key") == "serverUrl":
                    raw_url = item.get("value")
                    break

        # If not found, check 'pluginSettings' (dict or list)
        if not raw_url:
            alt_src = stash.JSON_INPUT.get("pluginSettings") or {}
            if isinstance(alt_src, dict):
                raw_url = alt_src.get("serverUrl")
            elif isinstance(alt_src, list):
                for item in alt_src:
                    if isinstance(item, dict) and item.get("key") == "serverUrl":
                        raw_url = item.get("value")
                        break

    if isinstance(raw_url, str) and raw_url.strip():
        return raw_url.strip()

    # 4️⃣ Fallback to GraphQL fetch of plugin settings (covers cases where
    #    the helper cannot read the UI settings directly)
    fetched_url = _fetch_server_url_from_settings(stash.JSON_INPUT or {})
    if isinstance(fetched_url, str) and fetched_url.strip():
        return fetched_url.strip()

    # 5️⃣ environment variable
    env_url = os.getenv("WHISPER_SERVER_URL")
    if isinstance(env_url, str) and env_url.strip():
        return env_url.strip()

    # 6️⃣ built‑in default
    return "http://127.0.0.1:9191/inference"

# Resolve once at import time (the value is immutable for the lifetime of the run)
server_url = _resolve_server_url()

translate_to_english = get_setting("translateToEnglish", False)
dry_run = get_setting("zzdryRun", False)
# New timeout setting (seconds) – defaults to 3600 seconds if not configured.
timeout = get_setting("timeout", 3600.0)
try:
    timeout = float(timeout)
    if timeout <= 0:
        timeout = 3600.0
except (TypeError, ValueError):
    timeout = 3600.0

# Additional whisper.cpp inference parameters exposed via UI settings.
language = get_setting("language", "")
temperature = get_setting("temperature", None)
max_context = get_setting("maxContext", None)
suppress_non_speech_tokens = get_setting("suppressNonSpeechTokens", False)
initial_prompt = get_setting("initialPrompt", "")

# SRT post-processing settings (WhisperJAV-style cleanup).
dedupe_repeats = get_setting("cleanSubtitles", False)            # collapse repeated lines
strip_markers = get_setting("stripNonSpeechMarkers", False)      # strip (moans)/[music] — off keeps them
_raw_max_caption = get_setting("maxCaptionSeconds", None)
try:
    max_caption_seconds = float(_raw_max_caption) if _raw_max_caption is not None and str(_raw_max_caption).strip() != "" else None
except (TypeError, ValueError):
    max_caption_seconds = None


def _build_extra_whisper_fields() -> dict:
    """
    Build the optional multipart form fields sent to the whisper.cpp server,
    derived from the UI settings. Only includes a field when the user set it,
    so unset values fall back to the server's own defaults.
    """
    fields: dict = {}
    if isinstance(language, str) and language.strip():
        fields["language"] = language.strip()
    else:
        # Force auto-detect explicitly so the server's CLI default (e.g. "-l ja")
        # cannot silently apply when the Language setting is left blank.
        fields["language"] = "auto"
    if temperature is not None and str(temperature).strip() != "":
        try:
            fields["temperature"] = str(float(temperature))
        except (TypeError, ValueError):
            pass
    # max_context: send even when 0 (0 disables context carry-over -> stops repetition loops).
    if max_context is not None and str(max_context).strip() != "":
        try:
            fields["max_context"] = str(int(float(max_context)))
        except (TypeError, ValueError):
            pass
    if suppress_non_speech_tokens:
        # Note: native whisper.cpp server does not support this field; it is sent
        # for compatibility with OpenAI/faster-whisper style servers that do.
        fields["suppress_non_speech_tokens"] = "true"
    if isinstance(initial_prompt, str) and initial_prompt.strip():
        # whisper.cpp expects "prompt"; OpenAI-style servers use "initial_prompt".
        # whisper.cpp ignores unknown fields, so sending both is safe.
        fields["prompt"] = initial_prompt.strip()
        fields["initial_prompt"] = initial_prompt.strip()
    return fields


extra_whisper_fields = _build_extra_whisper_fields()

# Concise, always-on summary of the effective configuration (Info level).
try:
    stash.Log(
        "Config: "
        f"server={server_url} "
        f"language={extra_whisper_fields.get('language')} "
        f"translate={'true' if translate_to_english else 'false'} "
        f"maxCaptionSeconds={max_caption_seconds} "
        f"dedupe={dedupe_repeats} stripMarkers={strip_markers} "
        f"maxContext={extra_whisper_fields.get('max_context', 'server-default')} "
        f"dryRun={dry_run}"
    )
except Exception:
    pass

# Optional verbose debug trace
try:
    if get_setting("zzdebugTracing", False):
        stash.Trace(f"Resolved serverUrl={server_url!r}")
        stash.Trace(f"Extra whisper fields={extra_whisper_fields!r}")
        stash.Trace(f"Raw UI settings keys={sorted(_ui_settings.keys()) if isinstance(_ui_settings, dict) else None}")
except Exception:
    pass

# Detect if invoked by a Scene.Update.Post hook and capture scene ID if provided
inputToUpdateScenePost = False
hookSceneID = None
try:
    hook_ctx = stash.JSON_INPUT.get("args", {}).get("hookContext") if stash.JSON_INPUT else None
    if hook_ctx is not None:
        # When input is None, treat as no-op. Otherwise mark as hook trigger.
        if hook_ctx.get("input") is not None:
            inputToUpdateScenePost = True
            if hook_ctx.get("id") is not None:
                hookSceneID = int(hook_ctx.get("id"))
except Exception:
    # best-effort only
    pass

# ----------------------------------------------------------------------
# Helper: transcribe a single scene's primary video file.
# ----------------------------------------------------------------------
def transcribe_scene(scene_id: int):
    """Fetch the scene's video file and run transcription."""
    try:
        # Minimal fragment to get the needed fields.
        fragment = """
            id title files { id path }
        """
        scene = stash.find_scene(scene_id, fragment)
        if not scene:
            stash.Error(f"Scene {scene_id} not found.")
            return

        if not scene.get("files"):
            stash.Warn(f"Scene {scene_id} has no associated files.")
            return

        video_path = scene["files"][0]["path"]
        if not os.path.isfile(video_path):
            stash.Warn(f"Video file does not exist: {video_path}")
            return


        # Call the shared transcription helper.
        # Use configured translate/server_url settings, and support dry-run.
        caption_language = "en" if translate_to_english else None
        caption_path = _build_caption_path(video_path, caption_language)
        if dry_run:
            stash.Log(f"Dry-run: would transcribe '{video_path}' -> '{caption_path}' (translate={translate_to_english}, server_url={server_url})")
        else:
            caption_path = transcribe_video(
                video_path,
                translate=translate_to_english,
                server_url=server_url,
                caption_language=caption_language,
                extra_fields=extra_whisper_fields,
                max_caption_seconds=max_caption_seconds,
                dedupe_repeats=dedupe_repeats,
                strip_markers=strip_markers,
            )
            # Scan for the caption only AFTER it has been written successfully, so a
            # failed transcription doesn't trigger a scan for a non-existent file.
            try:
                _trigger_metadata_scan([caption_path])
            except Exception as e:
                stash.Warn(f"Failed to start metadata scan for captions on scene {scene_id}: {e}")

        stash.Log(f"Transcription completed for scene {scene_id} (file: {video_path})")
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"Exception in transcribe_scene: {e}\nTraceBack={tb}")

# ----------------------------------------------------------------------
# Task: transcribe the most recently updated scene.
# ----------------------------------------------------------------------
def transcribe_last_scene():
    """Find the latest updated scene and run transcription on it."""
    try:
        all_scenes = stash.get_all_scenes()["allScenes"]
        if not all_scenes:
            stash.Error("No scenes found.")
            return

        latest = max(all_scenes, key=lambda s: s["updated_at"])
        scene_id = latest.get("id")
        if scene_id is None:
            stash.Error("Latest scene has no ID.")
            return

        transcribe_scene(scene_id)
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"Exception in transcribe_last_scene: {e}\nTraceBack={tb}")

def transcribe_scene_task():
    """
    Entry point used by the UI button.
    Expects a `scene_id` argument in the plugin's JSON input.
    """
    try:
        scene_id = stash.JSON_INPUT.get("args", {}).get("scene_id")
        if scene_id is None:
            stash.Error("No scene_id supplied to transcribe_scene_task")
            return
        scene_id = int(scene_id)
        transcribe_scene(scene_id)
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"Exception in transcribe_scene_task: {e}\nTraceBack={tb}")

# ----------------------------------------------------------------------
# Main entry point – mirrors the pattern used by RenameFile.
# ----------------------------------------------------------------------
try:
    if stash.PLUGIN_TASK_NAME == "transcribe_last_scene":
        stash.Trace(f"PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}")
        transcribe_last_scene()
    elif stash.PLUGIN_TASK_NAME == "transcribe_scene_task":
        stash.Trace(f"PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}")
        transcribe_scene_task()
    elif stash.JSON_INPUT and stash.JSON_INPUT.get("args", {}).get("mode") == "transcribe_scene_task":
        stash.Trace("Dispatch via args.mode=transcribe_scene_task")
        transcribe_scene_task()
    elif 'inputToUpdateScenePost' in globals() and inputToUpdateScenePost:
        stash.Trace("Triggered by Scene.Update.Post hook")
        if 'hookSceneID' in globals() and hookSceneID is not None:
            transcribe_scene(hookSceneID)
        else:
            # Fallback to latest scene if no id in hook context
            transcribe_last_scene()
    else:
        stash.Trace(f"No task specified (PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}). Nothing to do.")
except Exception as e:
    tb = traceback.format_exc()
    stash.Error(f"Exception while running plugin: {e}\nTraceBack={tb}")

stash.Trace("Exiting WhisperTranscribeJAV plugin")
