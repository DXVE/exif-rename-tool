import os
import platform
import queue
import random
import re
import shutil
import string
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, scrolledtext

# 默认配置 —— GUI 启动时的初始值，用户可在界面中覆盖
DEFAULT_SOURCE_FOLDER = os.path.expanduser(r"~\Pictures\Screenshots")
DEFAULT_PREFIX = "screenshot"
DEFAULT_TEMPLATE = "{prefix}-{YYYY}-{MM}-{DD}-{hh}{mm}-{id}"
DEFAULT_RECURSIVE = False
DEFAULT_COPY_TO_NEW = False
DEFAULT_IMAGE_CHECKED = True
DEFAULT_RAW_CHECKED = False
DEFAULT_CUSTOM_FILE_CHECKED = False
DEFAULT_VIDEO_CHECKED = True
DEFAULT_AUDIO_CHECKED = False

DEFAULT_SETTINGS = {
    'source_folder': DEFAULT_SOURCE_FOLDER,
    'prefix': DEFAULT_PREFIX,
    'template': DEFAULT_TEMPLATE,
    'recursive': DEFAULT_RECURSIVE,
    'copy_to_new': DEFAULT_COPY_TO_NEW,
    'target_folder': '',
    'categories': {
        'image': DEFAULT_IMAGE_CHECKED,
        'raw': DEFAULT_RAW_CHECKED,
        'custom_file': DEFAULT_CUSTOM_FILE_CHECKED,
        'video': DEFAULT_VIDEO_CHECKED,
        'audio': DEFAULT_AUDIO_CHECKED,
    },
}

# 按媒体类型分组的文件扩展名集合，用于文件分类筛选
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.heic')
RAW_EXTS = ('.arw', '.cr2', '.cr3', '.dng', '.nef', '.nrw',
            '.orf', '.pef', '.raf', '.rw2', '.srw', '.x3f')
VIDEO_EXTS = ('.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv',
              '.m4v', '.mpg', '.mpeg')
AUDIO_EXTS = ('.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma')
CUSTOM_FILE_EXTS = ()

# 预计算扩展名到类型标签的查找表，避免遍历 O(n) 查询
EXT_TO_TYPE = {}
for ext in IMAGE_EXTS:
    EXT_TO_TYPE[ext] = "IMAGE"
for ext in RAW_EXTS:
    EXT_TO_TYPE[ext] = "RAW"
for ext in VIDEO_EXTS:
    EXT_TO_TYPE[ext] = "VIDEO"
for ext in AUDIO_EXTS:
    EXT_TO_TYPE[ext] = "AUDIO"
for ext in CUSTOM_FILE_EXTS:
    EXT_TO_TYPE[ext] = "FILE"

# 优先使用 exifread（支持 RAW/HEIC），回退到 PIL 的基础 EXIF 支持
try:
    import exifread
    EXIFREAD_AVAILABLE = True
    EXIFREAD_VERSION = getattr(exifread, '__version__', 'unknown')
except ImportError:
    EXIFREAD_AVAILABLE = False
    EXIFREAD_VERSION = None

# EXIF 日期常见编码格式清单（含带时区偏移的变体）
DATE_FORMATS = [
    "%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
    "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
    "%Y:%m:%d", "%Y-%m-%d", "%Y/%m/%d",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
]

# ===== 文件路径长度限制 =====
# Windows MAX_PATH = 260（含 null 终止符），有效字符串最大长度 = 259
# 可通过环境变量 RENAME_TOOL_MAX_PATH 覆盖
MAX_PATH_LIMIT = int(os.environ.get("RENAME_TOOL_MAX_PATH", "259"))

# 使用 threading.local 保证每个线程拥有独立 EXIF 缓存，天然线程安全
_exif_cache = threading.local()

# ===== 核心处理函数 =====

def parse_date_string(date_str: str) -> datetime:
    """解析 EXIF 日期字符串，支持多种常见格式，自动剥离末尾的时区偏移部分"""
    original = date_str
    date_str = str(date_str).strip()
    date_str = re.split(r'[+-]\d{2}:\d{2}', date_str)[0]
    date_str = date_str.replace('T', ' ')
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析日期字符串: {original}")


def parse_xmp_content(xmp_str: str) -> datetime | None:
    """从 XMP/XML 元数据文本中提取日期时间字段"""
    patterns = [
        r'xmp:CreateDate="([^"]+)"', r'xmp:DateCreated="([^"]+)"',
        r'xmp:MetadataDate="([^"]+)"',
        r'<xmp:CreateDate>([^<]+)</xmp:CreateDate>',
        r'<xmp:DateCreated>([^<]+)</xmp:DateCreated>',
        r'<xmp:MetadataDate>([^<]+)</xmp:MetadataDate>',
        r'photoshop:DateCreated="([^"]+)"',
        r'<photoshop:DateCreated>([^<]+)</photoshop:DateCreated>',
    ]
    for pat in patterns:
        m = re.search(pat, xmp_str)
        if m:
            try:
                return parse_date_string(m.group(1).strip())
            except ValueError:
                continue
    return None


def parse_xmp_sidecar(filepath: str, log_func=None) -> datetime | None:
    """读取同名的 .xmp 侧边文件（Adobe XMP sidecar），从中解析拍摄日期"""
    xmp_path = os.path.splitext(filepath)[0] + '.xmp'
    if not os.path.exists(xmp_path):
        return None
    if log_func:
        log_func(f"找到图片附属XMP文件: {xmp_path}")
    try:
        with open(xmp_path, 'r', encoding='utf-8', errors='ignore') as f:
            xmp_str = f.read()
        dt = parse_xmp_content(xmp_str)
        if dt and log_func:
            log_func(f"[XMP] 解析到日期 {dt}")
        return dt
    except Exception as e:
        if log_func:
            log_func(f"[XMP] 读取失败: {e}", "WARNING")
    return None


# ---- EXIF 值安全解码辅助函数 ----
def _decode_exif_value(val):
    """将 EXIF 值安全转为字符串，处理 bytes 类型"""
    if isinstance(val, bytes):
        try:
            return val.decode('utf-8', errors='replace').strip().strip('\x00')
        except Exception:
            return val.decode('latin-1', errors='replace').strip().strip('\x00')
    return str(val).strip()


