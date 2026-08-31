import json
import math
import os
import os.path
import re
import shutil
import socket
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from functools import partial
from os import path
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams
from app.services import bgm as bgm_service
from app.services import (
    elevenlabs_music,
    llm,
    material,
    quality_baseline,
    render_quality,
    scheduled_job_notifications,
    sonilo,
    subtitle,
    twelvelabs,
    video,
    visual_pacing,
    voice,
    upload_post,
)
from app.services import state as sm
from app.utils import file_security, utils


# 发布请求最长可等待数分钟，不能继续占用视频生成任务的并发名额。
# 固定大小的线程池将发布吞吐限制在可控范围内，同时让视频产物生成后
# 立即进入完成状态。
_cross_post_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="mpt-cross-post",
)
_cross_post_max_pending_tasks = max(
    1,
    int(config.app.get("upload_post_max_pending_tasks", 10)),
)
_cross_post_slots = threading.BoundedSemaphore(_cross_post_max_pending_tasks)
_cross_post_registry_lock = threading.RLock()
_cross_post_futures: dict[str, Future] = {}
_cross_post_process_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
_ACTIVE_CROSS_POST_STATES = {
    const.CROSS_POST_STATE_PENDING,
    const.CROSS_POST_STATE_PROCESSING,
}
_CROSS_POST_STATE_WRITE_ATTEMPTS = 3
_CROSS_POST_STATE_RETRY_DELAY_SECONDS = 0.1
_INTERRUPTED_CROSS_POST_ERROR = (
    "cross-posting was interrupted before the process completed"
)
# 视频配乐服务只需实现 ``is_enabled`` 和 ``generate_bgm``。供应商差异集中在
# 文件扩展名、领域异常和 WebUI 警告代码；任务编排、0 音量短路及失败降级
# 全部复用同一路径，避免后续新增供应商时维护多份相似流程。
_VIDEO_MUSIC_PROVIDERS = {
    "sonilo": {
        "service": sonilo,
        "error_type": sonilo.SoniloError,
        "suffix": ".m4a",
        "warning_code": "sonilo_bgm_failed",
        "display_name": "Sonilo",
    },
    "elevenlabs": {
        "service": elevenlabs_music,
        "error_type": elevenlabs_music.ElevenLabsMusicError,
        "suffix": ".mp3",
        "warning_code": "elevenlabs_bgm_failed",
        "display_name": "ElevenLabs",
    },
}


def _get_video_music_prompt(params: VideoParams) -> str:
    """
    读取当前视频配乐供应商实际使用的提示词。

    新任务统一使用供应商无关字段；旧 Sonilo CLI 参数和历史任务仍可能只有
    ``sonilo_bgm_prompt``，因此仅在 Sonilo 通用字段为空时读取旧字段。
    """
    prompt = str(params.video_music_prompt or "").strip()
    if params.bgm_type == "sonilo" and not prompt:
        prompt = str(params.sonilo_bgm_prompt or "").strip()
    return prompt


def is_task_busy(task: dict | None) -> bool:
    """判断任务是否仍在生成或发布，供所有删除入口复用。"""
    if not task:
        return False

    state = task.get("state")
    try:
        state = int(state)
    except (TypeError, ValueError):
        pass

    # 视频生成和跨平台发布都可能继续读取任务目录。统一视为忙碌状态，
    # 可以避免 API 与 WebUI 分别维护规则后出现一个允许删除、另一个禁止
    # 删除的不一致行为。
    return (
        state == const.TASK_STATE_PROCESSING
        or task.get("cross_post_state") in _ACTIVE_CROSS_POST_STATES
    )


def _register_cross_post_future(task_id: str, future: Future) -> None:
    """登记当前进程持有的发布 Future，供启动恢复和测试判断真实运行状态。"""
    with _cross_post_registry_lock:
        _cross_post_futures[task_id] = future


def _unregister_cross_post_future(task_id: str, future: Future | None = None) -> None:
    """仅移除匹配的 Future，避免旧回调误删同任务后续注册的新工作。"""
    with _cross_post_registry_lock:
        current = _cross_post_futures.get(task_id)
        if current is None or (future is not None and current is not future):
            return
        _cross_post_futures.pop(task_id, None)


def _is_cross_post_active_in_process(task_id: str) -> bool:
    """判断当前进程是否仍持有未结束的发布任务。"""
    with _cross_post_registry_lock:
        future = _cross_post_futures.get(task_id)
        return future is not None and not future.done()


def _is_windows_process_alive(process_id: int) -> bool:
    """通过只读 Win32 API 判断进程状态，避免用 os.kill 误终止进程。"""
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # ctypes 默认把未声明的返回值当作 32 位 int。Windows 64 位进程句柄可能
    # 因此被截断，必须显式声明 Win32 函数签名后再调用。
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code == error_invalid_parameter:
            return False
        if error_code == error_access_denied:
            # 进程存在但当前用户无查询权限时，必须保守地视为存活，避免错误
            # 回收其它账户正在执行的发布任务。
            return True
        logger.warning(
            "failed to open cross-post owner process on Windows, "
            f"process_id: {process_id}, error_code: {error_code}"
        )
        return True

    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            error_code = ctypes.get_last_error()
            logger.warning(
                "failed to read cross-post owner process state on Windows, "
                f"process_id: {process_id}, error_code: {error_code}"
            )
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _is_cross_post_owner_alive(owner: str | None) -> bool:
    """判断持久化发布任务的本机进程是否仍存在。"""
    if not owner:
        return False

    try:
        hostname, process_id_text, _ = owner.split(":", 2)
        process_id = int(process_id_text)
    except (TypeError, ValueError):
        logger.warning(f"invalid cross-post owner metadata: {owner}")
        return False

    # 无法可靠探测其它主机上的进程。共享 Redis 的多主机部署中必须保守地
    # 视为仍在运行，避免当前节点误删另一节点正在读取的视频文件。
    if hostname != socket.gethostname():
        return True

    # 当前进程内是否仍有真实发布工作，已经由 Future 注册表准确判断。运行到
    # 这里说明注册表中没有对应 Future，即使 owner 与当前进程完全一致，也应
    # 视为已中断；这可以覆盖终态写入持续失败、Future 已结束的场景。
    if process_id == os.getpid():
        return False

    # Windows 的 os.kill(pid, 0) 与 POSIX 语义不同，可能直接终止目标进程。
    # 使用只申请查询权限的 Win32 API，不向目标进程发送任何信号。
    if os.name == "nt":
        return _is_windows_process_alive(process_id)

    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        logger.warning(
            f"failed to inspect cross-post owner process, owner: {owner}, error: {exc}"
        )
        return True
    return True


def _mark_task_failed(task_id: str, stage: str, error: str) -> dict:
    """记录结构化失败信息，并保留任务失败前已经到达的进度。"""
    existing_task = None
    try:
        existing_task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.warning(f"failed to read task state before failure update: {exc}")

    # 具体服务函数通常比编排层拥有更准确的错误原因。后续的空结果检查
    # 不能再用通用文案覆盖它，否则 API 调用方仍然只能看到模糊信息。
    if (
        existing_task
        and existing_task.get("state") == const.TASK_STATE_FAILED
        and existing_task.get("error")
    ):
        return existing_task

    message = str(error or "unknown task error").strip()
    progress = int((existing_task or {}).get("progress", 0) or 0)
    logger.error(
        f"task failed, task_id: {task_id}, stage: {stage}, error: {message}"
    )
    failure = {
        "task_id": task_id,
        "state": const.TASK_STATE_FAILED,
        "progress": progress,
        "failed_stage": stage,
        "error": message,
    }
    sm.state.update_task(
        task_id,
        state=failure["state"],
        progress=failure["progress"],
        failed_stage=failure["failed_stage"],
        error=failure["error"],
    )
    return failure


def generate_script(task_id, params, fallback_providers=None):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    if not video_script:
        script_kwargs = {
            "video_subject": params.video_subject,
            "language": params.video_language,
            "paragraph_number": params.paragraph_number,
            "video_script_prompt": params.video_script_prompt,
            "custom_system_prompt": params.custom_system_prompt,
        }
        if fallback_providers:
            script_kwargs["fallback_providers"] = fallback_providers
        video_script = llm.generate_script(**script_kwargs)
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video script.")
        return None

    return video_script


