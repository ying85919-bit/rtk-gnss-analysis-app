from __future__ import annotations

import io
import math
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st


APP_TITLE = "RTK/GNSS 静态测量数据自动检查与成果分析小程序"

SYS_NAME = {
    "G": "GPS",
    "C": "BDS",
    "R": "GLONASS",
    "E": "Galileo",
    "J": "QZSS",
    "S": "SBAS",
    "I": "IRNSS",
}
SYSTEM_COLS = list(SYS_NAME.values())


def is_rinex_obs_file(path: Path) -> bool:
    """判断是否为 RINEX 观测文件。支持 .obs、.o、.26o、.25o 等后缀。"""
    name = path.name.lower()
    return name.endswith(".obs") or name.endswith(".o") or re.search(r"\.\d{2}o$", name) is not None


def repair_zip_member_name(name: str, flag_bits: int) -> str:
    """修复中文 Windows 压缩包里常见的文件名乱码。

    有些测量软件或 Windows 压缩工具生成的 zip 没有标记 UTF-8，
    Python 会按 cp437 读取文件名，中文会显示成乱码。这里尝试把
    cp437 误读结果还原为 gb18030/gbk。
    """
    raw_name = name.replace("\\", "/")

    # zip 规范中第 11 位表示文件名是否为 UTF-8。若已标记 UTF-8，通常不用修。
    if flag_bits & 0x800:
        return raw_name

    for encoding in ("gb18030", "gbk"):
        try:
            fixed = raw_name.encode("cp437").decode(encoding)
            return fixed
        except Exception:
            pass

    return raw_name


def safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    """解压 zip 文件，修复中文文件名乱码，并防止路径穿越。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_name = repair_zip_member_name(member.filename, member.flag_bits)
            if member_name.startswith("/") or ".." in Path(member_name).parts:
                continue

            out_path = target_dir / member_name

            if member.is_dir() or member_name.endswith("/"):
                out_path.mkdir(parents=True, exist_ok=True)
                continue

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def scan_files(root: Path) -> dict[str, list[Path]]:
    """扫描输入目录，自动识别常见 GNSS 数据文件。"""
    files = {
        "gns": [],
        "rinex_obs": [],
        "html": [],
        "xlsx": [],
        "csv": [],
        "other": [],
    }

    for p in root.rglob("*"):
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.endswith(".gns"):
            files["gns"].append(p)
        elif is_rinex_obs_file(p):
            files["rinex_obs"].append(p)
        elif name.endswith((".html", ".htm")):
            files["html"].append(p)
        elif name.endswith(".xlsx"):
            files["xlsx"].append(p)
        elif name.endswith(".csv"):
            files["csv"].append(p)
        else:
            files["other"].append(p)

    for key in files:
        files[key] = sorted(files[key], key=lambda x: x.name.lower())
    return files


def station_key(path: Path) -> str:
    """用文件主名作为测站匹配键。"""
    return path.stem.strip().lower()


def compare_stations(gns_files: list[Path], rinex_files: list[Path]) -> pd.DataFrame:
    """比对原始 GNS 文件和内业 RINEX 观测文件，识别保留/剔除测站。"""
    raw_names = {station_key(p): p.name for p in gns_files}
    rinex_names = {station_key(p): p.name for p in rinex_files}
    kept = set(raw_names) & set(rinex_names)
    removed = set(raw_names) - set(rinex_names)

    rows = []
    for key in sorted(set(raw_names) | set(rinex_names)):
        if key in kept:
            status = "保留"
        elif key in removed:
            status = "被剔除"
        else:
            status = "仅内业存在"
        rows.append(
            {
                "测站文件名": key,
                "原始数据文件": raw_names.get(key, ""),
                "RINEX观测文件": rinex_names.get(key, ""),
                "原始数据中是否存在": "是" if key in raw_names else "否",
                "内业成果中是否保留": "是" if key in rinex_names else "否",
                "判断": status,
            }
        )
    return pd.DataFrame(rows)


def parse_float(value: str) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def parse_rinex_time_from_header(line: str) -> datetime | None:
    parts = line[:60].split()
    if len(parts) < 6:
        return None
    try:
        year, month, day, hour, minute = map(int, parts[:5])
        sec = float(parts[5])
        whole_sec = int(sec)
        microsecond = int(round((sec - whole_sec) * 1_000_000))
        return datetime(year, month, day, hour, minute, whole_sec, microsecond)
    except Exception:
        return None


def parse_rinex_epoch_time(parts: list[str]) -> datetime | None:
    if len(parts) < 6:
        return None
    try:
        year, month, day, hour, minute = map(int, parts[:5])
        sec = float(parts[5])
        whole_sec = int(sec)
        microsecond = int(round((sec - whole_sec) * 1_000_000))
        return datetime(year, month, day, hour, minute, whole_sec, microsecond)
    except Exception:
        return None


def parse_rinex_obs(path: Path) -> tuple[dict[str, Any], pd.DataFrame]:
    """解析 RINEX 3.x 观测文件头信息和每个历元的卫星数量。"""
    with open(path, "r", encoding="ascii", errors="ignore") as f:
        lines = f.readlines()

    header: dict[str, Any] = {
        "文件名": path.name,
        "测站名": "",
        "RINEX版本": "",
        "接收机": "",
        "天线": "",
        "近似X": None,
        "近似Y": None,
        "近似Z": None,
        "采样间隔(s)": None,
        "开始时间": None,
        "结束时间": None,
        "解析状态": "正常",
    }

    header_end = None
    for i, line in enumerate(lines):
        label = line[60:].strip() if len(line) >= 60 else ""
        content = line[:60]

        if "RINEX VERSION / TYPE" in label:
            header["RINEX版本"] = content[:20].strip()
        elif "MARKER NAME" in label:
            header["测站名"] = content.strip()
        elif "REC # / TYPE / VERS" in label:
            header["接收机"] = content.strip()
        elif "ANT # / TYPE" in label:
            header["天线"] = content.strip()
        elif "APPROX POSITION XYZ" in label:
            nums = content.split()
            if len(nums) >= 3:
                header["近似X"] = parse_float(nums[0])
                header["近似Y"] = parse_float(nums[1])
                header["近似Z"] = parse_float(nums[2])
        elif "INTERVAL" in label:
            nums = content.split()
            if nums:
                header["采样间隔(s)"] = parse_float(nums[0])
        elif "TIME OF FIRST OBS" in label:
            header["开始时间"] = parse_rinex_time_from_header(line)
        elif "TIME OF LAST OBS" in label:
            header["结束时间"] = parse_rinex_time_from_header(line)
        elif "END OF HEADER" in line:
            header_end = i
            break

    if header_end is None:
        header["解析状态"] = "未找到 END OF HEADER"
        return header, pd.DataFrame()

    epoch_rows = []
    i = header_end + 1

    while i < len(lines):
        line = lines[i]
        if not line.startswith(">"):
            i += 1
            continue

        parts = line[1:].split()
        epoch_time = parse_rinex_epoch_time(parts)
        if epoch_time is None or len(parts) < 8:
            i += 1
            continue

        try:
            epoch_flag = int(parts[6])
        except Exception:
            epoch_flag = None
        try:
            declared_nsat = int(parts[7])
        except Exception:
            declared_nsat = None

        counts = {name: 0 for name in SYS_NAME.values()}
        sat_ids = []

        j = i + 1
        while j < len(lines) and not lines[j].startswith(">"):
            sat = lines[j][:3].strip()
            if len(sat) >= 2 and sat[0] in SYS_NAME and sat[1:].isdigit():
                counts[SYS_NAME[sat[0]]] += 1
                sat_ids.append(sat)
            j += 1

        actual_nsat = len(sat_ids)
        row = {
            "文件名": path.name,
            "测站名": header["测站名"] or path.stem,
            "时间": epoch_time,
            "历元标志": epoch_flag,
            "卫星总数": actual_nsat if actual_nsat > 0 else declared_nsat,
            "声明卫星数": declared_nsat,
            "卫星列表": ",".join(sat_ids),
        }
        row.update(counts)
        epoch_rows.append(row)
        i = j

    epoch_df = pd.DataFrame(epoch_rows)
    if epoch_df.empty:
        header["解析状态"] = "未解析到历元数据；当前程序主要支持 RINEX 3.x 格式"

    return header, epoch_df


def summarize_rinex(headers: list[dict[str, Any]], epoch_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    header_df = pd.DataFrame(headers)

    if epoch_df.empty:
        return header_df, pd.DataFrame()

    rows = []
    for file_name, group in epoch_df.groupby("文件名"):
        first = group.iloc[0]
        row = {
            "文件名": file_name,
            "测站名": first.get("测站名", ""),
            "历元数": len(group),
            "平均卫星数": round(group["卫星总数"].mean(), 2),
            "最小卫星数": int(group["卫星总数"].min()),
            "最大卫星数": int(group["卫星总数"].max()),
            "开始时间": group["时间"].min(),
            "结束时间": group["时间"].max(),
        }
        for col in SYSTEM_COLS:
            row[f"{col}平均数"] = round(group[col].mean(), 2) if col in group.columns else 0
        rows.append(row)

    summary_df = pd.DataFrame(rows).sort_values("文件名")

    if not header_df.empty and "文件名" in header_df.columns:
        cols_to_merge = ["文件名", "RINEX版本", "采样间隔(s)", "接收机", "天线", "近似X", "近似Y", "近似Z", "解析状态"]
        cols_to_merge = [c for c in cols_to_merge if c in header_df.columns]
        summary_df = summary_df.merge(header_df[cols_to_merge], on="文件名", how="left")

    return header_df, summary_df


def read_html_tables(path: Path) -> list[pd.DataFrame]:
    """读取 HTML 表格，针对中文报告做容错。"""
    errors = []
    for encoding in [None, "utf-8", "gbk", "gb18030"]:
        try:
            if encoding:
                return pd.read_html(str(path), encoding=encoding)
            return pd.read_html(str(path))
        except Exception as exc:
            errors.append(str(exc))
    return []


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    copied = df.copy()
    if isinstance(copied.columns, pd.MultiIndex):
        new_cols = []
        for col in copied.columns:
            parts = [str(x) for x in col if not str(x).startswith("Unnamed")]
            new_cols.append(" ".join(parts).strip() or f"列{len(new_cols)}")
        copied.columns = new_cols
    else:
        copied.columns = [str(c) for c in copied.columns]
    return copied


def extract_project_report(html_files: list[Path]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """从内业 HTML 报告中提取项目汇总和同步环检核结果。"""
    summary_rows = []
    sync_rows = []
    metrics: dict[str, Any] = {}

    target_labels = ["观测文件总数", "站点个数", "形成基线总条数", "形成重复基线条数", "形成同步环个数"]

    for path in html_files:
        tables = read_html_tables(path)
        for table in tables:
            flat = flatten_columns(table)

            # 汇总指标：逐行扫描关键词
            for _, row in flat.iterrows():
                values = ["" if pd.isna(v) else str(v).strip() for v in row.tolist()]
                for label in target_labels:
                    for idx, value in enumerate(values):
                        if label in value and idx + 1 < len(values):
                            metrics[label] = values[idx + 1]

                            # 同一行中可能还有“合格/不合格”数量
                            if label == "形成同步环个数":
                                for j, v in enumerate(values):
                                    if v == "合格" and j + 1 < len(values):
                                        metrics["同步环合格数"] = values[j + 1]
                                    if v == "不合格" and j + 1 < len(values):
                                        metrics["同步环不合格数"] = values[j + 1]

            # 同步环表格：找“质量检查”和“环总长”等列
            col_names = list(flat.columns)
            q_cols = [c for c in col_names if "质量检查" in c]
            ring_like = any("环总长" in c or "WX" in c or "相对误差" in c for c in col_names)
            if q_cols and ring_like:
                q_col = q_cols[0]
                name_cols = [c for c in col_names if "名称" in c]
                name_col = name_cols[0] if name_cols else col_names[0]

                tmp = flat.copy()
                tmp["来源报告"] = path.name
                tmp["同步环名称"] = tmp[name_col]
                tmp["质量检查结果"] = tmp[q_col].astype(str).str.strip()
                tmp = tmp[tmp["质量检查结果"].isin(["合格", "不合格"])]
                if not tmp.empty:
                    sync_rows.append(tmp)

    if metrics:
        for key, value in metrics.items():
            summary_rows.append({"指标": key, "数值": value})

    summary_df = pd.DataFrame(summary_rows)
    sync_df = pd.concat(sync_rows, ignore_index=True) if sync_rows else pd.DataFrame()

    # 如果同步环明细表存在，用明细表补充合格/不合格数量
    if not sync_df.empty:
        counts = sync_df["质量检查结果"].value_counts().to_dict()
        metrics["同步环明细数量"] = len(sync_df)
        metrics["同步环合格数"] = counts.get("合格", metrics.get("同步环合格数", 0))
        metrics["同步环不合格数"] = counts.get("不合格", metrics.get("同步环不合格数", 0))

    return summary_df, sync_df, metrics


def read_quality_excels(xlsx_files: list[Path]) -> pd.DataFrame:
    """读取质检汇总 Excel。不同软件导出的表结构差异较大，这里先做通用预览。"""
    rows = []
    for path in xlsx_files:
        try:
            sheets = pd.read_excel(path, sheet_name=None)
            for sheet_name, df in sheets.items():
                rows.append(
                    {
                        "文件名": path.name,
                        "工作表": sheet_name,
                        "行数": df.shape[0],
                        "列数": df.shape[1],
                        "列名预览": "、".join([str(c) for c in df.columns[:8]]),
                    }
                )
        except Exception as exc:
            rows.append({"文件名": path.name, "工作表": "读取失败", "行数": 0, "列数": 0, "列名预览": str(exc)})
    return pd.DataFrame(rows)


def make_figures(epoch_df: pd.DataFrame, output_dir: Path) -> dict[str, Path]:
    fig_paths: dict[str, Path] = {}
    if epoch_df.empty:
        return fig_paths

    output_dir.mkdir(parents=True, exist_ok=True)

    # 粉蓝撞色图表主题
    palette = ["#ff6fae", "#4fc3f7", "#8b5cf6", "#7dd3fc", "#f9a8d4", "#60a5fa", "#fbbf24"]
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]

    fig1, ax1 = plt.subplots(figsize=(11.5, 5.6))
    fig1.patch.set_facecolor("#fff7fb")
    ax1.set_facecolor("#ffffff")
    for idx, (station, group) in enumerate(epoch_df.groupby("测站名")):
        group = group.sort_values("时间")
        ax1.plot(
            group["时间"],
            group["卫星总数"],
            label=str(station),
            color=palette[idx % len(palette)],
            linewidth=2.4,
            marker="o",
            markersize=2.8,
            alpha=0.95,
        )
    ax1.set_xlabel("Observation Time", fontsize=11)
    ax1.set_ylabel("Satellite Count", fontsize=11)
   ax1.set_title("Satellite Count by Station", fontsize=15, fontweight="bold", pad=16)
    ax1.grid(True, linestyle="--", linewidth=0.7, alpha=0.35, color="#8ecae6")
    ax1.legend(loc="best", frameon=True, fancybox=True, framealpha=0.9)
    for spine in ax1.spines.values():
        spine.set_color("#f0b6d5")
        spine.set_linewidth(1.1)
    plt.setp(ax1.get_xticklabels(), rotation=30, ha="right")
    fig1.tight_layout()
    path1 = output_dir / "satellite_count_by_station.png"
    fig1.savefig(path1, dpi=220, bbox_inches="tight", facecolor=fig1.get_facecolor())
    fig_paths["卫星数量变化图"] = path1
    plt.close(fig1)

    available_system_cols = [c for c in SYSTEM_COLS if c in epoch_df.columns]
    if available_system_cols:
        system_avg = epoch_df.groupby("测站名")[available_system_cols].mean().round(2)
        fig2, ax2 = plt.subplots(figsize=(11.5, 5.6))
        fig2.patch.set_facecolor("#f2fbff")
        ax2.set_facecolor("#ffffff")
        system_avg.plot(kind="bar", ax=ax2, color=palette[: len(available_system_cols)], width=0.78)
        ax2.set_xlabel("Station", fontsize=11)
        ax2.set_ylabel("Average Satellite Count", fontsize=11)
        ax2.set_title("Average Satellite Count by GNSS System", fontsize=15, fontweight="bold", pad=16)
        ax2.grid(axis="y", linestyle="--", linewidth=0.7, alpha=0.35, color="#8ecae6")
        ax2.legend(loc="best", frameon=True, fancybox=True, framealpha=0.9)
        for spine in ax2.spines.values():
            spine.set_color("#96d9ff")
            spine.set_linewidth(1.1)
        plt.setp(ax2.get_xticklabels(), rotation=0)
        fig2.tight_layout()
        path2 = output_dir / "system_average_satellite_count.png"
        fig2.savefig(path2, dpi=220, bbox_inches="tight", facecolor=fig2.get_facecolor())
        fig_paths["卫星系统平均数量图"] = path2
        plt.close(fig2)

    return fig_paths


def to_number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).strip())
    except Exception:
        return None


def generate_analysis_text(
    file_counts: dict[str, int],
    station_compare: pd.DataFrame,
    rinex_summary: pd.DataFrame,
    project_metrics: dict[str, Any],
) -> str:
    raw_count = file_counts.get("gns", 0)
    rinex_count = file_counts.get("rinex_obs", 0)

    removed_names = []
    if not station_compare.empty and "判断" in station_compare.columns:
        removed_names = station_compare.loc[station_compare["判断"] == "被剔除", "测站文件名"].astype(str).tolist()

    lines = []
    lines.append("RTK/GNSS 静态测量数据自动分析结论")
    lines.append("")
    lines.append(f"一、数据识别情况：程序共识别到原始 GNS 文件 {raw_count} 个，RINEX 观测文件 {rinex_count} 个。")

    if removed_names:
        lines.append(f"二、测站保留情况：原始数据中有 {len(removed_names)} 个测站未出现在内业 RINEX 成果中，疑似被剔除测站为：{', '.join(removed_names)}。")
        lines.append("这类测站通常需要结合外业记录、观测时长、卫星数量、周跳、多路径效应或内业解算稳定性进一步核对。")
    else:
        lines.append("二、测站保留情况：暂未发现原始 GNS 文件与 RINEX 观测文件之间存在明显测站缺失。")

    if not rinex_summary.empty:
        avg_sat = rinex_summary["平均卫星数"].mean()
        min_sat = rinex_summary["最小卫星数"].min()
        max_sat = rinex_summary["最大卫星数"].max()
        epoch_total = rinex_summary["历元数"].sum()
        lines.append(f"三、RINEX 观测情况：保留测站共解析到 {int(epoch_total)} 个观测历元，平均每历元卫星数约为 {avg_sat:.2f} 颗，最小卫星数为 {int(min_sat)} 颗，最大卫星数为 {int(max_sat)} 颗。")

        if avg_sat >= 20:
            lines.append("从卫星数量看，保留测站的观测条件整体较充足，多系统观测有利于提高静态测量成果稳定性。")
        elif avg_sat >= 10:
            lines.append("从卫星数量看，保留测站基本具备解算条件，但仍需关注卫星数偏低时段对成果稳定性的影响。")
        else:
            lines.append("从卫星数量看，部分时段卫星数量偏少，建议重点检查遮挡、多路径效应和数据完整性。")

        interval_values = []
        if "采样间隔(s)" in rinex_summary.columns:
            interval_values = [v for v in rinex_summary["采样间隔(s)"].dropna().unique().tolist()]
        if len(interval_values) > 1:
            lines.append(f"程序检测到不同测站采样间隔不完全一致，采样间隔包括：{', '.join(map(str, interval_values))} 秒。后续处理时应注意采样间隔差异对同步历元统计和基线解算的影响。")
    else:
        lines.append("三、RINEX 观测情况：暂未解析到可统计的历元数据。请检查观测文件是否为 RINEX 3.x 格式，或是否存在文件损坏。")

    total_loop = to_number(project_metrics.get("形成同步环个数")) or to_number(project_metrics.get("同步环明细数量"))
    pass_loop = to_number(project_metrics.get("同步环合格数"))
    fail_loop = to_number(project_metrics.get("同步环不合格数"))

    if total_loop is not None and pass_loop is not None:
        pass_rate = pass_loop / total_loop * 100 if total_loop else 0
        fail_text = f"，不合格 {int(fail_loop)} 个" if fail_loop is not None else ""
        lines.append(f"四、内业成果检核情况：内业报告中形成同步环 {int(total_loop)} 个，合格 {int(pass_loop)} 个{fail_text}，同步环合格率为 {pass_rate:.1f}%。")
        if fail_loop == 0 or fail_loop is None:
            lines.append("同步环检核结果整体较好，说明剔除异常测站后，最终基线网成果满足本次静态测量数据处理要求。")
        else:
            lines.append("存在不合格同步环，建议回查对应基线、观测时段和相关测站质量。")
    else:
        lines.append("四、内业成果检核情况：暂未从 HTML 报告中自动提取到同步环合格率。可以在程序输出表中人工核对项目报告。")

    lines.append("")
    lines.append("五、程序说明：本程序适用于类似 RTK/GNSS 静态测量数据包的快速检查，可自动识别 GNS 原始文件、RINEX 观测文件和内业 HTML 报告，并输出测站对比、卫星数量统计、同步环检核表和可视化图表。")

    return "\n".join(lines)


def save_dataframe(df: pd.DataFrame, path: Path) -> None:
    if df is not None and not df.empty:
        df.to_csv(path, index=False, encoding="utf-8-sig")


def zip_output_dir(output_dir: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in output_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(output_dir))
    buffer.seek(0)
    return buffer.getvalue()


def run_analysis(input_dir: Path, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = scan_files(input_dir)
    file_counts = {k: len(v) for k, v in files.items()}

    station_compare = compare_stations(files["gns"], files["rinex_obs"])

    headers = []
    epoch_dfs = []
    for obs_file in files["rinex_obs"]:
        header, epoch_df = parse_rinex_obs(obs_file)
        headers.append(header)
        if not epoch_df.empty:
            epoch_dfs.append(epoch_df)

    epoch_all = pd.concat(epoch_dfs, ignore_index=True) if epoch_dfs else pd.DataFrame()
    header_df, rinex_summary = summarize_rinex(headers, epoch_all)

    project_summary, sync_loop_df, project_metrics = extract_project_report(files["html"])
    excel_preview = read_quality_excels(files["xlsx"])
    fig_paths = make_figures(epoch_all, output_dir)

    analysis_text = generate_analysis_text(file_counts, station_compare, rinex_summary, project_metrics)

    save_dataframe(station_compare, output_dir / "1_station_compare.csv")
    save_dataframe(header_df, output_dir / "2_rinex_header_info.csv")
    save_dataframe(rinex_summary, output_dir / "3_rinex_station_summary.csv")
    save_dataframe(epoch_all, output_dir / "4_epoch_satellite_count.csv")
    save_dataframe(project_summary, output_dir / "5_project_summary_from_html.csv")
    save_dataframe(sync_loop_df, output_dir / "6_sync_loop_check.csv")
    save_dataframe(excel_preview, output_dir / "7_quality_excel_preview.csv")

    with open(output_dir / "8_auto_analysis_report.txt", "w", encoding="utf-8") as f:
        f.write(analysis_text)

    zip_bytes = zip_output_dir(output_dir)

    return {
        "files": files,
        "file_counts": file_counts,
        "station_compare": station_compare,
        "header_df": header_df,
        "rinex_summary": rinex_summary,
        "epoch_df": epoch_all,
        "project_summary": project_summary,
        "sync_loop_df": sync_loop_df,
        "excel_preview": excel_preview,
        "project_metrics": project_metrics,
        "fig_paths": fig_paths,
        "analysis_text": analysis_text,
        "zip_bytes": zip_bytes,
        "output_dir": output_dir,
    }


def save_uploaded_files(uploaded_files: list[Any], input_dir: Path) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    for uploaded in uploaded_files:
        out_path = input_dir / uploaded.name
        out_path.write_bytes(uploaded.getbuffer())
        if uploaded.name.lower().endswith(".zip"):
            extract_dir = input_dir / f"extracted_{Path(uploaded.name).stem}"
            safe_extract_zip(out_path, extract_dir)


def inject_cute_css() -> None:
    """粉蓝撞色 + 卡片式可爱界面。"""
    st.markdown(
        """
