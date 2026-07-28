# pip install pillow exifread

import os
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox
from PIL import Image
import exifread

class ExifDebugger:
    def __init__(self, root):
        self.root = root
        self.root.title("图片 EXIF 分析工具 (debug)")
        self.root.geometry("1000x600")

        self.current_image_path = None
        self.create_widgets()

    def create_widgets(self):
        top_frame = tk.Frame(self.root)
        top_frame.pack(pady=10, fill=tk.X)

        self.btn_open = tk.Button(top_frame, text="选择图片", command=self.open_image)
        self.btn_open.pack(side=tk.LEFT, padx=10)

        self.btn_export = tk.Button(top_frame, text="导出日志", command=self.export_log, state=tk.DISABLED)
        self.btn_export.pack(side=tk.LEFT, padx=10)

        self.lbl_status = tk.Label(top_frame, text="未选择图片", fg="gray")
        self.lbl_status.pack(side=tk.LEFT, padx=20)

        bottom_frame = tk.Frame(self.root)
        bottom_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 左侧：明确标注使用 PIL + exifread 常用字段
        left_frame = tk.LabelFrame(bottom_frame, text="基本信息 (PIL) & 常用 EXIF (exifread)")
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0,5))
        self.txt_left = scrolledtext.ScrolledText(left_frame, wrap=tk.WORD, width=45)
        self.txt_left.pack(fill=tk.BOTH, expand=True)

        # 右侧：明确标注使用 exifread 全部标签
        right_frame = tk.LabelFrame(bottom_frame, text="全部 EXIF 标签 (exifread，已过滤缩略图/二进制)")
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5,0))
        self.txt_right = scrolledtext.ScrolledText(right_frame, wrap=tk.WORD, width=45)
        self.txt_right.pack(fill=tk.BOTH, expand=True)

    def open_image(self):
        file_path = filedialog.askopenfilename(
            title="选择图片",
            filetypes=[("图片文件", "*.jpg *.jpeg *.png *.bmp *.tiff *.tif *.webp"), ("所有文件", "*.*")]
        )
        if not file_path:
            return
        self.current_image_path = file_path
        self.lbl_status.config(text=f"已加载: {os.path.basename(file_path)}", fg="green")
        self.btn_export.config(state=tk.NORMAL)
        self.analyze_image(file_path)

    def analyze_image(self, img_path):
        self.txt_left.delete(1.0, tk.END)
        self.txt_right.delete(1.0, tk.END)

        # ========== 左侧：基本信息（PIL） ==========
        try:
            pil_img = Image.open(img_path)
            info = "【图片基本信息 - 来自 PIL】\n"
            info += f"文件路径: {img_path}\n"
            info += f"文件大小: {os.path.getsize(img_path) / 1024:.2f} KB\n"
            info += f"图像尺寸: {pil_img.width} x {pil_img.height}\n"
            info += f"图像格式: {pil_img.format}\n"
            info += f"色彩模式: {pil_img.mode}\n"
            exif_dict = pil_img.getexif()
            if exif_dict:
                info += f"EXIF 数据存在: 是 (共 {len(exif_dict)} 条)\n"
            else:
                info += f"EXIF 数据存在: 否\n"
            self.txt_left.insert(tk.END, info + "\n")
        except Exception as e:
            self.txt_left.insert(tk.END, f"PIL 打开失败: {e}\n")
            self.txt_right.insert(tk.END, f"PIL 打开失败: {e}\n")
            return

        # ========== 右侧：全部 EXIF 标签（exifread，过滤缩略图） ==========
        try:
            with open(img_path, 'rb') as f:
                tags = exifread.process_file(f, details=True)

            if not tags:
                self.txt_right.insert(tk.END, "未找到任何 EXIF 标签。\n")
            else:
                filtered_tags = {}
                for tag, value in tags.items():
                    if 'thumbnail' in tag.lower():
                        continue
                    if isinstance(value, bytes) and len(value) > 200:
                        continue
                    if tag in ('JPEGThumbnail', 'TIFFThumbnail', 'EXIF Thumbnail', 'Thumbnail'):
                        continue
                    filtered_tags[tag] = value

                self.txt_right.insert(tk.END, "【全部 EXIF 标签 - 来自 exifread】\n")
                for tag in sorted(filtered_tags.keys()):
                    val = filtered_tags[tag]
                    if hasattr(val, 'printable'):
                        val_str = val.printable
                    else:
                        val_str = str(val)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "...(截断)"
                    self.txt_right.insert(tk.END, f"{tag}: {val_str}\n")
        except Exception as e:
            self.txt_right.insert(tk.END, f"exifread 解析出错: {e}\n")

        # ========== 左侧补充：常用 EXIF 字段（exifread 筛选） ==========
        try:
            with open(img_path, 'rb') as f:
                tags = exifread.process_file(f, details=True)

            common_tags = {
                'Image Make': '相机品牌',
                'Image Model': '相机型号',
                'EXIF ExposureTime': '曝光时间',
                'EXIF FNumber': '光圈值',
                'EXIF ISOSpeedRatings': 'ISO',
                'EXIF FocalLength': '焦距',
                'EXIF DateTimeOriginal': '拍摄时间',
                'EXIF ShutterSpeedValue': '快门速度值',
                'EXIF ApertureValue': '光圈值(APEX)',
                'EXIF ExposureBiasValue': '曝光补偿',
                'EXIF Flash': '闪光灯',
                'EXIF WhiteBalance': '白平衡',
                'EXIF LensModel': '镜头型号',
            }
            self.txt_left.insert(tk.END, "\n【常用 EXIF 字段 - 来自 exifread】\n")
            found_any = False
            for tag, desc in common_tags.items():
                if tag in tags:
                    val = tags[tag]
                    if hasattr(val, 'printable'):
                        val_str = val.printable
                    else:
                        val_str = str(val)
                    self.txt_left.insert(tk.END, f"{desc} ({tag}): {val_str}\n")
                    found_any = True
            if not found_any:
                self.txt_left.insert(tk.END, "未找到常用 EXIF 字段。\n")
        except Exception as e:
            self.txt_left.insert(tk.END, f"常用 EXIF 解析出错: {e}\n")

    def export_log(self):
        if not self.current_image_path:
            messagebox.showwarning("警告", "没有加载任何图片，无法导出。")
            return
        base_name = os.path.splitext(os.path.basename(self.current_image_path))[0]
        default_name = os.path.join(os.path.dirname(self.current_image_path), f"{base_name}_exif_log.txt")
        save_path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialfile=os.path.basename(default_name),
            initialdir=os.path.dirname(self.current_image_path)
        )
        if save_path:
            try:
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write("左侧区域内容：\n")
                    f.write(self.txt_left.get(1.0, tk.END))
                    f.write("\n\n右侧区域内容：\n")
                    f.write(self.txt_right.get(1.0, tk.END))
                messagebox.showinfo("成功", f"日志已保存到:\n{save_path}")
            except Exception as e:
                messagebox.showerror("错误", f"保存失败: {e}")

if __name__ == "__main__":
    root = tk.Tk()
    app = ExifDebugger(root)
    root.mainloop()