def generate_terms(task_id, params, video_script, fallback_providers=None):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    smart_scene_queries = bool(getattr(params, "smart_scene_queries", False))
    ordered_materials = bool(
        params.match_materials_to_script or smart_scene_queries
    )
    if not video_terms:
        # 开启素材按文案顺序匹配后，关键词本身也必须按脚本叙事顺序生成；
        # 否则后续即使顺序下载和顺序拼接，也只能复用一组全局主题词，
        # 无法改善“后面内容的画面提前出现”的问题。
        amount = 8 if ordered_materials else 5
        if smart_scene_queries:
            scene_query_kwargs = {
                "video_subject": params.video_subject,
                "video_script": video_script,
                "amount": amount,
                "language": params.video_language or "",
            }
            if fallback_providers:
                scene_query_kwargs["fallback_providers"] = fallback_providers
            video_terms = llm.generate_scene_queries(
                **scene_query_kwargs,
            )
        if not video_terms:
            term_kwargs = {
                "video_subject": params.video_subject,
                "video_script": video_script,
                "amount": amount,
                "match_script_order": ordered_materials,
            }
            if fallback_providers:
                term_kwargs["fallback_providers"] = fallback_providers
            video_terms = llm.generate_terms(**term_kwargs)
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video terms.")
        return None

    # 可选的 TwelveLabs Marengo 语义重排：未启用时返回原顺序，无任何副作用。
    # 顺序匹配模式下关键词顺序本身就是脚本叙事顺序，必须保持原样，故跳过。
    if not ordered_materials:
        video_terms = twelvelabs.rerank_terms_by_subject(
            video_subject=params.video_subject,
            search_terms=video_terms,
        )

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


def _load_resume_checkpoint(task_id: str):
    """Load only the safe, already-generated inputs needed to resume a task."""
    try:
        script_file = file_security.resolve_path_within_directory(
            utils.task_dir(),
            path.join(str(task_id), "script.json"),
        )
    except (TypeError, ValueError):
        return None

    try:
        with open(script_file, encoding="utf-8") as file:
            saved_data = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    if not isinstance(saved_data, dict):
        return None
    video_script = saved_data.get("script")
    params_data = saved_data.get("params")
    if not isinstance(video_script, str) or not video_script.strip():
        return None
    if not isinstance(params_data, dict):
        return None

    try:
        params = VideoParams.model_validate(params_data)
    except (TypeError, ValueError):
        return None

    video_terms = saved_data.get("search_terms")
    if params.video_source == "local":
        return params, video_script, []
    if not isinstance(video_terms, list) or not all(
        isinstance(term, str) and term.strip() for term in video_terms
    ):
        return None
    return params, video_script, video_terms


def _load_resume_media(task_directory: str, params: VideoParams):
    """Return existing local narration/subtitles only when they are usable."""
    audio_file = ""
    audio_duration = None
    for filename in ("audio.normalized.wav", "audio.mp3"):
        candidate = path.join(task_directory, filename)
        try:
            if not path.isfile(candidate) or path.getsize(candidate) <= 0:
                continue
            duration = math.ceil(float(voice.get_audio_duration(candidate) or 0))
        except (OSError, TypeError, ValueError, OverflowError):
            continue
        if duration > 0:
            audio_file = candidate
            audio_duration = duration
            break

    if not audio_file or not getattr(params, "subtitle_enabled", True):
        return audio_file, audio_duration, ""

    subtitle_filenames = (
        ("subtitle.ass", "subtitle.srt")
        if getattr(params, "subtitle_style", "classic") == "karaoke"
        else ("subtitle.srt", "subtitle.ass")
    )
    for filename in subtitle_filenames:
        candidate = path.join(task_directory, filename)
        try:
            if path.isfile(candidate) and path.getsize(candidate) > 0:
                return audio_file, audio_duration, candidate
        except OSError:
            continue
    return audio_file, audio_duration, ""


def resume_interrupted_task(task_id: str):
    """Manually resume an interrupted task without regenerating its LLM output.

    Resumed tasks always queue any upload for review. They never restore an
    interrupted task automatically at application startup.
    """
    saved_task = sm.state.get_task(task_id)
    if not isinstance(saved_task, dict) or not saved_task.get("interrupted"):
        logger.warning("task is not an interrupted task that can be resumed")
        return None

    checkpoint = _load_resume_checkpoint(task_id)
    if checkpoint is None:
        logger.warning("interrupted task has no valid saved script checkpoint")
        return None

    params, video_script, video_terms = checkpoint
    try:
        script_file = file_security.resolve_path_within_directory(
            utils.task_dir(),
            path.join(str(task_id), "script.json"),
        )
    except (TypeError, ValueError):
        logger.warning("interrupted task checkpoint is no longer available")
        return None
    task_directory = path.dirname(script_file)
    resume_audio_file, resume_audio_duration, resume_subtitle_path = (
        _load_resume_media(task_directory, params)
    )
    logger.info("resuming interrupted task from its saved script checkpoint")
    return start(
        task_id=task_id,
        params=params,
        stop_at="video",
        require_upload_review=True,
        resume_video_script=video_script,
        resume_video_terms=video_terms,
        resume_audio_file=resume_audio_file or None,
        resume_audio_duration=resume_audio_duration,
        resume_subtitle_path=resume_subtitle_path or None,
    )


def resolve_custom_audio_file(task_id: str, custom_audio_file: str | None) -> str:
    requested_file = (custom_audio_file or "").strip()
    if not requested_file:
        return ""

    task_dir = utils.task_dir(task_id)
    try:
        return file_security.resolve_path_within_directory(
            task_dir,
            requested_file,
        )
    except ValueError as exc:
        task_dir_error = exc

    server_audio_file = path.realpath(
        requested_file
        if path.isabs(requested_file)
        else path.join(utils.root_dir(), requested_file)
    )
    if not path.isabs(requested_file):
        project_root = path.realpath(utils.root_dir())
        try:
            if path.commonpath([project_root, server_audio_file]) != project_root:
                raise ValueError(
                    "relative custom audio paths must stay within the project directory"
                )
        except ValueError as exc:
            raise ValueError(
                "custom audio file must be task-local or an existing server-side file"
            ) from exc

    if not path.isfile(server_audio_file):
        raise ValueError(
            "custom audio file does not exist or is not a file"
        ) from task_dir_error

    return server_audio_file


def preflight_custom_audio(task_id: str, params) -> dict:
    """Check a manually supplied narration before paid generation starts."""
    requested_file = getattr(params, "custom_audio_file", None)
    if not str(requested_file or "").strip():
        return {
            "selected": False,
            "ready": True,
            "duration": None,
            "reason": None,
        }

    try:
        audio_file = resolve_custom_audio_file(task_id, requested_file)
    except ValueError:
        return {
            "selected": True,
            "ready": False,
            "duration": None,
            "reason": "invalid_file",
        }

    try:
        duration = voice.get_audio_duration(audio_file)
    except Exception:
        return {
            "selected": True,
            "ready": False,
            "duration": None,
            "reason": "unreadable_duration",
        }

    if not duration or duration <= 0:
        return {
            "selected": True,
            "ready": False,
            "duration": None,
            "reason": "unreadable_duration",
        }
    return {
        "selected": True,
        "ready": True,
        "duration": duration,
        "reason": None,
    }


def _resolve_reusable_voice_preview(
    task_id: str,
    params,
    video_script: str,
    voice_preview: dict | None,
) -> tuple[str, float, object] | None:
    """
    校验并解析 WebUI 提交的完整试听缓存。

    该载荷不是公开 API 参数，只能来自当前进程的 WebUI。即便如此，后台任务
    仍重新核对文案和全部配音参数，并限制音频位于当前任务目录；任何不一致都
    回退普通 TTS，不让过期试听污染正式成片。
    """
    if not voice_preview:
        return None

    expected_values = {
        "script": str(video_script or "").strip(),
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
    }
    if not math.isclose(float(params.voice_volume), 1.0) or any(
        voice_preview.get(key) != value for key, value in expected_values.items()
    ):
        logger.info(
            f"skip stale voice preview cache, task_id: {task_id}, "
            "reason: voice parameters changed"
        )
        return None

    preview_file = path.realpath(str(voice_preview.get("audio_file") or ""))
    task_root = path.realpath(utils.task_dir(task_id))
    try:
        preview_is_task_local = path.commonpath([task_root, preview_file]) == task_root
    except ValueError:
        preview_is_task_local = False

    duration = voice_preview.get("duration")
    sub_maker = voice_preview.get("sub_maker")
    if (
        not preview_is_task_local
        or not path.isfile(preview_file)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
        or sub_maker is None
    ):
        logger.warning(
            f"skip invalid voice preview cache, task_id: {task_id}, "
            f"audio_file: {preview_file or '<empty>'}"
        )
        return None

    logger.info(
        f"using full voice preview audio, task_id: {task_id}, duration: {duration:.2f}s"
    )
    return preview_file, math.ceil(duration), sub_maker