<style>
:root {
    --pink: #ff6fae;
    --pink-soft: #ffe2f1;
    --blue: #48b8ff;
    --blue-soft: #e5f6ff;
    --purple: #8b5cf6;
    --ink: #263143;
    --muted: #6b7280;
    --card: rgba(255, 255, 255, 0.88);
}

/* 隐藏 Streamlit 顶部默认栏 */
[data-testid="stHeader"] {
    display: none;
}

[data-testid="stToolbar"] {
    display: none;
}

[data-testid="stDecoration"] {
    display: none;
}

#MainMenu {
    visibility: hidden;
}

footer {
    visibility: hidden;
}

/* 压缩页面顶部空白 */
.block-container {
    padding-top: 1rem !important;
    padding-bottom: 2rem !important;
}

/* 调整首页封面与顶部距离 */
.hero-section {
    margin-top: 0rem !important;
}

.stApp {
    background:
        radial-gradient(circle at 8% 10%, rgba(255, 111, 174, 0.18), transparent 30%),
        radial-gradient(circle at 90% 8%, rgba(72, 184, 255, 0.20), transparent 28%),
        linear-gradient(135deg, #fff7fb 0%, #f2fbff 46%, #ffffff 100%);
    color: var(--ink);
}

.block-container {
    padding-top: 2.2rem;
    padding-bottom: 3rem;
    max-width: 1240px;
}

h1, h2, h3 {
    color: var(--ink);
    letter-spacing: 0.02em;
}

.hero-card {
    padding: 2rem 2.2rem;
    border-radius: 30px;
    background: linear-gradient(120deg, rgba(255, 111, 174, 0.96) 0%, rgba(86, 196, 255, 0.96) 100%);
    box-shadow: 0 22px 48px rgba(72, 184, 255, 0.20), 0 14px 32px rgba(255, 111, 174, 0.18);
    color: white;
    margin-bottom: 1.2rem;
    border: 1px solid rgba(255,255,255,0.55);
}

.hero-title {
    font-size: 2.05rem;
    font-weight: 850;
    line-height: 1.25;
    margin-bottom: 0.65rem;
}

.hero-subtitle {
    font-size: 1.02rem;
    line-height: 1.8;
    opacity: 0.95;
    max-width: 980px;
}

.feature-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1rem;
    margin: 1rem 0 1.2rem;
}