def get_media_datetime(filepath: str, log_func=None) -> tuple[datetime, bool, str]:
    """获取媒体文件的拍摄时间，优先级：EXIF → XMP → 文件修改时间"""
    cache = _exif_cache.__dict__
    if filepath in cache:
        return cache[filepath]

    ext = os.path.splitext(filepath)[1].lower()
    image_types = set(IMAGE_EXTS + RAW_EXTS)

    if ext not in image_types:
        mtime = os.path.getmtime(filepath)
        result = (datetime.fromtimestamp(mtime), True, "File Modification Time")
        cache[filepath] = result
        return result

    if EXIFREAD_AVAILABLE:
        try:
            with open(filepath, 'rb') as f:
                tags = exifread.process_file(f, details=True)
            tag_priority = ['EXIF DateTimeOriginal', 'Image DateTime',
                            'EXIF DateTimeDigitized', 'EXIF DateTime',
                            'EXIF_DateTimeOriginal', 'Image_DateTime']
            for tag_name in tag_priority:
                if tag_name in tags:
                    try:
                        raw_value = str(tags[tag_name])
                        dt = parse_date_string(raw_value)
                        short_reason = tag_name.split()[-1] if ' ' in tag_name else tag_name
                        result = (dt, ('Original' not in tag_name), short_reason)
                        cache[filepath] = result
                        return result
                    except ValueError:
                        continue
            for xmp_tag in ('Image XMP', 'Image XML', 'XMP', 'XML'):
                if xmp_tag in tags:
                    dt = parse_xmp_content(str(tags[xmp_tag]))
                    if dt:
                        result = (dt, False, "XMP")
                        cache[filepath] = result
                        return result
            dt = parse_xmp_sidecar(filepath, log_func)
            if dt:
                result = (dt, False, "XMP sidecar")
                cache[filepath] = result
                return result
        except Exception as e:
            if log_func:
                log_func(f"[ExifRead] 异常: {e}", "WARNING")

    PIL_AVAILABLE = False
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        PIL_AVAILABLE = True
    except ImportError:
        pass

    if PIL_AVAILABLE:
        try:
            img = Image.open(filepath)
            try:
                exif = img.getexif()
                if exif:
                    pil_tags = {TAGS.get(tid, str(tid)): _decode_exif_value(val) for tid, val in exif.items()}
                    for tag_name in ('DateTimeOriginal', 'DateTime', 'DateTimeDigitized'):
                        if tag_name in pil_tags:
                            try:
                                raw_value = pil_tags[tag_name]
                                dt = parse_date_string(raw_value)
                                result = (dt, (tag_name != 'DateTimeOriginal'), f"PIL:{tag_name}")
                                cache[filepath] = result
                                return result
                            except ValueError:
                                continue
            finally:
                img.close()
        except Exception as e:
            if log_func:
                log_func(f"[PIL] 异常: {e}", "ERROR")

    mtime = os.path.getmtime(filepath)
    result = (datetime.fromtimestamp(mtime), True, "File Modification Time")
    cache[filepath] = result
    return result


def generate_unique_id(existing_ids: set[str]) -> str:
    """生成四位随机字母数字 ID，用于文件名去重"""
    chars = string.ascii_uppercase + string.digits
    max_attempts = 10000
    for _ in range(max_attempts):
        new_id = ''.join(random.choices(chars, k=4))
        if new_id not in existing_ids:
            return new_id
    raise RuntimeError("无法生成唯一ID：已用尽所有重试次数")


def apply_template(template: str, dt: datetime,
                   prefix: str, file_id: str, seq: int, filetype: str) -> str:
    """将模板中的占位符替换为实际值，生成文件名"""
    repl = {
        "{YYYY}": dt.strftime("%Y"),
        "{YY}": dt.strftime("%y"),
        "{MM}": dt.strftime("%m"),
        "{DD}": dt.strftime("%d"),
        "{hh}": dt.strftime("%H"),
        "{mm}": dt.strftime("%M"),
        "{ss}": dt.strftime("%S"),
        "{prefix}": prefix,
        "{filetype}": filetype,
        "{id}": file_id,
    }
    result = template
    for k, v in repl.items():
        result = result.replace(k, v)

    def replace_seq(match):
        full = match.group(0)
        if full == "{seq}":
            return str(seq)
        fmt_match = re.match(r"\{seq:(.+?)\}", full)
        if fmt_match:
            fmt = fmt_match.group(1)
            try:
                return format(seq, fmt)
            except ValueError:
                return str(seq)
        return full

    result = re.sub(r"\{seq(?::[^}]+)?\}", replace_seq, result)
    result = re.sub(r"\{[^{}]*\}", "", result)
    return result


def sanitize_filename(filename: str, max_name_len: int = 200) -> str:
    """清理文件名中的非法字符并限制长度"""
    for ch in ['\\', '/', ':', '*', '?', '"', '<', '>', '|']:
        filename = filename.replace(ch, '_')
    if len(filename) > max_name_len:
        filename = filename[:max_name_len]
    return filename


def get_filetype(ext: str) -> str:
    """根据扩展名返回媒体类型标签"""
    return EXT_TO_TYPE.get(ext, "")


# 系统级隐藏文件，遍历目录时自动跳过
DEFAULT_IGNORED_FILENAMES = {
    'desktop.ini',
    'thumbs.db',
    '.ds_store',
    'ehthumbs.db',
}


def _is_selected_category(ext: str, selected: dict[str, bool]) -> bool:
    """判断文件扩展名是否属于用户勾选的处理类型类别"""
    if ext in IMAGE_EXTS:
        return selected.get('image', False)
    if ext in RAW_EXTS:
        return selected.get('raw', False)
    if ext in CUSTOM_FILE_EXTS:
        return selected.get('custom_file', False)
    if ext in VIDEO_EXTS:
        return selected.get('video', False)
    if ext in AUDIO_EXTS:
        return selected.get('audio', False)
    return False