def generate_audio(task_id, params, video_script, voice_preview=None):
    '''
    Generate audio for the video script.
    If a custom audio file is provided, it will be used directly.
    There will be no subtitle maker object returned in this case.
    Otherwise, TTS will be used to generate the audio.
    Returns:
        - audio_file: path to the generated or provided audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object if TTS is used, None otherwise
    '''
    logger.info("\n\n## generating audio")
    # /audio 和 /subtitle 请求模型不包含 custom_audio_file，
    # 这里统一做兼容读取，避免直调接口时抛属性错误。
    requested_custom_audio_file = getattr(params, "custom_audio_file", None)
    try:
        custom_audio_file = resolve_custom_audio_file(
            task_id, requested_custom_audio_file
        )
    except ValueError as exc:
        logger.error(
            "custom audio file is invalid, "
            f"task_id: {task_id}, path: {requested_custom_audio_file}, error: {str(exc)}"
        )
        _mark_task_failed(
            task_id,
            "audio",
            f"invalid custom audio file: {exc}",
        )
        return None, None, None

    if not custom_audio_file:
        reusable_preview = _resolve_reusable_voice_preview(
            task_id,
            params,
            video_script,
            voice_preview,
        )
        if reusable_preview:
            return reusable_preview

        logger.info("no custom audio file provided, using TTS to generate audio.")
        audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
        tts_audio_file = audio_file
        voice_name = voice.parse_voice_name(params.voice_name)
        sub_maker = voice.tts(
            text=video_script,
            voice_name=voice_name,
            voice_rate=params.voice_rate,
            voice_file=audio_file,
        )
        if sub_maker is None:
            _mark_task_failed(
                task_id,
                "audio",
                "failed to synthesize audio; verify the selected voice and "
                "TTS connectivity",
            )
            logger.error(
                """failed to generate audio:
1. check if the language of the voice matches the language of the video script.
2. check if the network is available. If you are in China, it is recommended to use a VPN and enable the global traffic mode.
            """.strip()
            )
            return None, None, None
        if (
            config.app.get("audio_loudness_normalization_enabled", False)
            and not voice.is_no_voice(voice_name)
        ):
            audio_file = voice.normalize_narration_loudness(
                audio_file,
                output_path=path.join(utils.task_dir(task_id), "audio.normalized.wav"),
            )
        audio_duration_source = (
            audio_file if audio_file != tts_audio_file else sub_maker
        )
        audio_duration = math.ceil(voice.get_audio_duration(audio_duration_source))
        if audio_duration == 0:
            _mark_task_failed(task_id, "audio", "generated audio duration is zero")
            logger.error("failed to get audio duration.")
            return None, None, None
        return audio_file, audio_duration, sub_maker
    else:
        logger.info(f"using custom audio file: {custom_audio_file}")
        audio_file = custom_audio_file
        if config.app.get("audio_loudness_normalization_enabled", False):
            audio_file = voice.normalize_narration_loudness(
                audio_file,
                output_path=path.join(utils.task_dir(task_id), "audio.normalized.wav"),
            )
        audio_duration = voice.get_audio_duration(audio_file)
        if audio_duration == 0:
            _mark_task_failed(task_id, "audio", "custom audio duration is zero")
            logger.error("failed to get audio duration from custom audio file.")
            return None, None, None
        return audio_file, audio_duration, None

def _write_subtitle_suspicion_report(task_id, params, subtitle_path, video_script):
    report = subtitle.build_subtitle_suspicion_report(
        subtitle_file=subtitle_path,
        video_script=video_script,
        language=getattr(params, "video_language", None),
    )
    if not report or not report["subtitle_count"]:
        return False

    report_path = path.join(utils.task_dir(task_id), "subtitle.review.json")
    if subtitle.write_subtitle_suspicion_report(report, report_path):
        logger.info(f"subtitle suspicion report created: {report_path}")
        return True
    return False


def _create_script_timed_subtitle_for_custom_audio(
    audio_file, video_script, subtitle_file
) -> bool:
    """Keep captions available when Whisper cannot transcribe user-supplied audio."""
    try:
        audio_duration = float(voice.get_audio_duration(audio_file) or 0)
    except (OSError, TypeError, ValueError):
        audio_duration = 0

    if audio_duration <= 0:
        logger.warning(
            "custom-audio subtitle fallback skipped because audio duration is unavailable"
        )
        return False

    try:
        script_sub_maker = voice.ensure_legacy_submaker_fields(voice.SubMaker())
        script_sub_maker = voice.populate_legacy_submaker_with_full_text(
            script_sub_maker,
            video_script,
            audio_duration,
        )
        voice.create_subtitle(
            sub_maker=script_sub_maker,
            text=video_script,
            subtitle_file=subtitle_file,
        )
    except Exception as error:
        logger.warning(
            f"custom-audio script-timed subtitle fallback failed: {error}"
        )
        return False

    return bool(subtitle.file_to_subtitles(subtitle_file))


def _karaoke_ass_style_options(params) -> dict:
    return {
        "font_name": getattr(params, "font_name", None),
        "font_size": getattr(params, "font_size", None),
        "text_fore_color": getattr(params, "text_fore_color", None),
        "stroke_color": getattr(params, "stroke_color", None),
        "stroke_width": getattr(params, "stroke_width", None),
        "subtitle_position": getattr(params, "subtitle_position", None),
        "custom_position": getattr(params, "custom_position", None),
    }