.feature-card, .sweet-card, .chart-card, .download-card {
    background: var(--card);
    border: 1px solid rgba(255,255,255,0.82);
    border-radius: 24px;
    padding: 1rem 1.1rem;
    box-shadow: 0 14px 34px rgba(72, 184, 255, 0.12), 0 10px 26px rgba(255, 111, 174, 0.10);
    backdrop-filter: blur(10px);
}

.feature-card {
    min-height: 112px;
}

.feature-icon {
    font-size: 1.7rem;
    margin-bottom: 0.25rem;
}

.feature-title {
    font-weight: 800;
    font-size: 1rem;
    color: var(--ink);
    margin-bottom: 0.2rem;
}

.feature-text {
    color: var(--muted);
    font-size: 0.92rem;
    line-height: 1.55;
}

.metric-card {
    background: linear-gradient(145deg, #ffffff 0%, #fff2f8 48%, #eaf8ff 100%);
    border: 1px solid rgba(255, 111, 174, 0.22);
    border-radius: 22px;
    padding: 0.95rem 1rem;
    box-shadow: 0 12px 26px rgba(255, 111, 174, 0.11), 0 8px 20px rgba(72, 184, 255, 0.09);
    min-height: 104px;
}

.metric-label {
    font-size: 0.86rem;
    color: #667085;
    margin-bottom: 0.25rem;
}

.metric-value {
    font-size: 1.75rem;
    font-weight: 850;
    color: #1f2a44;
    line-height: 1.2;
}

.metric-note {
    font-size: 0.78rem;
    color: #9b5f86;
    margin-top: 0.18rem;
}

.section-title {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.55rem 0.92rem;
    border-radius: 999px;
    color: #1f2a44;
    font-weight: 850;
    background: linear-gradient(90deg, #ffe2f1, #e5f6ff);
    border: 1px solid rgba(255,255,255,0.8);
    box-shadow: 0 8px 18px rgba(72, 184, 255, 0.10);
    margin: 0.3rem 0 0.75rem;
}

.small-tip {
    color: #667085;
    line-height: 1.65;
    margin: -0.15rem 0 0.8rem;
}

[data-testid="stFileUploader"] section {
    border: 2px dashed rgba(255, 111, 174, 0.48) !important;
    background: linear-gradient(145deg, rgba(255, 226, 241, 0.65), rgba(229, 246, 255, 0.78)) !important;
    border-radius: 24px !important;
}

.stButton > button {
    border-radius: 999px !important;
    font-weight: 820 !important;
    border: 0 !important;
    background: linear-gradient(90deg, #ff6fae, #48b8ff) !important;
    color: white !important;
    box-shadow: 0 12px 25px rgba(255, 111, 174, 0.20), 0 8px 18px rgba(72, 184, 255, 0.16) !important;
}

.stDownloadButton > button {
    border-radius: 999px !important;
    font-weight: 820 !important;
    border: 0 !important;
    background: linear-gradient(90deg, #48b8ff, #8b5cf6) !important;
    color: white !important;
}

.stTabs [data-baseweb="tab-list"] {
    gap: 0.45rem;
}

.stTabs [data-baseweb="tab"] {
    border-radius: 999px;
    padding: 0.55rem 1rem;
    background: rgba(255,255,255,0.68);
    border: 1px solid rgba(255, 111, 174, 0.18);
}

.stTabs [aria-selected="true"] {
    background: linear-gradient(90deg, #ffe2f1, #e5f6ff) !important;
    color: #1f2a44 !important;
    font-weight: 800;
}

[data-testid="stDataFrame"] {
    border-radius: 18px;
    overflow: hidden;
    box-shadow: 0 10px 24px rgba(72, 184, 255, 0.08);
}

textarea {
    border-radius: 18px !important;
}

@media (max-width: 900px) {
    .feature-grid { grid-template-columns: 1fr; }
    .hero-title { font-size: 1.55rem; }
}
</style>
        """,
        unsafe_allow_html=True,
    )


def render_hero() -> None:
    st.markdown(
        f"""
<div class="hero-card">
  <div class="hero-title">🛰️ {APP_TITLE}</div>
  <div class="hero-subtitle">
    上传外业原始数据与内业处理成果，程序会自动完成测站识别、RINEX 观测统计、同步环检核提取与成果分析。<br>
  </div>
</div>
<div class="feature-grid">
  <div class="feature-card"><div class="feature-icon">📁</div><div class="feature-title">自动识别数据</div><div class="feature-text">支持 zip、GNS、RINEX、HTML 报告、Excel 质检表等常见文件。</div></div>
  <div class="feature-card"><div class="feature-icon">📡</div><div class="feature-title">观测质量统计</div><div class="feature-text">统计历元数、卫星数量、卫星系统组成和采样间隔。</div></div>
  <div class="feature-card"><div class="feature-icon">🌸</div><div class="feature-title">成果展示更清楚</div><div class="feature-text">卡片式指标、粉蓝图表、自动结论，适合直接放进实验报告。</div></div>
</div>
        """,
        unsafe_allow_html=True,
    )


def render_section_title(title: str, icon: str = "✨") -> None:
    st.markdown(f'<div class="section-title">{icon} {title}</div>', unsafe_allow_html=True)


def render_metric_card(label: str, value: Any, note: str = "", icon: str = "💗") -> None:
    st.markdown(
        f"""
<div class="metric-card">
  <div class="metric-label">{icon} {label}</div>
  <div class="metric-value">{value}</div>
  <div class="metric-note">{note}</div>
</div>
        """,
        unsafe_allow_html=True,
    )


def render_file_list(title: str, files: list[Path], icon: str = "📄") -> None:
    with st.expander(f"{icon} {title}：{len(files)} 个", expanded=False):
        if files:
            st.write([p.name for p in files])
        else:
            st.write("未识别到相关文件")


def render_chart_card(name: str, path: Path, note: str) -> None:
    st.markdown(f"### {name}")
    st.caption(note)
    st.image(str(path), use_container_width=True)
    st.markdown("<br>", unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide", page_icon="🛰️")
    inject_cute_css()
    render_hero()

    render_section_title("上传数据", "📦")
    st.markdown('<div class="small-tip">建议一次上传：原始数据 zip + 内业成果 zip。程序会自动解压并识别里面的文件。</div>', unsafe_allow_html=True)

    upload_box = st.container(border=True)
    with upload_box:
        uploaded_files = st.file_uploader(
            "把数据文件放到这里",
            type=["zip", "gns", "obs", "o", "26o", "25o", "24o", "23o", "22o", "html", "htm", "xlsx", "csv"],
            accept_multiple_files=True,
            help="建议直接上传：原始数据.zip + 内业成果.zip。",
        )

        col1, col2 = st.columns([1, 3])
        with col1:
            start = st.button("开始分析 ✨", type="primary", use_container_width=True)
        with col2:
            st.caption("分析完成后会生成 CSV 表格、PNG 图表和 TXT 自动分析结论，可一键下载。")

    if start:
        if not uploaded_files:
            st.warning("请先上传至少一个数据文件。")
            return

        temp_root = Path(tempfile.mkdtemp(prefix="rtk_gnss_"))
        input_dir = temp_root / "input"
        output_dir = temp_root / "analysis_output"

        try:
            save_uploaded_files(uploaded_files, input_dir)
            with st.spinner("正在分析数据，请稍等……"):
                result = run_analysis(input_dir, output_dir)
            st.session_state["analysis_result"] = result
        except Exception as exc:
            st.error("程序运行时出现错误。")
            st.exception(exc)
            return

    result = st.session_state.get("analysis_result")
    if not result:
        st.info("先上传数据，再点击“开始分析”。")
        return

    st.success("分析完成，可以查看下方结果。")

    files = result["files"]
    counts = result["file_counts"]
    station_compare = result["station_compare"]
    rinex_summary = result["rinex_summary"]
    project_metrics = result["project_metrics"]

    removed_count = 0
    if not station_compare.empty and "判断" in station_compare.columns:
        removed_count = int((station_compare["判断"] == "被剔除").sum())

    avg_sat = "—"
    if not rinex_summary.empty and "平均卫星数" in rinex_summary.columns:
        avg_sat = f"{rinex_summary['平均卫星数'].mean():.2f}"

    loop_rate = "—"
    total_loop = to_number(project_metrics.get("形成同步环个数")) or to_number(project_metrics.get("同步环明细数量"))
    pass_loop = to_number(project_metrics.get("同步环合格数"))
    if total_loop and pass_loop is not None:
        loop_rate = f"{pass_loop / total_loop * 100:.1f}%"

    render_section_title("关键指标", "💎")
    metric_cols = st.columns(6)
    with metric_cols[0]:
        render_metric_card("GNS 原始文件", counts.get("gns", 0), "外业原始数据", "📁")
    with metric_cols[1]:
        render_metric_card("RINEX 文件", counts.get("rinex_obs", 0), "内业观测文件", "🛰️")
    with metric_cols[2]:
        render_metric_card("剔除测站", removed_count, "自动比对得出", "🌷")
    with metric_cols[3]:
        render_metric_card("平均卫星数", avg_sat, "保留测站均值", "📡")
    with metric_cols[4]:
        render_metric_card("同步环合格率", loop_rate, "来自内业报告", "✅")
    with metric_cols[5]:
        render_metric_card("输出结果", "ZIP", "表格+图表+结论", "🎀")

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "📁 文件识别",
        "🌷 测站对比",
        "📡 RINEX统计",
        "✅ 内业成果",
        "📊 粉蓝图表",
        "📝 结论下载",
    ])

    with tab1:
        render_section_title("自动识别到的数据文件", "📁")
        col_a, col_b = st.columns(2)
        with col_a:
            render_file_list("GNS 原始文件", files["gns"], "📁")
            render_file_list("RINEX 观测文件", files["rinex_obs"], "🛰️")
            render_file_list("HTML 内业报告", files["html"], "📑")
        with col_b:
            render_file_list("Excel 质检表", files["xlsx"], "📊")
            render_file_list("CSV 表格", files["csv"], "📄")
            render_file_list("其他文件", files["other"], "🧩")

    with tab2:
        render_section_title("原始测站与内业保留测站对比", "🌷")
        df = result["station_compare"]
        if df.empty:
            st.warning("没有可对比的 GNS 与 RINEX 文件。")
        else:
            st.markdown('<div class="small-tip">程序会自动判断哪些测站存在于原始数据中、哪些测站进入了内业 RINEX 成果。</div>', unsafe_allow_html=True)
            st.dataframe(df, use_container_width=True, hide_index=True)

    with tab3:
        render_section_title("RINEX 文件头信息", "🛰️")
        header_df = result["header_df"]
        if header_df.empty:
            st.warning("未解析到 RINEX 文件头信息。")
        else:
            st.dataframe(header_df, use_container_width=True, hide_index=True)

        render_section_title("测站观测统计", "📡")
        rinex_summary = result["rinex_summary"]
        if rinex_summary.empty:
            st.warning("未解析到 RINEX 历元数据。")
        else:
            st.dataframe(rinex_summary, use_container_width=True, hide_index=True)

    with tab4:
        render_section_title("项目汇总指标", "✅")
        project_summary = result["project_summary"]
        if project_summary.empty:
            st.warning("暂未从 HTML 报告中提取到项目汇总指标。")
        else:
            st.dataframe(project_summary, use_container_width=True, hide_index=True)

        render_section_title("同步环检核明细", "🔗")
        sync_loop_df = result["sync_loop_df"]
        if sync_loop_df.empty:
            st.warning("暂未从 HTML 报告中提取到同步环检核明细。")
        else:
            display_cols = [c for c in ["来源报告", "同步环名称", "质量检查结果"] if c in sync_loop_df.columns]
            remaining_cols = [c for c in sync_loop_df.columns if c not in display_cols]
            st.dataframe(sync_loop_df[display_cols + remaining_cols[:8]], use_container_width=True, hide_index=True)

        render_section_title("Excel 质检表预览", "📊")
        excel_preview = result["excel_preview"]
        if excel_preview.empty:
            st.info("未识别到 Excel 质检表，或暂未读取到表格内容。")
        else:
            st.dataframe(excel_preview, use_container_width=True, hide_index=True)

    with tab5:
        render_section_title("可视化图表", "📊")
        fig_paths = result["fig_paths"]
        if not fig_paths:
            st.warning("没有可绘图的历元卫星数量数据。")
        else:
            for name, path in fig_paths.items():
                if "卫星数量" in name:
                    note = "观察不同测站在观测时段内的卫星数量是否稳定，便于发现遮挡、掉星或数据波动。"
                else:
                    note = "比较 GPS、BDS、GLONASS、Galileo 等系统的平均参与情况，体现多系统观测条件。"
                render_chart_card(name, path, note)

    with tab6:
        render_section_title("自动分析结论", "📝")
        st.text_area("分析文本", result["analysis_text"], height=360)
        st.markdown('<div class="download-card">', unsafe_allow_html=True)
        st.download_button(
            "下载全部分析结果 ZIP 🎀",
            data=result["zip_bytes"],
            file_name="rtk_gnss_analysis_output.zip",
            mime="application/zip",
            use_container_width=True,
        )
        st.markdown('</div>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()