def collect_files_with_categories(folder: str, recursive: bool,
                                  selected: dict[str, bool],
                                  log_func=None,
                                  progress_callback=None) -> tuple[list[str], list[str], list[str]]:
    """
    遍历目录收集文件，按勾选的类型分类为媒体文件和忽略文件
    :param progress_callback: 可选回调，接收 (已扫描数, 已发现媒体数, 是否完成)
    """
    all_files = []
    media_files = []
    total_processed = 0
    if recursive:
        try:
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith('$')]
                try:
                    for f in files:
                        if f.lower() in DEFAULT_IGNORED_FILENAMES:
                            continue
                        if f.startswith('.') or f.startswith('._'):
                            continue
                        if f.startswith('$'):
                            continue
                        full = os.path.join(root, f)
                        all_files.append(full)
                        ext = os.path.splitext(f)[1].lower()
                        if _is_selected_category(ext, selected):
                            media_files.append(full)
                        total_processed += 1
                        if progress_callback and total_processed % 50 == 0:
                            progress_callback(len(all_files), len(media_files), final=False)
                except PermissionError:
                    if log_func:
                        log_func(f"权限不足，跳过目录: {root}", "WARNING")
        except PermissionError as e:
            if log_func:
                log_func(f"权限不足，无法遍历目录: {folder} - {e}", "WARNING")
    else:
        try:
            for f in os.listdir(folder):
                if f.lower() in DEFAULT_IGNORED_FILENAMES:
                    continue
                if f.startswith('.') or f.startswith('._'):
                    continue
                if f.startswith('$'):
                    continue
                full = os.path.join(folder, f)
                if os.path.isfile(full):
                    all_files.append(full)
                    ext = os.path.splitext(f)[1].lower()
                    if _is_selected_category(ext, selected):
                        media_files.append(full)
                    total_processed += 1
                    if progress_callback and total_processed % 50 == 0:
                        progress_callback(len(all_files), len(media_files), final=False)
        except PermissionError:
            if log_func:
                log_func(f"权限不足，无法列出目录: {folder}", "WARNING")
    ignored = sorted(set(all_files) - set(media_files))
    if progress_callback:
        progress_callback(len(all_files), len(media_files), final=True)
    return all_files, media_files, ignored


def safe_relpath(path: str, start: str) -> str:
    """安全的 os.path.relpath，跨驱动器时返回空字符串"""
    try:
        rel = os.path.relpath(path, start)
        return "" if rel in ('.', '') else rel
    except ValueError:
        return ""


def get_relative_dir(full_path: str, base_folder: str) -> str:
    return safe_relpath(os.path.dirname(full_path), base_folder)


def prepare_rename_plan(folder: str, all_files: list[str], media_files: list[str],
                        ignored_fullpaths: list[str],
                        template: str, prefix: str, copy_mode: bool, target_base: str | None,
                        log_func=None) -> dict:
    """分析所有媒体文件，生成重命名方案（含冲突检测和回退预览）"""
    plan = {}
    plan['total_files'] = len(all_files)
    plan['media_count'] = len(media_files)
    plan['ignored_count'] = len(ignored_fullpaths)
    plan['ignored_list'] = [safe_relpath(f, folder) for f in ignored_fullpaths]
    plan['ignored_fullpaths'] = ignored_fullpaths
    if log_func:
        log_func(f"找到 {len(media_files)} 个媒体文件，忽略 {len(ignored_fullpaths)} 个非媒体文件")

    if not media_files:
        plan['rename_map'] = []
        plan['fallback_files'] = []
        plan['fallback_previews'] = {}
        plan['failed_analysis'] = []
        return plan

    has_seq = bool(re.search(r"\{seq(?::[^}]+)?\}", template))

    file_infos = []
    fallback_files = []
    fallback_previews = {}
    failed_analysis = []

    for idx, filepath in enumerate(media_files, 1):
        if log_func:
            log_func(f"———————— 正在处理 ———————— [{idx}/{len(media_files)}]: {os.path.basename(filepath)}", "INFO")
        try:
            original_mtime = os.path.getmtime(filepath)
        except OSError as e:
            failed_analysis.append(filepath)
            if log_func:
                log_func(f"无法读取文件信息: {e}", "WARNING")
            continue

        ext = os.path.splitext(filepath)[1].lower()
        ext = '.jpg' if ext == '.jpeg' else ext
        filetype = get_filetype(ext)
        dt, is_fallback, reason = get_media_datetime(filepath, log_func)
        file_infos.append((filepath, dt, ext, filetype, is_fallback, reason, original_mtime))
        if is_fallback:
            fallback_files.append(filepath)
            preview_name = apply_template(template, dt, prefix, "A1B2", 1, filetype)
            preview_name = sanitize_filename(preview_name) + ext
            fallback_previews[filepath] = {'preview': preview_name, 'reason': reason}
            if log_func:
                short_reason = reason.split(':')[-1].strip() if ':' in reason else reason
                log_func(f"{os.path.basename(filepath)} 使用 {short_reason}", "WARNING")
                log_func(f"预览: {os.path.basename(filepath)} -> {preview_name}", "INFO")

    file_infos.sort(key=lambda x: (x[1], x[0]))
    plan['fallback_files'] = fallback_files
    plan['fallback_previews'] = fallback_previews

    rename_map = []
    used_target_paths = set()
    existing_ids = set()
    created_dirs = set()

    for (filepath, dt, ext, filetype, is_fallback, reason, original_mtime) in file_infos:
        if not os.path.exists(filepath):
            failed_analysis.append(filepath)
            if log_func:
                log_func(f"文件已不存在，跳过: {filepath}", "WARNING")
            continue
        current_mtime = os.path.getmtime(filepath)
        if current_mtime != original_mtime:
            failed_analysis.append(filepath)
            if log_func:
                log_func(f"文件修改时间已变化，跳过以保证数据一致: {filepath}", "WARNING")
            continue

        file_id = generate_unique_id(existing_ids)
        existing_ids.add(file_id)

        local_seq = 1
        max_retries = 10000
        conflict_suffix = 1

        while True:
            if has_seq:
                seq = local_seq
            else:
                seq = 1

            base = apply_template(template, dt, prefix, file_id, seq, filetype)
            base = sanitize_filename(base)

            if not has_seq and conflict_suffix > 1:
                new_filename = f"{base}_{conflict_suffix}{ext}"
            else:
                new_filename = base + ext

            if copy_mode:
                rel_dir = get_relative_dir(filepath, folder)
                if rel_dir:
                    target_dir = os.path.join(target_base, rel_dir)
                else:
                    target_dir = target_base
                if target_dir not in created_dirs:
                    os.makedirs(target_dir, exist_ok=True)
                    created_dirs.add(target_dir)
                new_path = os.path.join(target_dir, new_filename)
            else:
                new_path = os.path.join(os.path.dirname(filepath), new_filename)
                if new_path == filepath:
                    if log_func:
                        log_func(f"文件名已符合模板，无需更改: {filepath}", "INFO")
                    break

            if len(new_path) > MAX_PATH_LIMIT:
                if log_func:
                    log_func(f"新路径过长 ({len(new_path)} 字符)，跳过", "WARNING")
                failed_analysis.append(filepath)
                break

            if os.path.exists(new_path) or new_path in used_target_paths:
                if has_seq:
                    local_seq += 1
                    if local_seq > max_retries:
                        if log_func:
                            log_func(f"无法为 {filepath} 生成不冲突的序号（已达最大重试次数），跳过", "ERROR")
                        failed_analysis.append(filepath)
                        break
                    continue
                else:
                    conflict_suffix += 1
                    if conflict_suffix > max_retries:
                        if log_func:
                            log_func(f"无法为 {filepath} 生成不冲突的后缀（已达最大重试次数），跳过", "ERROR")
                        failed_analysis.append(filepath)
                        break
                    continue

            used_target_paths.add(new_path)
            rename_map.append((filepath, new_path))
            if has_seq:
                local_seq += 1
            else:
                conflict_suffix = 1
            break

    plan['rename_map'] = rename_map
    plan['failed_analysis'] = failed_analysis
    return plan