def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    '''
    Generate subtitle for the video script.
    If subtitle generation is disabled or the selected provider cannot work without
    a subtitle maker, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    '''
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled:
        return ""

    task_directory = utils.task_dir(task_id)
    subtitle_path = path.join(task_directory, "subtitle.srt")
    subtitle_baseline_path = path.join(task_directory, "subtitle.generated.srt")
    subtitle_corrections_path = path.join(task_directory, "subtitle.corrections.json")
    captured_manual_corrections = subtitle.capture_manual_subtitle_corrections(
        subtitle_path,
        subtitle_baseline_path,
        subtitle_corrections_path,
    )
    if captured_manual_corrections:
        logger.info(
            f"captured {captured_manual_corrections} manual subtitle correction(s)"
        )
    selected_subtitle_path = subtitle_path
    subtitle_review_written = False
    subtitle_style = getattr(params, "subtitle_style", "classic")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    word_timing_file = ""
    edge_karaoke_subtitle_created = False
    used_script_timed_fallback = False
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    if sub_maker is None and subtitle_provider != "whisper":
        custom_audio_subtitle_provider = (
            config.app.get("custom_audio_subtitle_provider", "whisper")
            .strip()
            .lower()
        )
        if custom_audio_subtitle_provider in {"whisper", "auto"}:
            logger.warning(
                "subtitle maker is missing for provider "
                f"{subtitle_provider}; using whisper for custom audio subtitles"
            )
            subtitle_provider = "whisper"
        else:
            logger.warning(
                "subtitle maker is missing, skip subtitle generation for provider: "
                f"{subtitle_provider}"
            )
            return ""

    subtitle_fallback = False
    if subtitle_provider == "edge":
        if subtitle_style == "karaoke":
            created = voice.create_karaoke_subtitle(
                text=video_script,
                sub_maker=sub_maker,
                subtitle_file=subtitle_path,
            )
            if created:
                edge_karaoke_subtitle_created = True
            else:
                logger.warning(
                    "karaoke subtitle unavailable, fallback to classic subtitle"
                )
                voice.create_subtitle(
                    text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
                )
        else:
            voice.create_subtitle(
                text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
            )
        if not os.path.exists(subtitle_path):
            subtitle_fallback = True
            logger.warning("subtitle file not found, fallback to whisper")

    if subtitle_provider == "whisper" or subtitle_fallback:
        whisper_create_kwargs = {
            "audio_file": audio_file,
            "subtitle_file": subtitle_path,
        }
        if subtitle_style == "karaoke":
            word_timing_file = path.join(
                utils.task_dir(task_id), "subtitle.words.json"
            )
            whisper_create_kwargs["word_timing_file"] = word_timing_file
        language = getattr(params, "video_language", None)
        if language:
            whisper_create_kwargs["language"] = language
        subtitle.create(**whisper_create_kwargs)
        if sub_maker is None and not subtitle.file_to_subtitles(subtitle_path):
            used_script_timed_fallback = _create_script_timed_subtitle_for_custom_audio(
                audio_file=audio_file,
                video_script=video_script,
                subtitle_file=subtitle_path,
            )
            if used_script_timed_fallback:
                logger.warning(
                    "Whisper subtitle generation was unavailable; using script-timed "
                    "captions for custom audio"
                )
        subtitle_review_written = _write_subtitle_suspicion_report(
            task_id,
            params,
            subtitle_path,
            video_script,
        )
        if not used_script_timed_fallback:
            logger.info("\n\n## correcting subtitle")
            subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    if not subtitle_review_written:
        _write_subtitle_suspicion_report(
            task_id,
            params,
            subtitle_path,
            video_script,
        )

    subtitle.save_subtitle_generated_baseline(subtitle_path, subtitle_baseline_path)
    restored_manual_corrections = subtitle.apply_manual_subtitle_corrections(
        subtitle_path,
        subtitle_corrections_path,
    )
    if restored_manual_corrections:
        logger.info(
            f"restored {restored_manual_corrections} manual subtitle correction(s)"
        )

    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    word_timings = []
    if subtitle_style == "karaoke" and (
        subtitle_provider == "whisper" or subtitle_fallback
    ):
        word_timings = subtitle.read_word_timings(word_timing_file)

    render_subtitle_lines = voice.reflow_subtitle_items(
        subtitle_lines,
        word_timings=word_timings,
    )
    if render_subtitle_lines != subtitle_lines:
        render_subtitle_path = path.join(task_directory, "subtitle.render.srt")
        if subtitle.write_subtitle_items(render_subtitle_path, render_subtitle_lines):
            selected_subtitle_path = render_subtitle_path
            logger.info("created compact render subtitle variant")
        else:
            render_subtitle_lines = subtitle_lines

    if subtitle_style == "karaoke":
        ass_subtitle_path = path.join(task_directory, "subtitle.ass")
        ass_style_options = _karaoke_ass_style_options(params)
        if subtitle_provider == "whisper" or subtitle_fallback:
            ass_created = voice.create_karaoke_ass_from_word_timings(
                subtitle_items=render_subtitle_lines,
                word_timings=word_timings,
                subtitle_file=ass_subtitle_path,
                video_aspect=getattr(params, "video_aspect", None),
                style_options=ass_style_options,
            )
            if not ass_created:
                ass_created = voice.create_karaoke_ass_subtitle(
                    sub_maker=voice.SubMaker(),
                    subtitle_file=ass_subtitle_path,
                    video_aspect=getattr(params, "video_aspect", None),
                    text=video_script,
                    subtitle_items=render_subtitle_lines,
                    style_options=ass_style_options,
                )
            if ass_created:
                selected_subtitle_path = ass_subtitle_path
        elif edge_karaoke_subtitle_created:
            ass_created = voice.create_karaoke_ass_subtitle(
                sub_maker=sub_maker,
                subtitle_file=ass_subtitle_path,
                video_aspect=getattr(params, "video_aspect", None),
                text=video_script,
                subtitle_items=render_subtitle_lines,
                style_options=ass_style_options,
            )
            if ass_created:
                selected_subtitle_path = ass_subtitle_path

    return selected_subtitle_path


def _normalize_cooldown_stats(cooldown_stats):
    if not cooldown_stats:
        return None
    moved_recent_count = int(cooldown_stats.get("moved_recent_count", 0) or 0)
    if moved_recent_count <= 0:
        return None
    try:
        days = max(1, int(cooldown_stats.get("days", 7) or 7))
    except (TypeError, ValueError):
        days = 7
    return {
        "moved_recent_count": moved_recent_count,
        "days": days,
    }


def _config_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _build_pending_uploads(video_paths, title, platforms):
    pending_uploads = []
    for video_path in video_paths:
        pending_uploads.append(
            {
                "video_path": video_path,
                "title": title,
                "platforms": list(platforms or []),
                "status": "pending",
            }
        )
    return pending_uploads


def _render_aspects(params):
    return list(
        dict.fromkeys(
            getattr(params, "video_aspects", None) or [params.video_aspect]
        )
    )


def _selected_online_video_materials(params):
    return [
        item
        for item in (getattr(params, "video_materials", None) or [])
        if getattr(item, "provider", "") != "local"
    ]


def _get_video_materials_for_aspects(
    task_id,
    params,
    video_terms,
    audio_duration,
    cooldown_stats=None,
    material_attributions=None,
):
    render_aspects = _render_aspects(params)
    if (
        len(render_aspects) <= 1
        or params.video_source == "local"
        or _selected_online_video_materials(params)
    ):
        return get_video_materials(
            task_id,
            params,
            video_terms,
            audio_duration,
            cooldown_stats=cooldown_stats,
            material_attributions=material_attributions,
        )

    materials_by_aspect = {}
    for video_aspect in render_aspects:
        aspect_materials = get_video_materials(
            task_id,
            params,
            video_terms,
            audio_duration,
            cooldown_stats=cooldown_stats,
            material_attributions=material_attributions,
            video_aspect=video_aspect,
            allow_empty=video_aspect != render_aspects[0],
        )
        if aspect_materials:
            materials_by_aspect[video_aspect.value] = aspect_materials
        else:
            logger.warning(
                "no materials found for aspect {}; it will use a compatible fallback",
                video_aspect.value,
            )

    if render_aspects[0].value not in materials_by_aspect:
        return []
    return materials_by_aspect


