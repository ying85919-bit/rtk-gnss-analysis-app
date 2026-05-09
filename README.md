# RTK/GNSS 静态测量数据自动检查与成果分析小程序

这是一个基于 Streamlit 的网页小程序，用于分析 RTK/GNSS 静态测量外业原始数据与内业处理成果。

## 功能

- 上传原始数据 zip 和内业成果 zip
- 自动识别 GNS 原始文件、RINEX 观测文件、HTML 内业报告、Excel 质检表
- 自动比对原始测站与内业保留测站，识别疑似被剔除测站
- 解析 RINEX 文件头信息和历元卫星数量
- 统计 GPS、BDS、GLONASS、Galileo、QZSS 等卫星系统观测数量
- 提取同步环检核结果
- 生成粉蓝撞色图表和自动分析结论
- 一键下载全部分析结果 ZIP

## 运行方法

在 VS Code 终端进入包含 `app.py` 的文件夹后运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

如果已经创建过 `.venv`，可以跳过第一句。

## 页面风格

本版本采用粉蓝撞色、卡片式首页、关键指标卡片和美化图表，适合课程展示和报告截图。