def execute_rename(rename_map: list[tuple[str, str]],
                   copy_mode: bool,
                   log_func=None) -> tuple[int, list[str]]:
    """执行重命名或复制操作，返回成功数量和失败列表"""
    success = 0
    failed = []
    for old, new in rename_map:
        try:
            if copy_mode:
                shutil.copy2(old, new)
                if log_func:
                    log_func(f"复制并重命名: {os.path.basename(old)} -> {os.path.basename(new)}")
            else:
                os.rename(old, new)
                if log_func:
                    log_func(f"重命名: {os.path.basename(old)} -> {os.path.basename(new)}")
            success += 1
        except Exception as e:
            if log_func:
                log_func(f"失败: {os.path.basename(old)} - {e}", "ERROR")
            failed.append(old)
    return success, failed


# ===== 工具函数：安全获取真实路径（处理 NTFS 挂载点/符号链接） =====
def _safe_realpath(path: str) -> str:
    """获取真实路径，处理 NTFS junction / mount point 映射场景。
       在非 Windows 平台或解析失败时回退到原路径。"""
    try:
        if platform.system() == "Windows":
            return os.path.realpath(path)
        return path
    except Exception:
        return path

# ===== GUI 自定义对话框 =====

class CustomDialogBase:
    """自定义对话框基类，提供公共样式和居中逻辑"""
    def __init__(self, parent, title, width, height):
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.geometry(f"{width}x{height}")
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)
        self.dialog.withdraw()
        self.parent = parent
        self.result = False

    def center(self):
        """将对话框居中于父窗口"""
        self.dialog.update_idletasks()
        x = self.parent.winfo_x() + (self.parent.winfo_width() // 2) - (self.dialog.winfo_width() // 2)
        y = self.parent.winfo_y() + (self.parent.winfo_height() // 2) - (self.dialog.winfo_height() // 2)
        self.dialog.geometry(f"+{x}+{y}")
        self.dialog.deiconify()

    def show(self):
        self.center()
        self.parent.wait_window(self.dialog)


class CustomConfirmDialog(CustomDialogBase):
    def __init__(self, parent, title, message, items=None, yes_text="是", no_text="否", width=600, height=400):
        super().__init__(parent, title, width, height)
        self.result = False

        self.dialog.grid_rowconfigure(0, weight=0)
        self.dialog.grid_rowconfigure(1, weight=1)
        self.dialog.grid_rowconfigure(2, weight=0)
        self.dialog.grid_columnconfigure(0, weight=1)

        msg_label = ttk.Label(self.dialog, text=message, wraplength=width - 40, justify=tk.LEFT)
        msg_label.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")

        if items:
            text_frame = ttk.Frame(self.dialog)
            text_frame.grid(row=1, column=0, padx=10, pady=5, sticky="nsew")
            text_frame.grid_rowconfigure(0, weight=1)
            text_frame.grid_columnconfigure(0, weight=1)
            text_widget = scrolledtext.ScrolledText(text_frame, wrap=tk.WORD, font=('Consolas', 9))
            text_widget.grid(row=0, column=0, sticky="nsew")
            for item in items:
                text_widget.insert(tk.END, item + "\n")
            text_widget.config(state=tk.DISABLED)
        else:
            placeholder = ttk.Frame(self.dialog)
            placeholder.grid(row=1, column=0, sticky="nsew")

        btn_frame = ttk.Frame(self.dialog)
        btn_frame.grid(row=2, column=0, pady=10)
        ttk.Button(btn_frame, text=yes_text, command=self._on_yes).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text=no_text, command=self._on_no).pack(side=tk.LEFT, padx=10)

        self.show()

    def _on_yes(self):
        self.result = True
        self.dialog.destroy()

    def _on_no(self):
        self.result = False
        self.dialog.destroy()


class CombinedWarningDialog(CustomDialogBase):
    def __init__(self, parent, total_files, ignored_files, fallback_files, fallback_previews, base_folder):
        super().__init__(parent, "文件处理警告(右键点击文件查看选项)", 1200, 600)
        self.base_folder = base_folder
        self.fallback_previews = fallback_previews
        self.ignored_path_map = {}
        self.fallback_path_map = {}
        self.result = False

        main_frame = ttk.Frame(self.dialog)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        msg = f"当前目录下共有 {total_files} 个文件。\n\n"
        if ignored_files:
            msg += f"⚠ 其中 {len(ignored_files)} 个不是支持的媒体格式，将不会被处理：\n"
        if fallback_files:
            msg += f"⚠ 其中 {len(fallback_files)} 个未能获取原始拍摄时间，将使用文件修改时间：\n"
        msg_label = ttk.Label(main_frame, text=msg, wraplength=1160, justify=tk.LEFT)
        msg_label.pack(anchor=tk.W, pady=(0, 10))

        paned = ttk.PanedWindow(main_frame, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        if ignored_files:
            left_frame = ttk.LabelFrame(paned, text=f"忽略的文件 ({len(ignored_files)})", padding=5)
            paned.add(left_frame, weight=1)
            self._build_listbox(left_frame, ignored_files, is_fallback=False)
        else:
            left_frame = ttk.Frame(paned)
            paned.add(left_frame, weight=1)
            ttk.Label(left_frame, text="（无忽略文件）").pack(expand=True)

        if fallback_files:
            right_frame = ttk.LabelFrame(paned, text=f"使用修改时间的文件 ({len(fallback_files)})", padding=5)
            paned.add(right_frame, weight=1)
            self._build_listbox(right_frame, fallback_files, is_fallback=True)
        else:
            right_frame = ttk.Frame(paned)
            paned.add(right_frame, weight=1)
            ttk.Label(right_frame, text="（无使用修改时间的文件）").pack(expand=True)

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="继续", command=self._on_yes).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="取消", command=self._on_no).pack(side=tk.LEFT, padx=10)

        self.show()

    def _build_listbox(self, parent, files, is_fallback):
        """构建文件列表控件并绑定右键菜单"""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        listbox = tk.Listbox(frame, yscrollcommand=scrollbar.set, font=('Consolas', 9))
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)

        listbox.is_fallback = is_fallback

        for f in files:
            if is_fallback:
                info = self.fallback_previews.get(f, {})
                preview = info.get('preview', '?')
                display = f"{os.path.basename(f)} -> {preview}"
                listbox.insert(tk.END, display)
                self.fallback_path_map[display] = f
            else:
                rel_path = os.path.relpath(f, self.base_folder)
                listbox.insert(tk.END, rel_path)
                self.ignored_path_map[rel_path] = f

        menu = tk.Menu(listbox, tearoff=0)
        menu.add_command(label="打开文件", command=lambda: self._open_selected_file(listbox))
        menu.add_command(label="打开所在文件夹并选中", command=lambda: self._open_directory_and_select(listbox))

        def show_context_menu(event):
            listbox.selection_clear(0, tk.END)
            listbox.selection_set(event.widget.nearest(event.y))
            menu.post(event.x_root, event.y_root)

        listbox.bind("<Button-3>", show_context_menu)

    def _get_selected_path(self, listbox):
        selection = listbox.curselection()
        if not selection:
            return None
        item_text = listbox.get(selection[0])
        if listbox.is_fallback:
            return self.fallback_path_map.get(item_text)
        else:
            return self.ignored_path_map.get(item_text)

    def _open_selected_file(self, listbox):
        filepath = self._get_selected_path(listbox)
        if filepath and os.path.exists(filepath):
            self._open_file(filepath)

    def _open_directory_and_select(self, listbox):
        """在资源管理器中打开并选中文件。

        关键设计决策：
        - explorer 的 /select 参数必须与路径合并为一个字符串（不能拆成两个参数）
        - 使用 realpath 避免 NTFS 挂载点造成的路径偏差
        """
        filepath = self._get_selected_path(listbox)
        if not filepath or not os.path.exists(filepath):
            return
        real_path = _safe_realpath(filepath)
        system = platform.system()
        try:
            if system == "Windows":
                subprocess.run(['explorer', f'/select,{real_path}'])
            elif system == "Darwin":
                subprocess.run(['open', '-R', real_path])
            else:
                self._open_directory(real_path)
        except Exception:
            self._open_directory(real_path)

    def _open_file(self, filepath):
        real_path = _safe_realpath(filepath)
        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(real_path)
            else:
                subprocess.Popen(['xdg-open', real_path])
        except Exception:
            pass

    def _open_directory(self, filepath):
        dirpath = os.path.dirname(filepath)
        real_dir = _safe_realpath(dirpath)
        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(real_dir)
            else:
                subprocess.Popen(['xdg-open', real_dir])
        except Exception:
            pass

    def _on_yes(self):
        self.result = True
        self.dialog.destroy()

    def _on_no(self):
        self.result = False
        self.dialog.destroy()