def get_video_materials(
    task_id,
    params,
    video_terms,
    audio_duration,
    cooldown_stats=None,
    material_attributions=None,
    video_aspect=None,
    allow_empty=False,
):
    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        materials = video.preprocess_video(
            materials=params.video_materials,
            clip_duration=params.video_clip_duration,
            video_aspect=video_aspect or params.video_aspect,
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error(
                "no valid materials found, please check the materials and try again."
            )
            return None
        for material_info in materials:
            material.append_material_attribution_record(
                material_attributions,
                material_info,
                material_info.url,
            )
        return [material_info.url for material_info in materials]
    selected_online_materials = _selected_online_video_materials(params)
    if selected_online_materials:
        logger.info("\n\n## downloading user-selected online materials")
        downloaded_videos = material.download_selected_videos(
            task_id=task_id,
            selected_items=selected_online_materials,
            audio_duration=audio_duration * params.video_count,
            max_clip_duration=params.video_clip_duration,
            attribution_records=material_attributions,
        )
        if not downloaded_videos:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("failed to download selected video materials.")
            return None
        return downloaded_videos
    else:
        logger.info(f"\n\n## downloading videos from {params.video_source}")
        ordered_materials = params.match_materials_to_script or bool(
            getattr(params, "smart_scene_queries", False)
        )
        requested_material_duration = audio_duration * params.video_count
        minimum_unique_visual_count = video.required_unique_material_count(
            requested_material_duration,
            params.video_clip_duration,
            params.video_transition_mode,
        )
        # 顺序匹配模式只在用户显式开启时生效。这里强制素材下载按关键词顺序
        # 轮询，避免某个早期关键词下载太多素材，把后续脚本主题挤出最终时间线。
        downloaded_videos = material.download_videos(
            task_id=task_id,
            search_terms=video_terms,
            source=params.video_source,
            video_aspect=video_aspect or params.video_aspect,
            video_concat_mode=(
                VideoConcatMode.sequential
                if ordered_materials
                else params.video_concat_mode
            ),
            audio_duration=requested_material_duration,
            max_clip_duration=params.video_clip_duration,
            match_script_order=ordered_materials,
            cooldown_stats=cooldown_stats,
            attribution_records=material_attributions,
            minimum_unique_visual_count=minimum_unique_visual_count,
        )
        if not downloaded_videos:
            if allow_empty:
                logger.warning(
                    "no materials found for optional aspect {}; preserving the primary render",
                    getattr(video_aspect, "value", video_aspect),
                )
            else:
                sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
                logger.error(
                    "failed to download videos, maybe the network is not available. if you are in China, please use a VPN."
                )
            return None
        return downloaded_videos


def _subtitle_cue_end_times(subtitle_path: str) -> list[float]:
    if not subtitle_path:
        return []
    candidate_path = subtitle_path
    if path.splitext(subtitle_path)[1].lower() == ".ass":
        candidate_path = f"{path.splitext(subtitle_path)[0]}.srt"

    cue_end_times = []
    for _, time_range, _ in subtitle.file_to_subtitles(candidate_path):
        timestamps = re.findall(r"(\d+):(\d{2}):(\d{2}),(\d{3})", time_range)
        if len(timestamps) < 2:
            continue
        hour, minute, second, millisecond = (int(part) for part in timestamps[-1])
        cue_end_time = hour * 3600 + minute * 60 + second + millisecond / 1000
        if cue_end_time > 0:
            cue_end_times.append(cue_end_time)
    return sorted(set(cue_end_times))


def _karaoke_subtitle_path_for_aspect(task_id, subtitle_path, video_aspect):
    if not subtitle_path or path.splitext(subtitle_path)[1].lower() != ".ass":
        return subtitle_path

    aspect_suffix = video_aspect.value.replace(":", "x")
    aspect_ass_path = path.join(
        utils.task_dir(task_id), f"subtitle-{aspect_suffix}.ass"
    )
    if not voice.create_karaoke_ass_variant(
        source_subtitle_file=subtitle_path,
        subtitle_file=aspect_ass_path,
        video_aspect=video_aspect,
    ):
        return subtitle_path

    source_srt_path = f"{path.splitext(subtitle_path)[0]}.srt"
    aspect_srt_path = f"{path.splitext(aspect_ass_path)[0]}.srt"
    if path.exists(source_srt_path):
        try:
            shutil.copyfile(source_srt_path, aspect_srt_path)
        except OSError:
            logger.warning(
                "failed to create karaoke subtitle fallback for aspect {}",
                video_aspect.value,
            )
            return subtitle_path
    return aspect_ass_path


def _materials_for_aspect(downloaded_videos, video_aspect):
    if not isinstance(downloaded_videos, dict):
        return downloaded_videos

    aspect_materials = downloaded_videos.get(video_aspect.value)
    if aspect_materials:
        return aspect_materials

    for fallback_aspect, fallback_materials in downloaded_videos.items():
        if fallback_materials:
            logger.warning(
                "materials for aspect {} were unavailable; using {} materials",
                video_aspect.value,
                fallback_aspect,
            )
            return fallback_materials
    return []


def generate_final_videos(
    task_id,
    params,
    downloaded_videos,
    audio_file,
    subtitle_path,
    audio_duration=None,
    encoder_results=None,
    failed_aspects=None,
    render_quality_reports=None,
    expected_audio_duration=None,
):
    final_video_paths = []
    combined_video_paths = []
    warnings = []
    video_music_provider = _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type)
    video_music_requested = (
        video_music_provider is not None
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    )
    if not isinstance(audio_duration, (int, float)) or audio_duration <= 0:
        audio_duration = expected_audio_duration
    if not isinstance(audio_duration, (int, float)) or audio_duration <= 0:
        audio_duration = float(voice.get_audio_duration(audio_file) or 0)
    outro_image_file = str(
        getattr(params, "outro_image_file", "") or ""
    ).strip()
    try:
        outro_duration = float(getattr(params, "outro_duration", 0) or 0)
    except (TypeError, ValueError):
        outro_duration = 0.0
    if not outro_image_file or not math.isfinite(outro_duration) or outro_duration <= 0:
        outro_duration = 0.0
    expected_video_duration = audio_duration + outro_duration
    render_aspects = _render_aspects(params)
    use_aspect_filenames = bool(params.video_aspects)
    render_count = params.video_count * len(render_aspects)
    cue_end_times = _subtitle_cue_end_times(subtitle_path)
    subtitle_paths_by_aspect = {}
    if (
        use_aspect_filenames
        and getattr(params, "subtitle_style", "classic") == "karaoke"
    ):
        subtitle_paths_by_aspect = {
            video_aspect.value: _karaoke_subtitle_path_for_aspect(
                task_id,
                subtitle_path,
                video_aspect,
            )
            for video_aspect in render_aspects
        }
    # 多视频生成默认会打散素材以增加差异；但“按文案顺序匹配素材”追求的是
    # 时间线稳定性和可解释性，所以开启后所有输出都使用顺序拼接。
    if params.match_materials_to_script or bool(
        getattr(params, "smart_scene_queries", False)
    ):
        video_concat_mode = VideoConcatMode.sequential
    elif params.video_count == 1:
        video_concat_mode = params.video_concat_mode
    else:
        video_concat_mode = VideoConcatMode.random
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        for video_aspect in render_aspects:
            aspect_videos = _materials_for_aspect(downloaded_videos, video_aspect)
            if not aspect_videos:
                logger.error("no usable materials available for aspect {}", video_aspect.value)
                if failed_aspects is not None and video_aspect.value not in failed_aspects:
                    failed_aspects.append(video_aspect.value)
                _progress += 50 / render_count
                sm.state.update_task(task_id, progress=_progress)
                continue
            render_subtitle_path = subtitle_paths_by_aspect.get(
                video_aspect.value, subtitle_path
            )
            aspect_suffix = (
                f"-{video_aspect.value.replace(':', 'x')}"
                if use_aspect_filenames
                else ""
            )
            combined_video_path = path.join(
                utils.task_dir(task_id), f"combined-{index}{aspect_suffix}.mp4"
            )
            logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
            try:
                video.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=aspect_videos,
                    audio_file=audio_file,
                    video_aspect=video_aspect,
                    video_concat_mode=video_concat_mode,
                    video_transition_mode=video_transition_mode,
                    max_clip_duration=params.video_clip_duration,
                    threads=params.n_threads,
                    cue_end_times=cue_end_times,
                    clip_speed=getattr(params, "video_clip_speed", 1.0),
                    outro_image_file=outro_image_file,
                    outro_duration=outro_duration,
                )
            except Exception:
                logger.exception(
                    "Video render preparation failed for aspect {}.",
                    video_aspect.value,
                )
                if failed_aspects is not None and video_aspect.value not in failed_aspects:
                    failed_aspects.append(video_aspect.value)
                _progress += 50 / render_count
                sm.state.update_task(task_id, progress=_progress)
                continue

            _progress += 50 / render_count / 2
            sm.state.update_task(task_id, progress=_progress)

            final_video_path = path.join(
                utils.task_dir(task_id), f"final-{index}{aspect_suffix}.mp4"
            )

            bgm_file_override = "" if video_music_provider else None
            if video_music_requested:
                service = video_music_provider["service"]
                display_name = video_music_provider["display_name"]
                warning_code = video_music_provider["warning_code"]
                generated_bgm_path = path.join(
                    utils.task_dir(task_id),
                    (
                        f"{params.bgm_type}-bgm-{index}{aspect_suffix}"
                        f"{video_music_provider['suffix']}"
                    ),
                )
                try:
                    service.generate_bgm(
                        video_path=combined_video_path,
                        output_path=generated_bgm_path,
                        video_duration=expected_video_duration,
                        prompt=_get_video_music_prompt(params),
                    )
                    bgm_file_override = generated_bgm_path
                except video_music_provider["error_type"] as exc:
                    logger.warning(
                        f"{display_name} BGM generation failed: task_id={task_id}, "
                        f"video_index={index}, aspect={video_aspect.value}, error={exc}"
                    )
                    bgm_file_override = ""
                    warning = {
                        "code": warning_code,
                        "video_index": index,
                    }
                    if use_aspect_filenames:
                        warning["video_aspect"] = video_aspect.value
                    warnings.append(warning)

            logger.info(f"\n\n## generating video: {index} => {final_video_path}")
            try:
                encoder_result = video.generate_video(
                    video_path=combined_video_path,
                    audio_path=audio_file,
                    subtitle_path=render_subtitle_path,
                    output_file=final_video_path,
                    params=params,
                    video_aspect=video_aspect,
                    return_encoder_result=encoder_results is not None,
                    bgm_file_override=bgm_file_override,
                )
            except Exception:
                logger.exception(
                    "Video render failed for aspect {}.", video_aspect.value
                )
                if failed_aspects is not None and video_aspect.value not in failed_aspects:
                    failed_aspects.append(video_aspect.value)
                _progress += 50 / render_count / 2
                sm.state.update_task(task_id, progress=_progress)
                continue
            bgm_mix_succeeded = (
                encoder_result.get("bgm_mix_succeeded", True)
                if isinstance(encoder_result, dict)
                else encoder_result is not False
            )
            if (
                video_music_provider is not None
                and bgm_file_override
                and not bgm_mix_succeeded
            ):
                warning = {
                    "code": video_music_provider["warning_code"],
                    "video_index": index,
                }
                if use_aspect_filenames:
                    warning["video_aspect"] = video_aspect.value
                warnings.append(warning)
            if encoder_results is not None and isinstance(encoder_result, dict):
                encoder_results.append(
                    {
                        "video_path": final_video_path,
                        "configured_codec": str(
                            encoder_result.get("configured_codec") or ""
                        ),
                        "used_codec": str(encoder_result.get("used_codec") or ""),
                        "fallback_used": bool(encoder_result.get("fallback_used")),
                    }
                )
            if render_quality_reports is not None:
                try:
                    quality_kwargs = {
                        "expected_aspect": video_aspect,
                        "expected_duration": expected_video_duration,
                        "expected_encoding": video.get_video_encoding_contract(),
                    }
                    if (
                        voice.is_no_voice(getattr(params, "voice_name", ""))
                        and not getattr(params, "custom_audio_file", None)
                    ):
                        quality_kwargs["allow_silent_audio"] = True
                    quality_report = render_quality.inspect_rendered_video(
                        final_video_path,
                        **quality_kwargs,
                    )
                except Exception:
                    logger.warning("rendered video quality inspection failed")
                    quality_report = {
                        "ok": False,
                        "warnings": ["rendered video could not be inspected"],
                    }
                render_quality_reports.append(
                    {"video_path": final_video_path, **quality_report}
                )

            _progress += 50 / render_count / 2
            sm.state.update_task(task_id, progress=_progress)

            final_video_paths.append(final_video_path)
            combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths, warnings