class ErrorDialog(CustomDialogBase):
    def __init__(self, parent, message, title="错误"):
        super().__init__(parent, title, 400, 200)
        main_frame = ttk.Frame(self.dialog, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(main_frame, text=message, wraplength=360, justify=tk.LEFT).pack(expand=True)
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="确定", command=self.dialog.destroy).pack()
        self.show()


class FailureDialog(CustomDialogBase):
    def __init__(self, parent, failed_files):
        super().__init__(parent, "重命名完成（有失败/跳过）", 600, 400)
        main_frame = ttk.Frame(self.dialog, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        parent_width = parent.winfo_width()
        wrap_len = parent_width - 100 if parent_width > 200 else 500
        msg = f"有 {len(failed_files)} 个文件未成功重命名，请查看日志。\n\n文件列表（最多显示前10个）："
        ttk.Label(main_frame, text=msg, wraplength=wrap_len, justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 5))

        list_frame = ttk.Frame(main_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, font=('Consolas', 9))
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)

        for f in failed_files[:10]:
            listbox.insert(tk.END, os.path.basename(f))
        if len(failed_files) > 10:
            listbox.insert(tk.END, f"... 等共 {len(failed_files)} 个文件。")

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="确定", command=self.dialog.destroy).pack()
        self.show()


# ===== 后台工作线程 =====

class RenameWorker(threading.Thread):
    """后台线程：在独立线程中执行文件扫描、日期分析、弹窗请求与重命名操作，避免阻塞 GUI"""
    def __init__(self, params, log_queue, request_queue, response_queue):
        super().__init__()
        self.params = params
        self.log_queue = log_queue
        self.request_queue = request_queue
        self.response_queue = response_queue
        self.daemon = True

    def run(self):
        try:
            folder = self.params['folder']
            recursive = self.params['recursive']
            selected = self.params['selected']
            template = self.params['template']
            prefix = self.params['prefix']
            copy_mode = self.params['copy_mode']
            target_base = self.params['target_base']

            def log(msg, level='INFO'):
                self.log_queue.put((msg, level))

            def progress_report(scanned, media_found, final=False):
                if final:
                    msg = f"扫描完成: 共扫描 {scanned} 个文件，发现 {media_found} 个媒体文件"
                else:
                    msg = f"扫描进度: 已扫描 {scanned} 个文件，已发现 {media_found} 个媒体文件"
                self.log_queue.put((msg, "INFO"))

            all_files, media_files, ignored_fullpaths = collect_files_with_categories(
                folder, recursive, selected, log, progress_report
            )
            total_files = len(all_files)

            if not media_files:
                log("没有找到可处理的媒体文件", "WARNING")
                self.log_queue.put(('__no_files__', None))
                return

            log(f"找到 {len(media_files)} 个媒体文件，开始分析...", "INFO")

            plan = prepare_rename_plan(
                folder=folder,
                all_files=all_files,
                media_files=media_files,
                ignored_fullpaths=ignored_fullpaths,
                template=template,
                prefix=prefix,
                copy_mode=copy_mode,
                target_base=target_base,
                log_func=log
            )

            ignored_fullpaths = plan['ignored_fullpaths']
            fallback_files = plan['fallback_files']
            fallback_previews = plan['fallback_previews']

            if ignored_fullpaths or fallback_files:
                confirm_data = {
                    'type': 'combined_warning',
                    'total_files': total_files,
                    'ignored_files': ignored_fullpaths,
                    'fallback_files': fallback_files,
                    'fallback_previews': fallback_previews,
                    'base_folder': folder,
                }
                self.request_queue.put(confirm_data)
                while True:
                    try:
                        result = self.response_queue.get(timeout=0.1)
                        if isinstance(result, bool):
                            continue_rename = result
                            break
                    except queue.Empty:
                        continue
                if not continue_rename:
                    self.log_queue.put(('__cancelled__', None))
                    return

            rename_map = plan['rename_map']
            if not rename_map:
                log("没有需要重命名的文件", "INFO")
                self.log_queue.put(('__finished__', {'success': False, 'no_rename': True}))
                return

            confirm_data = {
                'type': 'rename_confirm',
                'rename_map': rename_map,
                'src_base': folder,
                'target_base': target_base,
                'copy_mode': copy_mode
            }
            self.request_queue.put(confirm_data)
            while True:
                try:
                    result = self.response_queue.get(timeout=0.1)
                    if isinstance(result, bool):
                        continue_rename = result
                        break
                except queue.Empty:
                    continue
            if not continue_rename:
                self.log_queue.put(('__cancelled__', None))
                return

            success_cnt, exec_failed = execute_rename(rename_map, copy_mode, log)
            failed_analysis = plan.get('failed_analysis', [])
            all_failed = exec_failed + failed_analysis
            total = plan['total_files']
            media = plan['media_count']
            ignored_cnt = plan['ignored_count']

            stats = {
                'success': True,
                'total': total,
                'media': media,
                'success_count': success_cnt,
                'failed_count': len(all_failed),
                'ignored_count': ignored_cnt,
                'failed_files': all_failed,
                'copy_mode': copy_mode,
                'source_folder': folder,
                'target_base': target_base
            }
            self.log_queue.put(('__finished__', stats))
        except Exception as e:
            self.log_queue.put((f"后台任务出错: {e}", "ERROR"))
            self.log_queue.put(('__error__', str(e)))


# ===== 主窗口 =====

class RenamerTool:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("EXIF Rename Tool · 文件批量重命名工具 · v1.0")

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()

        # 窗口尺寸按屏幕比例动态计算，适应不同分辨率
        width_percent = 0.6
        height_percent = 0.8
        window_width = int(screen_width * width_percent)
        window_height = int(screen_height * height_percent)

        window_width = min(window_width, screen_width - 100)
        window_height = min(window_height, screen_height - 100)

        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2

        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")
        self.root.resizable(False, True)

        from tkinter import font
        default_font = font.nametofont("TkDefaultFont")
        default_font.configure(family="Microsoft YaHei", size=9)

        style = ttk.Style()
        style.theme_use('xpnative')
        style.configure('.', font=('Microsoft YaHei', 9))

        self.src_var = tk.StringVar(value=DEFAULT_SETTINGS['source_folder'])
        self.copy_var = tk.BooleanVar(value=DEFAULT_SETTINGS['copy_to_new'])
        self.target_var = tk.StringVar(value=DEFAULT_SETTINGS['target_folder'])
        self.recursive_var = tk.BooleanVar(value=DEFAULT_SETTINGS['recursive'])
        self.prefix_var = tk.StringVar(value=DEFAULT_SETTINGS['prefix'])
        self.template_var = tk.StringVar(value=DEFAULT_SETTINGS['template'])
        self.preview_var = tk.StringVar()

        self.categories = dict(DEFAULT_SETTINGS['categories'])
        self.type_vars = {}
        self.startup_time = datetime.now()

        self.log_queue = queue.Queue()
        self.request_queue = queue.Queue()
        self.response_queue = queue.Queue()
        self.worker = None

        self._create_widgets()
        self._update_preview()
        self._poll_queues()

    def _create_widgets(self):
        style = ttk.Style()
        style.configure("TNotebook.Tab", padding=[40, 5])

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.settings_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.settings_tab, text="设置")

        settings_container = ttk.Frame(self.settings_tab)
        settings_container.pack(fill=tk.BOTH, expand=True)

        self._setup_scrollable_area(settings_container)

        bottom_btn_frame = ttk.Frame(self.settings_tab)
        bottom_btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=10)
        self.start_btn = ttk.Button(bottom_btn_frame, text="开始重命名", command=self.start_rename)
        self.start_btn.pack()

        self._create_log_tab()

    def _setup_scrollable_area(self, parent):
        """构建带鼠标滚轮支持的可滚动 Canvas 区域"""
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        canvas_window_id = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw", tags="frame")

        def configure_canvas_width(event):
            canvas_width = event.width
            if canvas_width > 50:
                canvas.itemconfig(canvas_window_id, width=canvas_width - 10)

        canvas.bind("<Configure>", configure_canvas_width)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def on_mousewheel(event):
            delta = -1 * (event.delta // 120) if event.delta else (1 if event.num == 4 else -1 if event.num == 5 else 0)
            canvas.yview_scroll(delta, "units")

        canvas.bind("<MouseWheel>", on_mousewheel)
        canvas.bind("<Button-4>", on_mousewheel)
        canvas.bind("<Button-5>", on_mousewheel)
        scrollbar.bind("<MouseWheel>", on_mousewheel)
        scrollbar.bind("<Button-4>", on_mousewheel)
        scrollbar.bind("<Button-5>", on_mousewheel)

        self.main_frame = scrollable_frame

        self._create_dir_frame()
        self._create_type_frame()
        self._create_placeholder_frame()
        self._create_output_frame()
        self._create_status_label()

    def _create_dir_frame(self):
        """创建目录输入区块（源文件夹、目标文件夹、子文件夹选项）"""
        dir_frame = ttk.LabelFrame(self.main_frame, text="目录输入", padding=5)
        dir_frame.pack(fill=tk.X, pady=5, padx=5)

        row1 = ttk.Frame(dir_frame)
        row1.pack(fill=tk.X, pady=5)
        ttk.Label(row1, text="源文件夹：").pack(side=tk.LEFT)
        self.src_entry = ttk.Entry(row1, textvariable=self.src_var)
        self.src_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(row1, text="浏览", width=12, command=self._select_folder).pack(side=tk.LEFT)

        row2 = ttk.Frame(dir_frame)
        row2.pack(fill=tk.X, pady=5)
        self.copy_cb = ttk.Checkbutton(row2, text="复制到新目录（保护原文件）",
                                       variable=self.copy_var, command=self._toggle_target)
        self.copy_cb.pack(side=tk.LEFT)
        self.target_entry = ttk.Entry(row2, textvariable=self.target_var, state='disabled')
        self.target_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.target_btn = ttk.Button(row2, text="浏览", width=12, command=self._select_target, state='disabled')
        self.target_btn.pack(side=tk.LEFT)

        row4 = ttk.Frame(dir_frame)
        row4.pack(fill=tk.X, pady=5)
        ttk.Checkbutton(row4, text="包含子文件夹", variable=self.recursive_var).pack(side=tk.LEFT)

    def _create_type_frame(self):
        """创建处理类型选择区块"""
        type_frame = ttk.LabelFrame(self.main_frame, text="处理类型", padding=5)
        type_frame.pack(fill=tk.X, pady=5, padx=5)

        for cat, label, exts in (
                ("image", "图像类", IMAGE_EXTS),
                ("raw", "RAW类", RAW_EXTS),
                ("video", "视频类", VIDEO_EXTS),
                ("audio", "音频类", AUDIO_EXTS),
                ("custom_file", "自定义文件    (默认为空，请在源代码中添加文件类型)", CUSTOM_FILE_EXTS),
        ):
            var = tk.BooleanVar(value=self.categories.get(cat, False))
            self.type_vars[cat] = var
            cb = ttk.Checkbutton(type_frame, text=f"{label} {' '.join(exts)}", variable=var)
            cb.pack(anchor=tk.W, padx=5, pady=2)
            var.trace_add('write', lambda *a, c=cat: self._on_category_changed(c))

    def _create_placeholder_frame(self):
        """创建文件名模板区块（前缀、模板输入框、占位符下拉菜单）"""
        ph_frame = ttk.LabelFrame(self.main_frame, text="文件名模板占位符（支持手动输入或点击插入）", padding=5)
        ph_frame.pack(fill=tk.X, pady=5, padx=5)

        prefix_row = ttk.Frame(ph_frame)
        prefix_row.pack(fill=tk.X, pady=5)
        ttk.Label(prefix_row, text="自定义前缀：").pack(side=tk.LEFT)
        self.prefix_entry = ttk.Entry(prefix_row, textvariable=self.prefix_var)
        self.prefix_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(prefix_row, text="清空", width=12, command=lambda: self.prefix_var.set("")).pack(side=tk.LEFT, padx=(0, 5))
        self.prefix_var.trace_add('write', lambda *a: self._update_preview())

        template_row = ttk.Frame(ph_frame)
        template_row.pack(fill=tk.X, pady=5)
        ttk.Label(template_row, text="文件名模板：").pack(side=tk.LEFT)
        self.template_entry = ttk.Entry(template_row, textvariable=self.template_var)
        self.template_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.template_var.trace_add('write', lambda *a: self._update_preview())

        placeholders = [
            ("{YYYY}", "年(四位)"), ("{YY}", "年(两位)"), ("{MM}", "月"), ("{DD}", "日"),
            ("{hh}", "时"), ("{mm}", "分"), ("{ss}", "秒"),
            ("{prefix}", "自定义前缀"), ("{filetype}", "文件类型"),
            ("{id}", "四位随机ID"), ("{seq}", "顺序编号(1 2 3 4)"),
            ("{seq:03d}", "顺序编号(001 002 003 004)"),
            ("{seq:04d}", "顺序编号(0001 0002)(位数可自定义)")
        ]

        self.placeholder_mb = tk.Menubutton(
            template_row,
            text="插入占位符 ▼",
            indicatoron=False,
            relief=tk.RAISED,
            bd=1,
            font=("Microsoft YaHei", 9),
            width=14
        )
        self.placeholder_mb.pack(side=tk.LEFT, padx=(5, 5))
        placeholder_menu = tk.Menu(self.placeholder_mb, tearoff=0)
        for ph, desc in placeholders:
            placeholder_menu.add_command(
                label=f"{ph}  -  {desc}",
                command=lambda p=ph: self._insert_placeholder(p)
            )
        placeholder_menu.add_separator()
        placeholder_menu.add_command(
            label="恢复默认模板",
            command=lambda: self.template_var.set(DEFAULT_TEMPLATE)
        )
        self.placeholder_mb["menu"] = placeholder_menu

    def _create_output_frame(self):
        """创建输出预览区块（时间戳示例和重命名预览）"""
        output_frame = ttk.LabelFrame(self.main_frame, text="输出示例", padding=5)
        output_frame.pack(fill=tk.X, pady=5, padx=5)

        output_frame.columnconfigure(0, weight=0)
        output_frame.columnconfigure(1, weight=1)

        ttk.Label(output_frame, text="当前时间戳示例：", anchor="w").grid(row=0, column=0, padx=5, pady=2, sticky="w")
        current_time = self.startup_time.strftime("%Y年 %m月 %d日 %H时 %M分 %S秒")
        ttk.Label(output_frame, text=current_time, foreground="black", anchor="w").grid(row=0, column=1, padx=5, pady=2,
                                                                                        sticky="w")

        ttk.Label(output_frame, text="当前重命名示例：", anchor="w").grid(row=1, column=0, padx=5, pady=2, sticky="w")
        self.preview_entry = ttk.Entry(output_frame, textvariable=self.preview_var, state='readonly')
        self.preview_entry.grid(row=1, column=1, padx=5, pady=2, sticky="ew")

    def _create_status_label(self):
        """显示当前 EXIF 解析库状态，提示用户安装 exifread 以获得更好的 RAW 支持"""
        status_text = (f"✓ 当前使用 ExifRead {EXIFREAD_VERSION} 处理 (支持RAW/HEIC)"
                       if EXIFREAD_AVAILABLE else "⚠ 当前使用 PIL 处理(基础EXIF支持，建议安装exifread，pip install exifread)")
        ttk.Label(self.main_frame, text=status_text, foreground='purple').pack(pady=2, padx=5)

    def _create_log_tab(self):
        """创建日志标签页"""
        self.log_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.log_tab, text="日志")

        log_frame = ttk.LabelFrame(self.log_tab, text="运行日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=('Consolas', 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_config("INFO", foreground="black")
        self.log_text.tag_config("WARNING", foreground="orange")
        self.log_text.tag_config("ERROR", foreground="red")

    def _insert_placeholder(self, placeholder):
        """在模板输入框光标位置插入选中的占位符"""
        cursor_pos = self.template_entry.index(tk.INSERT)
        current_text = self.template_var.get()
        new_text = current_text[:cursor_pos] + placeholder + current_text[cursor_pos:]
        self.template_var.set(new_text)
        self.template_entry.icursor(cursor_pos + len(placeholder))

    def _on_category_changed(self, cat):
        self.categories[cat] = self.type_vars[cat].get()

    def _toggle_target(self):
        """启用/禁用目标文件夹入口：仅在复制模式下需要指定目标"""
        enabled = self.copy_var.get()
        state = tk.NORMAL if enabled else tk.DISABLED
        self.target_entry.config(state=state)
        self.target_btn.config(state=state)
        if not enabled:
            self.target_var.set('')

    def _select_folder(self):
        folder = filedialog.askdirectory(title="选择源文件夹")
        if folder:
            self.src_var.set(folder)

    def _select_target(self):
        folder = filedialog.askdirectory(title="选择目标文件夹")
        if folder:
            self.target_var.set(folder)

    def _update_preview(self):
        """根据当前模板和前缀更新文件名预览"""
        dt = self.startup_time
        name = apply_template(
            self.template_var.get(),
            dt,
            self.prefix_var.get(),
            "A1B2",
            123,
            "IMAGE"
        )
        self.preview_var.set(f"{name}.jpg")

    def log(self, message, level="INFO"):
        formatted = f"[{level}] {message}\n"
        self.log_text.config(state=tk.NORMAL)
        tag = level if level in ("INFO", "WARNING", "ERROR") else "INFO"
        self.log_text.insert(tk.END, formatted, tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _poll_queues(self):
        """轮询后台队列：每 50ms 处理日志和对话框请求，保持 GUI 响应"""
        # —— 处理日志队列 ——
        count = 0
        while True:
            try:
                item = self.log_queue.get_nowait()
                count += 1
                if item[0] == '__finished__':
                    self.on_rename_finished(item[1])
                elif item[0] == '__no_files__':
                    self.start_btn.config(state=tk.NORMAL, text="开始重命名")
                elif item[0] == '__error__':
                    ErrorDialog(self.root, f"处理过程中发生错误：\n{item[1]}")
                    self.start_btn.config(state=tk.NORMAL, text="开始重命名")
                elif item[0] == '__cancelled__':
                    self.start_btn.config(state=tk.NORMAL, text="开始重命名")
                    self.log("用户取消重命名", "INFO")
                else:
                    msg, level = item
                    self.log(msg, level)
                if count % 5 == 0:
                    self.root.update()
            except queue.Empty:
                break
        if count > 0 and count % 5 != 0:
            self.root.update()

        # —— 处理对话框请求队列（每次只取一条，延迟到独立回调，防止嵌套 wait_window） ——
        try:
            req = self.request_queue.get_nowait()
            self.root.after(0, self._handle_dialog_request, req)
            return
        except queue.Empty:
            pass

        self.root.after(50, self._poll_queues)

    def _handle_dialog_request(self, req):
        """在独立回调中处理对话框请求（含 wait_window 阻塞），完成后恢复轮询"""
        if req['type'] == 'combined_warning':
            result = CombinedWarningDialog(
                self.root, req['total_files'],
                req['ignored_files'], req['fallback_files'],
                req['fallback_previews'], req['base_folder']
            ).result
        elif req['type'] == 'rename_confirm':
            items = []
            for old, new in req['rename_map']:
                old_rel = os.path.relpath(old, req['src_base'])
                if req['copy_mode']:
                    new_rel = os.path.relpath(new, req['target_base']) if req['target_base'] else new
                else:
                    new_rel = os.path.basename(new)
                items.append(f"{old_rel} → {new_rel}")
            result = CustomConfirmDialog(
                self.root,
                title="确认重命名",
                message=f"将重命名以下 {len(req['rename_map'])} 个文件：",
                items=items,
                yes_text="开始处理",
                no_text="取消",
                width=750,
                height=550
            ).result
        else:
            result = False
        self.response_queue.put(result)
        self.root.after(50, self._poll_queues)

    def start_rename(self):
        """校验输入参数，启动后台重命名工作线程"""
        # 清理当前线程的 EXIF 缓存，避免旧数据干扰新任务
        _exif_cache.__dict__.clear()

        folder = self.src_var.get().strip()
        if not folder or not os.path.isdir(folder):
            ErrorDialog(self.root, "请选择一个有效的源文件夹", "错误")
            return

        copy_mode = self.copy_var.get()
        target_base = None
        if copy_mode:
            target_base = self.target_var.get().strip()
            if not target_base:
                ErrorDialog(self.root, "请选择一个目标文件夹", "错误")
                return
            os.makedirs(target_base, exist_ok=True)

        template = self.template_var.get().strip()
        if not template:
            ErrorDialog(self.root, "文件名模板不能为空", "错误")
            return
        prefix = self.prefix_var.get().strip()

        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)

        self.start_btn.config(state=tk.DISABLED, text="处理中...")
        self.notebook.select(self.log_tab)

        selected = {k: v for k, v in self.categories.items()}
        recursive = self.recursive_var.get()

        params = {
            'folder': folder,
            'recursive': recursive,
            'selected': selected,
            'template': template,
            'prefix': prefix,
            'copy_mode': copy_mode,
            'target_base': target_base,
        }

        for q in (self.log_queue, self.request_queue, self.response_queue):
            while not q.empty():
                q.get()

        self.worker = RenameWorker(params, self.log_queue, self.request_queue, self.response_queue)
        self.worker.start()

    def on_rename_finished(self, stats):
        """重命名完成回调：输出统计、弹窗提示、打开目标文件夹"""
        self.start_btn.config(state=tk.NORMAL, text="开始重命名")
        if stats.get('success'):
            self.log("处理完成，统计信息：")
            self.log(f"*总文件数：{stats['total']}")
            self.log(f"*忽略的非媒体文件：{stats['ignored_count']}")
            self.log(f"*媒体文件：{stats['media']}")
            self.log(f"*成功重命名：{stats['success_count']}")
            self.log(f"*失败（含跳过）：{stats['failed_count']}")

            if stats['failed_count'] > 0:
                FailureDialog(self.root, stats['failed_files'])

            if stats['success_count'] > 0:
                folder_to_open = stats['target_base'] if stats['copy_mode'] and stats['target_base'] else stats['source_folder']
                self._open_folder(folder_to_open)
        else:
            if not stats.get('no_rename'):
                self.log("处理未完成或已取消", "INFO")

    def _open_folder(self, path):
        """调用系统文件管理器打开指定路径（使用 realpath 处理 NTFS 挂载点路径偏差）"""
        real_path = _safe_realpath(path)
        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(real_path)
            elif system == "Darwin":
                subprocess.run(['open', real_path])
            else:
                subprocess.run(['xdg-open', real_path])
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def main():
    app = RenamerTool()
    app.run()


if __name__ == "__main__":
    main()