def _patch_cross_post_state(task_id: str, **kwargs) -> bool | None:
    """安全更新发布字段；短暂状态后端故障时有限重试。"""
    for attempt in range(1, _CROSS_POST_STATE_WRITE_ATTEMPTS + 1):
        try:
            return sm.state.patch_task(task_id, **kwargs)
        except Exception as exc:
            # Redis 短暂断连不应让任务永久停留在 pending/processing。发布状态
            # 写入频率很低，这里使用固定次数和短等待即可覆盖瞬时故障，同时
            # 避免后台线程无限阻塞。最后一次失败保留完整堆栈便于定位。
            if attempt >= _CROSS_POST_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    f"failed to update cross-post state after retries, "
                    f"task_id: {task_id}, fields: {', '.join(kwargs)}, "
                    f"attempts: {attempt}, error: {exc}"
                )
                return None

            logger.warning(
                f"retry cross-post state update, task_id: {task_id}, "
                f"fields: {', '.join(kwargs)}, attempt: {attempt}, error: {exc}"
            )
            time.sleep(_CROSS_POST_STATE_RETRY_DELAY_SECONDS)

    return None


def _record_cross_post_failure(
    task_id: str,
    error: Exception,
    results: list[dict] | None = None,
) -> None:
    """尽最大努力保存发布失败；状态后端不可用时由日志保留诊断信息。"""
    updated = _patch_cross_post_state(
        task_id,
        cross_post_state=const.CROSS_POST_STATE_FAILED,
        cross_post_results=results or None,
        cross_post_error=str(error),
        cross_post_owner=None,
    )
    if updated is False:
        logger.warning(f"discard cross-post failure for missing task: {task_id}")


def _ensure_cross_post_terminal_state(task_id: str) -> None:
    """Future 结束后把仍处于活动态的任务收敛为失败。"""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        # 此处已经是 Future 的最终回调，没有后续同步调用方可以处理异常。
        # 状态后端恢复后，下一次进程启动仍会通过恢复逻辑处理遗留状态。
        logger.exception(
            f"failed to verify final cross-post state, task_id: {task_id}, error: {exc}"
        )
        return

    if not task or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES:
        return

    logger.warning(
        f"cross-post worker ended without terminal state, task_id: {task_id}, "
        f"state: {task.get('cross_post_state')}"
    )
    _record_cross_post_failure(
        task_id,
        RuntimeError("cross-post worker ended without persisting a terminal state"),
        task.get("cross_post_results"),
    )


def recover_interrupted_cross_posts(page_size: int = 100) -> int | None:
    """
    将进程重启后无法恢复的发布任务标记为失败。

    跨平台发布使用当前进程内的线程池，不是持久化任务队列。进程启动时，
    Redis 中残留的 pending/processing 不会自动继续执行；如果继续把它们视为
    运行中，用户将永久无法删除任务。这里分页扫描状态，只处理当前进程没有
    对应 Future 的活动记录，并保留已经生成的视频结果。
    """
    recovered = 0
    page = 1

    while True:
        try:
            tasks, total = sm.state.get_all_tasks(page, page_size)
        except Exception as exc:
            logger.exception(f"failed to recover interrupted cross-post tasks: {exc}")
            return None

        for task in tasks:
            task_id = str(task.get("task_id") or "")
            if (
                not task_id
                or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES
                or _is_cross_post_active_in_process(task_id)
                or _is_cross_post_owner_alive(task.get("cross_post_owner"))
            ):
                continue

            updated = _patch_cross_post_state(
                task_id,
                cross_post_state=const.CROSS_POST_STATE_FAILED,
                cross_post_error=_INTERRUPTED_CROSS_POST_ERROR,
                cross_post_owner=None,
            )
            if updated is True:
                recovered += 1

        if page * page_size >= total or not tasks:
            break
        page += 1

    if recovered:
        logger.warning(f"recovered interrupted cross-post tasks: {recovered}")
    return recovered


def _run_cross_post(
    task_id: str,
    video_paths: tuple[str, ...],
    video_subject: str,
    video_script: str,
    video_language: str,
    platforms: tuple[str, ...],
    youtube_privacy_status: str,
    material_attributions: tuple[dict, ...] = (),
    fallback_providers: object = None,
) -> None:
    """后台执行跨平台发布，并只补充发布相关的任务字段。"""
    results = []
    try:
        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_PROCESSING,
            cross_post_error=None,
            cross_post_owner=_cross_post_process_owner,
        )
        if state_updated is not True:
            # False 表示任务已删除，None 表示状态后端暂时不可用。两种情况都
            # 不应继续调用第三方接口，否则用户无法查询或控制这次发布。
            if state_updated is False:
                logger.warning(f"skip cross-post for missing task: {task_id}")
            else:
                _record_cross_post_failure(
                    task_id,
                    RuntimeError("failed to persist cross-post processing state"),
                )
            return

        logger.info(
            f"cross-post started, task_id: {task_id}, platforms: {', '.join(platforms)}"
        )
        youtube_extra = None
        if any(platform.startswith("youtube") for platform in platforms):
            metadata_kwargs = {
                "video_subject": video_subject,
                "video_script": video_script,
                "language": video_language or "",
                "platform": "youtube_shorts",
            }
            if fallback_providers:
                metadata_kwargs["fallback_providers"] = fallback_providers
            metadata = llm.generate_social_metadata(**metadata_kwargs)
            youtube_extra = {
                "youtube_title": metadata.get("title", video_subject),
                "youtube_description": material.append_material_attributions(
                    metadata.get("caption", ""),
                    list(material_attributions),
                ),
                "tags": metadata.get("hashtags", []),
                "privacyStatus": youtube_privacy_status,
                "containsSyntheticMedia": True,
            }

        for video_path in video_paths:
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=video_subject or "Check out this video! #shorts #viral",
                platforms=list(platforms),
                youtube_extra=youtube_extra,
            )
            if not isinstance(result, dict):
                result = {
                    "success": False,
                    "error": "Upload-Post returned an invalid response",
                }
            results.append(result)

        failures = [result for result in results if not result.get("success")]
        if failures:
            error_messages = [
                str(
                    result.get("error")
                    or result.get("message")
                    or "unknown upload error"
                )
                for result in failures
            ]
            cross_post_state = const.CROSS_POST_STATE_FAILED
            cross_post_error = "; ".join(error_messages)
            logger.warning(
                f"cross-post completed with failures, task_id: {task_id}, "
                f"failed: {len(failures)}, total: {len(results)}"
            )
        else:
            cross_post_state = const.CROSS_POST_STATE_COMPLETE
            cross_post_error = None
            logger.success(
                f"cross-post completed, task_id: {task_id}, videos: {len(results)}"
            )

        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=cross_post_state,
            cross_post_results=results,
            cross_post_error=cross_post_error,
            cross_post_owner=None,
        )
        if state_updated is False:
            logger.warning(f"discard cross-post result for missing task: {task_id}")
        elif state_updated is None:
            # 上传已经结束但结果没有持久化时，不能继续保留 processing。
            # 失败状态写入会再次经过有限重试，至少让调用方得到明确终态。
            _record_cross_post_failure(
                task_id,
                RuntimeError("failed to persist final cross-post result"),
                results,
            )
    except Exception as exc:
        # 发布失败只影响发布状态，不能反向覆盖已经完成的视频任务。
        # 异常原文写入任务状态，API 调用方无需访问服务端日志也能定位问题。
        logger.exception(f"cross-post failed, task_id: {task_id}, error: {exc}")
        _record_cross_post_failure(task_id, exc, results)


def _run_cross_post_with_slot(*args) -> None:
    """执行发布任务，并确保成功、失败或异常时都会归还队列容量。"""
    try:
        _run_cross_post(*args)
    except Exception as exc:
        # _run_cross_post 已处理预期异常；这里是最后一道保护，避免未来新增
        # 逻辑抛出的异常只保存在无人读取的 Future 中。
        task_id = str(args[0]) if args else "unknown"
        logger.exception(
            f"cross-post worker crashed, task_id: {task_id}, error: {exc}"
        )
        if args:
            _record_cross_post_failure(task_id, exc)
    finally:
        _cross_post_slots.release()


def _finalize_cross_post_future(task_id: str, future: Future) -> None:
    """清理 Future 注册，并确保取消、异常和状态写入失败都能收敛。"""
    _unregister_cross_post_future(task_id, future)

    try:
        error = future.exception()
    except CancelledError:
        logger.warning(f"cross-post future was cancelled, task_id: {task_id}")
        # Future 在开始执行前被取消时，worker 的 finally 不会运行，因此需要
        # 在回调中归还队列容量，并把持久化状态改为失败。
        _cross_post_slots.release()
        _record_cross_post_failure(
            task_id,
            RuntimeError("cross-post job was cancelled before execution"),
        )
        return
    except Exception as exc:
        logger.exception(
            f"failed to inspect cross-post future, task_id: {task_id}, error: {exc}"
        )
        _ensure_cross_post_terminal_state(task_id)
        return

    if error is not None:
        logger.error(
            f"cross-post future failed, task_id: {task_id}, "
            f"error: {type(error).__name__}: {error}"
        )

    _ensure_cross_post_terminal_state(task_id)


def _schedule_cross_post(
    task_id: str,
    video_paths: list[str],
    params: VideoParams,
    video_script: str,
    platforms: list[str],
    youtube_privacy_status: str,
    material_attributions: list[dict] | None = None,
    fallback_providers: object = None,
) -> str | None:
    """提交后台发布任务；成功返回 None，调度失败返回可查询的错误原因。"""
    if not _cross_post_slots.acquire(blocking=False):
        error = "cross-post queue is full; publishing was skipped"
        logger.warning(
            f"skip cross-post because queue is full, task_id: {task_id}, "
            f"capacity: {_cross_post_max_pending_tasks}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=error,
            cross_post_owner=None,
        )
        return error

    try:
        future = _cross_post_executor.submit(
            _run_cross_post_with_slot,
            task_id,
            tuple(video_paths),
            params.video_subject or "",
            video_script,
            params.video_language or "",
            tuple(platforms),
            youtube_privacy_status,
            tuple(material_attributions or ()),
            fallback_providers,
        )
        _register_cross_post_future(task_id, future)
        future.add_done_callback(partial(_finalize_cross_post_future, task_id))
    except RuntimeError as exc:
        _unregister_cross_post_future(task_id)
        _cross_post_slots.release()
        logger.exception(
            f"failed to schedule cross-post, task_id: {task_id}, error: {exc}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=f"failed to schedule cross-post: {exc}",
            cross_post_owner=None,
        )
        return f"failed to schedule cross-post: {exc}"

    return None


_VIDEO_QUALITY_PARAM_FIELDS = (
    "video_codec",
    "video_crf",
    "video_encoder_preset",
    "video_fps",
    "audio_bitrate",
)


def _video_quality_config_from_params(params: VideoParams) -> dict[str, object]:
    return {
        field: value
        for field in _VIDEO_QUALITY_PARAM_FIELDS
        if (value := getattr(params, field, None)) is not None
    }


def start(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    require_upload_review: bool | None = None,
    fallback_providers: object = None,
    resume_video_script: str | None = None,
    resume_video_terms: list[str] | None = None,
    resume_audio_file: str | None = None,
    resume_audio_duration: int | None = None,
    resume_subtitle_path: str | None = None,
    voice_preview: dict | None = None,
):
    try:
        with video.video_quality_config(_video_quality_config_from_params(params)):
            return _start(
                task_id,
                params,
                stop_at,
                require_upload_review,
                fallback_providers,
                resume_video_script,
                resume_video_terms,
                resume_audio_file,
                resume_audio_duration,
                resume_subtitle_path,
                voice_preview,
            )
    except Exception as exc:
        logger.exception(
            f"unexpected task pipeline failure, task_id: {task_id}, "
            f"error_type: {type(exc).__name__}"
        )
        return _mark_task_failed(
            task_id,
            "pipeline",
            f"{type(exc).__name__}: unexpected task failure",
        )


def _refresh_automatic_render_quality_baseline(render_quality_reports) -> None:
    if not isinstance(render_quality_reports, list) or not render_quality_reports:
        return
    try:
        update = quality_baseline.refresh_automatic_render_quality_baseline(
            render_quality_reports
        )
    except Exception:
        logger.warning("Automatic render-quality baseline update failed.")
        return
    if not isinstance(update, dict):
        return
    summary = update.get("notification_summary")
    if summary:
        try:
            scheduled_job_notifications.notify_render_quality_attention(summary)
        except Exception:
            logger.warning("Automatic render-quality notification failed.")


def _start(
    task_id,
    params: VideoParams,
    stop_at: str = "video",
    require_upload_review: bool | None = None,
    fallback_providers: object = None,
    resume_video_script: str | None = None,
    resume_video_terms: list[str] | None = None,
    resume_audio_file: str | None = None,
    resume_audio_duration: int | None = None,
    resume_subtitle_path: str | None = None,
    voice_preview: dict | None = None,
):
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=5)

    video_music_provider = _VIDEO_MUSIC_PROVIDERS.get(params.bgm_type)
    video_music_enabled = (
        stop_at == "video"
        and video_music_provider is not None
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
    )
    if video_music_enabled:
        service = video_music_provider["service"]
        display_name = video_music_provider["display_name"]
        if not service.is_enabled():
            return _mark_task_failed(
                task_id,
                "preflight",
                f"{display_name} background music requires an API key",
            )

        music_prompt = _get_video_music_prompt(params)
        max_prompt_length = int(getattr(service, "MAX_PROMPT_LENGTH", 0) or 0)
        if max_prompt_length and len(music_prompt) > max_prompt_length:
            return _mark_task_failed(
                task_id,
                "preflight",
                f"{display_name} music prompt exceeds {max_prompt_length} characters",
            )

        validate_access = getattr(service, "validate_generation_access", None)
        if callable(validate_access):
            try:
                validate_access()
            except video_music_provider["error_type"] as exc:
                return _mark_task_failed(task_id, "preflight", str(exc))

    if stop_at not in {"script", "terms"}:
        custom_audio_preflight = preflight_custom_audio(task_id, params)
        if (
            custom_audio_preflight["selected"]
            and not custom_audio_preflight["ready"]
        ):
            logger.error(
                "custom audio preflight failed before script generation, "
                f"reason: {custom_audio_preflight['reason']}"
            )
            return _mark_task_failed(
                task_id,
                "audio",
                "invalid custom audio file",
            )

    # 1. Generate script
    if resume_video_script is not None:
        video_script = resume_video_script
        logger.info("reusing saved script from interrupted task")
    else:
        script_kwargs = (
            {"fallback_providers": fallback_providers} if fallback_providers else {}
        )
        video_script = generate_script(task_id, params, **script_kwargs)
    if not video_script or "Error: " in video_script:
        return _mark_task_failed(
            task_id,
            "script",
            "failed to generate video script",
        )

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=10)

    if stop_at == "script":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, script=video_script
        )
        return {"script": video_script}

    # 2. Generate terms
    video_terms = ""
    if resume_video_terms is not None:
        video_terms = resume_video_terms
        logger.info("reusing saved material terms from interrupted task")
    elif params.video_source != "local":
        term_kwargs = (
            {"fallback_providers": fallback_providers} if fallback_providers else {}
        )
        video_terms = generate_terms(task_id, params, video_script, **term_kwargs)
        if not video_terms:
            return _mark_task_failed(
                task_id,
                "terms",
                "failed to generate video search terms",
            )

    save_script_data(task_id, video_script, video_terms, params)

    if stop_at == "terms":
        sm.state.update_task(
            task_id, state=const.TASK_STATE_COMPLETE, progress=100, terms=video_terms
        )
        return {"script": video_script, "terms": video_terms}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    # 3. Generate audio
    if resume_audio_file and resume_audio_duration:
        audio_file = resume_audio_file
        audio_duration = resume_audio_duration
        sub_maker = None
        logger.info("reusing saved narration audio from interrupted task")
    else:
        audio_file, audio_duration, sub_maker = generate_audio(
            task_id,
            params,
            video_script,
            voice_preview=voice_preview,
        )
    if not audio_file:
        return _mark_task_failed(
            task_id,
            "audio",
            "failed to generate narration audio",
        )

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=30)

    if stop_at == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            audio_file=audio_file,
        )
        return {"audio_file": audio_file, "audio_duration": audio_duration}

    # 4. Generate subtitle
    if resume_subtitle_path:
        subtitle_path = resume_subtitle_path
        logger.info("reusing saved subtitle from interrupted task")
    else:
        subtitle_path = generate_subtitle(
            task_id, params, video_script, sub_maker, audio_file
        )

    if stop_at == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            subtitle_path=subtitle_path,
        )
        return {"subtitle_path": subtitle_path}

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=40)

    # 5. Get video materials
    cooldown_stats = {"moved_recent_count": 0, "days": config.app.get("video_cooldown_days", 7)}
    material_attributions = []
    downloaded_videos = _get_video_materials_for_aspects(
        task_id,
        params,
        video_terms,
        audio_duration,
        cooldown_stats=cooldown_stats,
        material_attributions=material_attributions,
    )
    if not downloaded_videos:
        return _mark_task_failed(
            task_id,
            "materials",
            "failed to obtain usable video materials",
        )

    if stop_at == "materials":
        if isinstance(downloaded_videos, dict):
            primary_aspect = _render_aspects(params)[0]
            result = {
                "materials": _materials_for_aspect(
                    downloaded_videos, primary_aspect
                ),
                "materials_by_aspect": downloaded_videos,
            }
        else:
            result = {"materials": downloaded_videos}
        cooldown = _normalize_cooldown_stats(cooldown_stats)
        if cooldown:
            result["cooldown"] = cooldown
        if material_attributions:
            result["material_attributions"] = material_attributions
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            **result,
        )
        return result

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=50)

    # 仅完整视频生成流程才需要处理视频拼接模式；
    # 这样可以避免 /subtitle 和 /audio 这类请求访问不存在的字段。
    if type(params.video_concat_mode) is str:
        params.video_concat_mode = VideoConcatMode(params.video_concat_mode)

    # 6. Generate final videos
    video_encoder_results = []
    render_quality_reports = []
    failed_aspects = []
    final_video_paths, combined_video_paths, generation_warnings = generate_final_videos(
        task_id,
        params,
        downloaded_videos,
        audio_file,
        subtitle_path,
        audio_duration=audio_duration,
        encoder_results=video_encoder_results,
        failed_aspects=failed_aspects,
        render_quality_reports=render_quality_reports,
    )

    if not final_video_paths:
        failure = _mark_task_failed(
            task_id,
            "video",
            "failed to generate final video",
        )
        if failed_aspects:
            sm.state.patch_task(
                task_id,
                failed_aspects=failed_aspects,
            )
            failure = {**failure, "failed_aspects": failed_aspects}
        return failure

    logger.success(
        f"task {task_id} finished, generated {len(final_video_paths)} videos."
    )

    # 7. Complete video generation first, then queue optional publishing.
    pending_uploads = []
    cross_post_enabled = (
        upload_post.upload_post_service.is_configured()
        and upload_post.upload_post_service.auto_upload
    )
    platforms = (
        list(upload_post.upload_post_service.platforms)
        if cross_post_enabled
        else []
    )
    require_review = (
        bool(require_upload_review)
        if require_upload_review is not None
        else _config_bool(
            config.app.get("upload_post_require_review", True),
            default=True,
        )
    )
    should_cross_post = cross_post_enabled and bool(platforms) and not require_review
    cross_post_state = (
        const.CROSS_POST_STATE_PENDING if should_cross_post else None
    )
    if cross_post_enabled and not platforms:
        logger.warning(
            f"skip cross-post because no platforms are configured, task_id: {task_id}"
        )
    elif cross_post_enabled and require_review:
        upload_title = params.video_subject or "Check out this video! #shorts #viral"
        pending_uploads = _build_pending_uploads(
            final_video_paths,
            upload_title,
            platforms,
        )
        logger.info(
            f"\n\n## queued {len(pending_uploads)} videos for upload review"
        )

    primary_materials = downloaded_videos
    if isinstance(downloaded_videos, dict):
        primary_materials = _materials_for_aspect(
            downloaded_videos, _render_aspects(params)[0]
        )

    pacing_cue_end_times = (
        _subtitle_cue_end_times(subtitle_path)
        if subtitle_path and path.isfile(subtitle_path)
        else []
    )
    visual_pacing_report = visual_pacing.build_visual_pacing_budget(
        audio_duration,
        params.video_clip_duration,
        scene_count=video_terms,
        cue_end_times=pacing_cue_end_times,
    )

    kwargs = {
        "videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": video_script,
        "terms": video_terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "visual_pacing": visual_pacing_report,
        "subtitle_path": subtitle_path,
        "materials": primary_materials,
        "material_attributions": material_attributions if material_attributions else None,
        "video_encoder_results": video_encoder_results if video_encoder_results else None,
        "render_quality_reports": render_quality_reports if render_quality_reports else None,
        "cross_post_state": cross_post_state,
        "cross_post_results": None,
        "cross_post_error": None,
        "cross_post_owner": _cross_post_process_owner if should_cross_post else None,
        "pending_uploads": pending_uploads if pending_uploads else None,
        "warnings": generation_warnings or None,
    }
    if isinstance(downloaded_videos, dict):
        kwargs["materials_by_aspect"] = downloaded_videos
    if failed_aspects:
        kwargs["partial_success"] = True
        kwargs["failed_aspects"] = failed_aspects
    cooldown = _normalize_cooldown_stats(cooldown_stats)
    if cooldown:
        kwargs["cooldown"] = cooldown
    sm.state.update_task(
        task_id, state=const.TASK_STATE_COMPLETE, progress=100, **kwargs
    )
    _refresh_automatic_render_quality_baseline(render_quality_reports)

    if should_cross_post:
        scheduling_error = _schedule_cross_post(
            task_id=task_id,
            video_paths=final_video_paths,
            params=params,
            video_script=video_script,
            platforms=platforms,
            youtube_privacy_status=(
                upload_post.upload_post_service.youtube_privacy_status
            ),
            material_attributions=material_attributions,
            fallback_providers=fallback_providers,
        )
        if scheduling_error:
            kwargs = {
                **kwargs,
                "cross_post_state": const.CROSS_POST_STATE_FAILED,
                "cross_post_error": scheduling_error,
                "cross_post_owner": None,
            }

    return kwargs


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
